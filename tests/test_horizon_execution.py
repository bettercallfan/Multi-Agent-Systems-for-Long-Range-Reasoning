import tempfile
import unittest
import json
from pathlib import Path

from orchestration.core.run_state import RunState
from orchestration.core.workflow import (
    _execute_graph_in_horizons,
    _progressive_execution_enabled,
)
from orchestration.execution.capability_registry import CapabilityRegistry
from orchestration.execution.node_executor import (
    ExecutorDescriptor,
    NodeExecutionContext,
    NodeExecutionResult,
)
from orchestration.graph.task_graph import NodeStatus, TaskGraph, TaskNode


def node(node_id: str, dependencies: list[str] | None = None) -> TaskNode:
    return TaskNode(
        node_id=node_id,
        description=f"execute {node_id}",
        capability="test",
        dependencies=dependencies or [],
        success_criteria=[{"type": "node_result"}],
        max_retries=0,
    )


class HorizonExecutor:
    descriptor = ExecutorDescriptor(
        executor_id="horizon-test",
        executor_type="test",
        capabilities=["test"],
    )

    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(
        self,
        context: NodeExecutionContext,
    ) -> NodeExecutionResult:
        self.executed.append(context.node.node_id)
        return NodeExecutionResult(
            node_id=context.node.node_id,
            executor_id=self.descriptor.executor_id,
            status="completed",
            summary=f"completed {context.node.node_id}",
            structured_output={"node_id": context.node.node_id},
        )


class HorizonExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_interactive_execution_persists_running_node_before_executor_returns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observations = []

            class ObservedExecutor(HorizonExecutor):
                async def execute(self, context):
                    snapshot = json.loads((root / "run_state.json").read_text())
                    observations.append(snapshot["nodes"][context.node.node_id]["status"])
                    return await super().execute(context)

            registry = CapabilityRegistry()
            registry.register(ObservedExecutor())
            state = RunState(directory)
            state.start_stage("execute")
            spec = {"task_type": "general_complex_task", "accept_runtime_injections": True,
                    "horizon_policy": {"max_active_nodes": 2, "persist_every_epochs": 10}}
            graph = TaskGraph(graph_id="visible", goal="observe running state",
                              nodes=[node("A"), node("B", ["A"])])
            await _execute_graph_in_horizons(
                task_graph=graph, task_spec=spec, registry=registry, run_state=state,
                run_dir=root, replan_callback=None, trace=[], validate_task_contract=False,
            )
            self.assertEqual(observations, ["running", "running"])
            self.assertEqual(state._save_defer_depth, 0)

    def test_auto_mode_keeps_small_document_graph_one_shot(self):
        graph = TaskGraph(
            graph_id="short",
            goal="short task",
            nodes=[node("A")],
        )
        spec = {
            "task_type": "document_analysis",
            "horizon_policy": {
                "mode": "auto",
                "progressive_execution": True,
                "max_active_nodes": 8,
            },
        }
        self.assertFalse(_progressive_execution_enabled(spec, graph))
        spec["horizon_policy"]["mode"] = "progressive"
        self.assertTrue(_progressive_execution_enabled(spec, graph))

    async def test_progressive_epochs_persist_bounded_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            graph = TaskGraph(
                graph_id="long",
                goal="progressive chain",
                nodes=[
                    node("A"),
                    node("B", ["A"]),
                    node("C", ["B"]),
                ],
            )
            registry = CapabilityRegistry()
            executor = HorizonExecutor()
            registry.register(executor)
            state = RunState(directory)
            state.start_stage("classify")
            state.start_stage("plan")
            state.start_stage("execute")
            spec = {
                "task_id": "long-test",
                "task_type": "general_complex_task",
                "horizon_policy": {
                    "mode": "progressive",
                    "max_active_nodes": 1,
                    "max_epochs": 8,
                    "checkpoint_each_epoch": True,
                },
                "recovery_policy": {
                    "max_node_retries": 0,
                    "max_executor_switches": 0,
                    "max_replans": 0,
                },
            }

            completed = await _execute_graph_in_horizons(
                task_graph=graph,
                task_spec=spec,
                registry=registry,
                run_state=state,
                run_dir=run_dir,
                replan_callback=None,
                trace=[],
                validate_task_contract=False,
            )

            self.assertTrue(completed.is_finished())
            self.assertTrue(all(
                item.status == NodeStatus.COMPLETED
                for item in completed.nodes
            ))
            self.assertEqual(executor.executed, ["A", "B", "C"])
            horizon = state.get("horizon")
            self.assertEqual(horizon["mode"], "progressive")
            self.assertEqual(horizon["epoch"], 3)
            self.assertEqual(horizon["total_completed_nodes"], 3)
            self.assertEqual(len(horizon["checkpoints"]), 3)
            self.assertTrue(
                (run_dir / "checkpoints" / "epoch-0003.json").is_file()
            )
            checkpoint = json.loads(
                (run_dir / "checkpoints" / "epoch-0003.json").read_text(
                    encoding="utf-8",
                )
            )
            self.assertEqual(
                checkpoint["protected_context"]["global_goal"],
                "progressive chain",
            )
            self.assertEqual(
                checkpoint["protected_context"]["completed_node_ids"],
                ["A", "B", "C"],
            )

    async def test_finished_frontier_can_expand_without_replaying_completed_node(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            graph = TaskGraph(
                graph_id="growing", goal="grow by unmet requirements",
                nodes=[node("A")],
            )
            registry = CapabilityRegistry()
            executor = HorizonExecutor()
            registry.register(executor)
            state = RunState(directory)
            state.start_stage("classify")
            state.start_stage("plan")
            state.start_stage("execute")
            spec = {
                "task_id": "growing",
                "task_type": "general_complex_task",
                "horizon_policy": {
                    "mode": "progressive", "max_active_nodes": 1,
                    "max_epochs": 8, "checkpoint_each_epoch": True,
                },
                "recovery_policy": {
                    "max_node_retries": 0, "max_executor_switches": 0,
                    "max_replans": 0,
                },
            }
            expansion_calls = 0

            async def expand(current):
                nonlocal expansion_calls
                expansion_calls += 1
                if expansion_calls > 1:
                    return None
                return TaskGraph(
                    graph_id=current.graph_id,
                    version=current.version + 1,
                    goal=current.goal,
                    nodes=[
                        current.get_node("A").model_copy(deep=True),
                        node("B", ["A"]),
                    ],
                )

            completed = await _execute_graph_in_horizons(
                task_graph=graph,
                task_spec=spec,
                registry=registry,
                run_state=state,
                run_dir=run_dir,
                replan_callback=None,
                trace=[],
                validate_task_contract=False,
                expand_callback=expand,
            )

            self.assertEqual(executor.executed, ["A", "B"])
            self.assertEqual(expansion_calls, 2)
            self.assertEqual(len(completed.nodes), 2)
            self.assertTrue(completed.is_finished())
            self.assertEqual(state.get("horizon")["total_generated_nodes"], 2)

    async def test_epoch_does_not_execute_descendant_before_other_root_target(self):
        """The active frontier is a strict allow-list even under id sorting."""
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            graph = TaskGraph(
                graph_id="strict-frontier",
                goal="do not leak into descendants",
                nodes=[
                    node("root_z"),
                    node("child_a", ["root_z"]),
                    node("root_y"),
                    node("join", ["child_a", "root_y"]),
                ],
            )
            registry = CapabilityRegistry()
            executor = HorizonExecutor()
            registry.register(executor)
            state = RunState(directory)
            state.start_stage("classify")
            state.start_stage("plan")
            state.start_stage("execute")
            spec = {
                "task_id": "strict-frontier",
                "task_type": "general_complex_task",
                "horizon_policy": {
                    "mode": "progressive",
                    "max_active_nodes": 2,
                    "max_epochs": 8,
                    "checkpoint_each_epoch": True,
                },
                "recovery_policy": {
                    "max_node_retries": 0,
                    "max_executor_switches": 0,
                    "max_replans": 0,
                },
            }

            await _execute_graph_in_horizons(
                task_graph=graph,
                task_spec=spec,
                registry=registry,
                run_state=state,
                run_dir=run_dir,
                replan_callback=None,
                trace=[],
                validate_task_contract=False,
            )

            first = json.loads(
                (run_dir / "checkpoints" / "epoch-0001.json").read_text(
                    encoding="utf-8",
                )
            )
            self.assertEqual(
                first["active_node_ids"], ["root_y", "root_z"],
            )
            self.assertEqual(
                first["completed_node_ids"], ["root_y", "root_z"],
            )
            self.assertEqual(
                executor.executed[:2], ["root_y", "root_z"],
            )


if __name__ == "__main__":
    unittest.main()
