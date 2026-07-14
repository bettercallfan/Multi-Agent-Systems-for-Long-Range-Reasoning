"""Registry and deterministic router for heterogeneous node executors."""

from __future__ import annotations

import inspect
from typing import Any

from pydantic import BaseModel, Field

from orchestration.node_executor import ExecutorDescriptor, NodeExecutor
from orchestration.task_graph import TaskNode


class RoutingCandidate(BaseModel):
    """Serializable score inputs for one eligible executor."""

    rank: int = Field(ge=1)
    executor_id: str
    quality_score: float
    cost_score: float
    latency_score: float


class RoutingDecision(BaseModel):
    """Auditable explanation of a capability routing decision."""

    node_id: str
    requested_capability: str
    selected_executor_id: str
    candidates: list[RoutingCandidate]
    reason: str


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

    def __init__(self) -> None:
        self._executors: dict[str, NodeExecutor] = {}

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

    def find_candidates(self, capability: str) -> list[NodeExecutor]:
        """Return eligible executors in deterministic selection order."""
        requested = capability.strip()
        if not requested:
            raise ValueError("capability 不能为空")
        candidates = [
            executor
            for executor in self._executors.values()
            if executor.descriptor.available
            and requested in executor.descriptor.capabilities
        ]
        return sorted(candidates, key=self._selection_key)

    def select_executor(self, node: TaskNode) -> tuple[NodeExecutor, RoutingDecision]:
        """Select an executor and return a persistable routing explanation."""
        candidates = self.find_candidates(node.capability)
        if not candidates:
            raise CapabilityNotFoundError(
                f"节点 {node.node_id} 没有可用执行器支持能力: {node.capability}"
            )

        ranked = [
            RoutingCandidate(
                rank=index,
                executor_id=executor.descriptor.executor_id,
                quality_score=executor.descriptor.quality_score,
                cost_score=executor.descriptor.cost_score,
                latency_score=executor.descriptor.latency_score,
            )
            for index, executor in enumerate(candidates, start=1)
        ]
        selected = candidates[0]
        decision = RoutingDecision(
            node_id=node.node_id,
            requested_capability=node.capability,
            selected_executor_id=selected.descriptor.executor_id,
            candidates=ranked,
            reason=(
                "仅考虑 available=true 且 capability 精确匹配的执行器；"
                "依次按 quality_score 降序、cost_score 升序、"
                "latency_score 升序和 executor_id 升序选择。"
            ),
        )
        return selected, decision

    @staticmethod
    def _selection_key(executor: NodeExecutor) -> tuple[Any, ...]:
        descriptor = executor.descriptor
        return (
            -descriptor.quality_score,
            descriptor.cost_score,
            descriptor.latency_score,
            descriptor.executor_id,
        )

