import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from main import apply_runtime_policy_overrides
from orchestration.core.run_state import RunState
from orchestration.execution.capability_registry import CapabilityRegistry
from orchestration.execution.node_executor import (
    ExecutorDescriptor, NodeExecutionResult,
)
from orchestration.execution.runtime_injections import normalize_injection_policy
from orchestration.graph.graph_scheduler import GraphScheduler
from orchestration.graph.task_graph import TaskGraph, TaskNode


class SuccessfulExecutor:
    descriptor = ExecutorDescriptor(
        executor_id="demo-worker", executor_type="deterministic",
        capabilities=["analysis"], resource_location="cloud",
    )

    async def execute(self, context):
        await asyncio.sleep(0)
        return NodeExecutionResult(
            node_id=context.node.node_id,
            executor_id=self.descriptor.executor_id,
            status="completed",
            summary=f"completed {context.node.node_id}",
            evidence_refs=["task_spec.json"],
        )


class RuntimeInjectionTests(unittest.IsolatedAsyncioTestCase):
    def make_policy(self):
        return {
            "enabled": True,
            "injections": [
                {"injection_id": "node-down", "type": "node_failure",
                 "after_completed_nodes": 0},
                {"injection_id": "new-requirement", "type": "requirement_change",
                 "after_completed_nodes": 1,
                 "change_text": "追加需求：输出必须保留审计证据。"},
                {"injection_id": "bad-data", "type": "data_anomaly",
                 "after_completed_nodes": 1},
            ],
        }

    async def test_all_three_injections_recover_inside_real_scheduler(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "inputs").mkdir()
            source = root / "inputs" / "task.md"
            source.write_text("original input", encoding="utf-8")
            original = source.read_bytes()
            task_spec = {
                "task_type": "injection_demo",
                "code_policy": {"mode": "none", "max_retries": 0},
                "required_artifacts": [],
                "input": {"files": [str(source)]},
                "routing_policy": {"mode": "agent_prune_lite"},
                "recovery_policy": {
                    "max_node_retries": 1,
                    "max_executor_switches": 0,
                    "max_replans": 0,
                },
                "runtime_injection_policy": self.make_policy(),
            }
            (root / "task_spec.json").write_text(
                json.dumps(task_spec, ensure_ascii=False), encoding="utf-8",
            )
            state = RunState(str(root), task_type="injection_demo")
            state.configure(task_spec)
            state.start_stage("execute")
            registry = CapabilityRegistry()
            registry.register(SuccessfulExecutor())
            graph = TaskGraph(
                graph_id="injection-demo", goal="prove autonomous recovery",
                nodes=[
                    TaskNode(node_id="a", description="first", capability="analysis",
                             success_criteria=[{"type": "node_result"}]),
                    TaskNode(node_id="b", description="second", capability="analysis",
                             dependencies=["a"], success_criteria=[{"type": "node_result"}]),
                    TaskNode(node_id="c", description="third", capability="analysis",
                             dependencies=["b"], success_criteria=[{"type": "node_result"}]),
                ],
            )
            completed = await GraphScheduler(
                registry, state, root, validate_task_contract=False,
            ).run(graph, task_spec)

            self.assertTrue(completed.is_finished())
            self.assertFalse(completed.has_failed_nodes())
            self.assertEqual(completed.get_node("a").attempts, 2)
            self.assertEqual(completed.get_node("b").attempts, 2)
            self.assertEqual(completed.version, 2)
            self.assertIn("追加需求", completed.get_node("b").description)
            self.assertIn(
                "evidence_refs_nonempty",
                [item["type"] for item in completed.get_node("b").success_criteria],
            )
            self.assertEqual(source.read_bytes(), original)
            runtime = state.get("runtime_injections")
            self.assertEqual(runtime["recovered_count"], 3)
            self.assertTrue(all(
                item["status"] == "recovered"
                for item in runtime["items"].values()
            ))
            bad_data = json.loads((
                root / "injections/bad-data/evidence.json"
            ).read_text(encoding="utf-8"))
            self.assertEqual(bad_data["original_sha256"], bad_data["restored_sha256"])
            self.assertNotEqual(bad_data["original_sha256"], bad_data["corrupted_sha256"])

    def test_policy_rejects_unknown_and_duplicate_injections(self):
        with self.assertRaises(ValueError):
            normalize_injection_policy({
                "enabled": True,
                "injections": [{"type": "network_partition"}],
            })
        with self.assertRaises(ValueError):
            normalize_injection_policy({
                "enabled": True,
                "injections": [
                    {"injection_id": "same", "type": "node_failure"},
                    {"injection_id": "same", "type": "data_anomaly"},
                ],
            })

    def test_recovery_demo_cli_freezes_all_three_injections(self):
        task_spec = {
            "communication_policy": {},
            "recovery_policy": {"max_node_retries": 0},
        }
        updated = apply_runtime_policy_overrides(task_spec, SimpleNamespace(
            llmlingua_device_map=None,
            llmlingua_allow_download=False,
            injection_profile="recovery-demo",
            inject=[],
            requirement_change_text="追加动态审计要求",
        ))
        policy = normalize_injection_policy(updated["runtime_injection_policy"])
        self.assertEqual(
            [item["type"] for item in policy["injections"]],
            ["node_failure", "requirement_change", "data_anomaly"],
        )
        self.assertEqual(updated["recovery_policy"]["max_node_retries"], 1)


if __name__ == "__main__":
    unittest.main()
