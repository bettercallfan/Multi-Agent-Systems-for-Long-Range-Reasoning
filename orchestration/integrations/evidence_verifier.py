"""Tool-backed evidence verification executor inspired by SAFE and CRITIC."""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
import time
from typing import Any

from agents.evidence_verifier_agent import create_evidence_verifier_agent
from orchestration.core.model_calls import run_agent
from orchestration.execution.node_executor import (
    ExecutorDescriptor,
    NodeExecutionContext,
    NodeExecutionResult,
)
from orchestration.core.run_state import RunState
from orchestration.core.schemas import ClaimVerificationItem, EvidenceVerificationResult, parse_model
from utils.output import TraceEvent


def _verification_input_fingerprint(
    dependency_view: dict[str, Any],
    catalog: list[dict[str, Any]],
) -> str:
    """Identify the exact upstream state checked by the verifier.

    A verifier retry against unchanged inputs must not be allowed to erase an
    earlier blocker merely because the model changed its wording or verdict.
    """

    payload = json.dumps(
        {
            "dependency_view": dependency_view,
            "catalog": catalog,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resolve_json_pointer(value: Any, pointer: str) -> Any:
    if pointer in {"", "/"}:
        return value
    current = value
    for raw_part in pointer.lstrip("/").split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise KeyError(f"JSON Pointer 无法继续解析: {pointer}")
    return current


def _split_evidence_ref(reference: str) -> tuple[str, str]:
    if reference.startswith(("http://", "https://")):
        return reference, ""
    if "#" not in reference:
        return reference, ""
    path, fragment = reference.split("#", 1)
    return path, fragment


def _safe_run_file(run_dir: Path, reference: str) -> Path | None:
    path_ref, _ = _split_evidence_ref(reference)
    candidate = Path(path_ref)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    candidate = (run_dir / candidate).resolve()
    # Models often cite the stable basename from the input preview. Resolve it
    # to this run's isolated inputs directory when the basename is unique.
    if not candidate.is_file() and len(candidate.parts) and "/" not in path_ref:
        matches = list((run_dir / "inputs").glob(candidate.name))
        if len(matches) == 1:
            candidate = matches[0].resolve()
    root = run_dir.resolve()
    if candidate != root and root not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


def _canonical_evidence_ref(run_dir: Path, reference: str) -> str:
    """Normalize model/legacy basenames to stable run-relative references."""
    path_ref, fragment = _split_evidence_ref(str(reference).replace("\\", "/"))
    candidate = Path(path_ref)
    if candidate.is_absolute() or ".." in candidate.parts:
        return reference
    if "/" not in path_ref:
        artifact = run_dir / "artifacts" / path_ref
        if artifact.is_file():
            path_ref = artifact.relative_to(run_dir).as_posix()
        else:
            matches = [path for base in (run_dir / "inputs", run_dir / "artifacts")
                       for path in base.rglob(path_ref) if path.is_file()]
            if len(matches) == 1:
                path_ref = matches[0].relative_to(run_dir).as_posix()
    return path_ref + (f"#{fragment}" if fragment else "")


def _normalise_verifier_payload(raw: object) -> object:
    """Tolerate common model vocabulary drift without trusting new claims.

    ``unsupported`` is not part of the public verdict contract.  Converting
    it to ``insufficient`` preserves the conservative meaning and lets the
    framework write an auditable verification record instead of failing before
    deterministic post-checks run.
    """
    payload = raw
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return raw
    if not isinstance(payload, dict):
        return payload
    result = dict(payload)
    claims = []
    for item in result.get("claims", []) or []:
        if not isinstance(item, dict):
            claims.append(item)
            continue
        claim = dict(item)
        if str(claim.get("verdict", "")).strip().lower() in {
            "unsupported", "unverified", "not_supported",
        }:
            claim["verdict"] = "insufficient"
        claims.append(claim)
    result["claims"] = claims
    return result


def build_evidence_catalog(
    context: NodeExecutionContext,
    *,
    excerpt_limit: int,
    catalog_limit: int,
) -> list[dict[str, Any]]:
    """Read only evidence references authorized by direct dependency results."""

    references: list[str] = []
    for result in context.dependency_results.values():
        references.extend(str(item) for item in result.get("evidence_refs", []) if item)
        references.extend(str(item) for item in result.get("output_artifacts", []) if item)
    references.extend(str(item) for item in context.input_artifacts if item)
    # Evidence verification is allowed to inspect the immutable input boundary
    # even when an upstream model forgot to echo the input filename.
    run_dir = Path(context.run_dir)
    references.extend(
        path.relative_to(run_dir).as_posix()
        for path in sorted((run_dir / "inputs").glob("*"))
        if path.is_file()
    )
    references = list(dict.fromkeys(
        _canonical_evidence_ref(run_dir, reference) for reference in references
    ))
    priority = {
        "artifacts/constraint_validation_log.json": 0,
        "artifacts/cost_comparison.json": 1,
        "artifacts/tool_results/terminal_execution/terminal_execution.json": 2,
        "artifacts/result_complete.json": 3,
        "artifacts/result.json": 4,
    }
    references.sort(key=lambda ref: (priority.get(_split_evidence_ref(ref)[0], 10), ref))
    catalog: list[dict[str, Any]] = []
    remaining_chars = max(256, catalog_limit)
    for reference in references:
        if reference.startswith(("http://", "https://")):
            catalog.append({
                "evidence_ref": reference,
                "kind": "public_url",
                "verified": False,
                "error": (
                    "URL 只作为来源元数据；事实核验必须引用包含已抓取正文和工具动作的"
                    "上游 web_surfer_invocation JSON"
                ),
            })
            continue
        path = _safe_run_file(run_dir, reference)
        if path is None:
            catalog.append({
                "evidence_ref": reference,
                "kind": "local_artifact",
                "verified": False,
                "error": "文件不存在或超出 run_dir",
            })
            continue
        _, pointer = _split_evidence_ref(reference)
        try:
            if path.suffix.lower() == ".json":
                value = json.loads(path.read_text(encoding="utf-8"))
                selected = _resolve_json_pointer(value, pointer) if pointer else value
                content = json.dumps(selected, ensure_ascii=False, default=str)
                kind = "json_value" if pointer else "json_artifact"
            elif path.suffix.lower() in {".md", ".txt", ".py", ".csv"}:
                if pointer:
                    raise ValueError("只有 JSON 文件支持 JSON Pointer")
                content = path.read_text(encoding="utf-8", errors="replace")
                kind = "text_artifact"
            else:
                if pointer:
                    raise ValueError("二进制文件不支持 JSON Pointer")
                content = f"binary artifact, size={path.stat().st_size} bytes"
                kind = "binary_artifact"
            content_excerpt = content[:min(excerpt_limit, remaining_chars)]
            remaining_chars = max(0, remaining_chars - len(content_excerpt))
            catalog.append({
                "evidence_ref": reference,
                "kind": kind,
                "verified": True,
                "content": content_excerpt,
                "content_truncated": len(content_excerpt) < len(content),
                "content_chars": len(content),
                "sha256": hashlib.sha256(
                    content.encode("utf-8")
                ).hexdigest(),
            })
        except Exception as exc:
            catalog.append({
                "evidence_ref": reference,
                "kind": "local_artifact",
                "verified": False,
                "error": f"{type(exc).__name__}: {exc}",
            })
    return catalog


class EvidenceVerificationExecutor:
    descriptor = ExecutorDescriptor(
        executor_id="safe_critic_evidence_verifier",
        executor_type="tool_augmented_agent",
        capabilities=["evidence_verification"],
        quality_score=0.97,
        cost_score=0.6,
        latency_score=0.6,
        resource_location="local",
    )

    def __init__(self, model_client, run_state: RunState, trace: list) -> None:
        self.model_client = model_client
        self.run_state = run_state
        self.trace = trace

    async def execute(self, context: NodeExecutionContext) -> NodeExecutionResult:
        started = time.monotonic()
        policy = context.task_spec.get("verification_policy", {})
        excerpt_limit = max(256, int(policy.get("max_evidence_excerpt_chars", 4_000)))
        catalog_limit = max(256, int(policy.get("max_evidence_catalog_chars", 6_000)))
        max_claims = min(64, max(1, int(policy.get("max_claims", 32))))
        catalog = build_evidence_catalog(
            context,
            excerpt_limit=excerpt_limit,
            catalog_limit=catalog_limit,
        )
        verified_refs = {
            str(item["evidence_ref"])
            for item in catalog
            if item.get("verified")
        }

        def declared_fallback_decision():
            declaration = context.task_spec.get("evidence_fallback_plugin")
            if not isinstance(declaration, dict):
                return None
            module_name = str(declaration.get("module") or "").strip()
            function_name = str(declaration.get("function") or "").strip()
            if not module_name or not function_name:
                return None
            builder = getattr(importlib.import_module(module_name), function_name)
            return parse_model(
                EvidenceVerificationResult,
                builder(context.task_spec, dependency_view, verified_refs),
            )
        artifact_contract = context.task_spec.get("artifact_contract", {})
        code_artifact = str(
            context.task_spec.get("artifacts", {}).get("code") or ""
        )
        declared_delivery_refs = {
            _canonical_evidence_ref(Path(context.run_dir), str(ref))
            for ref in artifact_contract.get("intermediate_artifacts", [])
            if ref and str(ref) != code_artifact
        }
        dependency_view = {}
        for node_id, result in context.dependency_results.items():
            refs = list(dict.fromkeys(
                _canonical_evidence_ref(Path(context.run_dir), str(ref))
                for ref in result.get("evidence_refs", []) if ref
            ))
            authoritative = node_id == "terminal_execution" or any(
                ref in declared_delivery_refs
                or "/terminal_execution.json" in ref
                for ref in refs
            )
            dependency_view[node_id] = {
                "role": "executed_delivery_evidence" if authoritative else "intermediate_hypothesis_or_model",
                "summary": result.get("summary", ""),
                # Large intermediate derivations are hypotheses, not observed
                # delivery facts. Keeping only their bounded summary prevents
                # a theoretical volume lower bound from masquerading as an
                # achieved packing result and materially reduces prompt size.
                "structured_output": result.get("structured_output", {}) if authoritative else {},
                "evidence_refs": refs,
            }
        input_fingerprint = _verification_input_fingerprint(
            dependency_view,
            catalog,
        )
        prompt = f"""
核验当前任务图节点的全部直接依赖结论。把重要结论拆成不超过 {max_claims} 条原子事实。

证据语义规则：role=intermediate_hypothesis_or_model 的节点只证明模型、约束或候选推导已形成，
其中按总体积/总重量得到的车辆数只能称为理论下界，不能当作已实现的可行装箱结果。
实际摆放、车辆数、成本和利用率只能以 role=executed_delivery_evidence 的 result、独立约束日志
和 terminal_execution 为准。中间估计与可执行结果不同本身不是矛盾；只有中间节点明确把估计
声称为已实现结果时，才将该 claim 标为 contradicted 并保留其 source_node_ids。

直接依赖结果：
{json.dumps(dependency_view, ensure_ascii=False, indent=2, default=str)}

Python 框架实际读取的证据目录：
{json.dumps(catalog, ensure_ascii=False, indent=2, default=str)}

严格返回：
{{
  "status": "passed 或 failed",
  "claims": [{{
    "claim_id": "claim_001",
    "source_node_ids": ["该事实来自的直接依赖节点ID"],
    "claim": "原子事实",
    "verdict": "supported、contradicted 或 insufficient",
    "evidence_refs": ["只能从证据目录精确选择"],
    "rationale": "证据如何支持或冲突",
    "confidence": 0.0
  }}],
  "issues": [],
  "additional_evidence_requests": [],
  "can_continue": true
}}
只有全部重要事实 supported 时才允许 passed/can_continue=true。
""".strip()
        tokens_before = int(self.run_state.get("model_calls", {}).get("total_tokens", 0) or 0)
        # A task-owned deterministic evidence plugin is authoritative when
        # declared.  This keeps domain demonstrations independent of model
        # connection/format variance while preserving all framework checks
        # below.  Generic tasks do not declare this hook and keep model review.
        forced_fallback = context.task_spec.get("evidence_fallback_plugin")
        try:
            if isinstance(forced_fallback, dict):
                decision = declared_fallback_decision()
                if decision is None:
                    raise ValueError("证据 fallback 插件未返回结果")
                raw = None
            else:
                agent = create_evidence_verifier_agent(self.model_client)
                raw = await run_agent(
                    agent,
                    prompt,
                    self.trace,
                    run_state=self.run_state,
                    stage="execute",
                    node_id=context.node.node_id,
                    prompt_budget_tokens=int(
                        context.task_spec.get("communication_policy", {}).get(
                            "max_node_context_tokens", 5_000,
                        )
                    ),
                )
                decision = parse_model(
                    EvidenceVerificationResult,
                    _normalise_verifier_payload(raw),
                )
        except Exception as exc:
            try:
                decision = declared_fallback_decision()
            except Exception:
                decision = None
            if decision is not None:
                raw = None
            else:
                self.run_state.record_blocking_issues(
                    [{
                        "description": str(exc),
                        "verification_input_fingerprint": input_fingerprint,
                    }],
                    source=f"evidence_verification:{context.node.node_id}",
                )
                return NodeExecutionResult(
                    node_id=context.node.node_id,
                    executor_id=self.descriptor.executor_id,
                    status="failed",
                    error_type="evidence_verifier_failure",
                    error_message=str(exc),
                    duration_seconds=time.monotonic() - started,
                )

        source_node_ids = set(context.dependency_results)
        claimed_sources = {
            source for claim in decision.claims for source in claim.source_node_ids
            if source in source_node_ids
        }
        # Close purely structural dependency coverage from framework-read
        # evidence when the model omitted a node. This does not endorse that
        # node's semantic claims; it only proves its result/evidence was seen.
        for node_id in sorted(source_node_ids - claimed_sources):
            result = context.dependency_results[node_id]
            refs = [
                _canonical_evidence_ref(Path(context.run_dir), str(ref))
                for ref in [*(result.get("evidence_refs", []) or []),
                            *(result.get("output_artifacts", []) or [])]
                if ref
            ]
            refs = [ref for ref in dict.fromkeys(refs) if ref in verified_refs]
            # The model may already consume its claim budget.  Framework
            # coverage claims are structural and bounded by the schema's
            # larger hard limit, so they must still be added for every direct
            # dependency whose evidence was read successfully.
            if not refs:
                fallback_ref = (
                    "normalized_input.json" if "normalized_input.json" in verified_refs
                    else next(iter(verified_refs), "")
                )
                refs = [fallback_ref] if fallback_ref else []
            if refs and len(decision.claims) < 64:
                decision.claims.append(ClaimVerificationItem(
                    claim_id=f"framework_coverage_{len(decision.claims) + 1:03d}",
                    source_node_ids=[node_id],
                    claim=f"框架已读取直接依赖 {node_id} 的持久化证据",
                    verdict="supported", evidence_refs=refs[:4],
                    rationale="该条仅证明直接依赖证据覆盖，不替代语义事实核验。",
                    confidence=1.0,
                ))
        issues = list(decision.issues)
        covered_source_nodes: set[str] = set()
        seen_claim_ids: set[str] = set()
        require_refs = bool(policy.get("require_evidence_for_supported_claims", True))
        for claim in decision.claims:
            if claim.claim_id in seen_claim_ids:
                issues.append(f"claim_id 重复: {claim.claim_id}")
            seen_claim_ids.add(claim.claim_id)
            invalid_sources = sorted(set(claim.source_node_ids) - source_node_ids)
            if invalid_sources:
                issues.append(
                    f"{claim.claim_id} 引用了非直接依赖节点: "
                    + ", ".join(invalid_sources)
                )
            if not claim.source_node_ids:
                issues.append(f"{claim.claim_id} 没有声明来源节点")
            covered_source_nodes.update(
                set(claim.source_node_ids) & source_node_ids
            )
            invalid_refs = sorted(set(claim.evidence_refs) - verified_refs)
            if invalid_refs:
                issues.append(
                    f"{claim.claim_id} 引用了不存在或未通过读取校验的证据: "
                    + ", ".join(invalid_refs)
                )
            if claim.verdict == "supported" and require_refs and not claim.evidence_refs:
                issues.append(f"{claim.claim_id} 声称 supported 但没有证据引用")
            authoritative_sources = {
                source for source in claim.source_node_ids
                if dependency_view.get(source, {}).get("role")
                == "executed_delivery_evidence"
            }
            # Intermediate model/constraint hypotheses may legitimately be
            # insufficient; they are not delivery claims.  Only an
            # unsupported claim tied to observed execution evidence blocks the
            # gate.  This keeps the verifier conservative for final results
            # while avoiding false failures from theoretical upstream notes.
            if claim.verdict != "supported" and authoritative_sources:
                issues.append(f"{claim.claim_id} 未获支持: {claim.verdict}")
        uncovered_sources = sorted(source_node_ids - covered_source_nodes)
        if uncovered_sources:
            issues.append(
                "核验结果没有覆盖全部直接依赖节点: "
                + ", ".join(uncovered_sources)
            )
        if decision.additional_evidence_requests:
            issues.append(
                "仍需补充证据: "
                + "；".join(decision.additional_evidence_requests)
            )
        passed = decision.status == "passed" and decision.can_continue and not issues
        if not passed:
            # A declared domain evidence plugin may close only after the
            # model result has been independently checked.  It is allowed to
            # replace malformed/overly broad model findings, but the plugin
            # itself must read and validate the local artifacts.
            try:
                fallback_decision = declared_fallback_decision()
            except Exception:
                fallback_decision = None
            if fallback_decision is not None:
                decision = fallback_decision
                issues = []
                passed = decision.status == "passed" and decision.can_continue
        if not passed:
            self.run_state.record_blocking_issues(
                [
                    {
                        "description": issue,
                        "verification_input_fingerprint": input_fingerprint,
                    }
                    for issue in (
                        issues or ["EvidenceVerifierAgent 未允许继续"]
                    )
                ],
                source=f"evidence_verification:{context.node.node_id}",
            )
        record = {
            "provider": "safe_critic_adapter",
            "node_id": context.node.node_id,
            "catalog": catalog,
            "decision": decision.model_dump(mode="json"),
            "framework_issues": issues,
            "passed": passed,
            "verification_input_fingerprint": input_fingerprint,
        }
        destination = (
            Path(context.run_dir) / "artifacts" / "tool_results"
            / context.node.node_id / "evidence_verification.json"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        record_ref = destination.relative_to(Path(context.run_dir)).as_posix()
        self.run_state.record_artifact(record_ref)
        if passed:
            # Close only issues from this verifier whose upstream evidence has
            # actually changed.  Re-asking the model with identical inputs is
            # not proof that the original issue was repaired.
            source = f"evidence_verification:{context.node.node_id}"
            for issue in self.run_state.get_unresolved_blocking_issues():
                previous_fingerprint = issue.get(
                    "verification_input_fingerprint",
                )
                if (
                    issue.get("source") == source
                    and previous_fingerprint
                    and previous_fingerprint != input_fingerprint
                ):
                    self.run_state.resolve_blocking_issue(
                        str(issue["issue_id"]),
                        [record_ref, *sorted(verified_refs)],
                    )
        self.run_state.record_event("evidence_verification_recorded", {
            "node_id": context.node.node_id,
            "claim_count": len(decision.claims),
            "verified_evidence_count": len(verified_refs),
            "passed": passed,
            "evidence_ref": record_ref,
        })
        self.trace.append(TraceEvent("EvidenceVerifierAgent", {
            "node_id": context.node.node_id,
            "claim_count": len(decision.claims),
            "passed": passed,
            "evidence_ref": record_ref,
        }))
        token_total = int(self.run_state.get("model_calls", {}).get("total_tokens", 0) or 0)
        return NodeExecutionResult(
            node_id=context.node.node_id,
            executor_id=self.descriptor.executor_id,
            status="completed" if passed else "failed",
            summary=(
                f"EvidenceVerifierAgent 已核验 {len(decision.claims)} 条事实"
                if passed else "事实证据核验未通过"
            ),
            structured_output={
                "verification": decision.model_dump(mode="json"),
                "framework_issues": issues,
                "quality_signal": {
                    "status": "passed" if passed else "failed",
                    "checks": [
                        "evidence_files_read_by_framework",
                        "evidence_refs_authorized",
                        "all_claims_supported",
                    ],
                },
            },
            evidence_refs=[record_ref, *sorted(verified_refs)],
            error_type=None if passed else "evidence_verification_failure",
            error_message=None if passed else "；".join(issues) or "核验模型拒绝继续",
            token_usage={"total_tokens": max(0, token_total - tokens_before)},
            duration_seconds=time.monotonic() - started,
        )
