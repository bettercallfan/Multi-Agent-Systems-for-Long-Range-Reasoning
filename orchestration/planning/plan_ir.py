"""Model-facing hierarchical planning intermediate representation."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PlanNodeType(str, Enum):
    COMPOUND = "compound"
    PRIMITIVE = "primitive"


class LogicalOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    output_id: str
    output_type: str = "structured_result"
    description: str
    schema_ref: str | None = None


class InputRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_type: Literal["user_input", "artifact", "node_output"]
    name: str
    producer_node_id: str | None = None
    fields: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def non_empty_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("InputRef.name 不能为空")
        return value


class PlanNode(BaseModel):
    model_config = ConfigDict(extra="ignore")

    node_id: str
    objective: str
    node_type: PlanNodeType
    parent_node_id: str | None = None
    decomposition_depth: int = Field(default=0, ge=0)
    dependencies: list[str] = Field(default_factory=list)
    primary_capability: str
    supporting_capabilities: list[str] = Field(default_factory=list)
    required_inputs: list[InputRef] = Field(default_factory=list)
    expected_output: LogicalOutput | None = None
    output_artifacts: list[str] = Field(default_factory=list)
    acceptance_criteria: list[dict[str, Any]] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)
    acceptance_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("node_id", "objective", "primary_capability")
    @classmethod
    def non_empty_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("PlanNode 必填文本不能为空")
        return value

    @field_validator(
        "dependencies", "supporting_capabilities", "output_artifacts",
        "requirement_ids", "acceptance_refs",
    )
    @classmethod
    def normalize_unique(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class PlanIR(BaseModel):
    model_config = ConfigDict(extra="ignore")

    global_goal: str
    deliverables: list[str] = Field(default_factory=list)
    nodes: list[PlanNode]
    edges: list[tuple[str, str]] = Field(default_factory=list)
    version: int = Field(default=1, ge=1)

    @field_validator("global_goal")
    @classmethod
    def non_empty_goal(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("PlanIR.global_goal 不能为空")
        return value


class PlanPatch(BaseModel):
    """A local refinement response; it is not a replacement full plan."""

    model_config = ConfigDict(extra="ignore")

    target_node_id: str
    replacement_nodes: list[PlanNode] = Field(default_factory=list)
    replacement_edges: list[tuple[str, str]] = Field(default_factory=list)


class PlanningContract(BaseModel):
    model_config = ConfigDict(extra="ignore")

    graph_deliverables: list[str] = Field(default_factory=list)
    postprocess_deliverables: list[str] = Field(default_factory=list)
    framework_artifacts: list[str] = Field(default_factory=list)
    validation_requirements: list[dict[str, Any]] = Field(default_factory=list)
    requirement_contract: dict[str, Any] = Field(default_factory=dict)


class PlanningPolicy(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: Literal["auto", "legacy", "hierarchical"] = "auto"
    hierarchical_validation_enabled: bool = True
    max_revision_rounds: int = Field(default=2, ge=0, le=4)
    max_decomposition_depth: int = Field(default=3, ge=0, le=8)
    max_plan_nodes: int = Field(default=32, ge=1, le=256)
    max_new_nodes_per_refinement: int = Field(default=8, ge=1, le=64)
    max_refinement_prompt_tokens: int = Field(default=4000, ge=500)
    fallback_to_legacy_plan: bool = False
    enable_llm_plan_critic: bool = False
    preserve_hierarchy_metadata: bool = True


class PlanValidationResult(BaseModel):
    passed: bool = False
    oversized_nodes: list[str] = Field(default_factory=list)
    missing_deliverables: list[str] = Field(default_factory=list)
    missing_outputs: list[str] = Field(default_factory=list)
    missing_acceptance_criteria: list[str] = Field(default_factory=list)
    dependency_errors: list[str] = Field(default_factory=list)
    missing_validation_steps: list[str] = Field(default_factory=list)
    invalid_decompositions: list[str] = Field(default_factory=list)
    graph_errors: list[str] = Field(default_factory=list)
    goal_errors: list[str] = Field(default_factory=list)
    problem_node_ids: list[str] = Field(default_factory=list)
    revision_instructions: list[str] = Field(default_factory=list)
    missing_requirement_ids: list[str] = Field(default_factory=list)
    missing_acceptance_ids: list[str] = Field(default_factory=list)
    covered_requirement_ids: list[str] = Field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(len(getattr(self, field)) for field in (
            "oversized_nodes", "missing_deliverables", "missing_outputs",
            "missing_acceptance_criteria", "dependency_errors",
            "missing_validation_steps", "invalid_decompositions",
            "graph_errors", "goal_errors", "missing_requirement_ids",
            "missing_acceptance_ids",
        ))


class FlattenResult(BaseModel):
    semantic_graph: Any
    removed_compound_nodes: list[str] = Field(default_factory=list)
    entry_nodes: dict[str, list[str]] = Field(default_factory=dict)
    exit_nodes: dict[str, list[str]] = Field(default_factory=dict)
    flattened_edges: list[tuple[str, str]] = Field(default_factory=list)
