"""Deterministic recovery decisions and safe full-graph replacement.

The controller deliberately does not call a model or execute a node.  It owns
only framework policy and accounting, so a scheduler can ask what recovery
action is allowed and a workflow can safely merge a planner-produced graph.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from orchestration.graph.task_graph import GraphValidationError, NodeStatus, TaskGraph, TaskNode


class RecoveryAction(str, Enum):
    """Framework actions available after a node failure."""

    SAME_EXECUTOR_RETRY = "same_executor_retry"
    SWITCH_EXECUTOR = "switch_executor"
    REPLAN = "replan"
    FAIL = "fail"


class RecoveryLimitExceededError(RuntimeError):
    """Raised when a caller tries to consume recovery budget it does not own."""


class ReplanValidationError(GraphValidationError):
    """Raised when a replacement graph violates an immutable-state invariant."""


class RecoveryPolicy(BaseModel):
    """Resolved framework-owned limits for one workflow run."""

    model_config = ConfigDict(extra="forbid")

    max_node_retries: int = Field(default=1, ge=0)
    max_executor_switches: int = Field(default=1, ge=0)
    max_replans: int = Field(default=1, ge=0)

    @classmethod
    def from_task_spec(cls, task_spec: dict[str, Any]) -> "RecoveryPolicy":
        """Resolve compatible policy keys without mutating ``task_spec``.

        Recovery policy is authoritative.  ``team_policy`` is accepted as the
        fallback source for executor switching because team selection owns that
        concern in older TaskSpec drafts.  ``max_retries`` remains a supported
        alias for ``max_node_retries``.
        """

        recovery = task_spec.get("recovery_policy", {})
        team = task_spec.get("team_policy", {})
        if recovery is None:
            recovery = {}
        if team is None:
            team = {}
        if not isinstance(recovery, dict):
            raise ValueError("TaskSpec.recovery_policy 必须是对象")
        if not isinstance(team, dict):
            raise ValueError("TaskSpec.team_policy 必须是对象")

        return cls(
            max_node_retries=recovery.get(
                "max_node_retries", recovery.get("max_retries", 1)
            ),
            max_executor_switches=recovery.get(
                "max_executor_switches", team.get("max_executor_switches", 0)
            ),
            # Legacy/embedded TaskSpecs that predate explicit recovery policy
            # must not unexpectedly add model calls.  The normalizer writes an
            # explicit value for all new runs.
            max_replans=recovery.get("max_replans", 0),
        )


class NodeRecoveryHistory(BaseModel):
    """Serializable execution history and consumed per-node recovery budget."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    executor_attempts: list[str] = Field(default_factory=list)
    tried_executors: list[str] = Field(default_factory=list)
    same_executor_retries_used: int = Field(default=0, ge=0)
    executor_switches_used: int = Field(default=0, ge=0)

    @property
    def last_executor_id(self) -> str | None:
        return self.executor_attempts[-1] if self.executor_attempts else None


class RecoveryDecision(BaseModel):
    """Persistable explanation of one framework recovery decision."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    action: RecoveryAction
    selected_executor_id: str | None = None
    attempted_executors: list[str] = Field(default_factory=list)
    same_executor_retries_used: int = Field(ge=0)
    max_node_retries: int = Field(ge=0)
    executor_switches_used: int = Field(ge=0)
    max_executor_switches: int = Field(ge=0)
    replans_used: int = Field(ge=0)
    max_replans: int = Field(ge=0)
    reason: str

    @model_validator(mode="after")
    def executor_matches_action(self) -> "RecoveryDecision":
        executor_action = self.action in {
            RecoveryAction.SAME_EXECUTOR_RETRY,
            RecoveryAction.SWITCH_EXECUTOR,
        }
        if executor_action and not self.selected_executor_id:
            raise ValueError(f"动作 {self.action.value} 必须指定 executor")
        if not executor_action and self.selected_executor_id is not None:
            raise ValueError(f"动作 {self.action.value} 不得指定 executor")
        return self


class RecoveryController:
    """Apply TaskSpec recovery limits and retain auditable attempt history.

    Intended integration sequence::

        controller.record_attempt(node_id, executor_id)
        decision = controller.decide_after_failure(...)
        # scheduler performs retry/switch, or workflow obtains a replacement
        # graph when decision.action == RecoveryAction.REPLAN.
    """

    def __init__(self, task_spec: dict[str, Any]) -> None:
        self.policy = RecoveryPolicy.from_task_spec(task_spec)
        self._nodes: dict[str, NodeRecoveryHistory] = {}
        self._replans_used = 0
        self._archived_epochs: list[dict[str, Any]] = []

    @property
    def replans_used(self) -> int:
        return self._replans_used

    def history_for(self, node_id: str) -> NodeRecoveryHistory:
        """Return a defensive snapshot of one node's attempt history."""

        history = self._nodes.get(node_id)
        if history is None:
            return NodeRecoveryHistory(node_id=node_id)
        return history.model_copy(deep=True)

    def record_attempt(self, node_id: str, executor_id: str) -> NodeRecoveryHistory:
        """Record an actual execution attempt and consume retry/switch budget.

        The first attempt is free.  Reusing the immediately previous executor
        consumes one retry.  Changing executor consumes one switch.  Recording
        an action beyond TaskSpec limits fails before mutating history.
        """

        node_id = node_id.strip()
        executor_id = executor_id.strip()
        if not node_id or not executor_id:
            raise ValueError("node_id 和 executor_id 不能为空")

        history = self._nodes.setdefault(node_id, NodeRecoveryHistory(node_id=node_id))
        previous = history.last_executor_id
        if previous == executor_id:
            if history.same_executor_retries_used >= self.policy.max_node_retries:
                raise RecoveryLimitExceededError(
                    f"节点 {node_id} 已达到重试上限 {self.policy.max_node_retries}"
                )
            history.same_executor_retries_used += 1
        elif previous is not None:
            if history.executor_switches_used >= self.policy.max_executor_switches:
                raise RecoveryLimitExceededError(
                    f"节点 {node_id} 已达到执行器切换上限 "
                    f"{self.policy.max_executor_switches}"
                )
            history.executor_switches_used += 1

        history.executor_attempts.append(executor_id)
        if executor_id not in history.tried_executors:
            history.tried_executors.append(executor_id)
        return history.model_copy(deep=True)

    def decide_after_failure(
        self,
        node_id: str,
        failed_executor_id: str,
        candidate_executor_ids: Iterable[str] = (),
        *,
        retry_allowed: bool = True,
        switch_allowed: bool = True,
        replan_allowed: bool = True,
        node_retry_limit: int | None = None,
    ) -> RecoveryDecision:
        """Choose retry -> switch -> replan -> fail under hard policy limits.

        Candidates should already be ordered by Capability Registry scoring.
        A switch always selects the first executor never tried by this node.
        ``node_retry_limit`` can further narrow, but never expand, TaskSpec's
        run-wide per-node retry limit.
        """

        history = self._nodes.get(node_id)
        if history is None or history.last_executor_id != failed_executor_id:
            raise ValueError(
                "必须先用 record_attempt 记录本次失败，且 failed_executor_id "
                "必须等于最近一次尝试"
            )
        if node_retry_limit is not None and node_retry_limit < 0:
            raise ValueError("node_retry_limit 不能小于 0")
        retry_limit = self.policy.max_node_retries
        if node_retry_limit is not None:
            retry_limit = min(retry_limit, node_retry_limit)

        common = {
            "node_id": node_id,
            "attempted_executors": list(history.tried_executors),
            "same_executor_retries_used": history.same_executor_retries_used,
            "max_node_retries": retry_limit,
            "executor_switches_used": history.executor_switches_used,
            "max_executor_switches": self.policy.max_executor_switches,
            "replans_used": self._replans_used,
            "max_replans": self.policy.max_replans,
        }

        if retry_allowed and history.same_executor_retries_used < retry_limit:
            return RecoveryDecision(
                action=RecoveryAction.SAME_EXECUTOR_RETRY,
                selected_executor_id=failed_executor_id,
                reason="同一执行器仍有 TaskSpec 允许的节点重试额度。",
                **common,
            )

        candidates = _normalize_executor_ids(candidate_executor_ids)
        untried = [item for item in candidates if item not in history.tried_executors]
        if (
            switch_allowed
            and history.executor_switches_used < self.policy.max_executor_switches
            and untried
        ):
            return RecoveryDecision(
                action=RecoveryAction.SWITCH_EXECUTOR,
                selected_executor_id=untried[0],
                reason="同能力存在未尝试执行器，且执行器切换额度尚未耗尽。",
                **common,
            )

        if replan_allowed and self._replans_used < self.policy.max_replans:
            return RecoveryDecision(
                action=RecoveryAction.REPLAN,
                reason="节点级重试与可用执行器切换均不可用，仍有全图重规划额度。",
                **common,
            )

        reasons: list[str] = []
        if not retry_allowed:
            reasons.append("本次错误不允许同执行器重试")
        elif history.same_executor_retries_used >= retry_limit:
            reasons.append("节点重试额度耗尽")
        if not switch_allowed:
            reasons.append("本次错误不允许切换执行器")
        elif history.executor_switches_used >= self.policy.max_executor_switches:
            reasons.append("执行器切换额度耗尽")
        elif not untried:
            reasons.append("没有未尝试的同能力执行器")
        if not replan_allowed:
            reasons.append("本次错误不允许重规划")
        elif self._replans_used >= self.policy.max_replans:
            reasons.append("重规划额度耗尽")
        return RecoveryDecision(
            action=RecoveryAction.FAIL,
            reason="；".join(reasons) or "没有可用恢复动作。",
            **common,
        )

    def begin_replan(self) -> int:
        """Consume one replan attempt before an external planner is called."""

        if self._replans_used >= self.policy.max_replans:
            raise RecoveryLimitExceededError(
                f"已达到重规划上限 {self.policy.max_replans}"
            )
        self._replans_used += 1
        return self._replans_used

    def merge_replanned_graph(
        self,
        current_graph: TaskGraph,
        replacement_graph: TaskGraph | dict[str, Any],
        *,
        available_capabilities: Iterable[str] | None = None,
    ) -> TaskGraph:
        """Consume one replan attempt and safely merge a full replacement.

        The attempt is consumed before validation.  Therefore a planner cannot
        bypass ``max_replans`` by repeatedly returning invalid graphs.
        """

        self.begin_replan()
        merged = merge_full_replacement_graph(
            current_graph,
            replacement_graph,
            available_capabilities=available_capabilities,
        )
        reset_ids = {
            node.node_id for node in merged.nodes
            if node.status != NodeStatus.COMPLETED
        }
        archived = {
            node_id: self._nodes[node_id].model_dump(mode="json")
            for node_id in sorted(reset_ids)
            if node_id in self._nodes
        }
        if archived:
            self._archived_epochs.append({
                "closed_by_graph_version": merged.version,
                "nodes": archived,
            })
        for node_id in reset_ids:
            self._nodes.pop(node_id, None)
        return merged

    def snapshot(self) -> dict[str, Any]:
        """Return state suitable for framework persistence and audit logs."""

        return {
            "policy": self.policy.model_dump(mode="json"),
            "replans_used": self._replans_used,
            "archived_epochs": list(self._archived_epochs),
            "nodes": {
                node_id: history.model_dump(mode="json")
                for node_id, history in sorted(self._nodes.items())
            },
        }


_NODE_DEFINITION_FIELDS = (
    "node_id",
    "description",
    "capability",
    "dependencies",
    "input_artifacts",
    "output_artifacts",
    "success_criteria",
    "max_retries",
)


def merge_full_replacement_graph(
    current_graph: TaskGraph,
    replacement_graph: TaskGraph | dict[str, Any],
    *,
    available_capabilities: Iterable[str] | None = None,
) -> TaskGraph:
    """Merge a planner's full replacement while freezing completed nodes.

    Rules enforced here:

    * every currently completed node must still exist with exactly the same
      planning definition;
    * its trusted runtime executor, attempt count and result come from the
      current graph, never from the replacement payload;
    * every non-completed node in the replacement starts at ``pending`` with no
      model-claimed runtime state, so failed nodes and descendants can be
      replaced or retried safely;
    * graph identity remains framework-owned and its version increments once;
    * the final graph must be a valid DAG and every unfinished node must have an
      available capability when a capability set is supplied.
    """

    try:
        current_graph.validate_graph()
        candidate = _copy_graph(replacement_graph)
    except Exception as exc:
        if isinstance(exc, ReplanValidationError):
            raise
        raise ReplanValidationError(f"重规划图无法解析或不是有效 DAG: {exc}") from exc

    completed = {
        node.node_id: node.model_copy(deep=True)
        for node in current_graph.nodes
        if node.status == NodeStatus.COMPLETED
    }
    for node_id, trusted in completed.items():
        try:
            proposed = candidate.get_node(node_id)
        except KeyError as exc:
            raise ReplanValidationError(
                f"重规划图缺少已完成的不可变节点: {node_id}"
            ) from exc
        changed = [
            field
            for field in _NODE_DEFINITION_FIELDS
            if getattr(trusted, field) != getattr(proposed, field)
        ]
        if changed:
            raise ReplanValidationError(
                f"重规划图修改了已完成节点 {node_id} 的定义字段: "
                + ", ".join(changed)
            )

    # Planner runtime claims are untrusted.  Reset first, then overlay only
    # framework-verified completed nodes from the current graph.
    candidate.reset_runtime_state()
    merged_nodes: list[TaskNode] = []
    for proposed in candidate.nodes:
        trusted = completed.get(proposed.node_id)
        merged_nodes.append((trusted or proposed).model_copy(deep=True))

    try:
        merged = TaskGraph(
            graph_id=current_graph.graph_id,
            version=current_graph.version + 1,
            goal=candidate.goal,
            nodes=merged_nodes,
        )
        merged.validate_graph()
    except Exception as exc:
        raise ReplanValidationError(f"合并后的重规划图无效: {exc}") from exc

    if available_capabilities is not None:
        available = {item.strip() for item in available_capabilities if item.strip()}
        unavailable = [
            f"{node.node_id}({node.capability})"
            for node in merged.nodes
            if node.status != NodeStatus.COMPLETED
            and node.capability not in available
        ]
        if unavailable:
            raise ReplanValidationError(
                "重规划图包含没有可用执行器的未完成节点: " + ", ".join(unavailable)
            )
    return merged


def _copy_graph(value: TaskGraph | dict[str, Any]) -> TaskGraph:
    if isinstance(value, TaskGraph):
        graph = value.model_copy(deep=True)
        graph.validate_graph()
        return graph
    return TaskGraph.model_validate(value)


def _normalize_executor_ids(values: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("candidate_executor_ids 只能包含字符串")
        value = value.strip()
        if not value:
            raise ValueError("candidate_executor_ids 不能包含空值")
        if value not in normalized:
            normalized.append(value)
    return normalized
