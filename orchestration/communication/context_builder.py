"""Build sparse, low-entropy contexts from direct TaskGraph dependencies.

``ContextBuilder`` is an AgentPrune-lite policy, not a second scheduler.  It
projects each completed dependency to a stable message schema, removes exact
duplicates by SHA256, applies a free-text budget, and externalises large JSON
without truncating or asking a language model to rewrite structured values.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from orchestration.communication.communication_models import (
    CommunicationBudget,
    CommunicationPolicy,
    CommunicationSummary,
    ContextBuildResult,
    MessageEnvelope,
    MessageMetrics,
    content_sha256,
    estimate_tokens,
    utf8_size,
)
from orchestration.communication.context_compressors import ContextCompressor, build_compressor
from orchestration.execution.node_executor import NodeExecutionContext
from orchestration.graph.task_graph import TaskGraph, TaskNode


class CommunicationBudgetExceededError(ValueError):
    """Protected metadata alone cannot fit the configured hard budget."""


class ContentReferenceError(ValueError):
    """A content-addressed dependency reference is unsafe or corrupted."""


@dataclass(frozen=True)
class _SeenPayload:
    message_id: str
    payload_ref: str


def _json_safe(value: Any) -> Any:
    """Normalise executor results to persistable JSON without mutating input."""

    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _wire_payload(source_node: str, target_node: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_node": source_node,
        "target_node": target_node,
        **result,
    }


def _json_pointer_get(value: Any, pointer: str) -> Any:
    """Resolve one RFC 6901-style JSON Pointer without mutating the payload."""

    if pointer == "":
        return value
    current = value
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            raise KeyError(pointer)
    return _json_safe(current)


def resolve_content_reference(run_dir: str | Path, reference: dict[str, Any]) -> Any:
    """Read and SHA256-verify a framework content reference inside ``run_dir``."""

    relative = reference.get("$ref")
    expected = reference.get("sha256")
    if not isinstance(relative, str) or not relative:
        raise ContentReferenceError("内容引用缺少 $ref")
    if not isinstance(expected, str) or len(expected) != 64:
        raise ContentReferenceError("内容引用缺少有效 sha256")
    root = Path(run_dir).resolve()
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        raise ContentReferenceError(f"内容引用越出 run_dir: {relative}")
    if not target.is_file():
        raise ContentReferenceError(f"内容引用不存在: {relative}")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContentReferenceError(f"内容引用不可解析: {relative}: {exc}") from exc
    actual = content_sha256(payload)
    if actual != expected:
        raise ContentReferenceError(
            f"内容引用哈希不一致: {relative}: expected={expected}, actual={actual}"
        )
    return payload


class ContextBuilder:
    """Construct executor input plus auditable communication metrics.

    The in-memory deduplication cache is namespaced by ``run_dir`` so content
    from one run can never become an implicit dependency of another run.
    Content-addressed payload files make references usable across retries and
    process restarts.
    """

    def __init__(
        self,
        policy: CommunicationPolicy | dict[str, Any] | None = None,
        compressor: ContextCompressor | None = None,
    ) -> None:
        self._explicit_policy = (
            policy
            if isinstance(policy, CommunicationPolicy)
            else CommunicationPolicy.model_validate(policy)
            if policy is not None
            else None
        )
        self._explicit_compressor = compressor
        self._compressors: dict[tuple[Any, ...], ContextCompressor] = {}
        self._seen_payloads: dict[tuple[str, str, str], _SeenPayload] = {}
        self._globally_seen_payloads: dict[tuple[str, str], _SeenPayload] = {}
        self._sequence = 0

    def _policy_for(self, task_spec: dict[str, Any] | None) -> CommunicationPolicy:
        return self._explicit_policy or CommunicationPolicy.from_task_spec(task_spec)

    def _compressor_for(self, policy: CommunicationPolicy) -> ContextCompressor:
        if self._explicit_compressor is not None:
            return self._explicit_compressor
        key = (
            policy.compressor,
            policy.llmlingua_threshold_tokens,
            policy.llmlingua_model,
            policy.llmlingua_target_ratio,
            policy.llmlingua_device_map,
            policy.llmlingua_allow_download,
        )
        if key not in self._compressors:
            self._compressors[key] = build_compressor(
                policy.compressor,
                llmlingua_threshold_tokens=policy.llmlingua_threshold_tokens,
                llmlingua_model=policy.llmlingua_model,
                llmlingua_target_ratio=policy.llmlingua_target_ratio,
                llmlingua_device_map=policy.llmlingua_device_map,
                llmlingua_allow_download=policy.llmlingua_allow_download,
            )
        return self._compressors[key]

    @staticmethod
    def _project_result(result: dict[str, Any]) -> dict[str, Any]:
        """Apply the message protocol's allow-list (temporal noise pruning)."""

        safe = _json_safe(result or {})
        structured = safe.get("structured_output") or {}
        if not isinstance(structured, dict):
            structured = {"value": structured}
        return {
            "summary": str(safe.get("summary") or ""),
            "structured_output": structured,
            "output_artifacts": list(safe.get("output_artifacts") or []),
            "evidence_refs": list(safe.get("evidence_refs") or []),
        }

    @staticmethod
    def _structured_preview(value: Any, max_items: int = 16) -> dict[str, Any]:
        """Keep a small deterministic scalar view beside a content reference.

        Stateless executors cannot rely on having seen an earlier message.  A
        reference therefore carries real scalar facts (numbers, dates, flags,
        short strings) while the complete JSON remains content-addressed.
        """
        fields: dict[str, Any] = {}
        truncated = False

        def visit(item: Any, path: str) -> None:
            nonlocal truncated
            if len(fields) >= max_items:
                truncated = True
                return
            if isinstance(item, dict):
                for key in sorted(item, key=str):
                    visit(item[key], f"{path}/{key}")
                    if truncated:
                        return
            elif isinstance(item, list):
                for index, child in enumerate(item):
                    visit(child, f"{path}/{index}")
                    if truncated:
                        return
            elif item is None or isinstance(item, (bool, int, float)):
                fields[path or "/"] = item
            elif isinstance(item, str):
                fields[path or "/"] = item[:240]
                if len(item) > 240:
                    truncated = True
            else:
                rendered = str(item)
                fields[path or "/"] = rendered[:240]
                if len(rendered) > 240:
                    truncated = True

        visit(_json_safe(value), "")
        return {"fields": fields, "truncated": truncated}

    @staticmethod
    def _required_fields(projected: dict[str, Any], pointers: list[str]) -> dict[str, Any]:
        required: dict[str, Any] = {}
        warnings: list[str] = []
        for pointer in pointers:
            try:
                required[pointer] = _json_pointer_get(projected, pointer)
            except KeyError:
                # Dependency field names are generated during semantic
                # planning and are therefore hypotheses, not trustworthy
                # runtime facts.  Keep the complete content-addressed payload
                # available and expose the mismatch as auditable metadata
                # instead of killing the consumer before its executor runs.
                warnings.append(
                    f"依赖结果缺少规划声明字段 {pointer}；已保留完整上游结果供执行器判断"
                )
        if warnings:
            required["$contract_warnings"] = warnings
        return required

    @staticmethod
    def _run_key(run_dir: Path) -> str:
        return str(run_dir.resolve())

    @staticmethod
    def _persist_json(run_dir: Path, relative: Path, value: Any) -> str:
        target = run_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(
                json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
        return relative.as_posix()

    def _persist_full_payload(
        self, run_dir: Path, payload_hash: str, projected: dict[str, Any], policy: CommunicationPolicy,
    ) -> str:
        relative = Path("communication") / "payloads" / f"payload-{payload_hash}.json"
        if policy.persist_message_payloads:
            return self._persist_json(run_dir, relative, projected)
        # A reference must always be resolvable.  Even when routine payload
        # persistence is disabled, write content if dedup/externalisation uses it.
        return relative.as_posix()

    def _ensure_full_payload(
        self, run_dir: Path, payload_hash: str, projected: dict[str, Any], policy: CommunicationPolicy,
    ) -> str:
        relative = Path("communication") / "payloads" / f"payload-{payload_hash}.json"
        return self._persist_json(run_dir, relative, projected)

    def _externalize_structured(
        self,
        run_dir: Path,
        structured: dict[str, Any],
        required_fields: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str]:
        digest = content_sha256(structured)
        relative = Path("communication") / "structured" / f"structured-{digest}.json"
        reference = self._persist_json(run_dir, relative, structured)
        return {
            "$ref": reference,
            "sha256": digest,
            "media_type": "application/json",
            "bytes": utf8_size(structured),
            "preview": self._structured_preview(structured),
            "required_fields": required_fields or {},
        }, reference

    def _message_id(self, source: str, target: str, payload_hash: str) -> str:
        self._sequence += 1
        return f"msg-{source}-{target}-{payload_hash[:12]}-{self._sequence}"

    @staticmethod
    def _fits_message(
        source: str, target: str, result: dict[str, Any], budget: CommunicationBudget,
    ) -> bool:
        wire = _wire_payload(source, target, result)
        return (
            utf8_size(wire) <= budget.max_message_bytes
            and estimate_tokens(wire) <= budget.max_message_tokens
        )

    @staticmethod
    def _summary_budget(
        source: str,
        target: str,
        result_without_summary: dict[str, Any],
        budget: CommunicationBudget,
    ) -> CommunicationBudget | None:
        empty_wire = _wire_payload(source, target, result_without_summary)
        available_bytes = budget.max_message_bytes - utf8_size(empty_wire)
        available_tokens = budget.max_message_tokens - estimate_tokens(empty_wire)
        if available_bytes <= 0 or available_tokens <= 0 or budget.max_summary_chars <= 0:
            return None
        return budget.model_copy(update={
            "max_message_bytes": available_bytes,
            "max_message_tokens": available_tokens,
            "max_summary_chars": budget.max_summary_chars,
        })

    def _as_reference(
        self,
        *,
        message: MessageEnvelope,
        payload_ref: str,
        reference_to: str | None,
        preview_source: Any | None = None,
        keep_summary: bool = True,
        required_fields: dict[str, Any] | None = None,
    ) -> MessageEnvelope:
        # The canonical payload retains the complete path lists.  Bounding the
        # inline copy prevents a large evidence catalog from making the
        # content reference itself exceed the communication budget.
        output_artifacts = list(message.output_artifacts)
        evidence_refs = list(message.evidence_refs)
        return message.model_copy(update={
            "summary": message.summary if keep_summary else "",
            "structured_output": {
                "$ref": payload_ref,
                "$message_ref": reference_to,
                "sha256": message.payload_hash,
                "media_type": "application/json",
                "preview": self._structured_preview(
                    message.structured_output if preview_source is None else preview_source
                ),
                "required_fields": required_fields or {},
                "omitted_output_artifact_count": max(
                    0, len(output_artifacts) - 16,
                ),
                "omitted_evidence_ref_count": max(0, len(evidence_refs) - 16),
            },
            "output_artifacts": output_artifacts[:16],
            "evidence_refs": evidence_refs[:16],
            "payload_ref": payload_ref,
            "reference_to_message_id": reference_to,
            "reference_only": True,
            "message_type": "content_reference",
        })

    def _build_message(
        self,
        *,
        source: str,
        target: str,
        raw_result: dict[str, Any],
        run_dir: Path,
        policy: CommunicationPolicy,
        compressor: ContextCompressor,
        required_pointers: list[str] | None = None,
    ) -> tuple[MessageEnvelope, MessageMetrics, str]:
        budget = policy.budget
        projected = self._project_result(raw_result)
        required_fields = self._required_fields(projected, required_pointers or [])
        contract_warnings = list(
            required_fields.get("$contract_warnings", [])
        )
        payload_hash = content_sha256(projected)
        payload_ref = self._persist_full_payload(run_dir, payload_hash, projected, policy)
        # Another target has not received an earlier message with this hash.
        # Scope temporal references to the same logical receiver.
        seen_key = (self._run_key(run_dir), target, payload_hash)
        global_key = (self._run_key(run_dir), payload_hash)
        globally_seen = self._globally_seen_payloads.get(global_key)
        global_duplicate_observation = globally_seen is not None
        seen = self._seen_payloads.get(seen_key)
        message_id = self._message_id(source, target, payload_hash)

        raw_wire = _wire_payload(source, target, _json_safe(raw_result or {}))
        projected_wire = _wire_payload(source, target, projected)
        structured_externalized = False
        compression_fallback_used = False
        compression_warnings: list[str] = list(contract_warnings)
        attempted_compressor: str | None = None
        duplicate_scope = "none"

        # A fresh receiver may consume a shared reference only when all scalar
        # fields fit in the preview, or when its TaskNode explicitly declared
        # the fields that must remain inline.
        preview_complete = not self._structured_preview(
            projected["structured_output"]
        )["truncated"]
        global_reference_allowed = bool(
            policy.deduplicate_across_receivers
            and globally_seen is not None
            and (preview_complete or required_fields)
        )

        if policy.deduplicate_by_hash and (seen is not None or global_reference_allowed):
            # The persisted content, not transient chat history, is authoritative.
            payload_ref = self._ensure_full_payload(run_dir, payload_hash, projected, policy)
            message = self._as_reference(
                message=MessageEnvelope(
                message_id=message_id,
                source_node=source,
                target_node=target,
                payload_hash=payload_hash,
                summary=projected["summary"],
                structured_output=projected["structured_output"],
                output_artifacts=projected["output_artifacts"],
                evidence_refs=projected["evidence_refs"],
                ),
                payload_ref=payload_ref,
                reference_to=(seen or globally_seen).message_id,
                preview_source=projected["structured_output"],
                required_fields=required_fields,
            )
            without_summary = {**message.dependency_result(), "summary": ""}
            summary_budget = self._summary_budget(source, target, without_summary, budget)
            if summary_budget is None:
                message = message.model_copy(update={"summary": ""})
            else:
                compression = compressor.compress(projected["summary"], summary_budget)
                message = message.model_copy(update={"summary": compression.text})
                compression_fallback_used = compression.fallback_used
                compression_warnings = [
                    *contract_warnings,
                    *compression.warnings,
                ]
                attempted_compressor = compression.attempted_compressor
            compression_name = "hash_reference"
            exact_duplicate = True
            duplicate_scope = "receiver" if seen is not None else "run"
        else:
            delivered = _json_safe(projected)
            structured_size = utf8_size(delivered["structured_output"])
            protected_result = {**delivered, "summary": ""}
            if policy.externalize_large_payloads and (
                structured_size > budget.max_inline_structured_bytes
                or not self._fits_message(source, target, protected_result, budget)
            ):
                delivered["structured_output"], structured_ref = self._externalize_structured(
                    run_dir, delivered["structured_output"], required_fields,
                )
                payload_ref = structured_ref
                structured_externalized = True

            without_summary = {**delivered, "summary": ""}
            summary_budget = self._summary_budget(source, target, without_summary, budget)
            if summary_budget is None:
                compressed_summary = ""
                compression_name = "deterministic"
                was_compressed = bool(projected["summary"])
            else:
                compression = compressor.compress(projected["summary"], summary_budget)
                if not compression.within_budget:
                    raise CommunicationBudgetExceededError(
                        f"压缩器没有满足节点 {source} 到 {target} 的摘要预算"
                    )
                compressed_summary = compression.text
                compression_name = compression.compressor
                was_compressed = compression.compressed
                compression_fallback_used = compression.fallback_used
                compression_warnings = [
                    *contract_warnings,
                    *compression.warnings,
                ]
                attempted_compressor = compression.attempted_compressor
            delivered["summary"] = compressed_summary

            message = MessageEnvelope(
                message_id=message_id,
                source_node=source,
                target_node=target,
                summary=delivered["summary"],
                structured_output=delivered["structured_output"],
                output_artifacts=delivered["output_artifacts"],
                evidence_refs=delivered["evidence_refs"],
                payload_hash=payload_hash,
                payload_ref=payload_ref if structured_externalized else None,
            )
            exact_duplicate = False

            if not self._fits_message(source, target, message.dependency_result(), budget):
                # Free text is already empty/guarded.  Externalise the complete
                # projected result rather than dropping keys from valid JSON.
                payload_ref = self._ensure_full_payload(run_dir, payload_hash, projected, policy)
                message = self._as_reference(
                    message=message,
                    payload_ref=payload_ref,
                    reference_to=None,
                    preview_source=projected["structured_output"],
                    required_fields=required_fields,
                )
                structured_externalized = True
                compression_name = "payload_reference"
                was_compressed = True

            self._seen_payloads[seen_key] = _SeenPayload(
                message_id=message.message_id,
                payload_ref=(
                    self._ensure_full_payload(run_dir, payload_hash, projected, policy)
                    if policy.persist_message_payloads else payload_ref
                ),
            )

        canonical_ref = self._ensure_full_payload(
            run_dir, payload_hash, projected, policy,
        )
        self._globally_seen_payloads.setdefault(
            global_key,
            _SeenPayload(message_id=message.message_id, payload_ref=canonical_ref),
        )

        reference_payload = message.structured_output
        if isinstance(reference_payload, dict) and reference_payload.get("$ref"):
            # Delivery is rejected before executor invocation if the reference
            # cannot be resolved or its content-addressed hash is corrupted.
            resolve_content_reference(run_dir, reference_payload)

        delivered_result = message.dependency_result()
        within_budget = self._fits_message(source, target, delivered_result, budget)
        if not within_budget:
            raise CommunicationBudgetExceededError(
                f"节点 {source} 到 {target} 的受保护引用/路径超过消息预算；"
                "请提高 max_message_bytes/max_message_tokens"
            )

        if exact_duplicate:
            was_compressed = True
        metrics = MessageMetrics(
            message_id=message.message_id,
            source_node=source,
            target_node=target,
            raw_bytes=utf8_size(raw_wire),
            projected_bytes=utf8_size(projected_wire),
            delivered_bytes=utf8_size(_wire_payload(source, target, delivered_result)),
            raw_tokens=estimate_tokens(raw_wire),
            projected_tokens=estimate_tokens(projected_wire),
            delivered_tokens=estimate_tokens(_wire_payload(source, target, delivered_result)),
            compressor=compression_name,
            attempted_compressor=attempted_compressor,
            was_compressed=was_compressed,
            compression_fallback_used=compression_fallback_used,
            compression_warnings=compression_warnings,
            exact_duplicate=exact_duplicate,
            duplicate_scope=duplicate_scope,
            global_duplicate_observation=global_duplicate_observation,
            reference_only=message.reference_only,
            structured_payload_externalized=structured_externalized,
            within_message_budget=within_budget,
        )
        return message, metrics, canonical_ref

    @staticmethod
    def _within_node_budget(
        dependency_results: dict[str, dict[str, Any]], budget: CommunicationBudget,
    ) -> bool:
        if not dependency_results:
            return True
        return (
            utf8_size(dependency_results) <= budget.max_node_context_bytes
            and estimate_tokens(dependency_results) <= budget.max_node_context_tokens
        )

    def build(
        self,
        graph: TaskGraph,
        node: TaskNode,
        task_spec: dict[str, Any] | None = None,
        run_dir: str | Path = ".",
    ) -> ContextBuildResult:
        """Build direct-dependency results and metrics for one graph node."""

        # Resolve the canonical graph node so a caller cannot smuggle extra
        # dependency IDs through a modified copy.
        canonical_node = graph.get_node(node.node_id)
        direct_results = graph.direct_dependency_results(canonical_node.node_id)
        policy = self._policy_for(task_spec)
        if not policy.direct_dependencies_only:
            raise ValueError("当前 AgentPrune-lite 仅支持 direct_dependencies_only=true")
        compressor = self._compressor_for(policy)
        root = Path(run_dir)

        messages: list[MessageEnvelope] = []
        metrics: list[MessageMetrics] = []
        payload_refs: dict[str, str] = {}
        for source in canonical_node.dependencies:
            message, item_metrics, payload_ref = self._build_message(
                source=source,
                target=canonical_node.node_id,
                raw_result=direct_results.get(source) or {},
                run_dir=root,
                policy=policy,
                compressor=compressor,
                required_pointers=canonical_node.dependency_fields.get(source, []),
            )
            messages.append(message)
            metrics.append(item_metrics)
            payload_refs[source] = payload_ref

        dependency_results = {
            message.source_node: message.dependency_result() for message in messages
        }

        # If individually valid messages exceed the total node budget, replace
        # the largest full messages with content-addressed references first.
        while messages and not self._within_node_budget(dependency_results, policy.budget):
            candidates = [
                (index, item)
                for index, item in enumerate(messages)
                if not item.reference_only
            ]
            if not candidates:
                break
            index, current = max(
                candidates,
                key=lambda pair: utf8_size(pair[1].dependency_result()),
            )
            replacement = self._as_reference(
                message=current,
                payload_ref=payload_refs[current.source_node],
                reference_to=None,
                # The complete structured value is already protected by its
                # content-addressed payload.  Repeating its scalar preview for
                # every dependency defeats aggregate pruning (notably on the
                # global EvidenceVerifier fan-in).  Keep the bounded summary
                # needed to identify the upstream conclusion, but collapse the
                # redundant preview to an empty deterministic view.
                preview_source={},
                keep_summary=True,
                required_fields=self._required_fields(
                    self._project_result(direct_results.get(current.source_node) or {}),
                    canonical_node.dependency_fields.get(current.source_node, []),
                ),
            )
            resolve_content_reference(root, replacement.structured_output)
            messages[index] = replacement
            dependency_results[current.source_node] = replacement.dependency_result()
            old = metrics[index]
            delivered_wire = _wire_payload(
                current.source_node,
                canonical_node.node_id,
                replacement.dependency_result(),
            )
            metrics[index] = old.model_copy(update={
                "delivered_bytes": utf8_size(delivered_wire),
                "delivered_tokens": estimate_tokens(delivered_wire),
                "compressor": "node_budget_reference",
                "was_compressed": True,
                "reference_only": True,
                "structured_payload_externalized": True,
                "within_message_budget": self._fits_message(
                    current.source_node,
                    canonical_node.node_id,
                    replacement.dependency_result(),
                    policy.budget,
                ),
            })

        # Evidence verification is a deliberate fan-in: it must retain a
        # direct edge from every business node, but it does not need every
        # node's full structured result inline.  A large staged run can
        # therefore exceed the aggregate context target even after normal
        # per-message externalisation.  Collapse the inline view to signed
        # references, short summaries, artifact paths and evidence paths.  The
        # complete values remain in communication/payloads and can be
        # independently resolved by the verifier; no business fact is
        # silently rewritten or dropped from the persisted evidence plane.
        if (
            messages
            and not self._within_node_budget(dependency_results, policy.budget)
            and canonical_node.capability == "evidence_verification"
        ):
            compacted: dict[str, dict[str, Any]] = {}
            for index, message in enumerate(messages):
                structured = message.structured_output or {}
                if isinstance(structured, dict) and structured.get("$ref"):
                    preview = structured.get("preview") or {}
                    fields = preview.get("fields") if isinstance(preview, dict) else {}
                    if isinstance(fields, dict):
                        fields = dict(list(fields.items())[:4])
                    else:
                        fields = {}
                    compact_structured = {
                        "$ref": structured.get("$ref"),
                        "$message_ref": structured.get("$message_ref"),
                        "sha256": structured.get("sha256"),
                        "media_type": structured.get("media_type", "application/json"),
                        "preview": {"fields": fields, "truncated": True},
                        "required_fields": structured.get("required_fields") or {},
                    }
                else:
                    compact_structured = {}
                compact_message = message.model_copy(update={
                    "summary": message.summary[:160],
                    "structured_output": compact_structured,
                    "output_artifacts": message.output_artifacts[:16],
                    "evidence_refs": message.evidence_refs[:16],
                    "reference_only": True,
                    "message_type": "content_reference",
                })
                messages[index] = compact_message
                compacted[compact_message.source_node] = compact_message.dependency_result()
                old = metrics[index]
                delivered_wire = _wire_payload(
                    compact_message.source_node,
                    canonical_node.node_id,
                    compact_message.dependency_result(),
                )
                metrics[index] = old.model_copy(update={
                    "delivered_bytes": utf8_size(delivered_wire),
                    "delivered_tokens": estimate_tokens(delivered_wire),
                    "compressor": "evidence_fan_in_reference",
                    "was_compressed": True,
                    "reference_only": True,
                    "structured_payload_externalized": bool(structured.get("$ref"))
                    if isinstance(structured, dict) else False,
                    "within_message_budget": self._fits_message(
                        compact_message.source_node,
                        canonical_node.node_id,
                        compact_message.dependency_result(),
                        policy.budget,
                    ),
                })
            dependency_results = compacted

            # Keep the fallback deterministic if even the compact fan-in is
            # larger than the configured aggregate target.
            if not self._within_node_budget(dependency_results, policy.budget):
                for index, message in enumerate(messages):
                    compact_message = message.model_copy(update={
                        "summary": "",
                        "output_artifacts": message.output_artifacts[:8],
                        "evidence_refs": message.evidence_refs[:8],
                    })
                    messages[index] = compact_message
                    dependency_results[compact_message.source_node] = (
                        compact_message.dependency_result()
                    )

        within_node_budget = self._within_node_budget(dependency_results, policy.budget)
        if not within_node_budget:
            raise CommunicationBudgetExceededError(
                f"节点 {canonical_node.node_id} 的受保护依赖引用超过总上下文预算；"
                "请提高 max_node_context_bytes/max_node_context_tokens"
            )

        raw_bytes = sum(item.raw_bytes for item in metrics)
        projected_bytes = sum(item.projected_bytes for item in metrics)
        raw_tokens = sum(item.raw_tokens for item in metrics)
        projected_tokens = sum(item.projected_tokens for item in metrics)
        delivered_bytes = utf8_size(dependency_results) if dependency_results else 0
        delivered_tokens = estimate_tokens(dependency_results) if dependency_results else 0
        aggregate = CommunicationSummary(
            target_node=canonical_node.node_id,
            message_count=len(messages),
            raw_bytes=raw_bytes,
            projected_bytes=projected_bytes,
            delivered_bytes=delivered_bytes,
            raw_tokens=raw_tokens,
            projected_tokens=projected_tokens,
            delivered_tokens=delivered_tokens,
            exact_duplicate_count=sum(item.exact_duplicate for item in metrics),
            global_duplicate_observation_count=sum(
                item.global_duplicate_observation for item in metrics
            ),
            externalized_payload_count=sum(
                item.structured_payload_externalized for item in metrics
            ),
            within_node_budget=within_node_budget,
        )
        return ContextBuildResult(
            target_node=canonical_node.node_id,
            dependency_results=dependency_results,
            messages=messages,
            message_metrics=metrics,
            aggregate_metrics=aggregate,
        )

    def build_execution_context(
        self,
        graph: TaskGraph,
        node: TaskNode,
        task_spec: dict[str, Any],
        run_dir: str | Path,
    ) -> tuple[NodeExecutionContext, ContextBuildResult]:
        """Convenience bridge for the current ``NodeExecutor`` contract."""

        built = self.build(graph, node, task_spec, run_dir)
        context = NodeExecutionContext(
            graph_id=graph.graph_id,
            node=graph.get_node(node.node_id).model_copy(deep=True),
            task_spec=task_spec,
            dependency_results=built.dependency_results,
            input_artifacts=list(node.input_artifacts),
            run_dir=str(run_dir),
        )
        return context, built
