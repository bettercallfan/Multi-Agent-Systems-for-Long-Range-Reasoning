"""Generic, deterministic validation for artifacts declared by TaskSpec."""

from __future__ import annotations

import json
from pathlib import Path

from orchestration.core.schemas import ArtifactQualityResult


_FRAMEWORK_ARTIFACTS = {
    "final_report.md",
    "task_spec.json",
    "task_graph.json",
    "agent_trace.md",
    "run_state.json",
    "file_previews.json",
    "normalized_input.json",
}


def load_strict_json(path: Path):
    """Load RFC-compatible JSON and reject NaN/Infinity constants."""

    def reject_constant(value: str):
        raise ValueError(f"非标准 JSON 数值: {value}")

    return json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=reject_constant,
    )


def intermediate_artifacts(task_spec: dict) -> list[str]:
    """Return the framework-owned intermediate artifact contract."""
    contract = task_spec.get("artifact_contract", {})
    if "intermediate_artifacts" in contract:
        return list(contract["intermediate_artifacts"])
    return [
        path
        for path in task_spec.get("required_artifacts", [])
        if path not in _FRAMEWORK_ARTIFACTS
    ]


def validate_intermediate_artifacts(
    task_spec: dict,
    run_dir: str | Path,
) -> ArtifactQualityResult:
    """Validate only contracts expressible without domain-specific knowledge.

    The generic framework checks existence and strict JSON syntax. Semantic or
    domain assertions belong in TaskGraph nodes and evidence-based review, not
    in hard-coded task handlers.
    """
    root = Path(run_dir)
    required = intermediate_artifacts(task_spec)
    issues: list[str] = []
    failure_type = "none"

    for relative in required:
        path = root / relative
        if not path.is_file():
            issues.append(f"缺少中间产物：{relative}")
            failure_type = "artifact_missing"
            continue
        if path.suffix.lower() == ".json":
            try:
                load_strict_json(path)
            except (OSError, ValueError) as exc:
                issues.append(f"JSON 产物不符合标准：{relative}（{exc}）")
                failure_type = "artifact_json_invalid"

    return ArtifactQualityResult(
        status="failed" if issues else "passed",
        checked_artifacts=required,
        issues=issues,
        failure_type=failure_type,
        repair_target="code" if issues else "none",
        resume_stage="execute" if issues else "none",
    )
