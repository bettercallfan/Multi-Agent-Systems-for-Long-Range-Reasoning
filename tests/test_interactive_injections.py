import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from orchestration.core.run_state import RunState
from orchestration.execution.capability_registry import CapabilityRegistry
from orchestration.execution.node_executor import ExecutorDescriptor, NodeExecutionResult
from orchestration.graph.graph_scheduler import GraphScheduler
from orchestration.graph.task_graph import TaskGraph, TaskNode
from utils.runtime_injection_queue import (
    enqueue_injection, read_injection_queue, close_injection_queue,
)
from utils.injection_audit import audit_runtime_injections


class InteractiveInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_submitted_during_execution_recover_without_state_writer_race(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "inputs").mkdir()
            source = root / "inputs/task.md"
            source.write_text("original", encoding="utf-8")
            spec = {
                "task_type": "interactive_test", "accept_runtime_injections": True,
                "code_policy": {"mode": "none"}, "required_artifacts": [],
                "input": {"files": [str(source)]},
                "recovery_policy": {"max_node_retries": 1, "max_executor_switches": 0, "max_replans": 0},
            }
            (root / "task_spec.json").write_text(json.dumps(spec), encoding="utf-8")
            state = RunState(str(root), task_type="interactive_test")
            state.configure(spec)
            state.start_stage("execute")

            class Worker:
                descriptor = ExecutorDescriptor(executor_id="worker", executor_type="deterministic", capabilities=["analysis"])

                async def execute(self, context):
                    if context.node.node_id == "a":
                        # Requests arrive after the run started, while a node executes.
                        for kind in ("node_failure", "requirement_change", "data_anomaly"):
                            enqueue_injection(root, {"type": kind})
                        await asyncio.sleep(0)
                    return NodeExecutionResult(node_id=context.node.node_id, executor_id="worker",
                                               status="completed", summary="Validated work unit",
                                               evidence_refs=["task_spec.json"])

            registry = CapabilityRegistry()
            registry.register(Worker())
            graph = TaskGraph(graph_id="live", goal="prove interactive recovery", nodes=[
                TaskNode(node_id=name, capability="analysis", description=name,
                         dependencies=[] if i == 0 else ["abcd"[i-1]],
                         success_criteria=[{"type": "node_result"}])
                for i, name in enumerate("abcd")
            ])
            result = await GraphScheduler(registry, state, root, validate_task_contract=False).run(graph, spec)
            self.assertFalse(result.has_failed_nodes(), [(n.node_id, n.error) for n in result.nodes])
            self.assertEqual(state.get("runtime_injections")["recovered_count"], 3)
            self.assertEqual(result.get_node("a").attempts, 1)
            self.assertEqual(result.get_node("b").attempts, 2)
            self.assertEqual(result.get_node("c").attempts, 2)
            self.assertEqual(result.version, 2)
            self.assertEqual(source.read_text(), "original")
            self.assertTrue(all(r["status"] == "accepted" for r in read_injection_queue(root)["requests"]))
            self.assertTrue(audit_runtime_injections(root)["checks"]["interactive_requests_applied"])
            self.assertEqual(close_injection_queue(root), [])
            with self.assertRaisesRegex(ValueError, "窗口已关闭"):
                enqueue_injection(root, {"type": "node_failure"})

    def test_invalid_and_late_requests_are_not_silently_treated_as_recovered(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError):
                enqueue_injection(root, {"type": "shell", "command": "arbitrary"})
            with self.assertRaises(ValueError):
                enqueue_injection(root, {"type": "node_failure", "target_node_id": "../../outside"})
            request = enqueue_injection(root, {"type": "data_anomaly"})
            pending = close_injection_queue(root)
            self.assertEqual(pending[0]["injection_id"], request["injection_id"])
            self.assertEqual(read_injection_queue(root)["requests"][0]["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
