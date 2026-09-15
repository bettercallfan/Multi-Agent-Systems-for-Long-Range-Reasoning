"""Registry and deterministic router for heterogeneous node executors."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from orchestration.execution.executor_scoring import (
    DyLANLiteScorer,
    ExecutorScore,
    ExecutorScorer,
    ExecutorStatsStore,
    StaticExecutorScorer,
    token_count,
)
from orchestration.execution.node_executor import ExecutorDescriptor, NodeExecutor
from orchestration.execution.resource_registry import ResourceRegistry, default_resource_registry
from orchestration.graph.task_graph import TaskNode


class RoutingCandidate(BaseModel):
    """Serializable score inputs for one eligible executor."""

    rank: int = Field(ge=1)
    executor_id: str
    quality_score: float
    cost_score: float
    latency_score: float
    effective_score: float = 0.0
    history_snapshot: dict[str, Any] = Field(default_factory=dict)
    score_components: dict[str, float] = Field(default_factory=dict)
    score_reason: str = ""
    resource_id: str | None = None
    resource_location: str | None = None
    model_name: str | None = None


class RoutingDecision(BaseModel):
    """Auditable explanation of a capability routing decision."""

    node_id: str
    requested_capability: str
    selected_executor_id: str
    candidates: list[RoutingCandidate]
    reason: str
    task_type: str = "unknown"
    effective_score: float = 0.0
    history_snapshot: dict[str, Any] = Field(default_factory=dict)
    scoring_policy: str = "static"
    excluded_executor_ids: list[str] = Field(default_factory=list)
    selected_resource_id: str | None = None
    resource_location: str | None = None
    model_name: str | None = None
    resource_reason: str = ""


class CapabilityNotFoundError(LookupError):
    """Raised when no available executor can satisfy a node capability."""


class CapabilityRegistry:
    """Store executors and select one by a stable, explainable policy.

    Selection precedence is intentionally simple for the MVP:

    1. executor must be available;
    2. capability must match exactly;
    3. higher quality wins;
    4. then lower cost and lower latency;
    5. executor id is the final deterministic tie-breaker.
    """

    def __init__(
        self,
        *,
        scorer: ExecutorScorer | None = None,
        stats_path: str | Path | None = None,
        stats_store: ExecutorStatsStore | None = None,
        use_runtime_history: bool = False,
        resource_registry: ResourceRegistry | None = None,
    ) -> None:
        """Create a registry.

        Existing callers get the original deterministic static policy.  New
        callers can pass ``use_runtime_history=True`` (or an explicit scorer)
        to enable DyLAN-lite routing.  A supplied store takes precedence over
        ``stats_path`` so tests and embedding applications can share state.
        """
        if stats_store is not None and stats_path is not None:
            raise ValueError("stats_store 与 stats_path 不能同时传入")
        self._executors: dict[str, NodeExecutor] = {}
        self.stats_store = stats_store or ExecutorStatsStore(stats_path)
        self.scorer: ExecutorScorer = scorer or (
            DyLANLiteScorer() if use_runtime_history else StaticExecutorScorer()
        )
        self.resource_registry = resource_registry or default_resource_registry()

    def register(self, executor: NodeExecutor) -> None:
        """Register one executor, rejecting malformed or duplicate entries."""
        descriptor = getattr(executor, "descriptor", None)
        if not isinstance(descriptor, ExecutorDescriptor):
            raise TypeError("executor.descriptor 必须是 ExecutorDescriptor")
        execute = getattr(executor, "execute", None)
        if not callable(execute) or not inspect.iscoroutinefunction(execute):
            raise TypeError("executor.execute 必须是 async 方法")
        if descriptor.executor_id in self._executors:
            raise ValueError(f"executor_id 已注册: {descriptor.executor_id}")
        self._executors[descriptor.executor_id] = executor

    def unregister(self, executor_id: str) -> NodeExecutor:
        """Remove and return an executor; fail loudly for unknown ids."""
        try:
            return self._executors.pop(executor_id)
        except KeyError as exc:
            raise KeyError(f"executor_id 未注册: {executor_id}") from exc

    def get_executor(self, executor_id: str) -> NodeExecutor:
        """Return a registered executor by id."""
        try:
            return self._executors[executor_id]
        except KeyError as exc:
            raise KeyError(f"executor_id 未注册: {executor_id}") from exc

    def list_capabilities(self) -> list[str]:
        """List capabilities currently offered by available executors."""
        return sorted({
            capability
            for executor in self._executors.values()
            if executor.descriptor.available
            for capability in executor.descriptor.capabilities
        })

    def list_executors(self) -> list[ExecutorDescriptor]:
        """Return stable descriptor snapshots for observability and diagnostics."""

        return [
            self._executors[executor_id].descriptor.model_copy(deep=True)
            for executor_id in sorted(self._executors)
        ]

    def find_candidates(
        self,
        capability: str,
        task_type: str | None = None,
        excluded_executor_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    ) -> list[NodeExecutor]:
        """Return eligible executors in deterministic selection order."""
        requested = capability.strip()
        if not requested:
            raise ValueError("capability 不能为空")
        excluded = {
            executor_id.strip()
            for executor_id in (excluded_executor_ids or [])
            if executor_id.strip()
        }
        candidates = [
            executor
            for executor in self._executors.values()
            if executor.descriptor.available
            and requested in executor.descriptor.capabilities
            and executor.descriptor.executor_id not in excluded
        ]
        task = (task_type or "unknown").strip() or "unknown"
        return [entry[0] for entry in self._rank_candidates(candidates, task)]

    def select_executor(
        self,
        node: TaskNode,
        task_type: str | None = None,
        excluded_executor_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    ) -> tuple[NodeExecutor, RoutingDecision]:
        """Select an executor and return a persistable routing explanation."""
        task = (task_type or "unknown").strip() or "unknown"
        excluded = sorted({
            executor_id.strip()
            for executor_id in (excluded_executor_ids or [])
            if executor_id.strip()
        })
        eligible = [
            executor
            for executor in self._executors.values()
            if executor.descriptor.available
            and node.capability in executor.descriptor.capabilities
            and executor.descriptor.executor_id not in excluded
        ]
        eligible = [executor for executor in eligible if self._resource_allowed(executor, node)]
        ranked_entries = self._rank_candidates(eligible, task)
        if not ranked_entries:
            exclusion_note = f"；已排除: {', '.join(excluded)}" if excluded else ""
            raise CapabilityNotFoundError(
                f"节点 {node.node_id} 没有可用执行器支持能力: "
                f"{node.capability}{exclusion_note}"
            )

        ranked = [
            RoutingCandidate(
                rank=index,
                executor_id=executor.descriptor.executor_id,
                quality_score=executor.descriptor.quality_score,
                cost_score=executor.descriptor.cost_score,
                latency_score=executor.descriptor.latency_score,
                effective_score=score.effective_score,
                history_snapshot=self.stats_store.get(
                    executor.descriptor.executor_id, task
                ).snapshot(),
                score_components=score.components,
                score_reason=score.reason,
                resource_id=self._resource_id(executor.descriptor),
                resource_location=executor.descriptor.resource_location,
                model_name=self._model_name(executor.descriptor),
            )
            for index, (executor, score) in enumerate(ranked_entries, start=1)
        ]
        selected, selected_score = ranked_entries[0]
        selected_history = self.stats_store.get(
            selected.descriptor.executor_id, task
        ).snapshot()
        decision = RoutingDecision(
            node_id=node.node_id,
            requested_capability=node.capability,
            selected_executor_id=selected.descriptor.executor_id,
            candidates=ranked,
            task_type=task,
            effective_score=selected_score.effective_score,
            history_snapshot=selected_history,
            scoring_policy=self.scorer.name,
            excluded_executor_ids=excluded,
            reason=self._decision_reason(excluded),
            selected_resource_id=self._resource_id(selected.descriptor),
            resource_location=selected.descriptor.resource_location,
            model_name=self._model_name(selected.descriptor),
            resource_reason=self._resource_reason(selected, node),
        )
        return selected, decision

    def _resource_allowed(self, executor: NodeExecutor, node: TaskNode) -> bool:
        requirements = node.resource_requirements or self._inferred_requirements(node)
        if not requirements:
            return True
        descriptor = executor.descriptor
        location = descriptor.resource_location
        location = "device" if location in {"local", "device", "bounded_local"} else location
        allowed = requirements.get("allowed_locations")
        if allowed and location not in set(allowed):
            return False
        if not self.resource_registry.has_available_location(location):
            return False
        preferred = requirements.get("preferred_location")
        # Preference is handled by sorting; it is not a hard constraint.
        sensitivity = requirements.get("data_sensitivity", "internal")
        security_order = {"public": 0, "internal": 1, "bounded_local": 1, "confidential": 2}
        if max((security_order.get(s, 0) for s in descriptor.security_levels), default=0) < security_order.get(sensitivity, 1):
            return False
        if descriptor.quality_score < float(requirements.get("min_compute_score", 0)):
            return False
        resource_id = descriptor.resource_id
        if resource_id and resource_id in {item.resource_id for item in self.resource_registry.list_available()}:
            resource = self.resource_registry.get(resource_id)
            if resource.latency_ms > float(requirements.get("max_latency_ms", float("inf"))):
                return False
        return True

    @staticmethod
    def _inferred_requirements(node: TaskNode) -> dict[str, Any]:
        """Conservative defaults keep existing plans resource-aware."""
        if node.capability in {"math_modeling", "reasoning", "code"}:
            return {"preferred_location": "cloud", "allowed_locations": ["edge", "cloud"], "min_compute_score": 0.6}
        if node.capability in {"terminal_execution", "document_extraction", "input_normalization"}:
            return {"preferred_location": "device", "allowed_locations": ["device", "edge"], "data_sensitivity": "internal"}
        if node.capability == "evidence_verification":
            return {"preferred_location": "edge", "allowed_locations": ["device", "edge", "cloud"], "data_sensitivity": "internal"}
        return {}

    @staticmethod
    def _resource_id(descriptor: ExecutorDescriptor) -> str:
        if descriptor.resource_id:
            return descriptor.resource_id
        location = "device" if descriptor.resource_location in {"local", "bounded_local"} else descriptor.resource_location
        return f"{location}-default"

    def _model_name(self, descriptor: ExecutorDescriptor) -> str | None:
        if descriptor.model_name:
            return descriptor.model_name
        resource_id = self._resource_id(descriptor)
        try:
            resource = self.resource_registry.get(resource_id)
        except KeyError:
            location = "device" if descriptor.resource_location in {"local", "bounded_local"} else descriptor.resource_location
            resource = next(
                (item for item in self.resource_registry.snapshot()
                 if item.get("location") == location),
                None,
            )
            return str(resource.get("model_name") or "") or None if resource else None
        return resource.model_name or None

    @staticmethod
    def _resource_reason(executor: NodeExecutor, node: TaskNode) -> str:
        requirements = node.resource_requirements or {}
        location = executor.descriptor.resource_location
        if location in {"local", "bounded_local"}:
            location = "device"
        return f"选择 {location} 资源：满足 {requirements or '默认'}"

    def record_outcome(
        self,
        executor_id: str,
        task_type: str = "unknown",
        *,
        success: bool | None = None,
        quality_passed: bool | None = None,
        token_usage: int | dict[str, int] | None = None,
        latency_seconds: float | None = None,
        result: Any | None = None,
    ):
        """Record one executor outcome for future DyLAN-lite decisions.

        ``result`` may be a ``NodeExecutionResult`` or a compatible dictionary.
        Explicit keyword arguments override values derived from the result.
        """
        self.get_executor(executor_id)  # Reject typos instead of poisoning stats.
        payload: dict[str, Any] = {}
        if result is not None:
            if hasattr(result, "model_dump"):
                payload = result.model_dump(mode="json")
            elif isinstance(result, dict):
                payload = result
            else:
                raise TypeError("result 必须是 NodeExecutionResult 或字典")
        if success is None:
            status = payload.get("status")
            if status is None:
                raise ValueError("必须提供 success 或带 status 的 result")
            success = status in {"completed", "passed", "success"}
        if token_usage is None:
            token_usage = payload.get("token_usage")
        if latency_seconds is None:
            latency_seconds = float(payload.get("duration_seconds", 0.0) or 0.0)
        return self.stats_store.record(
            executor_id,
            (task_type or "unknown").strip() or "unknown",
            success=success,
            quality_passed=quality_passed,
            tokens=token_count(token_usage),
            latency_seconds=latency_seconds,
        )

    def _rank_candidates(
        self,
        candidates: list[NodeExecutor],
        task_type: str,
    ) -> list[tuple[NodeExecutor, ExecutorScore]]:
        scored = [
            (
                executor,
                self.scorer.score(
                    executor.descriptor,
                    self.stats_store.get(executor.descriptor.executor_id, task_type),
                ),
            )
            for executor in candidates
        ]
        if isinstance(self.scorer, StaticExecutorScorer):
            # Preserve the pre-DyLAN public behavior exactly.
            return sorted(scored, key=lambda entry: self._selection_key(entry[0]))
        return sorted(
            scored,
            key=lambda entry: (
                -entry[1].effective_score,
                *self._selection_key(entry[0]),
            ),
        )

    def _decision_reason(self, excluded: list[str]) -> str:
        if isinstance(self.scorer, StaticExecutorScorer):
            policy = (
                "依次按 quality_score 降序、cost_score 升序、"
                "latency_score 升序和 executor_id 升序选择"
            )
        else:
            policy = (
                f"使用 {self.scorer.name} effective_score 排序；"
                "静态能力为冷启动先验，历史置信度随运行次数渐进增加"
            )
        exclusion = f"；跳过已尝试执行器 {', '.join(excluded)}" if excluded else ""
        return (
            "仅考虑 available=true 且 capability 精确匹配的执行器；"
            f"{policy}{exclusion}。"
        )

    @staticmethod
    def _selection_key(executor: NodeExecutor) -> tuple[Any, ...]:
        descriptor = executor.descriptor
        return (
            -descriptor.quality_score,
            descriptor.cost_score,
            descriptor.latency_score,
            descriptor.executor_id,
        )
