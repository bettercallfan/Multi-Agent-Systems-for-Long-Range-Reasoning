import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from orchestration.core.model_calls import (
    PromptContextBlockedError,
    run_agent,
)
from orchestration.core.run_state import RunState
from orchestration.execution.capability_registry import CapabilityRegistry
from orchestration.execution.node_executor import (
    ExecutorDescriptor,
    NodeExecutionContext,
    NodeExecutionResult,
)
from orchestration.graph.graph_scheduler import GraphScheduler
from orchestration.graph.task_graph import TaskGraph, TaskNode


class _Message:
    def __init__(self, content):
        self.content = content
        self.models_usage = None


class _Result:
    def __init__(self, content="ok"):
        self.messages = [_Message(content)]


class _Agent:
    name = "budget-test-agent"

    def __init__(self):
        self.calls = []

    async def run(self, *, task, output_task_messages=False):
        self.calls.append(task)
        return _Result()


class _IntermittentAccessAgent(_Agent):
    async def run(self, *, task, output_task_messages=False):
        self.calls.append(task)
        if len(self.calls) < 3:
            raise RuntimeError(
                "403 AccessDenied.Unpurchased: Access to model denied"
            )
        return _Result()


class _GraphExecutor:
    descriptor = ExecutorDescriptor(
        executor_id="budget-graph-executor",
        executor_type="test",
        capabilities=["test"],
    )

    async def execute(self, context: NodeExecutionContext):
        if context.node.node_id == "blocked":
            return NodeExecutionResult(
                node_id="blocked", executor_id=self.descriptor.executor_id,
                status="blocked", error_type="prompt_context_blocked",
                structured_output={
                    "recoverable": True,
                    "recovery_action": "reduce_context_or_split_node",
                },
            )
        return NodeExecutionResult(
            node_id=context.node.node_id, executor_id=self.descriptor.executor_id,
            status="completed", summary="prior completed",
        )


class PromptBudgetSemanticsTests(unittest.TestCase):
    def _state(self, directory):
        state = RunState(directory, task_type="test")
        state.configure({
            "task_type": "test",
            "code_policy": {"mode": "none", "max_retries": 0},
            "required_artifacts": [],
            "communication_policy": {},
        })
        return state

    def test_soft_budget_warns_but_calls_model(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self._state(directory)
            agent = _Agent()
            asyncio.run(run_agent(
                agent, "a" * 400, [], run_state=state,
                stage="execute", node_id="n1",
                prompt_budget_tokens=10, model_input_limit_tokens=1000,
            ))
            self.assertEqual(len(agent.calls), 1)
            self.assertEqual(state.get("model_calls")["soft_prompt_budget_warnings"], 1)
            self.assertEqual(state.get("execution")["attempts"], 0)
            self.assertTrue(any(
                event.get("event") == "soft_prompt_budget_exceeded"
                or event.get("type") == "soft_prompt_budget_exceeded"
                for event in state.get("events")
            ))

    def test_real_limit_reduces_once_and_continues(self):
        agent = _Agent()
        seen = []
        result = asyncio.run(run_agent(
            agent, "x" * 4000, [], prompt_budget_tokens=10,
            model_input_limit_tokens=400,
            prompt_reducer=lambda prompt: seen.append(prompt) or prompt[:400],
        ))
        self.assertEqual(result, "ok")
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(agent.calls), 1)

    def test_intermittent_provider_access_error_does_not_consume_business_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self._state(directory)
            agent = _IntermittentAccessAgent()
            with patch(
                "orchestration.core.model_calls.asyncio.sleep",
                new=AsyncMock(),
            ):
                result = asyncio.run(run_agent(
                    agent, "provider retry", [], run_state=state,
                    stage="execute", node_id="code",
                ))
            self.assertEqual(result, "ok")
            self.assertEqual(len(agent.calls), 3)
            self.assertEqual(state.get("execution")["attempts"], 0)
            retries = [
                event for event in state.get("events", [])
                if event.get("type") == "model_transport_retry"
            ]
            self.assertEqual(len(retries), 2)

    def test_reduction_failure_is_recoverable_context_block(self):
        agent = _Agent()
        with self.assertRaises(PromptContextBlockedError):
            asyncio.run(run_agent(
                agent, "x" * 4000, [], prompt_budget_tokens=10,
                model_input_limit_tokens=400,
                prompt_reducer=lambda prompt: prompt,
            ))
        self.assertEqual(agent.calls, [])

    def test_blocked_node_preserves_completed_predecessor(self):
        async def execute():
            with tempfile.TemporaryDirectory() as directory:
                graph = TaskGraph(
                    graph_id="budget-graph", goal="preserve",
                    nodes=[
                        TaskNode(
                            node_id="prior", description="prior", capability="test",
                            success_criteria=[{"type": "node_result"}],
                        ),
                        TaskNode(
                            node_id="blocked", description="blocked", capability="test",
                            dependencies=["prior"],
                            success_criteria=[{"type": "node_result"}],
                        ),
                    ],
                )
                state = self._state(directory)
                state.start_stage("classify")
                state.start_stage("plan")
                state.start_stage("execute")
                registry = CapabilityRegistry()
                registry.register(_GraphExecutor())
                result = await GraphScheduler(
                    registry, state, directory, validate_task_contract=False,
                ).run(graph, {"task_id": "budget"})
                self.assertEqual(result.get_node("prior").status.value, "completed")
                self.assertEqual(result.get_node("blocked").status.value, "blocked")
                self.assertFalse(result.has_failed_nodes())
                self.assertEqual(state.get("graph_outcome"), "blocked")

        asyncio.run(execute())


if __name__ == "__main__":
    unittest.main()
