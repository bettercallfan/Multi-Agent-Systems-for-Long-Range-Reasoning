import tempfile
import unittest
from pathlib import Path

from orchestration.capability_registry import CapabilityRegistry
from orchestration.graph_scheduler import GraphScheduler
from orchestration.node_executor import ExecutorDescriptor, NodeExecutionContext, NodeExecutionResult
from orchestration.run_state import RunState
from orchestration.task_graph import NodeStatus, TaskGraph, TaskNode


def make_node(node_id, dependencies=None):
    return TaskNode(
        node_id=node_id,
        description=f"run {node_id}",
        capability="test",
        dependencies=dependencies or [],
        success_criteria=[{"type": "node_result"}],
        max_retries=0,
    )


class RecordingExecutor:
    def __init__(self, fail_nodes=None):
        self.descriptor = ExecutorDescriptor(
            executor_id="recording", executor_type="test", capabilities=["test"]
        )
        self.fail_nodes = set(fail_nodes or [])
        self.contexts = []

    async def execute(self, context: NodeExecutionContext) -> NodeExecutionResult:
        self.contexts.append(context.model_copy(deep=True))
        if context.node.node_id in self.fail_nodes:
            return NodeExecutionResult(
                node_id=context.node.node_id, executor_id="recording", status="failed",
                error_type="test_failure", error_message="boom",
            )
        return NodeExecutionResult(
            node_id=context.node.node_id, executor_id="recording", status="completed",
            summary=f"sentinel_{context.node.node_id}",
            structured_output={"sentinel": context.node.node_id},
        )


class FalseArtifactClaimExecutor(RecordingExecutor):
    async def execute(self, context: NodeExecutionContext) -> NodeExecutionResult:
        self.contexts.append(context.model_copy(deep=True))
        return NodeExecutionResult(
            node_id=context.node.node_id,
            executor_id="recording",
            status="completed",
            summary="claims completion without reporting a produced artifact",
            output_artifacts=[],
        )


class GraphSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduler_uses_only_direct_dependency_results_and_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = TaskGraph(
                graph_id="g", goal="sparse",
                nodes=[make_node("A"), make_node("B"), make_node("C", ["A"])],
            )
            registry = CapabilityRegistry()
            executor = RecordingExecutor()
            registry.register(executor)
            state = RunState(directory)
            state.start_stage("classify")
            state.start_stage("plan")
            state.start_stage("execute")
            scheduler = GraphScheduler(registry, state, directory)
            result = await scheduler.run(graph, {"task_id": "test"})

            self.assertFalse(result.has_failed_nodes())
            contexts = {context.node.node_id: context for context in executor.contexts}
            self.assertEqual(contexts["C"].dependency_results.keys(), {"A"})
            self.assertNotIn("B", contexts["C"].dependency_results)
            self.assertTrue((Path(directory) / "task_graph.json").is_file())
            current = state.to_dict()
            self.assertEqual(current["graph_outcome"], "success")
            self.assertEqual(current["communication"]["dependency_edges_used"], [["A", "C"]])
            self.assertTrue(all(item["status"] == "completed" for item in current["nodes"].values()))

    async def test_failed_node_blocks_descendants_but_independent_branch_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = TaskGraph(
                graph_id="g", goal="failure",
                nodes=[make_node("A"), make_node("B", ["A"]), make_node("C")],
            )
            registry = CapabilityRegistry()
            executor = RecordingExecutor(fail_nodes={"A"})
            registry.register(executor)
            state = RunState(directory)
            state.start_stage("classify")
            state.start_stage("plan")
            state.start_stage("execute")
            result = await GraphScheduler(registry, state, directory).run(graph, {"task_id": "test"})

            self.assertEqual(result.get_node("A").status, NodeStatus.FAILED)
            self.assertEqual(result.get_node("B").status, NodeStatus.BLOCKED)
            self.assertEqual(result.get_node("C").status, NodeStatus.COMPLETED)
            self.assertEqual([c.node.node_id for c in executor.contexts], ["A", "C"])
            self.assertEqual(state.get("graph_outcome"), "failed")
            self.assertEqual(state.get("failure")["failure_type"], "graph_node_failure")

    async def test_preexisting_file_cannot_be_claimed_as_current_node_output(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "preexisting.json").write_text("{}", encoding="utf-8")
            node = make_node("A")
            node.output_artifacts = ["preexisting.json"]
            graph = TaskGraph(graph_id="g", goal="provenance", nodes=[node])
            registry = CapabilityRegistry()
            registry.register(FalseArtifactClaimExecutor())
            state = RunState(directory)
            state.start_stage("classify")
            state.start_stage("plan")
            state.start_stage("execute")

            result = await GraphScheduler(registry, state, directory).run(
                graph, {"task_id": "test"}
            )

            self.assertEqual(result.get_node("A").status, NodeStatus.FAILED)
            self.assertIn("没有声明实际生成", result.get_node("A").error["error_message"])


if __name__ == "__main__":
    unittest.main()
