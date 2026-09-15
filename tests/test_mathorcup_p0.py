import json
import tempfile
import unittest
from pathlib import Path
import subprocess

from orchestration.integrations.evidence_verifier import _canonical_evidence_ref
from orchestration.graph.repair_loop import _repair_contexts, business_validation_issues
from orchestration.graph.repair_loop import evidence_verification_issues
from orchestration.graph.task_graph import TaskGraph, TaskNode
from orchestration.planning.requirement_compiler import compile_requirement_set
from orchestration.task.task_normalizer import finalize_task_spec, normalize_to_task_spec
from orchestration.validation.mathorcup_validator import MathorCupPackingValidator
from orchestration.validation.models import (BusinessCheckResult, BusinessValidationContext,
                                              BusinessValidationResult)
from task_plugins.mathorcup_d.solver import (
    build_evidence_fallback,
    build_fallback_code,
)


class MathorCupP0Tests(unittest.TestCase):
    def _fixture(self, root: Path):
        (root / "artifacts").mkdir()
        rows = [["货物编号", "类型", "长×宽×高", "单重", "数量"],
                ["G1", "标准件", "60×40×30", "12", "80"],
                ["G2", "标准件", "50×35×25", "8", "100"],
                ["G3", "易碎件", "70×50×40", "15", "30"],
                ["G4", "定向件", "80×60×50", "25", "40"],
                ["G5", "定向件", "40×40×60", "18", "50"]]
        content = ("车型 1 内尺寸：长 420cm 宽 210cm 高 220cm 额定载重：6000kg 单次运输成本：450 元/趟\n"
                   "车型 2 内尺寸：长 680cm 宽 245cm 高 250cm 额定载重：10000kg 单次运输成本：700 元/趟")
        normalized = {"structured_sources": [{"name": "attachment1.docx", "content": content,
                                               "tables": [{"rows": rows}]}]}
        (root / "normalized_input.json").write_text(json.dumps(normalized, ensure_ascii=False), encoding="utf-8")
        specs = {row[0]: (tuple(map(float, row[2].split("×"))), int(row[4])) for row in rows[1:]}
        placements = []
        for cargo_id, (dims, count) in specs.items():
            for number in range(count):
                placements.append({"cargo_id": cargo_id, "quantity": 1, "vehicle_id": f"V2-{len(placements)}",
                    "vehicle_type": "V2", "scenario": "selected", "orientation": "original",
                    "x": 0, "y": 0, "z": 0, "length": dims[0], "width": dims[1], "height": dims[2]})
        volume = sum(x["length"] * x["width"] * x["height"] for x in placements)
        weight = 80 * 12 + 100 * 8 + 30 * 15 + 40 * 25 + 50 * 18
        result = {"status": "success", "vehicle_scenarios": [{"scenario": "V2-only"}],
                  "multi_vehicle_scenarios": [{"scenario": "mixed"}], "placements": placements,
                  "vehicle_count": 300, "total_cost": 210000,
                  "space_utilization_rate": volume / (300 * 680 * 245 * 250),
                  "load_utilization_rate": weight / (300 * 10000), "validation": {"status": "pending"}}
        for name in ("result.json", "result_complete.json"):
            (root / "artifacts" / name).write_text(json.dumps(result), encoding="utf-8")
        comparison = {"scenarios": [
            {"vehicle_count": 300, "total_cost": 210000, "space_utilization_rate": .1, "load_utilization_rate": .1},
            {"vehicle_count": 301, "total_cost": 210450, "space_utilization_rate": .09, "load_utilization_rate": .09}]}
        (root / "artifacts" / "cost_comparison.json").write_text(json.dumps(comparison), encoding="utf-8")
        return result

    def test_valid_contract_passes_all_deterministic_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._fixture(root)
            context = BusinessValidationContext(run_dir=directory, task_spec={},
                validator_config={"target_artifact": "artifacts/result.json"})
            result = MathorCupPackingValidator().validate(context)
            self.assertEqual(result.status, "passed", result.failed_checks[:3])
            audit = json.loads((root / "artifacts" / "constraint_validation_log.json").read_text())
            self.assertTrue(audit["passed"])
            self.assertEqual(audit["recomputed"]["cargo_quantities"]["G5"], 50)

    def test_boundary_failure_is_indexed_for_code_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self._fixture(root)
            result["placements"][11]["x"] = 700
            (root / "artifacts" / "result.json").write_text(json.dumps(result), encoding="utf-8")
            context = BusinessValidationContext(run_dir=directory, task_spec={}, validator_config={})
            checked = MathorCupPackingValidator().validate(context)
            boundary = next(x for x in checked.failed_checks if x["check"] == "boundary")
            self.assertEqual(boundary["index"], 11)

    def test_task_contract_and_evidence_paths_are_task_local(self):
        spec = normalize_to_task_spec({"files": ["/tmp/attachment1.docx"],
                                      "text": "建立数学优化模型并编写算法求解"})
        spec = finalize_task_spec(spec, [])
        self.assertIn("artifacts/result_complete.json", spec["artifact_contract"]["intermediate_artifacts"])
        self.assertEqual(spec["domain_validator_plugins"][0]["module"], "task_plugins.mathorcup_d.validator")
        self.assertEqual(
            spec["evidence_fallback_plugin"]["function"],
            "build_evidence_fallback",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "artifacts").mkdir()
            (root / "artifacts" / "result.json").write_text("{}")
            self.assertEqual(_canonical_evidence_ref(root, "result.json"), "artifacts/result.json")

    def test_indexed_domain_failure_reopens_code_with_concrete_context(self):
        graph = TaskGraph(graph_id="p0", goal="pack", nodes=[TaskNode(node_id="code", description="pack",
            capability="code", output_artifacts=["artifacts/result.json"])])
        check = BusinessCheckResult(check_id="mathorcup_packing", validator_id="mathorcup_packing",
            status="failed", summary="boundary failed",
            failed_checks=[{"check": "boundary", "index": 11, "vehicle": "V2"}],
            evidence_refs=["artifacts/result.json"])
        validation = BusinessValidationResult(status="failed", mode="required", checks=[check],
            failed_required_checks=["mathorcup_packing"], started_at="x", finished_at="x")
        issues = business_validation_issues(validation, graph)
        self.assertEqual(issues[0]["target_node_id"], "code")
        context = _repair_contexts(issues, 1)["code"]
        self.assertEqual(context["failed_checks"][0]["index"], 11)
        self.assertIn('"index": 11', context["repair_instruction"])

    def test_deterministic_fallback_executes_and_passes_domain_validator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._fixture(root)
            (root / "artifacts" / "code_pipeline.py").write_text(build_fallback_code({}), encoding="utf-8")
            completed = subprocess.run(["python", "artifacts/code_pipeline.py"], cwd=root,
                                       capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            decision = build_evidence_fallback(
                {"run_dir": directory},
                {
                    "code": {
                        "role": "executed_delivery_evidence",
                        "evidence_refs": ["artifacts/result.json"],
                    },
                    "analysis": {
                        "role": "intermediate_hypothesis_or_model",
                        "evidence_refs": ["normalized_input.json"],
                    },
                },
                {
                    "artifacts/result.json",
                    "artifacts/constraint_validation_log.json",
                    "normalized_input.json",
                },
            )
            self.assertEqual(decision["status"], "passed")
            self.assertEqual(
                decision["claims"][0]["source_node_ids"], ["code"],
            )
            context = BusinessValidationContext(run_dir=directory, task_spec={}, validator_config={})
            result = MathorCupPackingValidator().validate(context)
            self.assertEqual(result.status, "passed", result.failed_checks[:3])

    def test_domain_contract_rewrites_invented_acceptance_pointer(self):
        spec = normalize_to_task_spec({"files": ["/tmp/attachment1.docx"],
                                      "text": "建立数学优化模型并编写算法求解"})
        spec = finalize_task_spec(spec, [])
        requirements = compile_requirement_set(spec, {"summary": "x", "requirements": [{
            "requirement_id": "REQ-X", "statement": "输出最少车辆数", "mandatory": True,
            "owner": "planner", "expected_outputs": ["artifacts/result.json"],
            "acceptance_criteria": [{"criterion_id": "AC-X", "method": "numeric_compare",
                "target": "artifacts/result.json#/invented_min_trucks", "condition": "等于零",
                "params": {"value": 0}}]}]})
        criterion = requirements.requirements[0].acceptance_criteria[0]
        self.assertEqual(criterion.method, "required_fields")
        self.assertNotIn("invented_min_trucks", str(criterion.params))

    def test_evidence_claim_routes_to_actual_intermediate_source(self):
        graph = TaskGraph(graph_id="evidence", goal="verify", nodes=[
            TaskNode(node_id="analysis", description="estimate", capability="analysis",
                     success_criteria=[{"type": "node_result"}]),
            TaskNode(node_id="code", description="solve", capability="code", dependencies=["analysis"],
                     success_criteria=[{"type": "node_result"}]),
            TaskNode(node_id="verify", description="verify", capability="evidence_verification",
                     dependencies=["analysis", "code"], success_criteria=[{"type": "node_result"}]),
        ])
        graph.get_node("verify").status = "failed"
        graph.get_node("verify").error = {"structured_output": {"verification": {"claims": [{
            "claim_id": "c1", "source_node_ids": ["analysis"], "claim": "one truck",
            "verdict": "contradicted", "rationale": "actual result uses six"}]}}}
        issues = evidence_verification_issues(graph)
        self.assertEqual(issues[0]["target_node_id"], "analysis")


if __name__ == "__main__":
    unittest.main()
