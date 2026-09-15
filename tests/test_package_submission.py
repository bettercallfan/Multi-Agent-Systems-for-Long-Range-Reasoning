from pathlib import Path
import tempfile
import unittest

from utils.package_submission import _candidate_files, _input_manifest, _secret_hits, validate_submission


class PackageSubmissionTests(unittest.TestCase):
    def test_failed_historical_run_cannot_be_packaged(self):
        # Keep the failed-run gate regression portable in a curated source bundle.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "failed_run"
            run.mkdir()
            (run / "run_state.json").write_text('{"outcome":"failed","finished":true}')
            with self.assertRaisesRegex(ValueError, "未通过完整审计"):
                validate_submission(root, [run])

    def test_secret_scan_rejects_key_patterns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.py"
            path.write_text('MODEL_API_KEY = "' + "sk-" + 'example"', encoding="utf-8")
            self.assertEqual(_secret_hits([path]), [path.as_posix()])

    def test_evidence_inputs_are_replaced_by_hash_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "outputs" / "runs" / "demo"
            (run / "inputs").mkdir(parents=True)
            (run / "artifacts").mkdir()
            (run / "inputs" / "large.bin").write_bytes(b"raw-input")
            (run / "artifacts" / "result.json").write_text("{}", encoding="utf-8")
            files = _candidate_files(root, [run])
            self.assertIn(run / "artifacts" / "result.json", files)
            self.assertNotIn(run / "inputs" / "large.bin", files)
            manifest = _input_manifest(run)
            self.assertFalse(manifest["raw_inputs_embedded"])
            self.assertEqual(manifest["file_count"], 1)
            self.assertEqual(len(manifest["files"][0]["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
