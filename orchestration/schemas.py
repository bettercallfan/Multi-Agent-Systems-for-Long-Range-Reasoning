"""Typed contracts exchanged between the framework and language-model agents."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError


class PlanStep(BaseModel):
    id: str
    description: str
    depends_on: list[str] = Field(default_factory=list)
    capability: Literal["analysis", "research", "reasoning", "code", "review", "report"]


class PlanResult(BaseModel):
    task_summary: str
    execution_mode: Literal["fixed_pipeline"] = "fixed_pipeline"
    steps: list[PlanStep]
    requires_code: bool = False
    use_research: bool = False
    use_reasoning: bool = False
    deliverables: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class CodeGenerationResult(BaseModel):
    language: Literal["python"] = "python"
    code: str
    entrypoint: str = "artifacts/code_pipeline.py"
    expected_outputs: list[str] = Field(default_factory=list)
    notes: str = ""


class ExecutionResult(BaseModel):
    attempt: int
    phase: Literal["generation_validation", "execution"] = "execution"
    command: list[str]
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    produced_files: list[str] = Field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


class ErrorDecision(BaseModel):
    error_type: Literal["data", "code", "reasoning", "planning", "report", "unknown"] = "unknown"
    retry_recommended: bool = False
    retry_count_remaining: int = 0
    fallback_recommended: bool = False
    repair_instruction: str = ""


class ReviewDecision(BaseModel):
    status: Literal["passed", "partial", "failed"]
    can_generate_final_report: bool
    issues: list[str] = Field(default_factory=list)
    required_artifacts_checked: list[str] = Field(default_factory=list)


class SemanticReviewResult(BaseModel):
    """Advisory semantic assessment; it has no workflow transition authority."""

    blocking_issues: list[str] = Field(default_factory=list)
    advisory_issues: list[str] = Field(default_factory=list)
    evidence_references: list[str] = Field(default_factory=list)
    repair_recommended: bool = False
    repair_target: Literal["code", "analysis", "input", "human", "none"] = "none"


class ArtifactQualityResult(BaseModel):
    status: Literal["passed", "failed"]
    checked_artifacts: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    failure_type: Literal["artifact_missing", "artifact_json_invalid", "artifact_schema_failure", "none"] = "none"
    repair_target: Literal["code", "input", "human", "none"] = "none"
    resume_stage: Literal["execute", "prepare", "review", "none"] = "none"


class ArtifactContract(BaseModel):
    intermediate_artifacts: list[str] = Field(default_factory=list)
    final_artifacts: list[str] = Field(default_factory=list)
    framework_artifacts: list[str] = Field(default_factory=list)

    @property
    def all_required(self) -> list[str]:
        return list(dict.fromkeys(
            self.intermediate_artifacts + self.final_artifacts + self.framework_artifacts
        ))


def _json_candidates(text: str) -> list[str]:
    candidates = []
    for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE):
        candidates.append(match.group(1))

    start = text.find("{")
    if start >= 0:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:index + 1])
                    break
    return candidates


def parse_model(model_type: type[BaseModel], value: Any) -> BaseModel:
    """Validate a structured message, dict, or JSON text as ``model_type``."""
    if isinstance(value, model_type):
        return value
    if isinstance(value, BaseModel):
        return model_type.model_validate(value.model_dump())
    if isinstance(value, dict):
        return model_type.model_validate(value)
    if not isinstance(value, str):
        raise ValueError(f"无法解析 {model_type.__name__}: 不支持的类型 {type(value).__name__}")

    errors = []
    for candidate in _json_candidates(value):
        try:
            return model_type.model_validate(json.loads(candidate))
        except (json.JSONDecodeError, ValidationError) as exc:
            errors.append(str(exc))
    detail = errors[-1] if errors else "输出中没有 JSON 对象"
    raise ValueError(f"无法解析 {model_type.__name__}: {detail}")
