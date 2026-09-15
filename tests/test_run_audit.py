import json
from pathlib import Path
import tempfile
import unittest

from utils.run_audit import audit_run


class RunAuditTests(unittest.TestCase):
    def test_requires_every_final_delivery_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = (
                root / "artifacts" / "tool_results" / "evidence_verification"
                / "evidence_verification.json"
            )
            evidence.parent.mkdir(parents=True)
            evidence.write_text(json.dumps({"passed": True}), encoding="utf-8")
            (root / "final_report.md").write_text("# report\n", encoding="utf-8")
            state = {
                "outcome": "success",
                "finished": True,
                "business_validation": {"status": "passed"},
                "requirement_acceptance": {"status": "passed"},
                "review": {"can_generate_final_report": True, "issues": []},
                "nodes": {"code": {"status": "completed"}},
                "blocking_issues": [],
            }
            (root / "run_state.json").write_text(
                json.dumps(state), encoding="utf-8",
            )
            self.assertTrue(audit_run(root)["passed"])

            state["requirement_acceptance"]["status"] = "failed"
            (root / "run_state.json").write_text(
                json.dumps(state), encoding="utf-8",
            )
            result = audit_run(root)
            self.assertFalse(result["passed"])
            self.assertFalse(result["checks"]["requirement_acceptance_passed"])


if __name__ == "__main__":
    unittest.main()
