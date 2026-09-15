import tempfile
import unittest
from pathlib import Path

from orchestration.graph.task_graph import TaskGraph, TaskNode
from orchestration.memory.memory_models import MemoryRecord
from orchestration.memory.runtime_memory import RuntimeMemoryManager, RuntimeMemoryStore


def graph() -> TaskGraph:
    return TaskGraph(
        graph_id="memory-graph",
        goal="保持长程任务状态",
        nodes=[
            TaskNode(
                node_id="a", description="建立输入事实", capability="analysis",
                success_criteria=[{"type": "node_result"}], max_retries=0,
            ),
            TaskNode(
                node_id="b", description="核验输入事实", capability="evidence_verification",
                dependencies=["a"], success_criteria=[{"type": "node_result"}], max_retries=0,
            ),
        ],
    )


class RuntimeMemoryTests(unittest.TestCase):
    def test_coala_records_are_persistent_and_capsule_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            spec = {
                "task_id": "run-1",
                "task_name": "长程事实核验",
                "task_type": "document_analysis",
                "code_policy": {"mode": "none", "max_retries": 0},
                "required_report_sections": ["结论"],
                "success_criteria": {"execution_must_succeed_for_pass": True},
                "memory_policy": {
                    "enabled": True,
                    "runtime_enabled": True,
                    "max_runtime_memories": 2,
                },
                "communication_policy": {"compressor": "deterministic"},
            }
            manager = RuntimeMemoryManager(run_dir, spec)
            manager.initialize()
            current = graph()
            manager.remember_result(current, current.get_node("a"), {
                "status": "completed",
                "executor_id": "analysis_agent",
                "summary": "已经确认关键输入事实，不能作最终审批",
                "evidence_refs": ["normalized_input.json"],
            })
            capsule = manager.build_capsule(current, current.get_node("b"))
            self.assertIn("长程事实核验", capsule.global_goal)
            self.assertTrue(any("不得虚构" in item for item in capsule.critical_constraints))
            self.assertEqual(capsule.current_node["node_id"], "b")
            self.assertLessEqual(len(capsule.relevant_memories), 2)
            self.assertTrue((run_dir / "memory" / "runtime_memory.sqlite3").is_file())
            self.assertTrue((run_dir / "memory" / "capsules" / "b-attempt-1.json").is_file())
            self.assertGreaterEqual(capsule.metrics["critical_fields_retained"], 1)

            reopened = RuntimeMemoryStore(run_dir / "memory" / "runtime_memory.sqlite3")
            records = reopened.records("run-1")
            self.assertTrue(any(record.kind == "episodic" for record in records))
            self.assertTrue(any(record.tier == "core" for record in records))
            reopened.close()

    def test_unverified_claim_is_not_promoted_to_semantic_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            spec = {
                "task_id": "run-2", "task_name": "事实任务", "task_type": "general",
                "code_policy": {"mode": "none", "max_retries": 0},
                "memory_policy": {"enabled": True, "runtime_enabled": True},
            }
            manager = RuntimeMemoryManager(run_dir, spec)
            current = graph()
            manager.remember_result(current, current.get_node("a"), {
                "status": "completed", "summary": "模型猜测某个事实",
                "structured_output": {"claims": [{"claim": "未证实", "verdict": "unsupported"}]},
            })
            records = manager.store.records("run-2")
            self.assertFalse(any(record.kind == "semantic" for record in records))

    def test_verified_evidence_becomes_semantic_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            spec = {
                "task_id": "run-3", "task_name": "事实任务", "task_type": "general",
                "code_policy": {"mode": "none", "max_retries": 0},
                "memory_policy": {"enabled": True, "runtime_enabled": True},
            }
            manager = RuntimeMemoryManager(run_dir, spec)
            current = graph()
            manager.remember_result(current, current.get_node("a"), {
                "status": "completed", "summary": "证据已核验",
                "structured_output": {"verification": {"claims": [{
                    "claim": "输入边界已建立", "verdict": "supported",
                    "evidence_refs": ["normalized_input.json"], "confidence": 0.99,
                }]}},
            })
            records = manager.store.records("run-3")
            facts = [record for record in records if record.kind == "semantic"]
            self.assertEqual(len(facts), 1)
            self.assertTrue(facts[0].verified)
            self.assertEqual(facts[0].evidence_refs, ["normalized_input.json"])


if __name__ == "__main__":
    unittest.main()
