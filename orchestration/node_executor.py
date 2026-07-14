"""Common contracts for executors that run :class:`TaskNode` instances.

The scheduler talks only to this interface.  An implementation may call an
LLM agent, execute Python, invoke a deterministic parser, or proxy a remote
service; those implementation details are intentionally hidden from the
scheduler.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator, model_validator

from orchestration.task_graph import TaskNode


class ExecutorDescriptor(BaseModel):
    """Static, serializable information used for capability-based routing."""

    executor_id: str
    executor_type: str
    capabilities: list[str]
    cost_score: float = Field(default=1.0, ge=0)
    latency_score: float = Field(default=1.0, ge=0)
    quality_score: float = Field(default=1.0, ge=0)
    resource_location: str = "cloud"
    security_levels: list[str] = Field(default_factory=lambda: ["internal"])
    available: bool = True

    @field_validator("executor_id", "executor_type", "resource_location")
    @classmethod
    def _require_non_empty_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空")
        return normalized

    @field_validator("capabilities", "security_levels")
    @classmethod
    def _normalize_unique_values(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = value.strip()
            if not item:
                raise ValueError("列表中不能包含空值")
            if item not in seen:
                seen.add(item)
                normalized.append(item)
        if not normalized:
            raise ValueError("列表不能为空")
        return normalized


class NodeExecutionContext(BaseModel):
    """Minimum context visible to one node executor.

    ``dependency_results`` is deliberately restricted to the node's direct
    dependencies.  This turns DAG edges into the first version of the sparse
    communication topology and prevents accidental whole-graph broadcasts.
    """

    graph_id: str
    node: TaskNode
    task_spec: dict[str, Any]
    dependency_results: dict[str, dict[str, Any]] = Field(default_factory=dict)
    input_artifacts: list[str] = Field(default_factory=list)
    run_dir: str

    @field_validator("graph_id", "run_dir")
    @classmethod
    def _require_context_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空")
        return normalized

    @model_validator(mode="after")
    def _allow_only_direct_dependency_results(self) -> "NodeExecutionContext":
        dependency_ids = set(self.node.dependencies)
        unexpected = sorted(set(self.dependency_results) - dependency_ids)
        if unexpected:
            raise ValueError(
                "dependency_results 只能包含当前节点的直接依赖: "
                + ", ".join(unexpected)
            )
        return self


class NodeExecutionResult(BaseModel):
    """Serializable result returned by every executor implementation."""

    node_id: str
    executor_id: str
    status: Literal["completed", "failed"]
    summary: str = ""
    output_artifacts: list[str] = Field(default_factory=list)
    structured_output: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    error_type: str | None = None
    error_message: str | None = None
    token_usage: dict[str, int] = Field(default_factory=dict)
    duration_seconds: float = Field(default=0, ge=0)

    @field_validator("node_id", "executor_id")
    @classmethod
    def _require_result_identity(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空")
        return normalized

    @field_validator("output_artifacts", "evidence_refs")
    @classmethod
    def _deduplicate_references(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @field_validator("token_usage")
    @classmethod
    def _validate_token_usage(cls, values: dict[str, int]) -> dict[str, int]:
        if any(value < 0 for value in values.values()):
            raise ValueError("token_usage 不能包含负数")
        return values


@runtime_checkable
class NodeExecutor(Protocol):
    """Uniform asynchronous interface consumed by ``GraphScheduler``."""

    descriptor: ExecutorDescriptor

    async def execute(self, context: NodeExecutionContext) -> NodeExecutionResult:
        """Execute one node and return a structured, serializable result."""
        ...

