"""Contract tests for the MathorCup 1000-candidate demonstration."""

import json
import tempfile
import unittest
from pathlib import Path

from orchestration.validation.mathorcup_validator import MathorCupPackingValidator
from orchestration.validation.models import BusinessValidationContext
from task_plugins.mathorcup_d.long_horizon import (
    CandidateLedger,
    TOTAL_CANDIDATES,
    _configuration,
    evaluate_candidate,
    load_problem,
    run,
)


def _normalized_fixture() -> dict:
    rows = [
        ["货物编号", "类型", "长×宽×高", "单重", "数量"],
        ["G1", "标准件", "60×40×30", "12", "80"],
        ["G2", "标准件", "50×35×25", "8", "100"],
        ["G3", "易碎件", "70×50×40", "15", "30"],
        ["G4", "定向件", "80×60×50", "25", "40"],
        ["G5", "定向件", "40×40×60", "18", "50"],
    ]
    content = (
        "车型 1 内尺寸：长 420cm 宽 210cm 高 220cm 额定载重：6000kg 单次运输成本：450 元/趟\n"
        "车型 2 内尺寸：长 680cm 宽 245cm 高 250cm 额定载重：10000kg 单次运输成本：700 元/趟"
    )
    return {"structured_sources": [{
        "name": "attachment1.docx", "content": content, "tables": [{"rows": rows}]
    }]}


class MathorCupLongHorizonTests(unittest.TestCase):
    def test_candidate_space_is_deterministic_and_varied(self):
        cargo, vehicles = load_problem(_normalized_fixture())
        summaries = [evaluate_candidate(index, cargo, vehicles)[0] for index in range(1, 31)]
        self.assertTrue(all(item["feasible"] for item in summaries))
        self.assertEqual({item["mode"] for item in summaries}, {"v1_only", "v2_only", "mixed"})
        self.assertEqual(len({_configuration(index)["seed"] for index in range(1, 1001)}), 1000)
        self.assertGreater(len({item["placement_digest"] for item in summaries}), 10)

    def test_tampered_resume_checkpoint_is_rejected(self):
        cargo, vehicles = load_problem(_normalized_fixture())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = CandidateLedger(root)
            for index in range(1, 26):
                ledger.append(evaluate_candidate(index, cargo, vehicles)[0])
            checkpoint = root / "checkpoints" / "epoch-0001.json"
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            payload["goal_digest"] = "tampered"
            checkpoint.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "integrity mismatch"):
                CandidateLedger.resume_at(root, 25)

    def test_full_search_passes_long_horizon_and_domain_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normalized = _normalized_fixture()
            (root / "normalized_input.json").write_text(
                json.dumps(normalized, ensure_ascii=False), encoding="utf-8"
            )
            result = run(normalized, root)
            self.assertEqual(result["search_validation"]["completed_steps"], TOTAL_CANDIDATES)
            self.assertEqual(result["search_validation"]["duplicate_executions"], 0)
            self.assertTrue(result["search_validation"]["resume_checkpoint_valid"])
            check = MathorCupPackingValidator().validate(BusinessValidationContext(
                run_dir=directory,
                task_spec={"task_type": "math_modeling"},
                validator_config={"target_artifact": "artifacts/result.json"},
            ))
            self.assertEqual(check.status, "passed", check.failed_checks[:3])


if __name__ == "__main__":
    unittest.main()
