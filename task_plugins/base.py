from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from orchestration.schemas import ArtifactQualityResult


def load_strict_json(path: Path):
    """Load RFC-compatible JSON and reject NaN/Infinity constants."""
    def reject_constant(value: str):
        raise ValueError(f"非标准 JSON 数值: {value}")

    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)


class TaskPlugin(Protocol):
    plugin_id: str

    def prepare_inputs(self, task_spec: dict, run_dir: Path) -> dict: ...

    def code_instructions(self, task_spec: dict) -> str: ...

    def validate_intermediate(self, task_spec: dict, run_dir: Path) -> ArtifactQualityResult: ...
