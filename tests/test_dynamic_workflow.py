import json
from pathlib import Path
import tempfile
import unittest

from autogen_ext.models.replay import ReplayChatCompletionClient

from orchestration.core.run_state import RunState
from orchestration.graph.task_graph import TaskGraph
from orchestration.core.workflow import _build_graph_plan, run_explicit_workflow


SEMANTIC_REVIEW = {
    "blocking_issues": [], "advisory_issues": [], "evidence_references": [],
    "repair_recommended": False, "repair_target": "none",
}

ANALYSIS_RESULT = {
    "summary": "任务目标和约束已根据标准化输入完成核对。",
    "findings": ["任务要求执行受框架控制的动态任务图并生成验证报告。"],
    "evidence_refs": ["normalized_input.json"],
    "risks": [],
    "confidence": 0.9,
}

TASK_UNDERSTANDING_RESULT = {
    "summary": "分析输入任务并形成经过验证的报告。",
    "goals": ["完成输入分析", "生成最终报告"],
    "constraints": ["状态推进由 Python 框架控制"],
    "ambiguities": [],
    "risk_flags": [],
    "recommended_capabilities": ["analysis", "artifact_validation"],
    "confidence": 0.95,
}

REPORT = """# 任务理解
本次运行用于验证动态任务图能够根据结构化节点依赖选择异构执行者，并由框架控制全部状态转换。
系统没有让规划组件直接调用其他组件，也没有依赖自然语言声明判断任务完成状态。

## 结果分析
规划阶段生成的任务图经过 DAG、能力和产物契约校验后才进入执行阶段。分析节点与确定性验证节点按照依赖顺序执行，下游只接收直接依赖节点的结构化结果。运行状态、路由选择、实际使用的通信边以及节点结果都已实时保存。最终技术审查与交付验收均由固定外层框架执行，因此该报告对应的是已经通过框架校验的真实运行结果。
"""


def valid_no_code_graph():
    return {
        "graph_id": "llm_graph", "version": 1, "goal": "分析并验证任务",
        "nodes": [
            {
                "node_id": "analyze", "description": "分析任务目标与约束",
                "capability": "analysis", "dependencies": [], "input_artifacts": [],
                "output_artifacts": [], "success_criteria": [{"type": "node_result"}],
                "max_retries": 0,
            },
            {
                "node_id": "validate", "description": "校验中间产物契约",
                "capability": "artifact_validation", "dependencies": ["analyze"],
                "input_artifacts": [], "output_artifacts": [],
                "success_criteria": [{"type": "artifact_quality", "equals": "passed"}],
                "max_retries": 0,
            },
        ],
    }


def invalid_unknown_capability_graph():
    graph = valid_no_code_graph()
    graph["nodes"][0]["capability"] = "imaginary_full_mesh_agent"
    return graph


def graph_with_missing_declared_output():
    graph = valid_no_code_graph()
    graph["nodes"][0]["output_artifacts"] = ["artifacts/missing.json"]
    graph["nodes"][1]["input_artifacts"] = ["artifacts/missing.json"]
    return graph


class DynamicWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def test_graph_plan_flags_are_derived_from_validated_nodes(self):
        graph_payload = valid_no_code_graph()
        graph_payload["nodes"][0]["capability"] = "code"
        graph_payload["nodes"][1]["capability"] = "reasoning"
        graph = TaskGraph.model_validate(graph_payload)

        plan = _build_graph_plan(graph, ["code", "reasoning"])

        self.assertTrue(plan["requires_code"])
        self.assertTrue(plan["use_reasoning"])
        self.assertFalse(plan["use_research"])
        self.assertEqual(plan["edge_count"], 1)
        self.assertEqual(plan["topology_density"], 0.5)

    def make_spec_and_state(self, run_dir: Path):
        spec = {
            "task_id": "dynamic_test", "task_name": "动态无代码任务",
            "task_type": "document_analysis",
            "run_dir": str(run_dir),
            "input": {"type": "text", "files": [], "text": "分析任务"},
            "file_previews": [],
            "code_policy": {"mode": "none", "max_lines": 0, "max_retries": 0},
            "artifact_contract": {
                "intermediate_artifacts": [], "final_artifacts": ["final_report.md"],
                "framework_artifacts": [
                    "task_spec.json", "task_graph.json", "run_state.json",
                    "agent_trace.md", "normalized_input.json",
                ],
            },
            "required_artifacts": [
                "final_report.md", "task_spec.json", "task_graph.json",
                "run_state.json", "agent_trace.md", "normalized_input.json",
            ],
            "required_report_sections": ["任务理解", "结果分析"],
            "success_criteria": {},
            "artifacts": {"code": None, "results": [], "report": "final_report.md"},
        }
        (run_dir / "task_spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        state = RunState(
            str(run_dir), task_type=spec["task_type"], code_policy=spec["code_policy"],
            required_artifacts=spec["required_artifacts"],
        )
        state.start_stage("classify")
        state.configure(spec)
        return spec, state

    async def test_invalid_graph_is_repaired_once_then_drives_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            spec, state = self.make_spec_and_state(run_dir)
            client = ReplayChatCompletionClient([
                json.dumps(invalid_unknown_capability_graph(), ensure_ascii=False),
                json.dumps(valid_no_code_graph(), ensure_ascii=False),
                json.dumps(ANALYSIS_RESULT, ensure_ascii=False),
                json.dumps(SEMANTIC_REVIEW, ensure_ascii=False),
                REPORT,
            ])

            decision, _ = await run_explicit_workflow(spec, state, client)

            self.assertEqual(decision.status, "passed", decision.issues)
            current = state.to_dict()
            self.assertEqual(current["plan"]["execution_mode"], "task_graph")
            self.assertFalse(current["plan"]["requires_code"])
            self.assertEqual(current["plan"]["edge_count"], 1)
            self.assertEqual(current["graph_outcome"], "success")
            self.assertFalse(current["fallback"]["triggered"])
            self.assertEqual([item["selected_executor_id"] for item in current["routing"]], [
                "analysis_agent", "artifact_validation_executor",
            ])
            self.assertEqual(current["communication"]["dependency_edges_used"], [["analyze", "validate"]])
            self.assertEqual(current["communication"]["full_history_broadcasts"], 0)
            self.assertEqual(len(client.create_calls), 5)
            self.assertEqual(current["model_calls"]["count"], 5)
            self.assertEqual(len(current["model_calls"]["calls"]), 5)
            self.assertGreater(current["model_calls"]["prompt_tokens_estimated"], 0)
            self.assertEqual(current["model_calls"]["prompt_budget_violations"], 0)
            self.assertEqual(
                current["nodes"]["analyze"]["result"]["structured_output"]
                ["quality_signal"]["status"],
                "passed",
            )
            self.assertTrue(all(
                call["prompt_budget_tokens"] in {5000, 8000}
                for call in current["model_calls"]["calls"]
            ))
            event_types = [event["type"] for event in current["events"]]
            self.assertEqual(event_types.count("task_graph_validation_failed"), 1)

    async def test_task_understanding_agent_is_called_before_planner(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            spec, state = self.make_spec_and_state(run_dir)
            spec["task_understanding_policy"] = {
                "enabled": True,
                "max_preview_chars_per_file": 1500,
            }
            spec["memory_policy"] = {"enabled": False}
            state.configure(spec)
            client = ReplayChatCompletionClient([
                json.dumps(TASK_UNDERSTANDING_RESULT, ensure_ascii=False),
                json.dumps(valid_no_code_graph(), ensure_ascii=False),
                json.dumps(ANALYSIS_RESULT, ensure_ascii=False),
                json.dumps(SEMANTIC_REVIEW, ensure_ascii=False),
                REPORT,
            ])

            decision, _ = await run_explicit_workflow(spec, state, client)

            self.assertEqual(decision.status, "passed", decision.issues)
            understanding_path = run_dir / "planning" / "task_understanding.json"
            self.assertTrue(understanding_path.is_file())
            understanding = json.loads(understanding_path.read_text(encoding="utf-8"))
            self.assertEqual(understanding["status"], "completed")
            self.assertEqual(understanding["goals"], TASK_UNDERSTANDING_RESULT["goals"])
            event_types = [event["type"] for event in state.get("events")]
            self.assertIn("task_understanding_recorded", event_types)
            self.assertEqual(len(client.create_calls), 5)

    async def test_two_invalid_graphs_use_valid_deterministic_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            spec, state = self.make_spec_and_state(run_dir)
            client = ReplayChatCompletionClient([
                json.dumps(invalid_unknown_capability_graph(), ensure_ascii=False),
                json.dumps(invalid_unknown_capability_graph(), ensure_ascii=False),
                json.dumps(ANALYSIS_RESULT, ensure_ascii=False),
                json.dumps(SEMANTIC_REVIEW, ensure_ascii=False),
                REPORT,
            ])

            decision, _ = await run_explicit_workflow(spec, state, client)

            self.assertEqual(decision.status, "passed", decision.issues)
            current = state.to_dict()
            self.assertTrue(current["fallback"]["triggered"])
            persisted = json.loads((run_dir / "task_graph.json").read_text(encoding="utf-8"))
            self.assertEqual([node["node_id"] for node in persisted["nodes"]], [
                "analyze_task", "validate_artifacts",
            ])
            self.assertTrue(all(node["status"] == "completed" for node in persisted["nodes"]))
            self.assertEqual(len(client.create_calls), 5)

    async def test_failed_graph_blocks_report_agent_call(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            spec, state = self.make_spec_and_state(run_dir)
            spec["artifact_contract"]["intermediate_artifacts"] = ["artifacts/missing.json"]
            spec["required_artifacts"].append("artifacts/missing.json")
            (run_dir / "task_spec.json").write_text(
                json.dumps(spec, ensure_ascii=False), encoding="utf-8"
            )
            state.configure(spec)
            client = ReplayChatCompletionClient([
                json.dumps(graph_with_missing_declared_output(), ensure_ascii=False),
                "分析完成，但执行器没有伪造声明的文件产物。",
            ])

            decision, _ = await run_explicit_workflow(spec, state, client)

            self.assertEqual(decision.status, "failed")
            current = state.to_dict()
            self.assertEqual(current["graph_outcome"], "failed")
            self.assertEqual(current["nodes"]["analyze"]["status"], "failed")
            self.assertEqual(current["nodes"]["validate"]["status"], "blocked")
            self.assertFalse((run_dir / "final_report.md").exists())
            # Planning + analysis only: neither semantic review nor report was called.
            self.assertEqual(len(client.create_calls), 2)
            self.assertEqual(current["model_calls"]["count"], 2)


if __name__ == "__main__":
    unittest.main()
