import unittest

from orchestration.graph.graph_compiler import compile_semantic_graph
from orchestration.graph.graph_planner import validate_planned_graph


class GraphCompilerTests(unittest.TestCase):
    def setUp(self):
        self.capabilities = [
            "math_modeling", "code", "data_analysis",
            "terminal_execution", "evidence_verification",
            "artifact_validation",
        ]
        self.spec = {
            "task_id": "semantic_test",
            "task_name": "建模任务",
            "code_policy": {"mode": "complex"},
            "recovery_policy": {"max_node_retries": 1},
            "capability_contract": {
                "required": ["terminal_execution", "evidence_verification"],
            },
            "artifact_contract": {
                "intermediate_artifacts": [
                    "artifacts/result.json", "artifacts/code_pipeline.py",
                ],
            },
        }

    def test_compiles_small_semantic_graph_into_strict_execution_graph(self):
        semantic = {
            "goal": "设计、求解并分析模型",
            "nodes": [
                {
                    "node_id": "design_model", "objective": "设计数学模型",
                    "capability": "math_modeling", "dependencies": [],
                    "metadata": {
                        "requirement_ids": ["REQ-MODEL"],
                        "acceptance_refs": ["AC-MODEL"],
                    },
                    "expected_output": {
                        "output_id": "model_spec",
                        "output_type": "structured_result",
                    },
                    "acceptance_criteria": [{
                        "type": "required_fields",
                        "fields": ["/variables"],
                    }],
                },
                {
                    "node_id": "solve_model", "objective": "实现并求解模型",
                    "capability": "code", "dependencies": ["design_model"],
                },
                {
                    "node_id": "analyze_result", "objective": "分析求解结果",
                    "capability": "data_analysis", "dependencies": ["solve_model"],
                },
            ],
        }
        graph, report = compile_semantic_graph(
            semantic, self.spec, self.capabilities,
        )
        validated = validate_planned_graph(
            graph.model_dump(mode="json"), self.spec, self.capabilities,
        )

        self.assertEqual(validated.topological_order(), [
            "design_model", "solve_model", "terminal_execution",
            "analyze_result", "evidence_verification", "artifact_validation",
        ])
        self.assertEqual(
            set(validated.get_node("solve_model").output_artifacts),
            {"artifacts/result.json", "artifacts/code_pipeline.py"},
        )
        self.assertIn(
            "terminal_execution",
            validated.get_node("analyze_result").dependencies,
        )
        self.assertEqual(report["status"], "compiled")
        self.assertEqual(
            validated.get_node("design_model").requirement_ids,
            ["REQ-MODEL"],
        )
        self.assertEqual(
            validated.get_node("design_model").acceptance_refs,
            ["AC-MODEL"],
        )
        self.assertEqual(
            validated.get_node("design_model").acceptance_criteria[0]["type"],
            "required_fields",
        )
        self.assertEqual(
            validated.get_node("design_model").success_criteria,
            [{"type": "node_result"}],
        )

    def test_rejects_unknown_business_dependency(self):
        semantic = {
            "goal": "bad",
            "nodes": [{
                "node_id": "analyze", "objective": "分析",
                "capability": "data_analysis", "dependencies": ["missing"],
            }],
        }
        with self.assertRaisesRegex(ValueError, "依赖不存在"):
            compile_semantic_graph(semantic, self.spec, self.capabilities)

    def test_non_code_stage_converts_final_artifact_claims_before_graph_validation(self):
        """Logical model nodes may reference a later result without writing it."""

        stage_spec = {
            **self.spec,
            "code_policy": {"mode": "none"},
            "artifact_contract": {"intermediate_artifacts": []},
            "capability_contract": {"required": []},
        }
        semantic = {
            "goal": "produce two independently testable model specifications",
            "nodes": [
                {
                    "node_id": "model_a", "objective": "model objective A",
                    "capability": "math_modeling", "dependencies": [],
                    "output_artifacts": ["artifacts/result.json"],
                },
                {
                    "node_id": "model_b", "objective": "model objective B",
                    "capability": "math_modeling", "dependencies": [],
                    "output_artifacts": ["artifacts/result.json"],
                },
            ],
        }

        graph, report = compile_semantic_graph(
            semantic, stage_spec, self.capabilities,
            include_governance=False,
        )

        self.assertTrue(all(not node.output_artifacts for node in graph.nodes))
        converted = [
            item for item in report["repairs"]
            if item["action"] == "convert_undeclared_artifact_to_logical_output"
        ]
        self.assertEqual({item["node_id"] for item in converted}, {
            "model_a", "model_b",
        })


if __name__ == "__main__":
    unittest.main()
