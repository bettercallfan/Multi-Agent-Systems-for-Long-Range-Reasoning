"""Deterministic single-thread scheduler for validated dynamic task graphs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orchestration.capability_registry import CapabilityNotFoundError, CapabilityRegistry
from orchestration.node_executor import NodeExecutionContext, NodeExecutionResult
from orchestration.run_state import RunState
from orchestration.task_graph import NodeStatus, TaskGraph, TaskNode


class GraphDeadlockError(RuntimeError):
    """Raised when an unfinished graph has no executable or blockable node."""


class GraphScheduler:
    """Execute one ready node at a time and persist every state transition."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        run_state: RunState,
        run_dir: str | Path,
    ) -> None:
        self.registry = registry
        self.run_state = run_state
        self.run_dir = Path(run_dir)
        self.graph_path = self.run_dir / "task_graph.json"

    def _save_graph(self, graph: TaskGraph) -> None:
        self.graph_path.write_text(
            graph.model_dump_json(indent=2),
            encoding="utf-8",
        )
        self.run_state.record_artifact("task_graph.json")

    def _context_for(self, graph: TaskGraph, node: TaskNode, task_spec: dict) -> NodeExecutionContext:
        dependency_results = graph.direct_dependency_results(node.node_id)
        # This metadata proves sparse delivery without persisting full prompts/results.
        self.run_state.record_node_context(node.node_id, list(dependency_results))
        return NodeExecutionContext(
            graph_id=graph.graph_id,
            node=node.model_copy(deep=True),
            task_spec=task_spec,
            dependency_results=dependency_results,
            input_artifacts=list(node.input_artifacts),
            run_dir=str(self.run_dir),
        )

    def _validate_result(
        self,
        node: TaskNode,
        executor_id: str,
        result: NodeExecutionResult,
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
            else:
                issues.append(f"不支持的 success_criteria.type：{criterion_type}")
        return issues

    async def run(self, graph: TaskGraph, task_spec: dict) -> TaskGraph:
        graph.validate_graph(set(self.registry.list_capabilities()))
        recorded = self.run_state.get("task_graph") or {}
        if recorded.get("graph_id") != graph.graph_id:
            self.run_state.record_task_graph(graph)
        self._save_graph(graph)

        while not graph.is_finished():
            newly_blocked = graph.block_failed_dependencies()
            for blocked in newly_blocked:
                failed_dependencies = (blocked.error or {}).get("failed_dependencies", [])
                self.run_state.block_node(blocked.node_id, failed_dependencies)
            if newly_blocked:
                self._save_graph(graph)

            ready = graph.ready_nodes()
            if not ready:
                if graph.is_finished():
                    break
                states = {node.node_id: node.status.value for node in graph.nodes}
                raise GraphDeadlockError(f"任务图未结束但没有 ready 节点: {json.dumps(states, ensure_ascii=False)}")

            node = ready[0]  # ready_nodes is stably sorted by node_id.
            try:
                executor, routing = self.registry.select_executor(node)
            except CapabilityNotFoundError as exc:
                # Treat missing runtime capacity as a real node failure.
                node.status = NodeStatus.RUNNING
                node.attempts += 1
                result = NodeExecutionResult(
                    node_id=node.node_id,
                    executor_id="unavailable",
                    status="failed",
                    error_type="capability_unavailable",
                    error_message=str(exc),
                )
                graph.mark_failed(node.node_id, result.model_dump(mode="json"))
                self.run_state.record_node_result(node.node_id, result.model_dump(mode="json"))
                self._save_graph(graph)
                continue

            routing_payload = routing.model_dump(mode="json")
            self.run_state.record_routing_decision(routing_payload)
            graph.mark_running(node.node_id, executor.descriptor.executor_id)
            self.run_state.start_node(node.node_id, executor.descriptor.executor_id, node.attempts)
            self._save_graph(graph)

            context = self._context_for(graph, node, task_spec)
            try:
                result = await executor.execute(context)
            except Exception as exc:
                result = NodeExecutionResult(
                    node_id=node.node_id,
                    executor_id=executor.descriptor.executor_id,
                    status="failed",
                    error_type="executor_exception",
                    error_message=str(exc),
                )
            result = self._validate_result(node, executor.descriptor.executor_id, result)

            if result.status == "completed":
                graph.mark_completed(node.node_id, result.model_dump(mode="json"))
                self.run_state.record_node_result(node.node_id, result.model_dump(mode="json"))
            else:
                graph.mark_failed(node.node_id, result.model_dump(mode="json"))
                self.run_state.record_node_result(node.node_id, result.model_dump(mode="json"))
                if node.attempts <= node.max_retries:
                    graph.reset_for_retry(node.node_id)
                    self.run_state.retry_node(node.node_id, node.attempts, node.max_retries)
            self._save_graph(graph)

        outcome = "failed" if graph.has_failed_nodes() else "success"
        self.run_state.record_communication_metrics(
            node_count=len(graph.nodes),
            planned_edge_count=sum(len(node.dependencies) for node in graph.nodes),
        )
        self.run_state.finish_graph(outcome)
        if outcome == "failed" and self.run_state.get("failure") is None:
            failed = [
                f"{node.node_id}: {(node.error or {}).get('error_message') or (node.error or {}).get('error_type')}"
                for node in graph.nodes if node.status in {NodeStatus.FAILED, NodeStatus.BLOCKED}
            ]
            self.run_state.set_failure(
                "graph_node_failure", "task_graph", "execute", failed,
            )
        self._save_graph(graph)
        return graph
