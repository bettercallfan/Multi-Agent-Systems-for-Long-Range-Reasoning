"""Deterministic single-thread scheduler for validated dynamic task graphs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from orchestration.execution.capability_registry import CapabilityNotFoundError, CapabilityRegistry
from orchestration.communication.context_builder import (
    CommunicationBudgetExceededError,
    ContextBuilder,
)
from orchestration.communication.communication_models import CommunicationPolicy
from orchestration.communication.context_compressors import llmlingua_runtime_status
from orchestration.graph.graph_planner import validate_planned_graph
from orchestration.execution.node_executor import NodeExecutionContext, NodeExecutionResult
from orchestration.execution.runtime_injections import RuntimeInjectionController
from orchestration.graph.recovery_controller import (
    RecoveryAction,
    RecoveryController,
    RecoveryLimitExceededError,
)
from orchestration.core.run_state import RunState
from orchestration.graph.task_graph import NodeStatus, TaskGraph, TaskNode
from orchestration.memory.runtime_memory import RuntimeMemoryManager


class GraphDeadlockError(RuntimeError):
    """Raised when an unfinished graph has no executable or blockable node."""


ReplanCallback = Callable[
    [TaskGraph, TaskNode, NodeExecutionResult, int],
    Awaitable[TaskGraph | dict[str, Any] | None],
]


class GraphScheduler:
    """Execute one ready node at a time and persist every state transition."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        run_state: RunState,
        run_dir: str | Path,
        replan_callback: ReplanCallback | None = None,
        context_builder: ContextBuilder | None = None,
        memory_manager: RuntimeMemoryManager | None = None,
        validate_task_contract: bool = True,
        persist_graph_transitions: bool = True,
    ) -> None:
        self.registry = registry
        self.run_state = run_state
        self.run_dir = Path(run_dir)
        self.graph_path = self.run_dir / "task_graph.json"
        self.replan_callback = replan_callback
        self.context_builder = context_builder or ContextBuilder()
        self.memory_manager = memory_manager
        self._active_memory_manager: RuntimeMemoryManager | None = None
        self._recovery_controller: RecoveryController | None = None
        self._injection_controller: RuntimeInjectionController | None = None
        self.validate_task_contract = validate_task_contract
        self.persist_graph_transitions = persist_graph_transitions

    def _save_graph(self, graph: TaskGraph) -> None:
        if not self.persist_graph_transitions:
            return
        temporary = self.graph_path.with_suffix(".json.tmp")
        temporary.write_text(
            graph.model_dump_json(indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.graph_path)
        self.run_state.record_artifact("task_graph.json")

    def _context_for(self, graph: TaskGraph, node: TaskNode, task_spec: dict) -> NodeExecutionContext:
        context, built = self.context_builder.build_execution_context(
            graph, node, task_spec, self.run_dir,
        )
        metrics: list[dict[str, Any]] = []
        for envelope, item in zip(built.messages, built.message_metrics):
            payload = item.model_dump(mode="json")
            payload.update({
                "message_type": envelope.message_type,
                "visibility": envelope.visibility,
                "confidence": envelope.confidence,
                "payload_hash": envelope.payload_hash,
                "payload_ref": envelope.payload_ref,
                "evidence_refs": envelope.evidence_refs,
            })
            metrics.append(payload)
        # Persist only protocol metadata and metrics; full content remains in
        # content-addressed files owned by ContextBuilder.
        self.run_state.record_node_context(
            node.node_id,
            list(context.dependency_results),
            metrics,
            built.aggregate_metrics.model_dump(mode="json"),
        )
        if self._active_memory_manager is not None:
            capsule = self._active_memory_manager.build_capsule(graph, node)
            context = context.model_copy(update={
                "memory_capsule": capsule.model_dump(mode="json"),
            })
            capsule_metadata = {
                "node_id": node.node_id,
                "capsule": f"memory/capsules/{node.node_id}-attempt-{max(1, node.attempts)}.json",
                "metrics": capsule.metrics,
            }
            self.run_state.record_runtime_memory_capsule(capsule_metadata)
        return context

    def _validate_result(
        self,
        node: TaskNode,
        executor_id: str,
        result: NodeExecutionResult,
        before_artifacts: dict[str, tuple[int, int, str]] | None = None,
    ) -> NodeExecutionResult:
        issues: list[str] = []
        if result.node_id != node.node_id:
            issues.append(f"执行结果 node_id 不匹配：{result.node_id}")
        if result.executor_id != executor_id:
            issues.append(f"执行结果 executor_id 不匹配：{result.executor_id}")
        if result.status == "completed":
            unreported = [
                relative for relative in node.output_artifacts
                if relative not in result.output_artifacts
            ]
            if unreported:
                issues.append(
                    "执行器没有声明实际生成节点产物：" + ", ".join(unreported)
                )
            missing = [
                relative for relative in node.output_artifacts
                if not (self.run_dir / relative).is_file()
            ]
            if missing:
                issues.append("缺少节点声明产物：" + ", ".join(missing))
            for relative in node.output_artifacts:
                actual = self.run_dir / relative
                if not actual.is_file() or not before_artifacts or relative not in before_artifacts:
                    continue
                stat = actual.stat()
                current = (
                    stat.st_mtime_ns,
                    stat.st_size,
                    hashlib.sha256(actual.read_bytes()).hexdigest(),
                )
                if current == before_artifacts[relative]:
                    issues.append(f"节点本次执行没有更新声明产物：{relative}")
            issues.extend(self._validate_success_criteria(node, result))
        if not issues:
            return result
        return NodeExecutionResult(
            node_id=node.node_id,
            executor_id=executor_id,
            status="failed",
            summary="节点结果未通过框架校验",
            output_artifacts=result.output_artifacts,
            structured_output=result.structured_output,
            evidence_refs=result.evidence_refs,
            error_type="node_result_validation_failure",
            error_message="；".join(issues),
            token_usage=result.token_usage,
            duration_seconds=result.duration_seconds,
        )

    def _validate_success_criteria(
        self,
        node: TaskNode,
        result: NodeExecutionResult,
    ) -> list[str]:
        issues: list[str] = []
        for criterion in node.success_criteria:
            criterion_type = criterion.get("type")
            if criterion_type == "node_result":
                if not result.summary.strip() and not result.structured_output:
                    issues.append("node_result 为空")
            elif criterion_type == "artifact_exists":
                relative = criterion.get("path", "")
                if not relative or not (self.run_dir / relative).is_file():
                    issues.append(f"artifact_exists 未满足：{relative}")
            elif criterion_type == "execution_exit_code":
                expected = int(criterion.get("equals", 0))
                actual = (result.structured_output.get("execution_result") or {}).get("exit_code")
                if actual != expected:
                    issues.append(f"execution_exit_code 应为 {expected}，实际为 {actual}")
            elif criterion_type == "artifact_quality":
                expected = criterion.get("equals", "passed")
                actual = (result.structured_output.get("artifact_quality") or {}).get("status")
                if actual != expected:
                    issues.append(f"artifact_quality 应为 {expected}，实际为 {actual}")
            elif criterion_type == "evidence_refs_nonempty":
                if not result.evidence_refs:
                    issues.append("运行期新增需求要求至少保留一个 evidence_ref")
            else:
                issues.append(f"不支持的 success_criteria.type：{criterion_type}")
        return issues

    def _snapshot_declared_artifacts(
        self, node: TaskNode,
    ) -> dict[str, tuple[int, int, str]]:
        snapshot: dict[str, tuple[int, int, str]] = {}
        for relative in node.output_artifacts:
            path = self.run_dir / relative
            if not path.is_file():
                continue
            stat = path.stat()
            snapshot[relative] = (
                stat.st_mtime_ns,
                stat.st_size,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        return snapshot

    @staticmethod
    def _quality_passed(result: NodeExecutionResult) -> bool | None:
        quality = result.structured_output.get("artifact_quality")
        if isinstance(quality, dict) and "status" in quality:
            return quality.get("status") == "passed"
        signal = result.structured_output.get("quality_signal")
        if isinstance(signal, dict) and "status" in signal:
            return signal.get("status") == "passed"
        return None

    def _completed_artifact_issues(self, graph: TaskGraph) -> list[str]:
        """Verify files bound to completed nodes before accepting a replan."""
        state_nodes = self.run_state.get("nodes", {})
        issues: list[str] = []
        for node in graph.nodes:
            if node.status != NodeStatus.COMPLETED or not node.output_artifacts:
                continue
            manifest = {
                item.get("path"): item
                for item in state_nodes.get(node.node_id, {}).get("artifact_manifest", [])
            }
            for relative in node.output_artifacts:
                expected = manifest.get(relative)
                if expected is None:
                    issues.append(f"已完成节点 {node.node_id} 缺少产物来源记录: {relative}")
                    continue
                actual = self.run_dir / relative
                if not actual.is_file():
                    issues.append(f"已完成节点 {node.node_id} 的产物已丢失: {relative}")
                    continue
                digest = hashlib.sha256(actual.read_bytes()).hexdigest()
                if digest != expected.get("sha256"):
                    issues.append(f"已完成节点 {node.node_id} 的产物哈希已变化: {relative}")
        return issues

    async def _attempt_replan(
        self,
        graph: TaskGraph,
        node: TaskNode,
        result: NodeExecutionResult,
        recovery: RecoveryController,
        task_spec: dict,
    ) -> TaskGraph | None:
        if self.replan_callback is None:
            return None
        before = recovery.replans_used
        try:
            replacement = await self.replan_callback(
                graph.model_copy(deep=True),
                node.model_copy(deep=True),
                result.model_copy(deep=True),
                before + 1,
            )
            if replacement is None:
                raise ValueError("重规划回调没有返回替代 TaskGraph")
            if self.validate_task_contract:
                replacement = validate_planned_graph(
                    replacement,
                    task_spec,
                    self.registry.list_capabilities(),
                )
            artifact_issues = self._completed_artifact_issues(graph)
            if artifact_issues:
                raise ValueError("；".join(artifact_issues))
            merged = recovery.merge_replanned_graph(
                graph,
                replacement,
                available_capabilities=self.registry.list_capabilities(),
            )
        except Exception as exc:
            # A failed model call/parse still consumes the one explicit replan
            # budget; otherwise an invalid planner could loop indefinitely.
            if recovery.replans_used == before:
                try:
                    recovery.begin_replan()
                except RecoveryLimitExceededError:
                    pass
            self.run_state.record_event("task_graph_replan_failed", {
                "node_id": node.node_id,
                "attempt": before + 1,
                "error": str(exc),
                "recovery": recovery.snapshot(),
            })
            return None

        self.run_state.record_replan(
            f"{graph.graph_id}@v{graph.version}",
            f"{merged.graph_id}@v{merged.version}",
            result.error_message or result.error_type or "node_failure",
        )
        self.run_state.record_task_graph(merged)
        self._save_graph(merged)
        return merged

    async def run(
        self,
        graph: TaskGraph,
        task_spec: dict,
        *,
        stop_after_node_ids: set[str] | None = None,
    ) -> TaskGraph:
        """Run until the graph finishes or one active execution window closes.

        ``stop_after_node_ids`` is framework-owned horizon control.  It never
        marks unfinished nodes successful; it simply returns the still-pending
        graph so the caller can perform a stage check and open the next
        bounded window.
        """
        graph.validate_graph(set(self.registry.list_capabilities()))
        if self.validate_task_contract:
            # Validate a parsed copy so runtime state on a resumed graph is not
            # reset.  The Scheduler, not an arbitrary callback, owns this gate.
            validate_planned_graph(
                graph.model_dump(mode="json"),
                task_spec,
                self.registry.list_capabilities(),
            )
        routing_mode = task_spec.get("routing_policy", {}).get(
            "mode", "agent_prune_lite"
        )
        if routing_mode != "agent_prune_lite":
            raise ValueError(f"不支持的 routing_policy.mode: {routing_mode}")
        task_type = task_spec.get("task_type", "unknown")
        if self.memory_manager is None:
            self._active_memory_manager = RuntimeMemoryManager(self.run_dir, task_spec)
        else:
            self._active_memory_manager = self.memory_manager
        self._active_memory_manager.initialize()
        self.run_state.record_event(
            "communication_compressor_configured",
            llmlingua_runtime_status(CommunicationPolicy.from_task_spec(task_spec)),
        )
        if self._recovery_controller is None:
            self._recovery_controller = RecoveryController(task_spec)
        recovery = self._recovery_controller
        if self._injection_controller is None:
            self._injection_controller = RuntimeInjectionController(
                task_spec, self.run_state, self.run_dir,
            )
        injections = self._injection_controller
        forced_executors: dict[str, str] = {}
        window_targets = set(stop_after_node_ids or ())
        window_completed = False
        recorded = self.run_state.get("task_graph") or {}
        if recorded.get("graph_id") != graph.graph_id:
            self.run_state.record_task_graph(graph)
        self._save_graph(graph)

        while not graph.is_finished():
            injections.poll_requests(graph)
            newly_blocked = graph.block_failed_dependencies()
            for blocked in newly_blocked:
                failed_dependencies = (blocked.error or {}).get("failed_dependencies", [])
                self.run_state.block_node(blocked.node_id, failed_dependencies)
            if newly_blocked:
                self._save_graph(graph)

            if injections.apply_requirement_change(graph):
                # Requirement changes may only amend unfinished work.  Persist
                # both the new graph version and TaskSpec before routing it.
                if self.validate_task_contract:
                    validate_planned_graph(
                        graph.model_dump(mode="json"),
                        task_spec,
                        self.registry.list_capabilities(),
                    )
                self.run_state.record_task_graph(graph)
                self._save_graph(graph)

            ready = graph.ready_nodes()
            if not ready:
                if graph.is_finished():
                    break
                states = {node.node_id: node.status.value for node in graph.nodes}
                raise GraphDeadlockError(f"任务图未结束但没有 ready 节点: {json.dumps(states, ensure_ascii=False)}")

            if window_targets:
                # A horizon window is an execution allow-list, not merely a
                # stopping hint.  Without this filter, completing one root can
                # make a lexicographically earlier descendant ready while a
                # second root target is still pending, causing the scheduler
                # to leak into the next epoch.
                ready = [
                    candidate for candidate in ready
                    if candidate.node_id in window_targets
                ]
                if not ready:
                    target_statuses = {
                        node_id: graph.get_node(node_id).status
                        for node_id in window_targets
                    }
                    if all(
                        status == NodeStatus.COMPLETED
                        for status in target_statuses.values()
                    ):
                        window_completed = True
                        break
                    if any(
                        status in {NodeStatus.FAILED, NodeStatus.BLOCKED}
                        for status in target_statuses.values()
                    ):
                        break
                    raise GraphDeadlockError(
                        "执行窗口中的目标节点尚未结束但没有 ready 节点: "
                        + json.dumps(
                            {
                                node_id: status.value
                                for node_id, status in target_statuses.items()
                            },
                            ensure_ascii=False,
                        )
                    )

            node = ready[0]  # ready_nodes is stably sorted by node_id.
            try:
                forced = forced_executors.pop(node.node_id, None)
                excluded: list[str] = []
                if forced:
                    candidates = self.registry.find_candidates(node.capability, task_type)
                    excluded = [
                        candidate.descriptor.executor_id for candidate in candidates
                        if candidate.descriptor.executor_id != forced
                    ]
                executor, routing = self.registry.select_executor(
                    node,
                    task_type=task_type,
                    excluded_executor_ids=excluded,
                )
            except CapabilityNotFoundError as exc:
                # Treat missing runtime capacity as a real node failure.
                graph.mark_running(node.node_id, "unavailable")
                self.run_state.start_node(node.node_id, "unavailable", node.attempts)
                result = NodeExecutionResult(
                    node_id=node.node_id,
                    executor_id="unavailable",
                    status="failed",
                    error_type="capability_unavailable",
                    error_message=str(exc),
                )
                graph.mark_failed(node.node_id, result.model_dump(mode="json"))
                self.run_state.record_node_result(node.node_id, result.model_dump(mode="json"))
                if (
                    self.replan_callback is not None
                    and recovery.replans_used < recovery.policy.max_replans
                ):
                    replanned = await self._attempt_replan(
                        graph, node, result, recovery, task_spec,
                    )
                    if replanned is not None:
                        graph = replanned
                        forced_executors.clear()
                self._save_graph(graph)
                continue

            routing_payload = routing.model_dump(mode="json")
            self.run_state.record_routing_decision(routing_payload)
            previous_executor = recovery.history_for(node.node_id).last_executor_id
            recovery.record_attempt(node.node_id, executor.descriptor.executor_id)
            if previous_executor and previous_executor != executor.descriptor.executor_id:
                self.run_state.record_executor_switch(
                    node.node_id, previous_executor, executor.descriptor.executor_id,
                )
            graph.mark_running(node.node_id, executor.descriptor.executor_id)
            self.run_state.start_node(node.node_id, executor.descriptor.executor_id, node.attempts)
            self._save_graph(graph)

            before_artifacts = self._snapshot_declared_artifacts(node)
            context_built = False
            executor_invoked = False
            result = injections.failure_for(
                graph, node, executor.descriptor.executor_id,
            )
            if result is None:
                try:
                    context = self._context_for(graph, node, task_spec)
                    context_built = True
                    executor_invoked = True
                    result = await executor.execute(context)
                except Exception as exc:
                    if isinstance(exc, CommunicationBudgetExceededError):
                        self.run_state.record_communication_budget_violation(
                            node.node_id, str(exc),
                        )
                    result = NodeExecutionResult(
                        node_id=node.node_id,
                        executor_id=executor.descriptor.executor_id,
                        status="failed",
                        error_type=(
                            "communication_context_failure"
                            if not context_built
                            else "executor_exception"
                        ),
                        error_message=str(exc),
                    )
            result = self._validate_result(
                node,
                executor.descriptor.executor_id,
                result,
                before_artifacts,
            )
            framework_context_failure = result.error_type in {
                "communication_context_failure",
                "model_prompt_budget_exceeded",
                "prompt_context_blocked",
            }
            if executor_invoked and not framework_context_failure:
                try:
                    self.registry.record_outcome(
                        executor.descriptor.executor_id,
                        task_type,
                        result=result,
                        quality_passed=self._quality_passed(result),
                    )
                except (OSError, ValueError) as exc:
                    # Learned routing statistics are advisory.  Persistence
                    # failures must not invalidate a real node result.
                    self.run_state.record_event("executor_stats_record_failed", {
                        "node_id": node.node_id,
                        "executor_id": executor.descriptor.executor_id,
                        "reason": str(exc),
                    })

            if result.status == "completed":
                graph.mark_completed(node.node_id, result.model_dump(mode="json"))
                self.run_state.record_node_result(node.node_id, result.model_dump(mode="json"))
                self._active_memory_manager.remember_result(
                    graph, node, result.model_dump(mode="json"),
                )
                self.run_state.record_node_artifacts(
                    node.node_id, graph.version, list(node.output_artifacts),
                )
                injections.observe_completed_node(node)
                if (
                    task_spec.get("team_policy", {}).get("early_stop_on_success", True)
                    and len(routing.candidates) > 1
                ):
                    self.run_state.record_event("dynamic_team_early_stopped", {
                        "node_id": node.node_id,
                        "selected_executor_id": executor.descriptor.executor_id,
                        "unused_candidate_count": len(routing.candidates) - 1,
                        "reason": "节点成功标准已满足，不再调用额外候选执行器",
                    })
            elif result.status == "blocked":
                # Context overflow is recoverable node state, not a business
                # failure and must not consume node retry attempts.
                graph.mark_blocked(node.node_id, result.model_dump(mode="json"))
                self.run_state.record_node_result(
                    node.node_id, result.model_dump(mode="json")
                )
                self.run_state.block_node(
                    node.node_id,
                    [result.error_type or "prompt_context_blocked"],
                )
                self._active_memory_manager.remember_result(
                    graph, node, result.model_dump(mode="json"),
                )
                self.run_state.record_untrusted_node_artifacts(
                    node.node_id, graph.version, list(result.output_artifacts),
                )
                self.run_state.record_event("node_recoverable_blocked", {
                    "node_id": node.node_id,
                    "error_type": result.error_type,
                    "recovery_action": result.structured_output.get(
                        "recovery_action", "reduce_context_or_split_node"
                    ),
                })
            else:
                graph.mark_failed(node.node_id, result.model_dump(mode="json"))
                self.run_state.record_node_result(node.node_id, result.model_dump(mode="json"))
                self._active_memory_manager.remember_result(
                    graph, node, result.model_dump(mode="json"),
                )
                self.run_state.record_untrusted_node_artifacts(
                    node.node_id, graph.version, list(result.output_artifacts),
                )
                candidate_ids = [
                    candidate.descriptor.executor_id
                    for candidate in self.registry.find_candidates(node.capability, task_type)
                ]
                decision = recovery.decide_after_failure(
                    node.node_id,
                    executor.descriptor.executor_id,
                    candidate_ids,
                    node_retry_limit=node.max_retries,
                    retry_allowed=not framework_context_failure,
                    switch_allowed=not framework_context_failure,
                    replan_allowed=self.replan_callback is not None,
                )
                self.run_state.record_event(
                    "node_recovery_decision", decision.model_dump(mode="json")
                )
                if decision.action in {
                    RecoveryAction.SAME_EXECUTOR_RETRY,
                    RecoveryAction.SWITCH_EXECUTOR,
                }:
                    graph.reset_for_retry(node.node_id)
                    forced_executors[node.node_id] = decision.selected_executor_id or ""
                    self.run_state.schedule_node_recovery(
                        decision.model_dump(mode="json")
                    )
                elif decision.action == RecoveryAction.REPLAN:
                    replanned = await self._attempt_replan(
                        graph, node, result, recovery, task_spec,
                    )
                    if replanned is not None:
                        graph = replanned
                        forced_executors.clear()
            self._save_graph(graph)
            if window_targets and all(
                graph.get_node(node_id).status == NodeStatus.COMPLETED
                for node_id in window_targets
                if any(item.node_id == node_id for item in graph.nodes)
            ):
                window_completed = True
                break

        if window_completed and not graph.is_finished():
            self.run_state.record_event("graph_execution_window_closed", {
                "target_node_ids": sorted(window_targets),
                "pending_node_ids": [
                    node.node_id for node in graph.nodes
                    if node.status == NodeStatus.PENDING
                ],
            })
            self._save_graph(graph)
            return graph

        integrity_issues = self._completed_artifact_issues(graph)
        outcome = (
            "failed" if graph.has_failed_nodes() or integrity_issues
            else "blocked" if graph.has_blocked_nodes()
            else "success"
        )
        if integrity_issues:
            self.run_state.set_failure(
                "artifact_integrity_failure",
                "artifacts",
                "execute",
                integrity_issues,
            )
        self.run_state.record_communication_metrics(
            node_count=len(graph.nodes),
            planned_edge_count=sum(len(node.dependencies) for node in graph.nodes),
        )
        self.run_state.finish_graph(outcome)
        self.run_state.record_event("recovery_snapshot", recovery.snapshot())
        if outcome == "success":
            failure = self.run_state.get("failure")
            if failure and failure.get("resume_stage") == "execute":
                self.run_state.clear_failure()
        if outcome == "failed" and self.run_state.get("failure") is None:
            failed = [
                f"{node.node_id}: {(node.error or {}).get('error_message') or (node.error or {}).get('error_type')}"
                for node in graph.nodes if node.status == NodeStatus.FAILED
            ]
            self.run_state.set_failure(
                "graph_node_failure", "task_graph", "execute", failed,
            )
        self._save_graph(graph)
        return graph
