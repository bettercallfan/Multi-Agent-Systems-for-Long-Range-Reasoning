"""CoALA-inspired runtime-memory contracts.

The framework, rather than an LLM, owns these records.  The four memory kinds
follow CoALA's working/episodic/semantic/procedural taxonomy; artifact memory
is an engineering extension used to bind claims to immutable run files.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


MemoryKind = Literal["working", "episodic", "semantic", "procedural", "artifact"]
MemoryTier = Literal["core", "recall", "archival"]


class MemoryRecord(BaseModel):
    memory_id: str = ""
    run_id: str
    kind: MemoryKind
    tier: MemoryTier
    summary: str
    content: dict[str, Any] = Field(default_factory=dict)
    node_id: str | None = None
    graph_version: int | None = None
    capabilities: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    importance: float = Field(default=0.5, ge=0, le=1)
    confidence: float = Field(default=1.0, ge=0, le=1)
    verified: bool = False
    valid: bool = True
    sequence: int = Field(default=0, ge=0)
    created_at: str = Field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )

    @field_validator("capabilities", "tags", "evidence_refs", "artifact_refs")
    @classmethod
    def _deduplicate(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))

    def with_identity(self) -> "MemoryRecord":
        if self.memory_id:
            return self
        identity = self.model_dump(mode="json", exclude={"memory_id", "created_at"})
        digest = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:20]
        return self.model_copy(update={"memory_id": f"mem_{digest}"})


class RetrievedMemory(BaseModel):
    memory_id: str
    kind: MemoryKind
    tier: MemoryTier
    summary: str
    score: float = Field(ge=0)
    evidence_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    verified: bool = False
    score_components: dict[str, float] = Field(default_factory=dict)


class ContextCapsule(BaseModel):
    """MemGPT-style bounded main-context block for one DAG node."""

    schema_version: str = "1.0"
    global_goal: str
    critical_constraints: list[str] = Field(default_factory=list)
    current_node: dict[str, Any]
    graph_position: dict[str, Any]
    relevant_memories: list[RetrievedMemory] = Field(default_factory=list)
    unresolved_issues: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    compressed_memory_text: str = ""
    metrics: dict[str, Any] = Field(default_factory=dict)

