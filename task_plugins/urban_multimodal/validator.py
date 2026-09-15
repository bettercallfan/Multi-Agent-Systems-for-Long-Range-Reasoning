"""Independent validator for the urban multimodal aggregate profile."""

from __future__ import annotations

import json
from pathlib import Path
import time

from orchestration.validation.models import BusinessCheckResult, BusinessValidationContext
from .analysis import analyze


class UrbanMultimodalValidator:
    validator_id = "urban_multimodal"
    validator_type = "domain"

    def supports(self, task_spec: dict, validator_config: dict) -> bool:
        names = {Path(str(item)).name for item in task_spec.get("input", {}).get("files", [])}
        return {"law_articles_80k.jsonl", "phone_network_sichuan_open_voc_80k_utf8_bom.csv",
                "Shifu.pcap"}.issubset(names)

    def validate(self, context: BusinessValidationContext) -> BusinessCheckResult:
        started = time.monotonic()
        root = Path(context.run_dir)
        target = str(context.validator_config.get(
            "target_artifact", "artifacts/urban_multimodal_result.json"))
        failures = []
        try:
            actual = json.loads((root / target).read_text(encoding="utf-8"))
            audit_ref = "artifacts/urban_validation_log.json"
            audit = json.loads((root / audit_ref).read_text(encoding="utf-8"))
            expected = analyze(root)
        except (OSError, ValueError, TypeError) as exc:
            actual, audit, expected = {}, {}, {}
            failures.append({"check": "parse_and_recompute", "reason": f"{type(exc).__name__}: {exc}"})
        for field in ("source_coverage", "modality_profiles", "cross_modal_assessment",
                      "governance_recommendations", "limitations", "validation"):
            if field not in actual:
                failures.append({"check": "required_field", "field": field})
        if actual.get("source_coverage") != expected.get("source_coverage"):
            failures.append({"check": "source_coverage"})
        if actual.get("modality_profiles") != expected.get("modality_profiles"):
            failures.append({"check": "modality_profile_recompute"})
        assessment = actual.get("cross_modal_assessment") or {}
        validation = actual.get("validation") or {}
        if assessment.get("scenario_count") != 4:
            failures.append({"check": "scenario_count", "expected": 4,
                             "actual": assessment.get("scenario_count")})
        if assessment.get("entity_level_linkage_performed") is not False:
            failures.append({"check": "privacy_entity_linkage"})
        if validation.get("privacy_preserving") is not True:
            failures.append({"check": "privacy_declaration"})
        if validation.get("unsupported_causal_claims_made") is not False:
            failures.append({"check": "unsupported_causality"})
        recomputed = expected.get("modality_profiles", {})
        if audit.get("validator") != self.validator_id:
            failures.append({"check": "validation_log_validator"})
        if audit.get("passed") is not True or audit.get("failures") != []:
            failures.append({"check": "validation_log_status"})
        if audit.get("recomputed") != recomputed:
            failures.append({"check": "validation_log_recomputed"})
        if audit.get("privacy_checks") != {
            "entity_linkage": False, "payload_inspection": False,
        }:
            failures.append({"check": "validation_log_privacy"})
        return BusinessCheckResult(
            check_id=self.validator_id, validator_id=self.validator_id,
            status="failed" if failures else "passed",
            summary=("城市多模态聚合画像确定性校验通过" if not failures
                     else f"城市多模态校验发现 {len(failures)} 项错误"),
            failed_checks=failures, recomputed_metrics=recomputed,
            evidence_refs=[target, audit_ref, context.normalized_input_path],
            duration_seconds=time.monotonic() - started,
        )
