import tempfile
import unittest
from pathlib import Path

from orchestration.core.run_state import RunState
from utils.review_artifacts import final_validation, pre_report_review


class BlockingGateTests(unittest.TestCase):
    def _spec(self, directory: str) -> dict:
        return {
            "run_dir": directory,
            "task_type": "document_analysis",
            "code_policy": {"mode": "none"},
            "business_validation": {"mode": "optional"},
            "artifact_contract": {
                "intermediate_artifacts": [],
                "final_artifacts": [],
                "framework_artifacts": [],
            },
            "required_report_sections": [],
        }

    def test_blocking_issue_forces_review_failure_and_survives_pass_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            state = RunState(directory)
            state.configure(self._spec(directory))
            state.start_stage("classify")
            state.start_stage("plan")
            state.start_stage("execute")
            state.start_stage("review")
            state.record_blocking_issues(
                ["expense_total 与输入明细不一致"],
                source="evidence_verification",
            )
            state.record_review({
                "status": "passed",
                "can_generate_final_report": True,
                "issues": [],
                "required_artifacts_checked": [],
            })
            self.assertEqual(state.get("review")["status"], "failed")
            self.assertFalse(state.allow_report())

            # A later verifier/reviewer pass cannot erase the unresolved issue.
            state.record_review({
                "status": "passed",
                "can_generate_final_report": True,
                "issues": [],
                "required_artifacts_checked": [],
            })
            self.assertEqual(state.get("review")["status"], "failed")
            self.assertEqual(len(state.get_unresolved_blocking_issues()), 1)

    def test_explicit_resolution_requires_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            state = RunState(directory)
            state.record_blocking_issues(["bad result"], source="review_agent")
            issue_id = state.get_unresolved_blocking_issues()[0]["issue_id"]
            state.resolve_blocking_issue(issue_id, ["artifacts/result.json"])
            self.assertEqual(state.get_unresolved_blocking_issues(), [])
            state.record_blocking_issues(["bad result"], source="review_agent")
            self.assertEqual(len(state.get_unresolved_blocking_issues()), 1)

    def test_pre_and_final_review_block_when_ledger_has_issue(self):
        with tempfile.TemporaryDirectory() as directory:
            spec = self._spec(directory)
            state = RunState(directory)
            state.configure(spec)
            state.record_blocking_issues(["unverified claim"], source="evidence")
            pre = pre_report_review(spec, state.to_dict())
            self.assertEqual(pre.status, "failed")
            self.assertFalse(pre.can_generate_final_report)
            final = final_validation(spec, state.to_dict())
            self.assertEqual(final.status, "failed")
            self.assertFalse(final.can_generate_final_report)

    def test_final_validation_requires_closed_requirement_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            spec = self._spec(directory)
            spec["requirement_contract"] = {
                "contract_id": "requirements-test",
                "version": 1,
                "global_goal": "verified delivery",
                "requirements": [],
            }
            state = RunState(directory)
            state.configure(spec)
            state.record_requirement_ledger({
                "status": "unverified",
                "phase": "all",
                "entries": [],
                "mandatory_requirements_passed": False,
                "failed_blocking_criteria": [],
                "unverified_blocking_criteria": ["AC-1"],
                "evidence_refs": [],
                "started_at": "x",
                "finished_at": "x",
            }, "review/requirement_ledger.json")
            decision = final_validation(spec, state.to_dict())
            self.assertEqual(decision.status, "failed")
            self.assertIn(
                "强制需求验收尚未闭合：unverified", decision.issues,
            )


if __name__ == "__main__":
    unittest.main()
