"""Typed dynamic task graph and deterministic DAG state transitions."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class GraphValidationError(ValueError):
    """Raised when a planned graph violates a framework invariant."""


class InvalidNodeTransitionError(RuntimeError):
    """Raised when code attempts an illegal runtime state transition."""


class NodeStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class TaskNode(BaseModel):
    """One executable unit. Planning fields and runtime fields are separated."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    description: str
    capability: str
    dependencies: list[str] = Field(default_factory=list)
    # Optional AgentPrune-lite contract.  Values are JSON Pointers into the
    # dependency result (for example ``/structured_output/amount``) that must
    # remain inline even when the rest of a repeated/large payload is a $ref.
    dependency_fields: dict[str, list[str]] = Field(default_factory=dict)
    input_artifacts: list[str] = Field(default_factory=list)
    output_artifacts: list[str] = Field(default_factory=list)
    # Frozen requirement ownership survives PlanIR -> SemanticGraph ->
    # executable graph compilation.  Runtime executors may read these fields,
    # but only the framework acceptance layer interprets them.
    requirement_ids: list[str] = Field(default_factory=list)
    acceptance_refs: list[str] = Field(default_factory=list)
    expected_output: dict[str, Any] | None = None
    acceptance_criteria: list[dict[str, Any]] = Field(default_factory=list)
    # Scheduler-local completion checks remain deliberately small.  Business
    # acceptance above is evaluated later by AcceptanceRunner.
    success_criteria: list[dict[str, Any]] = Field(default_factory=list)
    max_retries: int = Field(default=1, ge=0)
    #端-边-云资源约束；由框架路由器解释，规划器只能提出候选策略。
    resource_requirements: dict[str, Any] = Field(default_factory=dict)

    # Runtime fields below are always reset after an LLM plan is parsed.
    status: NodeStatus = NodeStatus.PENDING
    assigned_executor: str | None = None
    attempts: int = Field(default=0, ge=0)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    # Framework-owned feedback attached only when a completed node is reopened
    # after independent business validation.  Planner-provided values are
    # discarded by ``reset_runtime_state``.
    recovery_context: dict[str, Any] = Field(default_factory=dict)

    @field_validator("node_id")
    @classmethod
    def validate_node_id(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", value):
            raise ValueError("node_id 必须以字母开头，且只能包含字母、数字、_、-")
        return value

    @field_validator("description", "capability")
    @classmethod
    def require_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("description/capability 不能为空")
        return value

    @field_validator(
        "dependencies", "input_artifacts", "output_artifacts",
        "requirement_ids", "acceptance_refs",
    )
    @classmethod
    def normalize_unique_lists(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("依赖和产物路径不能包含空值")
        if len(normalized) != len(set(normalized)):
            raise ValueError("依赖和产物路径不能重复")
        return normalized

    @model_validator(mode="after")
    def validate_dependency_fields(self) -> "TaskNode":
        unknown = sorted(set(self.dependency_fields) - set(self.dependencies))
        if unknown:
            raise ValueError(
                "dependency_fields 只能引用直接依赖: " + ", ".join(unknown)
            )
        normalized: dict[str, list[str]] = {}
        for dependency, pointers in self.dependency_fields.items():
            clean = [pointer.strip() for pointer in pointers]
            if any(not pointer.startswith("/") for pointer in clean):
                raise ValueError("dependency_fields 必须使用以 / 开头的 JSON Pointer")
            if len(clean) != len(set(clean)):
                raise ValueError("dependency_fields 中的 JSON Pointer 不能重复")
            normalized[dependency] = clean
        self.dependency_fields = normalized
        return self


class TaskGraph(BaseModel):
    """A validated directed acyclic execution graph."""

    model_config = ConfigDict(extra="forbid")

    graph_id: str
    version: int = Field(default=1, ge=1)
    goal: str
    nodes: list[TaskNode]

    @field_validator("graph_id", "goal")
    @classmethod
    def require_graph_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("graph_id/goal 不能为空")
        return value

    @model_validator(mode="after")
    def validate_on_creation(self) -> "TaskGraph":
        self.validate_graph()
        return self

    def validate_graph(self, available_capabilities: set[str] | None = None) -> None:
        if not self.nodes:
            raise GraphValidationError("任务图不能为空")

        node_ids = [node.node_id for node in self.nodes]
        duplicates = sorted({node_id for node_id in node_ids if node_ids.count(node_id) > 1})
        if duplicates:
            raise GraphValidationError(f"node_id 重复: {', '.join(duplicates)}")

        known = set(node_ids)
        roots = []
        output_owners: dict[str, str] = {}
        for node in self.nodes:
            if not node.dependencies:
                roots.append(node.node_id)
            if node.node_id in node.dependencies:
                raise GraphValidationError(f"节点不能依赖自己: {node.node_id}")
            missing = sorted(set(node.dependencies) - known)
            if missing:
                raise GraphValidationError(
                    f"节点 {node.node_id} 依赖不存在的节点: {', '.join(missing)}"
                )
            if available_capabilities is not None and node.capability not in available_capabilities:
                raise GraphValidationError(
                    f"节点 {node.node_id} 使用未注册 capability: {node.capability}"
                )
            if not node.output_artifacts and not node.success_criteria:
                raise GraphValidationError(
                    f"节点 {node.node_id} 必须声明 output_artifacts 或 success_criteria"
                )
            for relative in node.input_artifacts + node.output_artifacts:
                path = Path(relative)
                if path.is_absolute() or ".." in path.parts:
                    raise GraphValidationError(f"节点 {node.node_id} 包含不安全产物路径: {relative}")
            for relative in node.output_artifacts:
                owner = output_owners.get(relative)
                if owner is not None and owner != node.node_id:
                    raise GraphValidationError(
                        f"输出产物 {relative} 被多个节点写入: {owner}, {node.node_id}"
                    )
                output_owners[relative] = node.node_id

        if not roots:
            raise GraphValidationError("任务图必须至少包含一个根节点")
        self.topological_order()  # also detects cycles

    def get_node(self, node_id: str) -> TaskNode:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise KeyError(f"任务图中不存在节点: {node_id}")

    def topological_order(self) -> list[str]:
        index = {node.node_id: position for position, node in enumerate(self.nodes)}
        indegree = {node.node_id: len(node.dependencies) for node in self.nodes}
        children: dict[str, list[str]] = {node.node_id: [] for node in self.nodes}
        for node in self.nodes:
            for dependency in node.dependencies:
                children.setdefault(dependency, []).append(node.node_id)
        ready = sorted((node_id for node_id, degree in indegree.items() if degree == 0), key=index.get)
        ordered: list[str] = []
        while ready:
            node_id = ready.pop(0)
            ordered.append(node_id)
            for child in sorted(children.get(node_id, []), key=index.get):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
                    ready.sort(key=index.get)
        if len(ordered) != len(self.nodes):
            cyclic = sorted(node_id for node_id, degree in indegree.items() if degree > 0)
            raise GraphValidationError(f"任务图存在循环依赖: {', '.join(cyclic)}")
        return ordered

    def ready_nodes(self) -> list[TaskNode]:
        """Return nodes whose direct dependencies completed; block failed descendants."""
        self.block_failed_dependencies()
        ready: list[TaskNode] = []
        for node in self.nodes:
            if node.status not in {NodeStatus.PENDING, NodeStatus.READY}:
                continue
            dependencies = [self.get_node(node_id) for node_id in node.dependencies]
            if all(dependency.status in {NodeStatus.COMPLETED, NodeStatus.SKIPPED} for dependency in dependencies):
                node.status = NodeStatus.READY
                ready.append(node)
        return sorted(ready, key=lambda node: node.node_id)

    def block_failed_dependencies(self) -> list[TaskNode]:
        newly_blocked: list[TaskNode] = []
        changed = True
        while changed:
            changed = False
            for node in self.nodes:
                if node.status not in {NodeStatus.PENDING, NodeStatus.READY}:
                    continue
                dependencies = [self.get_node(node_id) for node_id in node.dependencies]
                if any(dep.status in {NodeStatus.FAILED, NodeStatus.BLOCKED} for dep in dependencies):
                    node.status = NodeStatus.BLOCKED
                    node.error = {
                        "error_type": "dependency_failure",
                        "failed_dependencies": [
                            dep.node_id for dep in dependencies
                            if dep.status in {NodeStatus.FAILED, NodeStatus.BLOCKED}
                        ],
                    }
                    newly_blocked.append(node)
                    changed = True
        return newly_blocked

    def mark_running(self, node_id: str, executor_id: str) -> TaskNode:
        node = self.get_node(node_id)
        if node.status not in {NodeStatus.PENDING, NodeStatus.READY}:
            raise InvalidNodeTransitionError(f"节点 {node_id} 不能从 {node.status.value} 进入 running")
        node.status = NodeStatus.RUNNING
        node.assigned_executor = executor_id
        node.attempts += 1
        node.error = None
        return node

    def mark_completed(self, node_id: str, result: dict[str, Any]) -> TaskNode:
        node = self.get_node(node_id)
        if node.status != NodeStatus.RUNNING:
            raise InvalidNodeTransitionError(f"节点 {node_id} 只能从 running 进入 completed")
        node.status = NodeStatus.COMPLETED
        node.result = result
        node.error = None
        return node

    def mark_failed(self, node_id: str, error: dict[str, Any]) -> TaskNode:
        node = self.get_node(node_id)
        if node.status != NodeStatus.RUNNING:
            raise InvalidNodeTransitionError(f"节点 {node_id} 只能从 running 进入 failed")
        node.status = NodeStatus.FAILED
        node.error = error
        node.result = None
        return node

    def mark_blocked(self, node_id: str, error: dict[str, Any]) -> TaskNode:
        """Stop a node without treating a recoverable context issue as failure."""
        node = self.get_node(node_id)
        if node.status != NodeStatus.RUNNING:
            raise InvalidNodeTransitionError(
                f"节点 {node_id} 只能从 running 进入 blocked"
            )
        node.status = NodeStatus.BLOCKED
        node.error = error
        node.result = None
        return node

    def reset_for_retry(self, node_id: str) -> TaskNode:
        node = self.get_node(node_id)
        if node.status != NodeStatus.FAILED:
            raise InvalidNodeTransitionError(f"节点 {node_id} 只能在 failed 后重试")
        node.status = NodeStatus.PENDING
        node.assigned_executor = None
        node.error = None
        return node

    def descendants_of(self, node_ids: list[str] | set[str]) -> list[str]:
        """Return roots and all transitive descendants in topological order."""

        roots = {str(node_id).strip() for node_id in node_ids if str(node_id).strip()}
        if not roots:
            raise ValueError("至少需要一个重开根节点")
        unknown = sorted(roots - {node.node_id for node in self.nodes})
        if unknown:
            raise KeyError("任务图中不存在节点: " + ", ".join(unknown))
        affected = set(roots)
        changed = True
        while changed:
            changed = False
            for node in self.nodes:
                if node.node_id in affected:
                    continue
                if any(dependency in affected for dependency in node.dependencies):
                    affected.add(node.node_id)
                    changed = True
        return [
            node_id for node_id in self.topological_order()
            if node_id in affected
        ]

    def reopen_subgraph(
        self,
        root_node_ids: list[str],
        recovery_contexts: dict[str, dict[str, Any]] | None = None,
    ) -> list[str]:
        """Reopen validated producers and their descendants after post-validation.

        This is deliberately separate from ``reset_for_retry``.  A normal
        executor failure can only retry a failed node, while this transition is
        reserved for framework-confirmed business failures discovered after a
        node reported completion.  Upstream and unrelated completed nodes are
        immutable and retain their results.
        """

        roots = list(dict.fromkeys(
            str(node_id).strip() for node_id in root_node_ids
            if str(node_id).strip()
        ))
        affected = self.descendants_of(roots)
        contexts = recovery_contexts or {}
        for root in roots:
            status = self.get_node(root).status
            if status not in {
                NodeStatus.COMPLETED,
                NodeStatus.FAILED,
                NodeStatus.BLOCKED,
                NodeStatus.SKIPPED,
            }:
                raise InvalidNodeTransitionError(
                    f"节点 {root} 处于 {status.value}，不能进行后验业务修复"
                )
        for node_id in affected:
            node = self.get_node(node_id)
            node.status = NodeStatus.PENDING
            node.assigned_executor = None
            node.result = None
            node.error = None
            node.recovery_context = dict(contexts.get(node_id, {}))
        return affected

    def reset_runtime_state(self) -> None:
        """Discard any runtime claims supplied by a planner model."""
        for node in self.nodes:
            node.status = NodeStatus.PENDING
            node.assigned_executor = None
            node.attempts = 0
            node.result = None
            node.error = None
            node.recovery_context = {}

    def is_finished(self) -> bool:
        terminal = {NodeStatus.COMPLETED, NodeStatus.FAILED, NodeStatus.BLOCKED, NodeStatus.SKIPPED}
        return all(node.status in terminal for node in self.nodes)

    def has_failed_nodes(self) -> bool:
        return any(node.status == NodeStatus.FAILED for node in self.nodes)

    def has_blocked_nodes(self) -> bool:
        return any(node.status == NodeStatus.BLOCKED for node in self.nodes)

    def direct_dependency_results(self, node_id: str) -> dict[str, dict[str, Any]]:
        node = self.get_node(node_id)
        return {
            dependency_id: (self.get_node(dependency_id).result or {})
            for dependency_id in node.dependencies
        }
