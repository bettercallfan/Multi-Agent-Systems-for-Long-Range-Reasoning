"""Typed contracts for sparse, budgeted inter-node communication.

These models deliberately keep the communication plane independent from the
graph scheduler.  A scheduler can adopt :class:`ContextBuildResult` without
changing the executor contract: ``dependency_results`` is already in the shape
accepted by
:class:`orchestration.execution.node_executor.NodeExecutionContext`.
"""

from __future__ import annotations

from hashlib import sha256
import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator


def canonical_json(value: Any) -> str:
    """Return stable compact JSON used by hashing and byte/token metrics."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def utf8_size(value: str | Any) -> int:
    """Measure UTF-8 bytes, serialising non-string values deterministically."""

    text = value if isinstance(value, str) else canonical_json(value)
    return len(text.encode("utf-8"))


def estimate_tokens(value: str | Any) -> int:
    """Conservatively estimate tokens for mixed Chinese/English text.

    No tokenizer dependency is required.  Each non-ASCII code point counts as
    one token, ASCII word runs count as one token per four characters, and
    punctuation counts individually.  This intentionally errs above the usual
    count for compact JSON so a local budget is not silently exceeded.
    Provider-reported token usage should still be recorded separately.
    """

    text = value if isinstance(value, str) else canonical_json(value)
    if not text:
        return 0
    count = 0
    for part in re.findall(r"[^\x00-\x7f]|[A-Za-z0-9_]+|\s+|[^A-Za-z0-9_\s]", text):
        if len(part) == 1 and ord(part) > 127:
            count += 1
        elif part[0].isalnum() or part[0] == "_":
            count += math.ceil(len(part) / 4)
        elif part.isspace():
            count += math.ceil(len(part) / 8)
        else:
            count += len(part)
    return count


class CommunicationBudget(BaseModel):
    """Communication targets plus an optional provider input-limit fact."""

    model_config = ConfigDict(extra="forbid")

    max_message_bytes: int = Field(default=12_000, ge=1)
    max_message_tokens: int = Field(default=1_500, ge=1)
    max_node_context_bytes: int = Field(default=48_000, ge=1)
    max_node_context_tokens: int = Field(default=5_000, ge=1)
    # Unlike the communication targets above, this is only used when the
    # deployment explicitly knows the model's actual context window.
    model_input_limit_tokens: int | None = Field(default=None, ge=1)
    max_control_prompt_tokens: int = Field(default=8_000, ge=1)
    max_summary_chars: int = Field(default=2_000, ge=0)
    max_inline_structured_bytes: int = Field(default=8_000, ge=1)


class CommunicationPolicy(BaseModel):
    """Configurable information-bottleneck policy.

    ``from_task_spec`` accepts both a nested ``budget`` object and the flat
    fields originally proposed for ``TaskSpec.communication_policy``.
    """

    model_config = ConfigDict(extra="forbid")

    compressor: Literal["deterministic", "llmlingua", "auto"] = "auto"
    budget: CommunicationBudget = Field(default_factory=CommunicationBudget)
    llmlingua_threshold_tokens: int = Field(default=512, ge=32)
    llmlingua_model: str = "microsoft/llmlingua-2-xlm-roberta-large-meetingbank"
    llmlingua_target_ratio: float = Field(default=0.6, gt=0.0, le=1.0)
    llmlingua_device_map: str = "cpu"
    llmlingua_allow_download: bool = False
    preserve_structured_fields: bool = True
    deduplicate_by_hash: bool = True
    deduplicate_across_receivers: bool = True
    direct_dependencies_only: bool = True
    externalize_large_payloads: bool = True
    persist_message_payloads: bool = True

    @field_validator("llmlingua_model", "llmlingua_device_map")
    @classmethod
    def _require_model_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("llmlingua_model 不能为空")
        return value.strip()

    @model_validator(mode="before")
    @classmethod
    def _accept_flat_budget_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        budget_data = dict(data.get("budget") or {})
        for name in CommunicationBudget.model_fields:
            if name in data:
                budget_data[name] = data.pop(name)
        if budget_data:
            data["budget"] = budget_data
        return data

    @classmethod
    def from_task_spec(cls, task_spec: dict[str, Any] | None) -> "CommunicationPolicy":
        spec = task_spec or {}
        value = dict(spec.get("communication_policy") or {})
        routing = spec.get("routing_policy") or {}
        if "direct_dependencies_only" not in value and isinstance(routing, dict):
            value["direct_dependencies_only"] = routing.get(
                "direct_dependencies_only", True
            )
        return cls.model_validate(value)


class MessageEnvelope(BaseModel):
    """Low-entropy structured message sent across one graph edge."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    message_id: str
    source_node: str
    target_node: str
    message_type: Literal["dependency_result", "content_reference"] = "dependency_result"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    visibility: Literal["direct_dependencies"] = "direct_dependencies"
    summary: str = ""
    structured_output: dict[str, Any] = Field(default_factory=dict)
    output_artifacts: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    payload_hash: str
    payload_ref: str | None = None
    reference_to_message_id: str | None = None
    reference_only: bool = False

    @field_validator("message_id", "source_node", "target_node", "payload_hash")
    @classmethod
    def _require_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("消息标识不能为空")
        return value.strip()

    @field_validator("payload_hash")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("payload_hash 必须是 SHA256 十六进制值")
        return value

    @field_validator("output_artifacts", "evidence_refs")
    @classmethod
    def _deduplicate_paths(cls, values: list[str]) -> list[str]:
        # Keep the first spelling and order.  Paths are protected data and are
        # never passed through a natural-language compressor.
        return list(dict.fromkeys(value for value in values if value))

    @model_validator(mode="after")
    def _reference_must_have_target(self) -> "MessageEnvelope":
        if self.reference_only and not (self.payload_ref or self.reference_to_message_id):
            raise ValueError("引用消息必须包含 payload_ref 或 reference_to_message_id")
        if self.reference_only:
            self.message_type = "content_reference"
        return self

    def dependency_result(self) -> dict[str, Any]:
        """Project the envelope to the existing executor-facing result shape."""

        return {
            "summary": self.summary,
            "structured_output": self.structured_output,
            "output_artifacts": list(self.output_artifacts),
            "evidence_refs": list(self.evidence_refs),
        }


class CompressionResult(BaseModel):
    """Result of compressing only one free-text field."""

    model_config = ConfigDict(extra="forbid")

    text: str
    compressor: str
    original_bytes: int = Field(ge=0)
    delivered_bytes: int = Field(ge=0)
    original_tokens: int = Field(ge=0)
    delivered_tokens: int = Field(ge=0)
    within_budget: bool
    compressed: bool = False
    truncated: bool = False
    fallback_used: bool = False
    warnings: list[str] = Field(default_factory=list)
    attempted_compressor: str | None = None

    @computed_field
    @property
    def compression_ratio(self) -> float:
        if self.delivered_bytes == 0:
            return float(max(1, self.original_bytes))
        return round(self.original_bytes / self.delivered_bytes, 4)


class MessageMetrics(BaseModel):
    """Raw, projected, and delivered sizes for one dependency edge."""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    source_node: str
    target_node: str
    raw_bytes: int = Field(ge=0)
    projected_bytes: int = Field(ge=0)
    delivered_bytes: int = Field(ge=0)
    raw_tokens: int = Field(ge=0)
    projected_tokens: int = Field(ge=0)
    delivered_tokens: int = Field(ge=0)
    compressor: str = "none"
    attempted_compressor: str | None = None
    was_compressed: bool = False
    compression_fallback_used: bool = False
    compression_warnings: list[str] = Field(default_factory=list)
    exact_duplicate: bool = False
    duplicate_scope: Literal["none", "receiver", "run"] = "none"
    global_duplicate_observation: bool = False
    reference_only: bool = False
    structured_payload_externalized: bool = False
    within_message_budget: bool = True

    @computed_field
    @property
    def compression_ratio(self) -> float:
        if self.delivered_bytes == 0:
            return float(max(1, self.raw_bytes))
        return round(self.raw_bytes / self.delivered_bytes, 4)

class CommunicationSummary(BaseModel):
    """Aggregate metrics for the context delivered to one target node."""

    model_config = ConfigDict(extra="forbid")

    target_node: str
    message_count: int = Field(ge=0)
    raw_bytes: int = Field(ge=0)
    projected_bytes: int = Field(default=0, ge=0)
    delivered_bytes: int = Field(ge=0)
    raw_tokens: int = Field(ge=0)
    projected_tokens: int = Field(default=0, ge=0)
    delivered_tokens: int = Field(ge=0)
    exact_duplicate_count: int = Field(ge=0)
    global_duplicate_observation_count: int = Field(default=0, ge=0)
    externalized_payload_count: int = Field(ge=0)
    within_node_budget: bool

    @computed_field
    @property
    def compression_ratio(self) -> float:
        if self.delivered_bytes == 0:
            return float(max(1, self.raw_bytes))
        return round(self.raw_bytes / self.delivered_bytes, 4)

    @computed_field
    @property
    def redundancy_rate(self) -> float:
        if self.message_count == 0:
            return 0.0
        return round(self.exact_duplicate_count / self.message_count, 4)

    @computed_field
    @property
    def global_duplicate_rate(self) -> float:
        if self.message_count == 0:
            return 0.0
        return round(self.global_duplicate_observation_count / self.message_count, 4)


class ContextBuildResult(BaseModel):
    """Scheduler-neutral output of the sparse context builder."""

    model_config = ConfigDict(extra="forbid")

    target_node: str
    dependency_results: dict[str, dict[str, Any]] = Field(default_factory=dict)
    messages: list[MessageEnvelope] = Field(default_factory=list)
    message_metrics: list[MessageMetrics] = Field(default_factory=list)
    aggregate_metrics: CommunicationSummary

    @model_validator(mode="after")
    def _messages_and_results_match(self) -> "ContextBuildResult":
        sources = [message.source_node for message in self.messages]
        if set(sources) != set(self.dependency_results):
            raise ValueError("messages 与 dependency_results 的来源节点不一致")
        if len(self.messages) != len(self.message_metrics):
            raise ValueError("每条消息必须有且只有一条指标记录")
        return self


def content_sha256(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()
