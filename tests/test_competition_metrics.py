import json
from pathlib import Path
import tempfile
import unittest

from utils.competition_metrics import build_comparison, render_markdown


class CompetitionMetricsTests(unittest.TestCase):
    def _run(self, parent: Path, run_id: str, task_type: str, passed: bool = True) -> Path:
        root = parent / run_id
        evidence = root / "artifacts" / "tool_results" / "evidence_verification"
        evidence.mkdir(parents=True)
        state = {
            "started_at": "2026-09-07T10:00:00",
            "finished_at": "2026-09-07T10:01:00",
            "outcome": "success" if passed else "failed", "finished": True,
            "business_validation": {"status": "passed"},
            "requirement_acceptance": {"status": "passed"},
            "review": {"can_generate_final_report": True, "issues": []},
            "blocking_issues": [], "nodes": {"deliver": {"status": "completed"}},
            "model_calls": {"count": 2, "prompt_tokens": 10,
                            "completion_tokens": 5, "total_tokens": 15,
                            "duration_seconds": 4, "complete_prompt_compression_ratio": 2},
            "plan": {"node_count": 3, "edge_count": 2, "topology_density": 0.333333},
            "communication": {"context_deliveries": 2, "full_history_broadcasts": 0},
            "execution": {"attempts": 1},
            "recovery": {"executor_switches": 0, "replans": 0,
                         "business_repairs": {"rounds_used": 1}},
            "horizon": {"epoch": 2, "logical_step_count": 4},
        }
        (root / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
        (root / "task_spec.json").write_text(json.dumps({
            "task_name": run_id, "task_type": task_type,
        }), encoding="utf-8")
        (root / "final_report.md").write_text("# report", encoding="utf-8")
        (evidence / "evidence_verification.json").write_text(
            json.dumps({"passed": True}), encoding="utf-8",
        )
        return root

    def test_two_audited_domains_render_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._run(root, "math", "math_modeling")
            second = self._run(root, "expense", "document_analysis")
            comparison = build_comparison([first, second])
        self.assertTrue(comparison["cross_domain"])
        self.assertEqual(comparison["success_rate"], 1.0)
        self.assertEqual(comparison["totals"]["total_tokens"], 30)
        self.assertIn("历史失败运行不会进入本表", render_markdown(comparison))

    def test_failed_run_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._run(root, "math", "math_modeling")
            failed = self._run(root, "failed", "document_analysis", passed=False)
            with self.assertRaisesRegex(ValueError, "refusing to publish failed"):
                build_comparison([first, failed])


if __name__ == "__main__":
    unittest.main()
