import unittest

from pydantic import ValidationError

from orchestration.capability_registry import (
    CapabilityNotFoundError,
    CapabilityRegistry,
)
from orchestration.node_executor import (
    ExecutorDescriptor,
    NodeExecutionContext,
    NodeExecutionResult,
)
from orchestration.task_graph import TaskNode


class FakeExecutor:
    def __init__(
        self,
        executor_id: str,
        *,
        capabilities: list[str] | None = None,
        quality: float = 1.0,
        cost: float = 1.0,
        latency: float = 1.0,
        available: bool = True,
    ) -> None:
        self.descriptor = ExecutorDescriptor(
            executor_id=executor_id,
            executor_type="test",
            capabilities=capabilities or ["analysis"],
            quality_score=quality,
            cost_score=cost,
            latency_score=latency,
            available=available,
        )

    async def execute(self, context: NodeExecutionContext) -> NodeExecutionResult:
        return NodeExecutionResult(
            node_id=context.node.node_id,
            executor_id=self.descriptor.executor_id,
            status="completed",
        )


def make_node(capability: str = "analysis", dependencies: list[str] | None = None) -> TaskNode:
    return TaskNode(
        node_id="analyze",
        description="分析输入",
        capability=capability,
        dependencies=dependencies or [],
        output_artifacts=["artifacts/analysis.json"],
    )


class ExecutorContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_uniform_executor_contract_is_awaitable(self):
        executor = FakeExecutor("analysis_agent")
        context = NodeExecutionContext(
            graph_id="graph-1",
            node=make_node(),
            task_spec={},
            run_dir="/tmp/run",
        )
        result = await executor.execute(context)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.executor_id, "analysis_agent")

    async def test_context_rejects_non_direct_dependency_results(self):
        with self.assertRaisesRegex(ValidationError, "直接依赖"):
            NodeExecutionContext(
                graph_id="graph-1",
                node=make_node(dependencies=["extract"]),
                task_spec={},
                dependency_results={"unrelated": {"summary": "不应广播"}},
                run_dir="/tmp/run",
            )


class CapabilityRegistryTests(unittest.TestCase):
    def test_register_unregister_and_list_available_capabilities(self):
        registry = CapabilityRegistry()
        analysis = FakeExecutor("analysis", capabilities=["analysis", "reasoning"])
        offline = FakeExecutor("offline", capabilities=["research"], available=False)
        registry.register(analysis)
        registry.register(offline)

        self.assertEqual(registry.list_capabilities(), ["analysis", "reasoning"])
        self.assertIs(registry.unregister("analysis"), analysis)
        self.assertEqual(registry.list_capabilities(), [])

    def test_duplicate_id_and_non_async_executor_are_rejected(self):
        registry = CapabilityRegistry()
        registry.register(FakeExecutor("same"))
        with self.assertRaisesRegex(ValueError, "已注册"):
            registry.register(FakeExecutor("same"))

        class InvalidExecutor:
            descriptor = ExecutorDescriptor(
                executor_id="invalid",
                executor_type="test",
                capabilities=["analysis"],
            )

            def execute(self, context):
                return None

        with self.assertRaisesRegex(TypeError, "async"):
            registry.register(InvalidExecutor())

    def test_candidates_filter_availability_and_capability(self):
        registry = CapabilityRegistry()
        selected = FakeExecutor("selected", capabilities=["analysis"])
        registry.register(FakeExecutor("wrong", capabilities=["research"], quality=10))
        registry.register(FakeExecutor("offline", capabilities=["analysis"], quality=10, available=False))
        registry.register(selected)

        self.assertEqual(registry.find_candidates("analysis"), [selected])

    def test_selection_is_quality_then_cost_then_latency_then_id(self):
        registry = CapabilityRegistry()
        for executor in [
            FakeExecutor("low-quality", quality=0.7, cost=0.1, latency=0.1),
            FakeExecutor("expensive", quality=0.9, cost=0.8, latency=0.1),
            FakeExecutor("slow", quality=0.9, cost=0.2, latency=0.8),
            FakeExecutor("winner-b", quality=0.9, cost=0.2, latency=0.2),
            FakeExecutor("winner-a", quality=0.9, cost=0.2, latency=0.2),
        ]:
            registry.register(executor)

        selected, decision = registry.select_executor(make_node())

        self.assertEqual(selected.descriptor.executor_id, "winner-a")
        self.assertEqual(decision.selected_executor_id, "winner-a")
        self.assertEqual(
            [candidate.executor_id for candidate in decision.candidates],
            ["winner-a", "winner-b", "slow", "expensive", "low-quality"],
        )
        self.assertIn("quality_score", decision.reason)

    def test_selection_is_independent_of_registration_order(self):
        forward = CapabilityRegistry()
        reverse = CapabilityRegistry()
        executors = [FakeExecutor("b"), FakeExecutor("a")]
        for executor in executors:
            forward.register(executor)
        for executor in reversed(executors):
            reverse.register(executor)

        self.assertEqual(
            forward.select_executor(make_node())[1].selected_executor_id,
            reverse.select_executor(make_node())[1].selected_executor_id,
        )

    def test_missing_capability_has_actionable_error(self):
        registry = CapabilityRegistry()
        registry.register(FakeExecutor("analysis", capabilities=["analysis"]))
        with self.assertRaisesRegex(CapabilityNotFoundError, "document_extraction"):
            registry.select_executor(make_node("document_extraction"))


if __name__ == "__main__":
    unittest.main()
