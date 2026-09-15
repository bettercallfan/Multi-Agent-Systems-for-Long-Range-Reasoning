import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from orchestration.task.input_loader import load_input
from orchestration.task.task_normalizer import finalize_task_spec, normalize_to_task_spec
from orchestration.validation.models import BusinessValidationContext
from task_plugins.urban_multimodal.solver import build_fallback_code
from task_plugins.urban_multimodal.validator import UrbanMultimodalValidator
from utils.file_reader import build_file_previews


class UrbanMultimodalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = Path(__file__).parents[1] / "城市多模态数据集"

    def _spec(self):
        raw = load_input(str(self.dataset))
        spec = normalize_to_task_spec(raw)
        previews = build_file_previews(spec["input"]["files"])
        spec = finalize_task_spec(spec, previews)
        return spec, previews

    def test_large_json_collection_preview_is_bounded_and_contract_is_declared(self):
        spec, previews = self._spec()
        collection = next(item for item in previews if item["type"] == "json_collection")
        self.assertEqual(collection["file_count"], 511)
        self.assertLessEqual(len(previews), 5)
        self.assertEqual(spec["code_policy"]["mode"], "complex")
        self.assertEqual(
            spec["domain_validator_plugins"][0]["module"],
            "task_plugins.urban_multimodal.validator",
        )
        self.assertIn(
            "artifacts/urban_validation_log.json",
            spec["artifact_contract"]["intermediate_artifacts"],
        )

    def test_fallback_reads_all_modalities_and_passes_independent_validation(self):
        spec, _ = self._spec()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs"
            artifacts = root / "artifacts"
            inputs.mkdir()
            artifacts.mkdir()
            for source in self.dataset.rglob("*"):
                if source.is_file():
                    (inputs / source.name).symlink_to(source)
            spec["run_dir"] = str(root)
            spec["input"]["files"] = [str(path) for path in inputs.iterdir()]
            code = build_fallback_code(spec)
            self.assertLessEqual(len(code.splitlines()), spec["code_policy"]["max_lines"])
            (artifacts / "code_pipeline.py").write_text(code, encoding="utf-8")
            completed = subprocess.run(
                ["python", "artifacts/code_pipeline.py"], cwd=root,
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(
                (artifacts / "urban_multimodal_result.json").read_text(encoding="utf-8")
            )
            profiles = result["modality_profiles"]
            self.assertEqual(profiles["remote_sensing"]["record_count"], 511)
            self.assertEqual(profiles["law_articles"]["record_count"], 80000)
            self.assertEqual(profiles["phone_network"]["record_count"], 80000)
            self.assertEqual(profiles["network_traffic"]["packet_count"], 500000)
            self.assertFalse(result["cross_modal_assessment"]["entity_level_linkage_performed"])
            (root / "normalized_input.json").write_text(
                json.dumps({"input": {"files": [path.name for path in inputs.iterdir()]}}),
                encoding="utf-8",
            )
            check = UrbanMultimodalValidator().validate(BusinessValidationContext(
                run_dir=str(root), task_spec=spec,
                validator_config={"target_artifact": "artifacts/urban_multimodal_result.json"},
            ))
            self.assertEqual(check.status, "passed", check.failed_checks)
            audit = json.loads(
                (artifacts / "urban_validation_log.json").read_text(encoding="utf-8")
            )
            self.assertTrue(audit["passed"])
            self.assertEqual(audit["recomputed"], profiles)


if __name__ == "__main__":
    unittest.main()
