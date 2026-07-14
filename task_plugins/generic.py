from __future__ import annotations

from pathlib import Path

from orchestration.schemas import ArtifactQualityResult
from task_plugins.base import load_strict_json


class GenericTaskPlugin:
    plugin_id = "generic"

    def prepare_inputs(self, task_spec: dict, run_dir: Path) -> dict:
        """Return a stable, JSON-serializable view for code generation.

        Generic tasks retain the framework-generated previews. Specialized
        plugins can replace this with lossless domain normalization.
        """
        return {"file_previews": task_spec.get("file_previews", [])}

    def code_instructions(self, task_spec: dict) -> str:
        return (
            "业务 JSON 必须是标准 JSON：禁止 NaN、Infinity 和空键；"
            "只生成 ArtifactContract 中声明的 intermediate_artifacts。"
        )

    def validate_intermediate(self, task_spec: dict, run_dir: Path) -> ArtifactQualityResult:
        contract = task_spec.get("artifact_contract", {})
        required = contract.get("intermediate_artifacts")
        if required is None:
            required = [
                path for path in task_spec.get("required_artifacts", [])
                if path != "final_report.md" and path not in {"task_spec.json", "agent_trace.md", "run_state.json", "file_previews.json"}
            ]
        issues = []
        checked = []
        failure_type = "none"
        for relative in required:
            path = run_dir / relative
            checked.append(relative)
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
            checked_artifacts=checked,
            issues=issues,
            failure_type=failure_type,
            repair_target="code" if issues else "none",
            resume_stage="execute" if issues else "none",
        )
