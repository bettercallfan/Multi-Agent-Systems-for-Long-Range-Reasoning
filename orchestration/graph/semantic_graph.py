"""Small, model-facing graph contract.

The planner only describes business work.  Framework-owned execution details
are added later by :mod:`graph_compiler`.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SemanticNode(BaseModel):
    model_config = ConfigDict(extra="ignore")

    node_id: str
    objective: str = ""
    capability: str
    dependencies: list[str] = Field(default_factory=list)
    required_inputs: dict[str, list[str]] = Field(default_factory=dict)
    # Artifact references are declarative inputs/outputs of a business node;
    # the compiler still decides whether they are allowed by TaskSpec.
    input_artifacts: list[str] = Field(default_factory=list)
    output_artifacts: list[str] = Field(default_factory=list)
    expected_output: dict[str, Any] | None = None
    acceptance_criteria: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Compatibility-only carrier for old TaskGraph responses. New planner
    # prompts never request it and the compiler ignores it for code tasks.
    legacy_output_artifacts: list[str] = Field(default_factory=list, exclude=True)

    @field_validator("node_id", "capability")
    @classmethod
    def non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("node_id/capability 不能为空")
        return value

    @field_validator("dependencies")
    @classmethod
    def unique_dependencies(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in values if item.strip()))


class SemanticGraph(BaseModel):
    model_config = ConfigDict(extra="ignore")

    graph_id: str = ""
    version: int = 1
    goal: str
    nodes: list[SemanticNode]


def coerce_semantic_graph(raw: Any) -> SemanticGraph:
    """Accept the new compact format and tolerate the old TaskGraph format.

    This compatibility boundary is intentionally one-way: runtime state and
    artifact contracts from an LLM response are never trusted.
    """
    if isinstance(raw, SemanticGraph):
        return raw
    if isinstance(raw, str):
        candidates = [raw.strip()]
        fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.S | re.I)
        if fenced:
            candidates.insert(0, fenced.group(1).strip())
        parsed = None
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
                break
            except json.JSONDecodeError:
                continue
        if parsed is None:
            raise ValueError("规划结果不是有效 JSON")
        raw = parsed
    if not isinstance(raw, dict):
        raise ValueError("规划结果必须是 JSON 对象")
    nodes = []
    for item in raw.get("nodes", []):
        if not isinstance(item, dict):
            raise ValueError("nodes 中每项必须是对象")
        nodes.append({
            "node_id": item.get("node_id") or item.get("id"),
            "objective": item.get("objective") or item.get("description") or "",
            "capability": item.get("capability"),
            "dependencies": item.get("dependencies", item.get("depends_on", [])),
            "required_inputs": item.get("required_inputs") or item.get("dependency_fields", {}),
            "input_artifacts": item.get("input_artifacts", []),
            "output_artifacts": item.get("output_artifacts", []),
            "expected_output": item.get("expected_output") or item.get("output"),
            "acceptance_criteria": item.get("acceptance_criteria", item.get("success_criteria", [])),
            "metadata": item.get("metadata", {}),
            "legacy_output_artifacts": item.get("output_artifacts", []),
        })
    return SemanticGraph(
        graph_id=str(raw.get("graph_id", "")),
        version=int(raw.get("version", 1)),
        goal=str(raw.get("goal", "")),
        nodes=nodes,
    )
