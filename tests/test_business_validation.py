import json
import tempfile
import unittest
from pathlib import Path

from orchestration.core.run_state import RunState
from orchestration.validation.models import BusinessCheckResult
from orchestration.validation.registry import BusinessValidatorRegistry
from orchestration.validation.runner import run_business_validation
from orchestration.task.task_normalizer import (
    finalize_task_spec,
    normalize_to_task_spec,
)
from utils.review_artifacts import pre_report_review


def make_spec(directory: str, *, mode="required", validators=None):
    return {
        "task_type": "generic_test",
        "run_dir": directory,
        "input": {"files": [str(Path(directory) / "source.txt")]},
        "code_policy": {"mode": "lightweight", "max_retries": 0},
        "artifact_contract": {
            "intermediate_artifacts": [
                "artifacts/result.json", "artifacts/code_pipeline.py",
            ],
            "final_artifacts": ["final_report.md"],
            "framework_artifacts": [],
        },
        "artifacts": {
            "code": "artifacts/code_pipeline.py",
            "results": ["artifacts/result.json"],
        },
        "required_artifacts": [
            "artifacts/result.json", "artifacts/code_pipeline.py",
        ],
        "business_validation": {
            "mode": mode,
            "success_rule": "all",
            "validators": validators if validators is not None else [
                {
                    "validator_id": "input_coverage",
                    "required": True,
                    "config": {},
                },
                {
                    "validator_id": "result_integrity",
                    "required": True,
                    "config": {
                        "target_artifact": "artifacts/result.json",
                    },
                },
                {
                    "validator_id": "code_shortcut_scan",
                    "required": True,
                    "config": {
                        "target_artifact": "artifacts/code_pipeline.py",
                    },
                },
            ],
        },
    }


def write_fixture(directory: str, code: str, result=None):
    root = Path(directory)
    (root / "artifacts").mkdir(parents=True)
    (root / "source.txt").write_text("source", encoding="utf-8")
    (root / "normalized_input.json").write_text(
        json.dumps({"files": ["source.txt"]}), encoding="utf-8",
    )
    (root / "artifacts" / "code_pipeline.py").write_text(
        code, encoding="utf-8",
    )
    (root / "artifacts" / "result.json").write_text(
        json.dumps(result or {"status": "success", "value": 2}),
        encoding="utf-8",
    )


class _PassingValidator:
    validator_id = "custom_pass"
    validator_type = "registered"

    def supports(self, task_spec, validator_config):
        return True

    def validate(self, context):
        return BusinessCheckResult(
            check_id=self.validator_id,
            validator_id=self.validator_id,
            status="passed",
            summary="independently verified",
            evidence_refs=["source.txt"],
        )


class BusinessValidationTests(unittest.TestCase):
    def test_final_classification_refreshes_framework_validation_policy(self):
        spec = normalize_to_task_spec({
            "input_type": "directory",
            "files": ["problem.pdf"],
            "text": "",
        })
        self.assertEqual(spec["code_policy"]["mode"], "none")
        self.assertEqual(
            [
                item["validator_id"]
                for item in spec["business_validation"]["validators"]
            ],
            ["input_coverage"],
        )

        finalized = finalize_task_spec(spec, [{
            "path": "problem.pdf",
            "type": "pdf",
            "status": "success",
            "text_preview": "请建立数学优化模型并编写算法求解。",
        }])

        self.assertEqual(finalized["code_policy"]["mode"], "complex")
        self.assertEqual(finalized["business_validation"]["mode"], "required")
        self.assertEqual(
            {
                item["validator_id"]
                for item in finalized["business_validation"]["validators"]
            },
            {
                "input_coverage",
                "result_integrity",
                "code_shortcut_scan",
            },
        )

    def test_explicit_business_validation_policy_is_not_overwritten(self):
        spec = normalize_to_task_spec({
            "input_type": "text",
            "files": [],
            "text": "普通任务",
        })
        spec["business_validation"] = {
            "mode": "optional",
            "success_rule": "all",
            "validators": [{
                "validator_id": "custom",
                "required": True,
                "config": {},
            }],
        }
        finalized = finalize_task_spec(spec, [{
            "path": "problem.pdf",
            "text_preview": "建立数学模型并编写算法求解",
        }])
        self.assertEqual(
            finalized["business_validation"]["validators"][0][
                "validator_id"
            ],
            "custom",
        )

    def test_self_reported_success_is_not_authoritative(self):
        with tempfile.TemporaryDirectory() as directory:
            write_fixture(directory, "value = 2\n")
            result = run_business_validation(
                directory, make_spec(directory), persist=False,
            )
            integrity = next(
                check for check in result.checks
                if check.check_id == "result_integrity"
            )
            marker = next(
                item for item in integrity.checked_items
                if item.get("check") == "self_reported_status_not_authoritative"
            )
            self.assertFalse(marker["authoritative"])

    def test_warning_or_default_result_cannot_pass_business_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            write_fixture(
                directory,
                "value = 2\n",
                result={
                    "status": "warning",
                    "default_values_applied": True,
                    "count": 20,
                },
            )
            result = run_business_validation(
                directory, make_spec(directory), persist=False,
            )
            self.assertEqual(result.status, "failed")
            integrity = next(
                check for check in result.checks
                if check.check_id == "result_integrity"
            )
            failed_types = {
                item["check"] for item in integrity.failed_checks
            }
            self.assertIn("result_not_delivery_ready", failed_types)
            self.assertIn("no_shortcut_result_flag", failed_types)

    def test_missing_input_branch_cannot_return_zero_with_default_result(self):
        code = """
products = []
vehicles = []
if not products or not vehicles:
    output = {
        "status": "warning",
        "default_values_applied": True,
        "products_count": 20,
    }
    return_code = 0
"""
        with tempfile.TemporaryDirectory() as directory:
            write_fixture(directory, code)
            result = run_business_validation(
                directory, make_spec(directory), persist=False,
            )
            self.assertEqual(result.status, "failed")
            scanner = next(
                check for check in result.checks
                if check.check_id == "code_shortcut_scan"
            )
            types = {
                item["type"] for item in scanner.detected_shortcuts
            }
            self.assertIn("declared_default_or_synthetic_result", types)

    def test_normal_control_flow_is_not_misread_as_missing_input_success(self):
        code = """
def solve(items):
    packed = []
    for item in items:
        can_stack = item.get("can_stack", False)
        if not can_stack:
            continue
        packed.append(item)
    if packed:
        return {"status": "success", "items": packed}
    return {"status": "failed"}
"""
        with tempfile.TemporaryDirectory() as directory:
            write_fixture(directory, code)
            result = run_business_validation(
                directory, make_spec(directory), persist=False,
            )
            scanner = next(
                check for check in result.checks
                if check.check_id == "code_shortcut_scan"
            )
            types = {
                item["type"] for item in scanner.detected_shortcuts
            }
            self.assertNotIn("missing_input_returns_success", types)

    def test_nonempty_literal_fallback_data_is_rejected(self):
        code = """
vehicles = parse_vehicles(source)
if not vehicles:
    vehicles = [
        {"type": "default", "capacity": 1000},
    ]
items = parse_items(source)
if not items:
    items = [{"id": "sample-1"}]
"""
        with tempfile.TemporaryDirectory() as directory:
            write_fixture(directory, code)
            result = run_business_validation(
                directory, make_spec(directory), persist=False,
            )
            self.assertEqual(result.status, "failed")
            scanner = next(
                check for check in result.checks
                if check.check_id == "code_shortcut_scan"
            )
            types = {
                item["type"] for item in scanner.detected_shortcuts
            }
            self.assertIn("synthetic_data_on_missing_input", types)

    def test_estimated_defaults_and_partial_threshold_fail(self):
        code = """
import pandas as pd
df = None
if df is None or df.empty:
    df = pd.DataFrame({"value": [1]})
total_value = 100  # Estimated aggregate
capacity = 80
ok = capacity >= total_value * 0.8
"""
        with tempfile.TemporaryDirectory() as directory:
            write_fixture(directory, code)
            result = run_business_validation(
                directory, make_spec(directory), persist=False,
            )
            self.assertEqual(result.status, "failed")
            scanner = next(
                check for check in result.checks
                if check.check_id == "code_shortcut_scan"
            )
            types = {item["type"] for item in scanner.detected_shortcuts}
            self.assertIn("default_data_on_missing_input", types)
            self.assertIn("estimated_fixed_aggregate", types)
            self.assertIn("partial_requirement_threshold", types)

    def test_keyword_in_comment_is_warning_not_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            write_fixture(
                directory,
                "# fallback is deliberately forbidden here\nvalue = 2\n",
            )
            result = run_business_validation(
                directory, make_spec(directory), persist=False,
            )
            scanner = next(
                check for check in result.checks
                if check.check_id == "code_shortcut_scan"
            )
            self.assertEqual(scanner.status, "passed")
            self.assertTrue(scanner.warnings)

    def test_missing_validator_is_unverified(self):
        with tempfile.TemporaryDirectory() as directory:
            write_fixture(directory, "value = 2\n")
            spec = make_spec(directory, validators=[{
                "validator_id": "not_registered",
                "required": True,
                "config": {},
            }])
            result = run_business_validation(
                directory, spec, persist=False,
            )
            self.assertEqual(result.status, "unverified")
            self.assertEqual(result.unresolved_validators, ["not_registered"])

    def test_optional_empty_validation_is_unverified(self):
        with tempfile.TemporaryDirectory() as directory:
            write_fixture(directory, "value = 2\n")
            result = run_business_validation(
                directory,
                make_spec(directory, mode="optional", validators=[]),
                persist=False,
            )
            self.assertEqual(result.status, "unverified")

    def test_registered_positive_validator_can_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            write_fixture(directory, "value = 2\n")
            registry = BusinessValidatorRegistry()
            registry.register(_PassingValidator())
            spec = make_spec(directory, validators=[{
                "validator_id": "custom_pass",
                "required": True,
                "config": {},
            }])
            result = run_business_validation(
                directory, spec, registry=registry, persist=False,
            )
            self.assertEqual(result.status, "passed")

    def test_run_state_and_review_gate_keep_dimensions_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            write_fixture(directory, "value = 2\n")
            spec = make_spec(directory)
            state = RunState(directory)
            state.configure(spec)
            state.start_stage("classify")
            state.start_stage("plan")
            state.start_stage("execute")
            state.record_execution({
                "attempt": 1, "phase": "execution",
                "command": ["python", "artifacts/code_pipeline.py"],
                "exit_code": 0,
                "produced_files": [
                    "artifacts/code_pipeline.py", "artifacts/result.json",
                ],
            })
            state.record_artifact_quality({
                "status": "passed",
                "checked_artifacts": [
                    "artifacts/code_pipeline.py", "artifacts/result.json",
                ],
                "failure_type": "none", "repair_target": "none",
                "resume_stage": "none",
            })
            failed = {
                "status": "failed", "mode": "required",
                "failed_required_checks": ["custom"],
                "unresolved_validators": [], "evidence_refs": [],
                "checks": [], "started_at": "x", "finished_at": "x",
            }
            state.record_business_validation(
                failed, "review/business_validation.json",
            )
            dimensions = state.get("success_dimensions")
            self.assertEqual(dimensions["execution"]["status"], "passed")
            self.assertEqual(dimensions["artifacts"]["status"], "passed")
            self.assertEqual(dimensions["business"]["status"], "failed")
            decision = pre_report_review(
                spec, state.to_dict(),
                state.get("artifact_quality")["current"],
            )
            self.assertEqual(decision.status, "failed")
            self.assertFalse(decision.can_generate_final_report)

    def test_historical_false_success_is_rejected(self):
        run_dir = Path("outputs/runs/20260727_153421")
        if not run_dir.is_dir():
            self.skipTest("历史回归运行不在当前工作区")
        spec = json.loads((run_dir / "task_spec.json").read_text())
        result = run_business_validation(
            run_dir, spec, persist=False,
        )
        self.assertEqual(result.status, "failed")
        scanner = next(
            check for check in result.checks
            if check.check_id == "code_shortcut_scan"
        )
        fatal = {
            item["type"] for item in scanner.detected_shortcuts
            if item["severity"] == "fatal"
        }
        self.assertIn("default_data_on_missing_input", fatal)
        self.assertIn("estimated_fixed_aggregate", fatal)
        self.assertIn("partial_requirement_threshold", fatal)


if __name__ == "__main__":
    unittest.main()
