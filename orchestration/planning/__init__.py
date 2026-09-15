"""Verifier-guided hierarchical planning frontend.

This package only transforms a validated hierarchical plan into the existing
SemanticGraph. It does not own execution, routing, retries, or reporting.
"""

from orchestration.planning.plan_ir import (
    FlattenResult,
    InputRef,
    LogicalOutput,
    PlanIR,
    PlanNode,
    PlanNodeType,
    PlanPatch,
    PlanValidationResult,
    PlanningContract,
    PlanningPolicy,
)
from orchestration.planning.plan_validator import validate_plan_ir
from orchestration.planning.plan_flattener import flatten_plan_ir_to_semantic_graph

__all__ = [
    "FlattenResult", "InputRef", "LogicalOutput", "PlanIR", "PlanNode",
    "PlanNodeType", "PlanPatch", "PlanValidationResult", "PlanningContract",
    "PlanningPolicy", "validate_plan_ir", "flatten_plan_ir_to_semantic_graph",
]
