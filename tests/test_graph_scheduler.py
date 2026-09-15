import tempfile
import unittest
from pathlib import Path

from orchestration.execution.capability_registry import CapabilityNotFoundError, CapabilityRegistry
from orchestration.communication.context_builder import ContextBuilder
from orchestration.communication.context_compressors import LLMLinguaCompressor
from orchestration.graph.graph_scheduler import GraphScheduler
from orchestration.execution.node_executor import ExecutorDescriptor, NodeExecutionContext, NodeExecutionResult
from orchestration.core.run_state import RunState
from orchestration.graph.task_graph import NodeStatus, TaskGraph, TaskNode


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


class LongSummaryExecutor(RecordingExecutor):
    async def execute(self, context: NodeExecutionContext) -> NodeExecutionResult:
        self.contexts.append(context.model_copy(deep=True))
        return NodeExecutionResult(
            node_id=context.node.node_id,
            executor_id="recording",
            status="completed",
            summary=("需要进行语义压缩的长事实说明" * 80 if context.node.node_id == "A"
                     else f"completed {context.node.node_id}"),
            structured_output={"sentinel": context.node.node_id},
        )


class ShortCompressionBackend:
    def compress_prompt(self, text, target_token):
        return {"compressed_prompt": text[:80]}


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


class StaleArtifactClaimExecutor(RecordingExecutor):
    async def execute(self, context: NodeExecutionContext) -> NodeExecutionResult:
        return NodeExecutionResult(
            node_id=context.node.node_id,
            executor_id="recording",
            status="completed",
            summary="claims a stale file without touching it",
            output_artifacts=["preexisting.json"],
        )


class NamedExecutor:
    def __init__(
        self, executor_id, *, quality=1.0, fail_nodes=None, capabilities=None,
    ):
        self.descriptor = ExecutorDescriptor(
            executor_id=executor_id,
            executor_type="test",
            capabilities=capabilities or ["test"],
            quality_score=quality,
        )
        self.fail_nodes = set(fail_nodes or [])
        self.contexts = []

    async def execute(self, context: NodeExecutionContext) -> NodeExecutionResult:
        self.contexts.append(context.model_copy(deep=True))
        if context.node.node_id in self.fail_nodes:
            return NodeExecutionResult(
                node_id=context.node.node_id,
                executor_id=self.descriptor.executor_id,
                status="failed",
                error_type="test_failure",
                error_message="intentional failure",
            )
        return NodeExecutionResult(
            node_id=context.node.node_id,
            executor_id=self.descriptor.executor_id,
            status="completed",
            summary=f"completed by {self.descriptor.executor_id}",
            structured_output={"executor": self.descriptor.executor_id},
        )


class ArtifactThenFailExecutor(NamedExecutor):
    async def execute(self, context: NodeExecutionContext) -> NodeExecutionResult:
        if context.node.node_id == "Produce":
            path = Path(context.run_dir) / "artifacts" / "verified.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("trusted", encoding="utf-8")
            return NodeExecutionResult(
                node_id="Produce", executor_id=self.descriptor.executor_id,
                status="completed", summary="produced trusted file",
                output_artifacts=["artifacts/verified.txt"],
            )
        return await super().execute(context)


class VanishingRegistry(CapabilityRegistry):
    def list_capabilities(self):
        return ["test"]

    def select_executor(self, node, task_type=None, excluded_executor_ids=None):
        raise CapabilityNotFoundError("capacity disappeared")


class GraphSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduler_closes_bounded_window_without_finishing_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = TaskGraph(
                graph_id="progressive-window",
                goal="execute in epochs",
                nodes=[
                    make_node("A"),
                    make_node("B"),
                    make_node("C", ["A", "B"]),
                ],
            )
            registry = CapabilityRegistry()
            executor = RecordingExecutor()
            registry.register(executor)
            state = RunState(directory)
            state.start_stage("classify")
            state.start_stage("plan")
            state.start_stage("execute")
            scheduler = GraphScheduler(
                registry, state, directory,
                validate_task_contract=False,
            )

            partial = await scheduler.run(
                graph,
                {"task_id": "test"},
                stop_after_node_ids={"A", "B"},
            )

            self.assertEqual(partial.get_node("A").status, NodeStatus.COMPLETED)
            self.assertEqual(partial.get_node("B").status, NodeStatus.COMPLETED)
            self.assertEqual(partial.get_node("C").status, NodeStatus.PENDING)
            self.assertEqual(state.get("graph_outcome"), "running")
            self.assertEqual(
                [context.node.node_id for context in executor.contexts],
                ["A", "B"],
            )

            completed = await scheduler.run(partial, {"task_id": "test"})
            self.assertEqual(completed.get_node("C").status, NodeStatus.COMPLETED)
            self.assertEqual(state.get("graph_outcome"), "success")

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
            scheduler = GraphScheduler(
                registry, state, directory, validate_task_contract=False,
            )
            result = await scheduler.run(graph, {"task_id": "test"})

            self.assertFalse(result.has_failed_nodes())
            contexts = {context.node.node_id: context for context in executor.contexts}
            self.assertEqual(contexts["C"].dependency_results.keys(), {"A"})
            self.assertNotIn("B", contexts["C"].dependency_results)
            self.assertTrue((Path(directory) / "task_graph.json").is_file())
            current = state.to_dict()
            self.assertEqual(current["graph_outcome"], "success")
            self.assertEqual(current["communication"]["dependency_edges_used"], [["A", "C"]])
            self.assertEqual(len(current["communication"]["messages"]), 1)
            self.assertGreater(current["communication"]["raw_bytes"], 0)
            self.assertGreater(current["communication"]["delivered_bytes"], 0)
            self.assertEqual(
                current["communication"]["messages"][0]["visibility"],
                "direct_dependencies",
            )
            self.assertTrue(current["communication"]["messages"][0]["payload_hash"])
            self.assertTrue(all(item["status"] == "completed" for item in current["nodes"].values()))

    async def test_llmlingua_and_run_scope_pruning_are_visible_in_run_state(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = TaskGraph(
                graph_id="communication-observability", goal="observe integrations",
                nodes=[make_node("A"), make_node("B", ["A"]), make_node("C", ["A"])],
            )
            registry = CapabilityRegistry()
            executor = LongSummaryExecutor()
            registry.register(executor)
            state = RunState(directory)
            state.start_stage("classify")
            state.start_stage("plan")
            state.start_stage("execute")
            policy = {
                "compressor": "deterministic",
                "max_message_bytes": 2_000,
                "max_message_tokens": 300,
                "max_node_context_bytes": 4_000,
                "max_node_context_tokens": 600,
                "max_summary_chars": 500,
                "max_inline_structured_bytes": 1_000,
                "deduplicate_across_receivers": True,
            }
            builder = ContextBuilder(
                policy,
                compressor=LLMLinguaCompressor(
                    backend_factory=ShortCompressionBackend,
                    strict=True,
                ),
            )

            await GraphScheduler(
                registry, state, directory, context_builder=builder,
                validate_task_contract=False,
            ).run(graph, {
                "task_id": "test",
                "task_type": "document_analysis",
                "communication_policy": policy,
            })

            communication = state.get("communication")
            self.assertGreaterEqual(communication["compressor_usage"]["llmlingua"], 1)
            self.assertGreaterEqual(communication["semantic_compressions"], 1)
            self.assertEqual(communication["duplicates_suppressed"], 1)
            self.assertIn("run", {
                item["duplicate_scope"] for item in communication["messages"]
            })
            self.assertGreater(communication["projection_saved_ratio"], 0)

    async def test_failed_primary_switches_to_untried_executor(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = TaskGraph(
                graph_id="switch", goal="dynamic fallback",
                nodes=[make_node("A")],
            )
            primary = NamedExecutor("primary", quality=1.0, fail_nodes={"A"})
            backup = NamedExecutor("backup", quality=0.8)
            registry = CapabilityRegistry()
            registry.register(primary)
            registry.register(backup)
            state = RunState(directory)
            state.start_stage("classify")
            state.start_stage("plan")
            state.start_stage("execute")
            task_spec = {
                "task_id": "test", "task_type": "document_analysis",
                "recovery_policy": {
                    "max_node_retries": 0,
                    "max_executor_switches": 1,
                    "max_replans": 0,
                },
                "team_policy": {"max_executor_switches": 1},
            }

            result = await GraphScheduler(
                registry, state, directory, validate_task_contract=False,
            ).run(graph, task_spec)

            self.assertEqual(result.get_node("A").status, NodeStatus.COMPLETED)
            self.assertEqual(
                [item["selected_executor_id"] for item in state.get("routing")],
                ["primary", "backup"],
            )
            self.assertEqual(state.get("recovery")["executor_switches"], 1)
            self.assertEqual(len(primary.contexts), 1)
            self.assertEqual(len(backup.contexts), 1)

    async def test_failed_node_can_trigger_one_safe_replan(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = TaskGraph(
                graph_id="replan", goal="replace failed subgraph",
                nodes=[make_node("A"), make_node("B", ["A"])],
            )
            executor = NamedExecutor("primary", fail_nodes={"A"})
            registry = CapabilityRegistry()
            registry.register(executor)
            state = RunState(directory)
            state.start_stage("classify")
            state.start_stage("plan")
            state.start_stage("execute")
            calls = []

            async def replace_graph(current, failed_node, result, attempt):
                calls.append((failed_node.node_id, attempt, result.error_type))
                return TaskGraph(
                    graph_id="untrusted-new-id",
                    version=99,
                    goal="repaired graph",
                    nodes=[make_node("Repaired")],
                )

            task_spec = {
                "task_id": "test", "task_type": "document_analysis",
                "recovery_policy": {
                    "max_node_retries": 0,
                    "max_executor_switches": 0,
                    "max_replans": 1,
                },
                "team_policy": {"max_executor_switches": 0},
            }
            result = await GraphScheduler(
                registry, state, directory, replan_callback=replace_graph,
                validate_task_contract=False,
            ).run(graph, task_spec)

            self.assertEqual(calls, [("A", 1, "test_failure")])
            self.assertEqual(result.graph_id, "replan")
            self.assertEqual(result.version, 2)
            self.assertEqual([node.node_id for node in result.nodes], ["Repaired"])
            self.assertEqual(result.get_node("Repaired").status, NodeStatus.COMPLETED)
            self.assertEqual(state.get("recovery")["replans"], 1)
            self.assertEqual(state.get("graph_outcome"), "success")

    async def test_strict_scheduler_rejects_replan_that_breaks_task_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = TaskGraph(
                graph_id="strict", goal="contract",
                nodes=[
                    make_node("Work"),
                    TaskNode(
                        node_id="Validate", description="validate",
                        capability="artifact_validation", dependencies=["Work"],
                        success_criteria=[{"type": "node_result"}], max_retries=0,
                    ),
                ],
            )
            registry = CapabilityRegistry()
            registry.register(NamedExecutor("worker", fail_nodes={"Work"}))
            registry.register(NamedExecutor(
                "validator", capabilities=["artifact_validation"],
            ))
            state = RunState(directory)
            state.start_stage("classify")
            state.start_stage("plan")
            state.start_stage("execute")

            async def invalid_replacement(current, failed_node, result, attempt):
                return TaskGraph(
                    graph_id="invalid", goal="omits validation",
                    nodes=[make_node("Replacement")],
                )

            task_spec = {
                "task_id": "strict", "task_type": "document_analysis",
                "code_policy": {"mode": "none", "max_retries": 0},
                "artifact_contract": {
                    "intermediate_artifacts": [],
                    "final_artifacts": ["final_report.md"],
                    "framework_artifacts": [],
                },
                "required_artifacts": ["final_report.md"],
                "recovery_policy": {
                    "max_node_retries": 0,
                    "max_executor_switches": 0,
                    "max_replans": 1,
                },
            }
            result = await GraphScheduler(
                registry, state, directory, replan_callback=invalid_replacement,
            ).run(graph, task_spec)

            self.assertEqual(result.get_node("Work").status, NodeStatus.FAILED)
            self.assertEqual(result.get_node("Validate").status, NodeStatus.BLOCKED)
            self.assertEqual(state.get("graph_outcome"), "failed")
            failures = [
                event for event in state.get("events")
                if event["type"] == "task_graph_replan_failed"
            ]
            self.assertEqual(len(failures), 1)
            self.assertIn("artifact_validation", failures[0]["payload"]["error"])

    async def test_replan_rejects_tampered_completed_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            produce = make_node("Produce")
            produce.output_artifacts = ["artifacts/verified.txt"]
            graph = TaskGraph(
                graph_id="provenance", goal="freeze completed artifacts",
                nodes=[produce, make_node("Fail", ["Produce"])],
            )
            executor = ArtifactThenFailExecutor("worker", fail_nodes={"Fail"})
            registry = CapabilityRegistry()
            registry.register(executor)
            state = RunState(directory)
            state.start_stage("classify")
            state.start_stage("plan")
            state.start_stage("execute")

            async def tampering_replan(current, failed_node, result, attempt):
                (Path(directory) / "artifacts" / "verified.txt").write_text(
                    "tampered", encoding="utf-8",
                )
                return TaskGraph(
                    graph_id="new", goal="invalid replacement",
                    nodes=[
                        produce.model_copy(deep=True),
                        make_node("Repair", ["Produce"]),
                    ],
                )

            task_spec = {
                "task_id": "test", "task_type": "document_analysis",
                "recovery_policy": {
                    "max_node_retries": 0,
                    "max_executor_switches": 0,
                    "max_replans": 1,
                },
            }
            result = await GraphScheduler(
                registry, state, directory, replan_callback=tampering_replan,
                validate_task_contract=False,
            ).run(graph, task_spec)

            self.assertEqual(result.get_node("Produce").status, NodeStatus.COMPLETED)
            self.assertEqual(result.get_node("Fail").status, NodeStatus.FAILED)
            failures = [
                event for event in state.get("events")
                if event["type"] == "task_graph_replan_failed"
            ]
            self.assertIn("哈希已变化", failures[0]["payload"]["error"])
            manifest = state.get("nodes")["Produce"]["artifact_manifest"]
            self.assertEqual(manifest[0]["status"], "verified")

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
            result = await GraphScheduler(
                registry, state, directory, validate_task_contract=False,
            ).run(graph, {"task_id": "test"})

            self.assertEqual(result.get_node("A").status, NodeStatus.FAILED)
            self.assertEqual(result.get_node("B").status, NodeStatus.BLOCKED)
            self.assertEqual(result.get_node("C").status, NodeStatus.COMPLETED)
            self.assertEqual([c.node.node_id for c in executor.contexts], ["A", "C"])
            self.assertEqual(state.get("graph_outcome"), "failed")
            self.assertEqual(state.get("failure")["failure_type"], "graph_node_failure")

    async def test_runtime_capacity_loss_records_a_real_started_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = TaskGraph(graph_id="loss", goal="capacity", nodes=[make_node("A")])
            state = RunState(directory)
            state.start_stage("classify")
            state.start_stage("plan")
            state.start_stage("execute")

            result = await GraphScheduler(
                VanishingRegistry(), state, directory, validate_task_contract=False,
            ).run(graph, {"task_id": "test"})

            self.assertEqual(result.get_node("A").attempts, 1)
            self.assertEqual(state.get("nodes")["A"]["attempts"], 1)
            starts = [
                event for event in state.get("events")
                if event["type"] == "graph_node_started"
            ]
            self.assertEqual(starts[0]["payload"]["executor_id"], "unavailable")

    async def test_communication_budget_failure_is_counted_not_charged_to_executor(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = TaskGraph(
                graph_id="budget", goal="budget attribution",
                nodes=[make_node("A"), make_node("B", ["A"])],
            )
            registry = CapabilityRegistry()
            registry.register(RecordingExecutor())
            state = RunState(directory)
            state.start_stage("classify")
            state.start_stage("plan")
            state.start_stage("execute")
            task_spec = {
                "task_id": "test", "task_type": "document_analysis",
                "communication_policy": {
                    "compressor": "deterministic",
                    "max_message_bytes": 1,
                    "max_message_tokens": 1,
                    "max_node_context_bytes": 1,
                    "max_node_context_tokens": 1,
                    "max_summary_chars": 0,
                    "max_inline_structured_bytes": 1,
                },
            }

            result = await GraphScheduler(
                registry, state, directory, validate_task_contract=False,
            ).run(graph, task_spec)

            self.assertEqual(result.get_node("A").status, NodeStatus.COMPLETED)
            self.assertEqual(result.get_node("B").status, NodeStatus.FAILED)
            self.assertEqual(state.get("communication")["budget_violations"], 1)
            stats = registry.stats_store.get("recording", "document_analysis")
            self.assertEqual(stats.runs, 1)
            self.assertEqual(stats.successes, 1)

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

            result = await GraphScheduler(
                registry, state, directory, validate_task_contract=False,
            ).run(
                graph, {"task_id": "test"}
            )

            self.assertEqual(result.get_node("A").status, NodeStatus.FAILED)
            self.assertIn("没有声明实际生成", result.get_node("A").error["error_message"])

    async def test_reported_but_unchanged_preexisting_file_is_not_current_output(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "preexisting.json").write_text("{}", encoding="utf-8")
            node = make_node("A")
            node.output_artifacts = ["preexisting.json"]
            graph = TaskGraph(graph_id="g", goal="provenance", nodes=[node])
            registry = CapabilityRegistry()
            registry.register(StaleArtifactClaimExecutor())
            state = RunState(directory)
            state.start_stage("classify")
            state.start_stage("plan")
            state.start_stage("execute")

            result = await GraphScheduler(
                registry, state, directory, validate_task_contract=False,
            ).run(graph, {"task_id": "test"})

            self.assertEqual(result.get_node("A").status, NodeStatus.FAILED)
            self.assertIn("没有更新声明产物", result.get_node("A").error["error_message"])


if __name__ == "__main__":
    unittest.main()
