"""Typed contracts for independent business-result validation."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


ValidationStatus = Literal[
    "passed", "failed", "unverified", "error", "not_applicable",
]

AcceptanceStatus = Literal["passed", "failed", "unverified"]


class AcceptanceCriterionContract(BaseModel):
    """One frozen, framework-evaluated requirement obligation."""

    criterion_id: str
    requirement_id: str
    statement: str
    mandatory: bool = True
    method: str
    source_method: str
    target: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    severity: Literal["blocking", "warning"] = "blocking"
    producer_node_id: str | None = None
    phase: Literal["business", "delivery"] = "business"
    compilation_status: Literal["ready", "unsupported", "unroutable"] = "ready"
    compilation_issue: str = ""


class AcceptanceContract(BaseModel):
    contract_id: str
    requirement_contract_version: int = 1
    criteria: list[AcceptanceCriterionContract] = Field(default_factory=list)
    supported_methods: list[str] = Field(default_factory=list)


class RequirementLedgerEntry(BaseModel):
    criterion_id: str
    requirement_id: str
    statement: str
    mandatory: bool
    severity: Literal["blocking", "warning"]
    method: str
    target: str = ""
    producer_node_id: str | None = None
    status: AcceptanceStatus
    summary: str
    evidence_refs: list[str] = Field(default_factory=list)
    evidence_hashes: dict[str, str] = Field(default_factory=dict)
    observed: Any = None
    expected: Any = None


class RequirementLedger(BaseModel):
    status: AcceptanceStatus
    phase: Literal["business", "delivery", "all"] = "business"
    entries: list[RequirementLedgerEntry] = Field(default_factory=list)
    mandatory_requirements_passed: bool = False
    failed_blocking_criteria: list[str] = Field(default_factory=list)
    unverified_blocking_criteria: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    started_at: str
    finished_at: str


class BusinessValidationContext(BaseModel):
    run_dir: str
    task_spec: dict[str, Any]
    normalized_input_path: str = "normalized_input.json"
    execution_result: dict[str, Any] | None = None
    artifact_paths: list[str] = Field(default_factory=list)
    validator_config: dict[str, Any] = Field(default_factory=dict)


class BusinessCheckResult(BaseModel):
    check_id: str
    validator_id: str
    status: ValidationStatus
    summary: str
    checked_items: list[dict[str, Any]] = Field(default_factory=list)
    failed_checks: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    detected_shortcuts: list[dict[str, Any]] = Field(default_factory=list)
    recomputed_metrics: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    duration_seconds: float = Field(default=0, ge=0)


class BusinessValidationResult(BaseModel):
    status: ValidationStatus
    mode: Literal["required", "optional", "disabled"]
    checks: list[BusinessCheckResult] = Field(default_factory=list)
    failed_required_checks: list[str] = Field(default_factory=list)
    unresolved_validators: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    requirement_ledger: RequirementLedger | None = None
    started_at: str
    finished_at: str
