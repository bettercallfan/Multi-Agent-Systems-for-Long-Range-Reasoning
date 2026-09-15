"""Stress and integrity checks for the long-horizon execution contract."""

import json
import tempfile
import unittest
from pathlib import Path

from orchestration.core.run_state import RunState
from orchestration.core.workflow import (
    _execute_graph_in_horizons,
    load_horizon_checkpoint,
)
from orchestration.execution.capability_registry import CapabilityRegistry
from orchestration.execution.node_executor import (
    ExecutorDescriptor,
    NodeExecutionResult,
)
from orchestration.graph.task_graph import TaskGraph, TaskNode
from utils.long_horizon_stress import run_stress


class _StressExecutor:
    descriptor = ExecutorDescriptor(
        executor_id="stress-executor", executor_type="test", capabilities=["stress"]
    )

    async def execute(self, context):
        return NodeExecutionResult(
            node_id=context.node.node_id,
            executor_id=self.descriptor.executor_id,
            status="completed",
            summary=f"completed {context.node.node_id}",
            structured_output={"step": context.node.node_id},
        )


def _chain(size: int) -> TaskGraph:
    return TaskGraph(
        graph_id="stress-chain",
        goal="retain the immutable global objective across a long chain",
        nodes=[TaskNode(
            node_id=f"step_{index:04d}",
            description="execute one bounded step",
            capability="stress",
            dependencies=[f"step_{index - 1:04d}"] if index else [],
            success_criteria=[{"type": "node_result"}],
        ) for index in range(size)],
    )


class LongHorizonContinuityTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_thousand_steps_resume_without_goal_drift_or_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            result = await run_stress(
                directory, total_steps=1000, resume_after=500, window_size=25,
            )
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["completed_steps"], 1000)
            self.assertEqual(result["unique_executed_steps"], 1000)
            self.assertEqual(result["duplicate_executions"], 0)
            self.assertTrue(result["goal_preserved"])
            self.assertTrue(result["resume_supported"])
            self.assertEqual(result["checkpoint_integrity_algorithm"], "sha256")

    async def test_three_hundred_steps_preserve_goal_and_checkpoint_integrity(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            state = RunState(directory)
            state.start_stage("classify")
            state.start_stage("plan")
            state.start_stage("execute")
            registry = CapabilityRegistry()
            registry.register(_StressExecutor())
            spec = {
                "task_id": "stress",
                "task_type": "general_complex_task",
                "required_artifacts": [],
                "success_criteria": {},
                "required_report_sections": [],
                "horizon_policy": {
                    "mode": "progressive", "max_active_nodes": 3,
                    "max_epochs": 400, "checkpoint_each_epoch": True,
                },
                "recovery_policy": {"max_node_retries": 0, "max_executor_switches": 0, "max_replans": 0},
            }
            graph = await _execute_graph_in_horizons(
                task_graph=_chain(300), task_spec=spec, registry=registry,
                run_state=state, run_dir=run_dir, replan_callback=None,
                trace=[], validate_task_contract=False,
            )
            self.assertTrue(graph.is_finished())
            self.assertEqual(sum(node.status.value == "completed" for node in graph.nodes), 300)
            latest = sorted((run_dir / "checkpoints").glob("epoch-*.json"))[-1]
            restored, payload = load_horizon_checkpoint(run_dir, expected_goal=graph.goal, expected_task_spec=spec)
            self.assertEqual(restored.goal, graph.goal)
            self.assertTrue(payload["resume_supported"])
            self.assertEqual(payload["protected_context"]["completed_node_ids"][-1], "step_0299")
            self.assertEqual(payload["integrity"]["algorithm"], "sha256")
            self.assertTrue(latest.is_file())

    async def test_tampered_checkpoint_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            state = RunState(directory)
            state.start_stage("classify"); state.start_stage("plan"); state.start_stage("execute")
            registry = CapabilityRegistry(); registry.register(_StressExecutor())
            spec = {"task_id": "tamper", "task_type": "general_complex_task", "required_artifacts": [], "success_criteria": {}, "required_report_sections": [], "horizon_policy": {"mode": "progressive", "max_active_nodes": 1, "max_epochs": 2}, "recovery_policy": {"max_node_retries": 0, "max_executor_switches": 0, "max_replans": 0}}
            await _execute_graph_in_horizons(task_graph=_chain(2), task_spec=spec, registry=registry, run_state=state, run_dir=run_dir, replan_callback=None, trace=[], validate_task_contract=False)
            path = sorted((run_dir / "checkpoints").glob("epoch-*.json"))[-1]
            data = json.loads(path.read_text())
            data["protected_context"]["global_goal"] = "tampered"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_horizon_checkpoint(run_dir)


if __name__ == "__main__":
    unittest.main()
