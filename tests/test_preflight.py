import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from utils.preflight import check


class PreflightTests(unittest.TestCase):
    def test_missing_model_configuration_is_reported_without_exposing_key(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {}, clear=True):
                passed, messages = check(directory, str(Path(directory) / "runs"))
        self.assertFalse(passed)
        self.assertTrue(any("MODEL_API_KEY" in item for item in messages))
        self.assertFalse(any("sk-" in item for item in messages))

    def test_configured_environment_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            input_dir = Path(directory) / "input"
            input_dir.mkdir()
            (input_dir / "task.md").write_text("task", encoding="utf-8")
            with patch.dict(os.environ, {
                "MODEL_API_KEY": "test-key",
                "MODEL_BASE_URL": "https://example.invalid/v1",
                "MODEL_NAME": "test-model",
            }, clear=True):
                passed, messages = check(str(input_dir), str(Path(directory) / "runs"))
        self.assertTrue(passed, messages)


if __name__ == "__main__":
    unittest.main()
