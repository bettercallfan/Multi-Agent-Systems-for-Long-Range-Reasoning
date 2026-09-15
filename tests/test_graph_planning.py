import unittest

from pydantic import ValidationError

from orchestration.graph.graph_planner import build_fallback_graph, validate_planned_graph
from orchestration.graph.task_graph import GraphValidationError, NodeStatus


class GraphPlanningTests(unittest.TestCase):
    def setUp(self):
        self.spec = {
            "task_id": "plan_test",
            "task_name": "规划测试",
            "code_policy": {"mode": "lightweight"},
            "artifact_contract": {
                "intermediate_artifacts": ["artifacts/result.json", "artifacts/code_pipeline.py"]
            },
        }
        self.capabilities = ["analysis", "code", "artifact_validation"]

    def valid_graph(self):
        return {
            "graph_id": "model_chosen_id",
            "version": 1,
            "goal": "produce result",
            "nodes": [
                {
                    "node_id": "code_node", "description": "produce", "capability": "code",
                    "dependencies": [], "input_artifacts": [],
                    "output_artifacts": ["artifacts/result.json", "artifacts/code_pipeline.py"],
                    "success_criteria": [{"type": "execution_exit_code", "equals": 0}],
                    "max_retries": 99,
                    "status": "completed", "assigned_executor": "fake", "attempts": 7,
                    "result": {"untrusted": True}, "error": None,
                },
                {
                    "node_id": "validate_node", "description": "validate",
                    "capability": "artifact_validation", "dependencies": ["code_node"],
                    "input_artifacts": ["artifacts/result.json"], "output_artifacts": [],
                    "success_criteria": [{"type": "artifact_quality", "equals": "passed"}],
                    "max_retries": 5,
                },
            ],
        }

    def test_valid_graph_resets_all_model_runtime_claims(self):
        graph = validate_planned_graph(self.valid_graph(), self.spec, self.capabilities)
        node = graph.get_node("code_node")
        self.assertEqual(graph.graph_id, "plan_test_graph")
        self.assertEqual(node.status, NodeStatus.PENDING)
        self.assertIsNone(node.assigned_executor)
        self.assertEqual(node.attempts, 0)
        self.assertIsNone(node.result)
        self.assertEqual(node.max_retries, 0)

    def test_unknown_capability_and_undeclared_output_rejected(self):
        unknown = self.valid_graph()
        unknown["nodes"][0]["capability"] = "invented_agent"
        with self.assertRaises((GraphValidationError, ValidationError)):
            validate_planned_graph(unknown, self.spec, self.capabilities)

        undeclared = self.valid_graph()
        undeclared["nodes"][0]["output_artifacts"].append("artifacts/hidden.json")
        with self.assertRaises(GraphValidationError):
            validate_planned_graph(undeclared, self.spec, self.capabilities)

    def test_graph_requires_single_terminal_validation_and_all_contract_outputs(self):
        missing_validator = self.valid_graph()
        missing_validator["nodes"] = missing_validator["nodes"][:1]
        with self.assertRaises(GraphValidationError):
            validate_planned_graph(missing_validator, self.spec, self.capabilities)

        missing_output = self.valid_graph()
        missing_output["nodes"][0]["output_artifacts"].remove("artifacts/result.json")
        with self.assertRaises(GraphValidationError):
            validate_planned_graph(missing_output, self.spec, self.capabilities)

    def test_only_single_code_node_may_own_code_task_artifacts(self):
        non_code_writer = self.valid_graph()
        non_code_writer["nodes"].insert(0, {
            "node_id": "analysis_writer", "description": "wrong writer",
            "capability": "analysis", "dependencies": [], "input_artifacts": [],
            "output_artifacts": ["artifacts/result.json"],
            "success_criteria": [{"type": "node_result"}], "max_retries": 0,
        })
        non_code_writer["nodes"][1]["output_artifacts"].remove("artifacts/result.json")
        non_code_writer["nodes"][2]["dependencies"].append("analysis_writer")
        with self.assertRaisesRegex(GraphValidationError, "只有 code 节点"):
            validate_planned_graph(non_code_writer, self.spec, self.capabilities)

        duplicate_code = self.valid_graph()
        duplicate_code["nodes"].insert(0, {
            "node_id": "second_code", "description": "duplicate pipeline",
            "capability": "code", "dependencies": [], "input_artifacts": [],
            "output_artifacts": [],
            "success_criteria": [{"type": "execution_exit_code", "equals": 0}],
            "max_retries": 0,
        })
        duplicate_code["nodes"][2]["dependencies"].append("second_code")
        with self.assertRaisesRegex(GraphValidationError, "只能包含一个 code 节点"):
            validate_planned_graph(duplicate_code, self.spec, self.capabilities)

    def test_deterministic_fallback_is_valid_dynamic_graph(self):
        graph = build_fallback_graph(self.spec, self.capabilities, "model unavailable")
        graph.validate_graph(set(self.capabilities))
        self.assertEqual(graph.topological_order(), [
            "analyze_task", "execute_code", "validate_artifacts",
        ])
        self.assertNotIn("report", [node.capability for node in graph.nodes])
        self.assertNotIn("review", [node.capability for node in graph.nodes])

    def test_fallback_topology_changes_with_task_code_policy(self):
        code_graph = build_fallback_graph(self.spec, self.capabilities, "model unavailable")
        no_code_spec = {
            **self.spec,
            "task_id": "no_code_plan_test",
            "code_policy": {"mode": "none"},
            "artifact_contract": {"intermediate_artifacts": []},
        }
        no_code_graph = build_fallback_graph(
            no_code_spec, self.capabilities, "model unavailable"
        )

        self.assertEqual(len(code_graph.nodes), 3)
        self.assertEqual(len(no_code_graph.nodes), 2)
        self.assertIn("code", [node.capability for node in code_graph.nodes])
        self.assertNotIn("code", [node.capability for node in no_code_graph.nodes])
        self.assertNotEqual(
            [node.dependencies for node in code_graph.nodes],
            [node.dependencies for node in no_code_graph.nodes],
        )


if __name__ == "__main__":
    unittest.main()
