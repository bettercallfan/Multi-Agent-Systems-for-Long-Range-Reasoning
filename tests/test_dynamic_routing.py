import json
import tempfile
import unittest
from pathlib import Path

from orchestration.execution.capability_registry import CapabilityRegistry
from orchestration.execution.default_executors import build_default_registry
from orchestration.execution.executor_scoring import (
    DyLANLiteScorer,
    ExecutorStatsStore,
)
from orchestration.execution.node_executor import (
    ExecutorDescriptor,
    NodeExecutionContext,
    NodeExecutionResult,
)
from orchestration.graph.task_graph import TaskNode
from orchestration.core.run_state import RunState
from orchestration.core.workflow_policy import (
    AFlowPolicyAdapter,
    DefaultWorkflowPolicy,
    WorkflowPolicyProvider,
)


class FakeExecutor:
    def __init__(
        self,
        executor_id: str,
        *,
        quality: float = 1.0,
        cost: float = 1.0,
        latency: float = 1.0,
    ) -> None:
        self.descriptor = ExecutorDescriptor(
            executor_id=executor_id,
            executor_type="test",
            capabilities=["analysis"],
            quality_score=quality,
            cost_score=cost,
            latency_score=latency,
        )

    async def execute(self, context: NodeExecutionContext) -> NodeExecutionResult:
        return NodeExecutionResult(
            node_id=context.node.node_id,
            executor_id=self.descriptor.executor_id,
            status="completed",
        )


def make_node() -> TaskNode:
    return TaskNode(
        node_id="analyze",
        description="分析输入",
        capability="analysis",
        success_criteria=[{"type": "manual_review"}],
    )


class ExecutorStatsTests(unittest.TestCase):
    def test_stats_aggregate_and_round_trip_by_executor_and_task_type(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "executor_stats.json"
            store = ExecutorStatsStore(path)
            store.record(
                "agent-a",
                "document_analysis",
                success=True,
                quality_passed=True,
                tokens=100,
                latency_seconds=2.0,
            )
            store.record(
                "agent-a",
                "document_analysis",
                success=False,
                quality_passed=False,
                tokens=300,
                latency_seconds=4.0,
            )
            store.record(
                "agent-a",
                "coding",
                success=True,
                tokens=50,
                latency_seconds=1.0,
            )

            restored = ExecutorStatsStore(path)
            stats = restored.get("agent-a", "document_analysis")
            self.assertEqual(stats.runs, 2)
            self.assertEqual(stats.successes, 1)
            self.assertEqual(stats.quality_passes, 1)
            self.assertEqual(stats.avg_tokens, 200)
            self.assertEqual(stats.avg_latency_seconds, 3)
            self.assertEqual(stats.consecutive_failures, 1)
            self.assertEqual(stats.snapshot()["success_rate"], 0.5)
            self.assertEqual(restored.get("agent-a", "coding").runs, 1)
            self.assertEqual(json.loads(path.read_text())["schema_version"], "1.0")


class DynamicRoutingTests(unittest.TestCase):
    def make_registry(self, path: Path | None = None) -> CapabilityRegistry:
        registry = CapabilityRegistry(
            scorer=DyLANLiteScorer(cold_start_runs=4),
            stats_path=path,
        )
        registry.register(FakeExecutor("strong", quality=0.9))
        registry.register(FakeExecutor("backup", quality=0.8))
        return registry

    def test_cold_start_is_deterministic_and_uses_static_prior(self):
        registry = self.make_registry()
        selected, decision = registry.select_executor(
            make_node(), task_type="document_analysis"
        )

        self.assertEqual(selected.descriptor.executor_id, "strong")
        self.assertEqual(decision.scoring_policy, "dylan_lite")
        self.assertEqual(decision.history_snapshot["runs"], 0)
        self.assertEqual(
            decision.candidates[0].score_components["history_confidence"], 0,
        )
        self.assertGreater(decision.effective_score, 0)

    def test_runtime_outcomes_can_promote_a_reliable_backup(self):
        registry = self.make_registry()
        for _ in range(12):
            registry.record_outcome(
                "strong", "document_analysis", success=False,
                quality_passed=False, token_usage=2000, latency_seconds=20,
            )
            registry.record_outcome(
                "backup", "document_analysis", success=True,
                quality_passed=True, token_usage=200, latency_seconds=1,
            )

        selected, decision = registry.select_executor(
            make_node(), task_type="document_analysis"
        )

        self.assertEqual(selected.descriptor.executor_id, "backup")
        self.assertEqual(decision.history_snapshot["success_rate"], 1)
        self.assertIn("历史置信度", decision.reason)

    def test_history_is_isolated_by_task_type(self):
        registry = self.make_registry()
        for _ in range(12):
            registry.record_outcome(
                "strong", "document_analysis", success=False,
                quality_passed=False,
            )
            registry.record_outcome(
                "backup", "document_analysis", success=True,
                quality_passed=True,
            )

        selected, decision = registry.select_executor(make_node(), task_type="coding")
        self.assertEqual(selected.descriptor.executor_id, "strong")
        self.assertEqual(decision.history_snapshot["runs"], 0)

    def test_exclusion_selects_an_untried_alternative(self):
        registry = self.make_registry()
        selected, decision = registry.select_executor(
            make_node(),
            task_type="document_analysis",
            excluded_executor_ids={"strong"},
        )

        self.assertEqual(selected.descriptor.executor_id, "backup")
        self.assertEqual(decision.excluded_executor_ids, ["strong"])
        self.assertIn("跳过已尝试执行器", decision.reason)
        self.assertEqual([item.executor_id for item in decision.candidates], ["backup"])

    def test_record_outcome_accepts_structured_execution_result(self):
        registry = self.make_registry()
        result = NodeExecutionResult(
            node_id="analyze",
            executor_id="strong",
            status="completed",
            token_usage={"prompt_tokens": 80, "completion_tokens": 20},
            duration_seconds=1.5,
        )
        stats = registry.record_outcome(
            "strong", "document_analysis", result=result, quality_passed=True,
        )
        self.assertEqual(stats.successes, 1)
        self.assertEqual(stats.total_tokens, 100)
        self.assertEqual(stats.avg_latency_seconds, 1.5)

    def test_default_team_uses_distinct_analysis_and_reasoning_roles(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            state = RunState(str(run_dir), task_type="document_analysis")
            spec = {
                "run_dir": str(run_dir),
                "team_policy": {
                    "mode": "dylan_lite",
                    "use_runtime_history": True,
                    "stats_path": str(run_dir / "executor_stats.json"),
                },
            }
            registry = build_default_registry(
                object(), state, [], {}, spec,
            )

            analysis = registry.get_executor("analysis_agent")
            reasoning = registry.get_executor("reasoning_agent")
            self.assertEqual(analysis.agent_factory.__name__, "create_analysis_agent")
            self.assertEqual(reasoning.agent_factory.__name__, "create_reasoning_agent")
            self.assertNotEqual(
                analysis.descriptor.model_dump(), reasoning.descriptor.model_dump(),
            )
            configured = [
                event for event in state.get("events", [])
                if event["type"] == "dynamic_team_configured"
            ]
            self.assertEqual(len(configured), 1)
            self.assertEqual(configured[0]["payload"]["scoring_policy"], "dylan_lite")


class WorkflowPolicyTests(unittest.TestCase):
    def test_default_policy_is_read_only_snapshot_with_deep_overrides(self):
        provider = DefaultWorkflowPolicy()
        self.assertIsInstance(provider, WorkflowPolicyProvider)
        task_spec = {
            "workflow_policy": {
                "recovery_policy": {"max_executor_switches": 2},
            }
        }
        first = provider.get_policy(task_spec)
        first["team_policy"]["mode"] = "mutated"
        second = provider.get_policy(task_spec)

        self.assertEqual(second["team_policy"]["mode"], "dylan_lite")
        self.assertEqual(second["recovery_policy"]["max_executor_switches"], 2)
        self.assertTrue(second["routing_policy"]["direct_dependencies_only"])

    def test_aflow_adapter_only_consumes_an_offline_snapshot(self):
        offline = {
            "policy_id": "aflow-experiment-7",
            "recovery_policy": {"max_executor_switches": 3},
        }
        provider = AFlowPolicyAdapter(offline)
        policy = provider.get_policy({})

        self.assertEqual(policy["policy_id"], "aflow-experiment-7")
        self.assertEqual(policy["policy_source"], "aflow_offline_snapshot")
        self.assertFalse(hasattr(provider, "optimize"))
        self.assertFalse(hasattr(provider, "mutate_graph"))


if __name__ == "__main__":
    unittest.main()
