import unittest
import json
import tempfile
from pathlib import Path

from utils.dashboard_server import (
    RUN_DIR_PATTERN, _artifact_preview, _demo_payload, _detail, _runtime_preflight, _summary,
)


class DashboardServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run_dir = Path(self.temp.name) / "20260907_182245"
        self.run_dir.mkdir()
        (self.run_dir / "run_state.json").write_text(json.dumps({
            "outcome": "failed", "finished": True,
            "nodes": {"example": {"status": "failed", "attempts": 1}},
        }))
        (self.run_dir / "task_graph.json").write_text(json.dumps({
            "version": 1, "nodes": [{"node_id": "example", "capability": "analysis", "dependencies": []}],
        }))
        (self.run_dir / "task_spec.json").write_text('{"task_type":"test_fixture"}')

    def test_summary_is_read_only_and_exposes_audit_metrics(self):
        summary = _summary(self.run_dir)
        self.assertEqual(summary["run_id"], "20260907_182245")
        self.assertIn("audit_passed", summary)
        self.assertIn("status_counts", summary)
        self.assertIn("total_tokens", summary)

    def test_detail_contains_nodes_gates_and_artifacts(self):
        detail = _detail(self.run_dir)
        self.assertEqual(detail["summary"]["run_id"], "20260907_182245")
        self.assertTrue(detail["nodes"])
        self.assertIn("checks", detail["audit"])
        self.assertIn("produced", detail["artifacts"])

    def test_demo_payload_uses_three_official_audited_runs(self):
        root = Path(__file__).parents[1]
        payload = _demo_payload(root, root / "outputs" / "runs")
        self.assertEqual(len(payload["official_runs"]), 3)
        self.assertTrue(all(item["audit_passed"] for item in payload["official_runs"]))
        self.assertEqual(payload["long_horizon"]["completed_steps"], 1000)
        self.assertEqual(
            payload["business_long_horizon"]["validation"]["completed_steps"], 1000
        )
        self.assertEqual(
            payload["business_long_horizon"]["validation"]["live_reasoning_calls"], 10
        )
        self.assertEqual(
            payload["mathorcup_long_horizon"]["search_validation"]["completed_steps"], 1000
        )
        self.assertEqual(payload["recovery"]["final_outcome"], "success")

    def test_artifact_preview_is_bounded_to_run_directory(self):
        preview = _artifact_preview(self.run_dir, "run_state.json")
        self.assertEqual(preview["path"], "run_state.json")
        with self.assertRaisesRegex(ValueError, "invalid artifact path"):
            _artifact_preview(self.run_dir, "../run_state.json")

    def test_runtime_preflight_does_not_expose_secret_values(self):
        root = Path(__file__).parents[1]
        payload = _runtime_preflight(root, True, 0)
        self.assertIn("model_api_key", payload["checks"])
        self.assertNotIn("MODEL_API_KEY", payload)
        self.assertEqual(len(payload["scenarios"]), 7)
        recovery = next(
            item for item in payload["scenarios"]
            if item["id"] == "recovery_demo"
        )
        self.assertTrue(recovery["available"])
        self.assertTrue(all(item["available"] for item in payload["scenarios"]))

    def test_long_run_directory_is_discovered_before_completion(self):
        match = RUN_DIR_PATTERN.search("运行目录: outputs/runs/20260909_123456")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "outputs/runs/20260909_123456")


if __name__ == "__main__":
    unittest.main()
