"""Framework-owned repair loop for post-execution business failures.

The normal :class:`GraphScheduler` owns executor failures.  This module covers
the different case where a producer completed and wrote valid files, but an
independent validator later proved the business result wrong.  It does not call
an Agent directly and does not introduce another scheduler.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from orchestration.core.run_state import RunState
from orchestration.graph.task_graph import NodeStatus, TaskGraph
from orchestration.validation.models import BusinessValidationResult


class BusinessRepairOutcome(BaseModel):
    status: Literal[
        "not_needed", "repaired", "blocked", "failed", "exhausted",
    ]
    graph: TaskGraph
    validation: BusinessValidationResult
    rounds_used: int = Field(default=0, ge=0)
    target_node_ids: list[str] = Field(default_factory=list)
    affected_node_ids: list[str] = Field(default_factory=list)
    resolved_issue_ids: list[str] = Field(default_factory=list)
    remaining_issue_ids: list[str] = Field(default_factory=list)


ReplayCallback = Callable[[TaskGraph], Awaitable[TaskGraph]]
ValidationCallback = Callable[[], BusinessValidationResult]


def _artifact_ref(reference: str) -> str:
    """Strip JSON pointers while retaining the run-relative artifact path."""

    return str(reference or "").split("#", 1)[0].strip()


def _artifact_owners(graph: TaskGraph) -> dict[str, str]:
    owners: dict[str, str] = {}
    for node in graph.nodes:
        for path in node.output_artifacts:
            owners[path] = node.node_id
    return owners


def _target_node_for_check(
    graph: TaskGraph,
    validator_id: str,
    evidence_refs: list[str],
    failed_item: dict[str, Any],
) -> str | None:
    explicit_producer = str(
        failed_item.get("producer_node_id") or ""
    ).strip()
    if explicit_producer:
        try:
            graph.get_node(explicit_producer)
            return explicit_producer
        except KeyError:
            return None
    owners = _artifact_owners(graph)
    references = [
        _artifact_ref(item) for item in evidence_refs
        if _artifact_ref(item)
    ]
    path = _artifact_ref(str(failed_item.get("path") or ""))
    if path:
        references.append(path)
    candidates = list(dict.fromkeys(
        owners[reference] for reference in references
        if reference in owners
    ))
    if candidates:
        code_candidates = [
            node_id for node_id in candidates
            if graph.get_node(node_id).capability == "code"
        ]
        return (code_candidates or candidates)[0]

    # Input normalization is framework-owned and cannot be repaired by asking a
    # completed code node to regenerate itself.
    if validator_id == "input_coverage":
        return None
    code_nodes = [
        node.node_id for node in graph.nodes if node.capability == "code"
    ]
    if len(code_nodes) == 1 and validator_id in {
        "result_integrity", "code_shortcut_scan", "mathorcup_packing",
    }:
        return code_nodes[0]
    return None


def business_validation_issues(
    validation: BusinessValidationResult,
    graph: TaskGraph,
) -> list[dict[str, Any]]:
    """Convert required validator failures into stable, routable issues."""

    required = set(validation.failed_required_checks)
    issues: list[dict[str, Any]] = []
    for check in validation.checks:
        if check.validator_id not in required and check.check_id not in required:
            continue
        failed_items = list(check.failed_checks) or [{
            "check": "validator_status",
            "reason": check.summary,
        }]
        for item in failed_items:
            target_node_id = _target_node_for_check(
                graph,
                check.validator_id,
                check.evidence_refs,
                item,
            )
            discriminator = (
                item.get("check")
                or item.get("type")
                or item.get("field")
                or "validator_failure"
            )
            identity = json.dumps({
                "validator_id": check.validator_id,
                "discriminator": discriminator,
                "target_node_id": target_node_id,
                "path": item.get("path"),
                "field": item.get("field"),
                "location": {
                    key: item.get(key) for key in (
                        "index", "first", "second", "cargo_id", "vehicle_id",
                    ) if item.get(key) is not None
                },
            }, ensure_ascii=False, sort_keys=True, default=str)
            issue_id = "business-" + hashlib.sha256(
                identity.encode("utf-8")
            ).hexdigest()[:16]
            reason = str(
                item.get("reason")
                or item.get("evidence")
                or check.summary
            )
            detail = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            evidence_refs = list(dict.fromkeys([
                *check.evidence_refs,
                *([str(item["path"])] if item.get("path") else []),
            ]))
            issues.append({
                "issue_id": issue_id,
                "source": "business_validation",
                "severity": "critical",
                "description": (
                    f"{check.validator_id}/{discriminator}: {reason}; detail={detail}"
                ),
                "validator_id": check.validator_id,
                "check": discriminator,
                "failed_check": item,
                "target_node_id": target_node_id,
                "repair_target": (
                    graph.get_node(target_node_id).capability
                    if target_node_id else "unresolved"
                ),
                "artifact_refs": evidence_refs,
            })
    for validator_id in validation.unresolved_validators:
        identity = f"unresolved-validator:{validator_id}"
        issues.append({
            "issue_id": "business-" + hashlib.sha256(
                identity.encode("utf-8")
            ).hexdigest()[:16],
            "source": "business_validation",
            "severity": "critical",
            "description": f"缺少必需业务验证器：{validator_id}",
            "validator_id": validator_id,
            "check": "validator_unresolved",
            "target_node_id": None,
            "repair_target": "framework",
            "artifact_refs": [],
        })
    return issues


def evidence_verification_issues(
    graph: TaskGraph,
) -> list[dict[str, Any]]:
    """Route failed independent evidence checks back to the code producer.

    Evidence verification is a governance gate, not the producer of the
    contradicted business result.  When exactly one code node owns the result
    pipeline, its structured verifier findings are safe repair feedback for
    that producer.
    """
    code_nodes = [
        node.node_id for node in graph.nodes if node.capability == "code"
    ]
    default_target = code_nodes[0] if len(code_nodes) == 1 else None
    issues: list[dict[str, Any]] = []
    for node in graph.nodes:
        if (
            node.capability != "evidence_verification"
            or node.status != NodeStatus.FAILED
        ):
            continue
        error = dict(node.error or {})
        structured = error.get("structured_output") or {}
        verification = structured.get("verification") or {}
        raw_issues = [
            {
                "issue_type": "unsupported_claim",
                "description": (
                    f"{claim.get('claim_id')}: {claim.get('claim')} -> "
                    f"{claim.get('verdict')}; {claim.get('rationale', '')}"
                ),
                "source_node_ids": list(claim.get("source_node_ids") or []),
                "claim": claim,
            }
            for claim in verification.get("claims", [])
            if claim.get("verdict") != "supported"
        ]
        raw_issues.extend(
            {"description": str(item), "issue_type": "verifier_issue"}
            for item in verification.get("issues", [])
        )
        if not raw_issues:
            raw_issues = list(structured.get("framework_issues") or [])
        if not raw_issues:
            raw_issues = [
                error.get("error_message")
                or error.get("summary")
                or "独立证据核验未通过"
            ]
        evidence_refs = list(dict.fromkeys(
            str(item) for item in error.get("evidence_refs", []) if item
        ))
        for raw_issue in raw_issues:
            parsed: Any = raw_issue
            if isinstance(raw_issue, str):
                try:
                    parsed = json.loads(raw_issue)
                except (TypeError, ValueError):
                    parsed = {"description": raw_issue}
            if not isinstance(parsed, dict):
                parsed = {"description": str(parsed)}
            description = str(
                parsed.get("description")
                or parsed.get("reason")
                or raw_issue
            )
            recommendation = str(parsed.get("recommendation") or "").strip()
            if recommendation:
                description += f"；建议：{recommendation}"
            declared_sources = [
                str(source) for source in parsed.get("source_node_ids", [])
                if str(source) in {candidate.node_id for candidate in graph.nodes}
                and graph.get_node(str(source)).capability not in {
                    "evidence_verification", "artifact_validation", "terminal_execution",
                }
            ]
            # A claim attributed to one direct dependency is repaired at that
            # producer. Global verifier issues retain the unique code producer
            # fallback because no narrower ownership is proven.
            target_node_id = declared_sources[0] if len(declared_sources) == 1 else default_target
            identity = json.dumps({
                "source_node_id": node.node_id,
                "issue_type": parsed.get("issue_type"),
                "description": description,
                "target_node_id": target_node_id,
            }, ensure_ascii=False, sort_keys=True)
            issue_id = "evidence-" + hashlib.sha256(
                identity.encode("utf-8")
            ).hexdigest()[:16]
            issues.append({
                "issue_id": issue_id,
                "source": "evidence_verification",
                "severity": "critical",
                "description": description,
                "validator_id": "evidence_verification",
                "check": str(
                    parsed.get("issue_type") or "verification_failure"
                ),
                "failed_check": parsed,
                "target_node_id": target_node_id,
                "repair_target": "code" if target_node_id else "unresolved",
                "artifact_refs": evidence_refs,
            })
    return issues


def _repair_contexts(
    issues: list[dict[str, Any]],
    round_number: int,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for issue in issues:
        target = issue.get("target_node_id")
        if target:
            grouped[str(target)].append(issue)
    contexts: dict[str, dict[str, Any]] = {}
    for node_id, node_issues in grouped.items():
        # Physical validators can report hundreds of instances of one defect.
        # Preserve concrete indexed examples while bounding the regeneration
        # prompt; the code must repair the general rule, not memorize a list.
        sampled_issues = node_issues[:32]
        descriptions = [str(item["description"]) for item in sampled_issues]
        if len(node_issues) > len(sampled_issues):
            descriptions.append(
                f"另有 {len(node_issues) - len(sampled_issues)} 个同轮结构化失败；"
                "必须修复对应通用约束并重新生成全部结果。"
            )
        forbidden = sorted({
            str(item.get("failed_check", {}).get("type"))
            for item in node_issues
            if item.get("failed_check", {}).get("type")
        })
        instruction = (
            "上一次节点虽然执行成功，但独立验证未通过。必须根据真实输入和"
            "既定上游方案修复结果，不得通过删除字段、弱化目标、自报 success、"
            "默认数据或估算常量绕过验证。\n需要修复：\n- "
            + "\n- ".join(descriptions)
        )
        if forbidden:
            instruction += "\n已检测并禁止重复出现的捷径：\n- " + "\n- ".join(forbidden)
        contexts[node_id] = {
            "kind": "post_business_validation_repair",
            "round": round_number,
            "issue_ids": [str(item["issue_id"]) for item in node_issues],
            "issues": descriptions,
            "failed_checks": [item.get("failed_check", {}) for item in sampled_issues],
            "total_issue_count": len(node_issues),
            "artifact_refs": list(dict.fromkeys(
                reference
                for item in node_issues
                for reference in item.get("artifact_refs", [])
            )),
            "repair_instruction": instruction,
        }
    return contexts


class BusinessRepairLoop:
    """Replay only validator-attributed producers and their descendants."""

    def __init__(
        self,
        run_state: RunState,
        *,
        max_rounds: int,
    ) -> None:
        self.run_state = run_state
        self.max_rounds = max(0, int(max_rounds))

    def _record_issues(self, issues: list[dict[str, Any]]) -> None:
        if issues:
            self.run_state.record_blocking_issues(
                issues, source="independent_validation",
            )

    def _resolve_absent(
        self,
        previous_ids: set[str],
        current_ids: set[str],
        evidence_refs: list[str],
    ) -> list[str]:
        resolved = sorted(previous_ids - current_ids)
        evidence = list(dict.fromkeys(
            [*evidence_refs, "review/business_validation.json"]
        ))
        for issue_id in resolved:
            try:
                self.run_state.resolve_blocking_issue(issue_id, evidence)
            except KeyError:
                continue
        return resolved

    async def run(
        self,
        graph: TaskGraph,
        initial_validation: BusinessValidationResult,
        *,
        replay: ReplayCallback,
        validate: ValidationCallback,
    ) -> BusinessRepairOutcome:
        validation = initial_validation
        issues = [
            *business_validation_issues(validation, graph),
            *evidence_verification_issues(graph),
        ]
        self._record_issues(issues)
        previous_ids = {str(item["issue_id"]) for item in issues}
        all_resolved: set[str] = set()
        all_targets: list[str] = []
        all_affected: list[str] = []

        if validation.status == "passed" and not issues:
            return BusinessRepairOutcome(
                status="not_needed", graph=graph, validation=validation,
            )
        non_repairable_failures = [
            node.node_id
            for node in graph.nodes
            if node.status in {NodeStatus.FAILED, NodeStatus.BLOCKED}
            and node.capability not in {
                "evidence_verification", "artifact_validation",
            }
        ]
        if non_repairable_failures:
            # Ordinary execution recovery has already been exhausted.  A
            # post-validation budget must not become a hidden extra executor
            # retry budget.
            return BusinessRepairOutcome(
                status="failed",
                graph=graph,
                validation=validation,
                remaining_issue_ids=sorted(previous_ids),
            )
        if (
            validation.status not in {"failed", "error"}
            and not evidence_verification_issues(graph)
        ) or self.max_rounds == 0:
            return BusinessRepairOutcome(
                status="blocked",
                graph=graph,
                validation=validation,
                remaining_issue_ids=sorted(previous_ids),
            )

        no_progress_streak = 0
        for round_number in range(1, self.max_rounds + 1):
            target_node_ids = list(dict.fromkeys(
                str(item["target_node_id"])
                for item in issues
                if item.get("target_node_id")
            ))
            if not target_node_ids:
                self.run_state.record_event("business_repair_unroutable", {
                    "round": round_number,
                    "issue_ids": sorted(previous_ids),
                    "reason": "业务验证问题没有可证明的责任节点",
                })
                return BusinessRepairOutcome(
                    status="blocked",
                    graph=graph,
                    validation=validation,
                    rounds_used=round_number - 1,
                    target_node_ids=all_targets,
                    affected_node_ids=all_affected,
                    resolved_issue_ids=sorted(all_resolved),
                    remaining_issue_ids=sorted(previous_ids),
                )

            contexts = _repair_contexts(issues, round_number)
            affected = graph.reopen_subgraph(target_node_ids, contexts)
            all_targets.extend(
                item for item in target_node_ids if item not in all_targets
            )
            all_affected.extend(
                item for item in affected if item not in all_affected
            )
            self.run_state.begin_business_repair(
                round_number=round_number,
                target_node_ids=target_node_ids,
                affected_node_ids=affected,
                issue_ids=sorted(previous_ids),
                repair_contexts=contexts,
            )

            graph = await replay(graph)
            non_repairable_failures = [
                node.node_id
                for node in graph.nodes
                if node.status in {NodeStatus.FAILED, NodeStatus.BLOCKED}
                and node.capability not in {
                    "evidence_verification", "artifact_validation",
                }
            ]
            if non_repairable_failures:
                self.run_state.finish_business_repair(
                    round_number=round_number,
                    status="failed",
                    validation_status=validation.status,
                    resolved_issue_ids=[],
                    remaining_issue_ids=sorted(previous_ids),
                    progress=False,
                )
                return BusinessRepairOutcome(
                    status="failed",
                    graph=graph,
                    validation=validation,
                    rounds_used=round_number,
                    target_node_ids=all_targets,
                    affected_node_ids=all_affected,
                    resolved_issue_ids=sorted(all_resolved),
                    remaining_issue_ids=sorted(previous_ids),
                )

            validation = validate()
            self.run_state.record_business_validation(
                validation.model_dump(mode="json"),
                "review/business_validation.json",
            )
            current_issues = [
                *business_validation_issues(validation, graph),
                *evidence_verification_issues(graph),
            ]
            current_ids = {
                str(item["issue_id"]) for item in current_issues
            }
            resolved = self._resolve_absent(
                previous_ids,
                current_ids,
                validation.evidence_refs,
            )
            all_resolved.update(resolved)
            self._record_issues(current_issues)
            progress = bool(resolved) or len(current_ids) < len(previous_ids)
            no_progress_streak = 0 if progress else no_progress_streak + 1

            if validation.status == "passed" and not current_ids:
                self.run_state.finish_business_repair(
                    round_number=round_number,
                    status="repaired",
                    validation_status=validation.status,
                    resolved_issue_ids=resolved,
                    remaining_issue_ids=[],
                    progress=True,
                )
                return BusinessRepairOutcome(
                    status="repaired",
                    graph=graph,
                    validation=validation,
                    rounds_used=round_number,
                    target_node_ids=all_targets,
                    affected_node_ids=all_affected,
                    resolved_issue_ids=sorted(all_resolved),
                    remaining_issue_ids=[],
                )

            final_round = round_number >= self.max_rounds
            stop_for_no_progress = no_progress_streak >= 2
            self.run_state.finish_business_repair(
                round_number=round_number,
                status=(
                    "blocked" if final_round or stop_for_no_progress
                    else "retryable"
                ),
                validation_status=(
                    "failed" if current_issues else validation.status
                ),
                resolved_issue_ids=resolved,
                remaining_issue_ids=sorted(current_ids),
                progress=progress,
            )
            if final_round or stop_for_no_progress:
                return BusinessRepairOutcome(
                    status="exhausted",
                    graph=graph,
                    validation=validation,
                    rounds_used=round_number,
                    target_node_ids=all_targets,
                    affected_node_ids=all_affected,
                    resolved_issue_ids=sorted(all_resolved),
                    remaining_issue_ids=sorted(current_ids),
                )
            issues = current_issues
            previous_ids = current_ids

        return BusinessRepairOutcome(
            status="exhausted",
            graph=graph,
            validation=validation,
            rounds_used=self.max_rounds,
            target_node_ids=all_targets,
            affected_node_ids=all_affected,
            resolved_issue_ids=sorted(all_resolved),
            remaining_issue_ids=sorted(previous_ids),
        )
