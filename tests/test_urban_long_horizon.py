"""Contract tests for the real-data urban 1000-step demonstration."""

import json
import tempfile
import unittest
from pathlib import Path

from task_plugins.urban_multimodal.long_horizon import (
    GOAL_DIGEST,
    UNIT_COUNTS,
    WorkUnitLedger,
    _partition,
    validate_run,
)


class UrbanLongHorizonTests(unittest.TestCase):
    def test_partition_covers_every_item_once(self):
        items = list(range(511))
        batches = _partition(items, 250)
        self.assertEqual(len(batches), 250)
        self.assertEqual([item for batch in batches for item in batch], items)
        self.assertTrue(all(batch for batch in batches))

    def test_checkpoint_resume_does_not_replay_completed_units(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            ledger = WorkUnitLedger(run_dir)
            for index in range(500):
                ledger.append("test", "TestAgent", [f"record={index}"], {"value": index})
            resumed = WorkUnitLedger.resume_at(run_dir, 500)
            resumed.append("test", "TestAgent", ["record=500"], {"value": 500})
            self.assertEqual(len(resumed.records), 501)
            self.assertEqual(resumed.records[-1]["step"], 501)
            self.assertEqual(len({item["work_unit_id"] for item in resumed.records}), 501)

    def test_tampered_checkpoint_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            ledger = WorkUnitLedger(run_dir)
            for index in range(25):
                ledger.append("test", "TestAgent", [], {"value": index})
            checkpoint = run_dir / "checkpoints/epoch-0001.json"
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            payload["goal_digest"] = "tampered"
            checkpoint.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "integrity mismatch"):
                WorkUnitLedger.resume_at(run_dir, 25)

    def test_validator_recomputes_full_thousand_step_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            ledger = WorkUnitLedger(run_dir)
            modalities = (
                [("remote_sensing", {"record_count": 511 if i == 0 else 0}) for i in range(250)]
                + [("law_articles", {"record_count": 320}) for _ in range(250)]
                + [("phone_network", {"record_count": 400}) for _ in range(200)]
                + [("network_traffic", {"packet_count": 2500}) for _ in range(200)]
                + [("cross_modal_synthesis", {"live_reasoning": {"content": "ok"}} if i % 10 == 9 else {}) for i in range(100)]
            )
            for modality, result in modalities:
                ledger.append(modality, "TestAgent", [], result)
            validation = validate_run(run_dir, require_live_reasoning=True)
            self.assertTrue(validation["passed"], validation["failures"])
            self.assertEqual(validation["completed_steps"], 1000)
            self.assertEqual(validation["modality_work_units"], UNIT_COUNTS)
            self.assertEqual(validation["live_reasoning_calls"], 10)
            self.assertTrue(validation["resume_checkpoint_valid"])
            self.assertEqual(validation["duplicate_executions"], 0)
            self.assertTrue(validation["goal_preserved"])
            self.assertEqual(GOAL_DIGEST, ledger.records[-1]["goal_digest"])


if __name__ == "__main__":
    unittest.main()
