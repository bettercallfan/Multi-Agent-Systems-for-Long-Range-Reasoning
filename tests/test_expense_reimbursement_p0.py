import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from orchestration.graph.repair_loop import business_validation_issues
from orchestration.graph.task_graph import TaskGraph, TaskNode
from orchestration.task.input_loader import load_input
from orchestration.task.input_preparation import prepare_normalized_input
from orchestration.task.task_normalizer import finalize_task_spec, normalize_to_task_spec
from orchestration.validation.models import BusinessValidationContext
from orchestration.validation.runner import run_business_validation
from task_plugins.expense_reimbursement.solver import (
    build_evidence_fallback,
    build_fallback_code,
)
from task_plugins.expense_reimbursement.validator import ExpenseSummaryValidator
from utils.file_reader import build_file_previews


class ExpenseReimbursementP0Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.example_dir = Path(__file__).parents[1] / "examples" / "expense_reimbursement"

    def _spec_and_input(self):
        raw = load_input(str(self.example_dir))
        spec = normalize_to_task_spec(raw)
        spec = finalize_task_spec(spec, build_file_previews(spec["input"]["files"]))
        return spec, prepare_normalized_input(spec)

    def _run_fallback(self, root: Path):
        spec, normalized = self._spec_and_input()
        spec["run_dir"] = str(root)
        (root / "artifacts").mkdir()
        (root / "normalized_input.json").write_text(
            json.dumps(normalized, ensure_ascii=False), encoding="utf-8",
        )
        code = build_fallback_code(spec)
        self.assertLessEqual(len(code.splitlines()), spec["code_policy"]["max_lines"])
        (root / "artifacts" / "code_pipeline.py").write_text(code, encoding="utf-8")
        completed = subprocess.run(
            ["python", "artifacts/code_pipeline.py"], cwd=root,
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return spec

    def test_task_contract_declares_task_local_plugins_and_decision_scope(self):
        spec, normalized = self._spec_and_input()
        self.assertEqual(spec["task_type"], "document_analysis")
        self.assertEqual(spec["report_policy"]["decision_scope"], "descriptive_only")
        self.assertIn(
            "artifacts/expense_validation_log.json",
            spec["artifact_contract"]["intermediate_artifacts"],
        )
        self.assertEqual(
            spec["domain_validator_plugins"][0]["module"],
            "task_plugins.expense_reimbursement.validator",
        )
        sources = {item["name"]: item for item in normalized["structured_sources"]}
        self.assertEqual(sources["travel_application.docx"]["status"], "loaded")
        self.assertTrue(sources["expense_policy.pdf"]["pages"][0]["content"])

    def test_fallback_derives_complete_result_and_passes_validator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = self._run_fallback(root)
            context = BusinessValidationContext(
                run_dir=directory, task_spec=spec,
                validator_config={"target_artifact": "artifacts/expense_summary.json"},
            )
            provisional = json.loads(
                (root / "artifacts" / "expense_validation_log.json")
                .read_text(encoding="utf-8")
            )
            self.assertIsNone(provisional["passed"])
            result = json.loads(
                (root / "artifacts" / "expense_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(result["trip_information"]["employee_name"], "张三")
            self.assertEqual(result["summary"]["record_count"], 5)
            self.assertEqual(result["summary"]["total_amount"], 1485.0)
            self.assertEqual(
                result["summary"]["category_totals"],
                {"交通": 405.0, "住宿": 960.0, "餐饮": 120.0},
            )
            issue_keys = {(item["type"], item["source_row"])
                          for item in result["issues"]}
            self.assertTrue({("missing_invoice", 4),
                             ("meal_limit_exceeded", 4),
                             ("outside_trip_dates", 6)}.issubset(issue_keys))
            decision = build_evidence_fallback(
                spec,
                {"code": {"role": "executed_delivery_evidence", "evidence_refs": [
                    "artifacts/expense_summary.json",
                    "artifacts/expense_validation_log.json",
                ]}},
                {"artifacts/expense_summary.json",
                 "artifacts/expense_validation_log.json"},
            )
            self.assertEqual(decision["status"], "passed")
            audit = json.loads(
                (root / "artifacts" / "expense_validation_log.json")
                .read_text(encoding="utf-8")
            )
            self.assertTrue(audit["passed"])
            checked = ExpenseSummaryValidator().validate(context)
            self.assertEqual(checked.status, "passed", checked.failed_checks)

    def test_wrong_total_fails_and_routes_back_to_code_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = self._run_fallback(root)
            result_path = root / "artifacts" / "expense_summary.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["summary"]["total_amount"] = 1
            result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            graph = TaskGraph(graph_id="expense", goal="expense", nodes=[TaskNode(
                node_id="code", description="extract", capability="code",
                output_artifacts=["artifacts/expense_summary.json",
                                  "artifacts/expense_validation_log.json"],
            )])
            validation = run_business_validation(
                root, spec, task_graph=graph, persist=False,
            )
            self.assertEqual(validation.status, "failed")
            issues = business_validation_issues(validation, graph)
            summary_issue = next(item for item in issues
                                 if item["check"] == "summary_recompute")
            self.assertEqual(summary_issue["target_node_id"], "code")


if __name__ == "__main__":
    unittest.main()
