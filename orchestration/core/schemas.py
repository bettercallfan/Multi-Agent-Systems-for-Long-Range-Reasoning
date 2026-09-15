"""Typed contracts exchanged between the framework and language-model agents."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator


class NodeAnalysisResult(BaseModel):
    """Low-entropy, evidence-bound output shared by analysis executors."""

    summary: str = Field(min_length=1, max_length=4_000)
    findings: list[str] = Field(min_length=1, max_length=32)
    evidence_refs: list[str] = Field(min_length=1, max_length=64)
    risks: list[str] = Field(default_factory=list, max_length=32)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class TaskUnderstandingResult(BaseModel):
    """Structured semantic briefing produced before TaskGraph planning."""

    summary: str = Field(min_length=1, max_length=2_000)
    goals: list[str] = Field(min_length=1, max_length=16)
    constraints: list[str] = Field(default_factory=list, max_length=32)
    ambiguities: list[str] = Field(default_factory=list, max_length=32)
    risk_flags: list[str] = Field(default_factory=list, max_length=32)
    recommended_capabilities: list[str] = Field(default_factory=list, max_length=32)
    requirements: list["RequirementItem"] = Field(default_factory=list, max_length=128)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class RequirementSourceRef(BaseModel):
    """Stable pointer back to the user-provided evidence."""

    artifact: str = Field(min_length=1, max_length=500)
    locator: str = Field(default="", max_length=500)
    excerpt: str = Field(default="", max_length=2_000)


class RequirementSourceCandidate(BaseModel):
    """Framework-located source span that may contain an obligation."""

    candidate_id: str = Field(min_length=1, max_length=120)
    artifact: str = Field(min_length=1, max_length=500)
    locator: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=2_000)


class RequirementAcceptance(BaseModel):
    """A machine- or reviewer-checkable acceptance obligation."""

    criterion_id: str = Field(min_length=1, max_length=100)
    method: str = Field(min_length=1, max_length=100)
    target: str = Field(default="", max_length=500)
    condition: str = Field(min_length=1, max_length=2_000)
    # Machine-readable operands for the finite acceptance vocabulary.  The
    # human-readable condition is retained for audit and model prompts, but it
    # is never evaluated as Python code or trusted as a control-flow signal.
    params: dict[str, Any] = Field(default_factory=dict)
    severity: Literal["blocking", "warning"] = "blocking"

    @field_validator("method", mode="before")
    @classmethod
    def normalize_acceptance_method(cls, value):
        text = str(value or "").strip().lower()
        # Common provider synonym for the framework's finite executable
        # vocabulary.  This changes only the wire label, not the check.
        return {
            "file_exists": "artifact_exists",
            "exists": "artifact_exists",
        }.get(text, text)

    @field_validator("severity", mode="before")
    @classmethod
    def normalize_severity(cls, value):
        text = str(value or "blocking").strip().lower()
        if text in {
            "blocking", "critical", "high", "major", "fatal", "required", "must",
        }:
            return "blocking"
        if text in {"warning", "advisory", "low", "medium", "info", "optional"}:
            return "warning"
        # Provider-specific non-blocking labels (minor, informational,
        # info_only, ...) must not invalidate the entire semantic brief.
        return "warning"


class RequirementItem(BaseModel):
    """One atomic, traceable requirement in the frozen task contract."""

    requirement_id: str = Field(min_length=1, max_length=120)
    parent_id: str | None = None
    # Execution dependency between atomic requirements. This differs from
    # parent_id, which only represents semantic decomposition.
    depends_on: list[str] = Field(default_factory=list, max_length=32)
    relation: Literal["and", "or", "optional"] = "and"
    requirement_type: Literal[
        "goal", "scenario", "objective", "input", "constraint",
        "deliverable", "metric", "quality", "validation",
    ] = "goal"
    statement: str = Field(min_length=1, max_length=2_000)
    mandatory: bool = True
    owner: Literal["planner", "compiler", "outer_workflow"] = "planner"
    source_refs: list[RequirementSourceRef] = Field(default_factory=list, max_length=16)
    expected_outputs: list[str] = Field(default_factory=list, max_length=32)
    acceptance_criteria: list[RequirementAcceptance] = Field(default_factory=list, max_length=32)
    status: Literal["explicit", "derived", "ambiguous"] = "explicit"

    @field_validator("depends_on")
    @classmethod
    def normalize_requirement_dependencies(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(
            str(value).strip() for value in values if str(value).strip()
        ))

    @field_validator("relation", mode="before")
    @classmethod
    def normalize_requirement_relation(cls, value):
        """Normalize provider labels without weakening requirement closure.

        ``parent``/``group`` describe hierarchy, which is already carried by
        parent_id.  Treating such groups as AND is the conservative mapping:
        all mandatory children still have to close.
        """
        text = str(value or "and").strip().lower()
        if text in {"or", "any", "either", "one_of"}:
            return "or"
        if text in {"optional", "option"}:
            return "optional"
        return "and"

    @field_validator("status", mode="before")
    @classmethod
    def normalize_requirement_status(cls, value):
        """Keep provenance status separate from hierarchy terminology."""

        text = str(value or "explicit").strip().lower()
        if text in {"explicit", "derived", "ambiguous"}:
            return text
        if text in {"compound", "parent", "group", "atomic", "primitive"}:
            return "explicit"
        # Unknown provider provenance is retained conservatively rather than
        # asserted to be explicit source truth.
        return "ambiguous"

    @field_validator("requirement_type", mode="before")
    @classmethod
    def normalize_requirement_type(cls, value):
        text = str(value or "goal").strip().lower()
        aliases = {
            "delivery": "deliverable",
            "output": "deliverable",
            "artifact": "deliverable",
            "compliance": "constraint",
            "rule": "constraint",
            "verification": "validation",
            "acceptance": "validation",
            "analysis": "objective",
            "report": "deliverable",
            # Some model providers place the provenance status in this field.
            # Treat it as a non-business quality/tracing item instead of
            # rejecting the complete RequirementSet.
            "derived": "quality",
            "kpi": "metric",
        }
        # Keep the wire protocol finite, but tolerate provider-specific labels
        # by retaining the requirement as a generic quality item.
        return aliases.get(text, text if text in {
            "goal", "scenario", "objective", "input", "constraint",
            "deliverable", "metric", "quality", "validation",
        } else "quality")


class RequirementSet(BaseModel):
    """Versioned requirement graph consumed by planning and final validation."""

    contract_id: str = Field(min_length=1, max_length=120)
    version: int = Field(default=1, ge=1)
    global_goal: str = Field(min_length=1, max_length=4_000)
    source_candidates: list[RequirementSourceCandidate] = Field(
        default_factory=list, max_length=128,
    )
    requirements: list[RequirementItem] = Field(default_factory=list, max_length=256)


class RequirementCoverageResult(BaseModel):
    """Deterministic closure check between requirements and PlanIR nodes."""

    passed: bool = False
    missing_requirement_ids: list[str] = Field(default_factory=list)
    missing_acceptance_ids: list[str] = Field(default_factory=list)
    orphan_requirement_ids: list[str] = Field(default_factory=list)
    invalid_requirement_ids: list[str] = Field(default_factory=list)
    covered_requirement_ids: list[str] = Field(default_factory=list)
    oversized_requirement_ids: list[str] = Field(default_factory=list)
    uncovered_source_candidate_ids: list[str] = Field(default_factory=list)
    revision_instructions: list[str] = Field(default_factory=list)


class ClaimVerificationItem(BaseModel):
    claim_id: str = Field(min_length=1, max_length=64)
    source_node_ids: list[str] = Field(default_factory=list, max_length=16)
    claim: str = Field(min_length=1, max_length=2_000)
    verdict: Literal["supported", "contradicted", "insufficient"]
    evidence_refs: list[str] = Field(default_factory=list, max_length=16)
    rationale: str = Field(default="", max_length=2_000)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class EvidenceVerificationResult(BaseModel):
    status: Literal["passed", "failed"]
    claims: list[ClaimVerificationItem] = Field(min_length=1, max_length=64)
    issues: list[str] = Field(default_factory=list, max_length=64)
    additional_evidence_requests: list[str] = Field(default_factory=list, max_length=32)
    can_continue: bool

    @field_validator("claims", mode="before")
    @classmethod
    def normalize_malformed_claims(cls, value):
        """Keep one malformed model item from discarding the whole review.

        A claim without its atomic fact is not silently trusted.  It is kept
        as auditable evidence, forced to ``insufficient``, and therefore makes
        the framework-level verification fail normally instead of raising a
        schema exception before the verification record can be written.
        """
        if not isinstance(value, list):
            return value
        normalized = []
        for index, item in enumerate(value, start=1):
            if not isinstance(item, dict):
                normalized.append(item)
                continue
            claim = dict(item)
            if not str(claim.get("claim", "")).strip():
                claim_id = str(claim.get("claim_id") or f"claim_{index:03d}")
                fallback = (
                    claim.get("description")
                    or claim.get("statement")
                    or claim.get("rationale")
                    or f"模型未提供原子事实文本（{claim_id}）"
                )
                claim["claim"] = str(fallback)
                claim["verdict"] = "insufficient"
                prefix = "原始结构缺少 claim 字段；已按不充分证据处理。"
                rationale = str(claim.get("rationale", "")).strip()
                claim["rationale"] = (
                    f"{prefix}{rationale}" if rationale else prefix
                )
            normalized.append(claim)
        return normalized

    @field_validator("issues", "additional_evidence_requests", mode="before")
    @classmethod
    def normalize_text_items(cls, value):
        """Keep evidence decisions structured while tolerating object issues.

        Models sometimes return an issue object with ``issue``/``reason``
        fields even though the public contract is a compact string list.  The
        object is serialized losslessly instead of being discarded, so review
        and runtime state remain auditable and parsing cannot block the whole
        graph for a representational difference.
        """
        if value is None:
            return []
        if not isinstance(value, list):
            value = [value]
        normalized = []
        for item in value:
            if isinstance(item, str):
                normalized.append(item)
            elif isinstance(item, dict):
                preferred = item.get("issue") or item.get("reason") or item.get("message")
                normalized.append(str(preferred) if preferred else json.dumps(item, ensure_ascii=False))
            else:
                normalized.append(str(item))
        return normalized


class WorkflowMemorySelection(BaseModel):
    selected_skill_ids: list[str] = Field(default_factory=list, max_length=16)
    planning_guidance: list[str] = Field(default_factory=list, max_length=16)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class WorkflowSkillCandidate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    kind: Literal["successful_workflow", "failure_lesson"]
    applicable_task_types: list[str] = Field(min_length=1, max_length=16)
    required_capabilities: list[str] = Field(default_factory=list, max_length=32)
    guidance: list[str] = Field(min_length=1, max_length=16)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class WorkflowMemoryCuration(BaseModel):
    candidates: list[WorkflowSkillCandidate] = Field(default_factory=list, max_length=8)


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
