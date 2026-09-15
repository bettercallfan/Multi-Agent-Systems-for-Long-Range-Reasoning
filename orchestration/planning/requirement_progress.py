"""Framework-owned progress ledger and bounded requirement-stage selection."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from orchestration.core.schemas import RequirementItem, RequirementSet
from orchestration.graph.task_graph import NodeStatus, TaskGraph


ProgressStatus = Literal[
    "pending", "planned", "running", "produced", "passed",
    "failed", "unverified", "blocked",
]


def requirement_needs_code(item: RequirementItem) -> bool:
    """Identify implementation deliverables without treating constraints as code.

    A constraint may cite ``code_pipeline.py`` as later verification evidence;
    that does not make definition of the constraint itself a code-writing
    stage.  Concrete planner-owned deliverables are the high-confidence signal.
    """

    outputs = [path.lower() for path in item.expected_outputs]
    code_outputs = any(
        path.endswith((".py", ".js", ".ts", ".java", ".cpp"))
        for path in outputs
    )
    # Constraints commonly cite generated code as verification evidence. They
    # belong to model/specification stages, not to the physical writer stage.
    if item.requirement_type in {"constraint", "input", "metric"}:
        return False
    if code_outputs:
        return True
    if item.requirement_type == "deliverable" and outputs:
        return True
    text = item.statement.lower()
    return any(word in text for word in (
        "代码", "程序", "实现", "执行", "求解结果",
        "code", "program", "implement", "executable",
    ))


class RequirementProgressItem(BaseModel):
    requirement_id: str
    statement: str
    depends_on: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)
    status: ProgressStatus = "pending"
    stage: int | None = None
    producer_node_ids: list[str] = Field(default_factory=list)
    attempts: int = 0
    evidence_refs: list[str] = Field(default_factory=list)


class RequirementProgressLedger(BaseModel):
    contract_id: str
    version: int = 1
    current_stage: int = 0
    items: list[RequirementProgressItem] = Field(default_factory=list)
    stage_history: list[dict] = Field(default_factory=list)
    updated_at: str = Field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )

    def item(self, requirement_id: str) -> RequirementProgressItem:
        for item in self.items:
            if item.requirement_id == requirement_id:
                return item
        raise KeyError(requirement_id)

    def pending_ids(self) -> list[str]:
        return [item.requirement_id for item in self.items if item.status == "pending"]

    def ready_ids(self) -> list[str]:
        completed = {
            item.requirement_id for item in self.items
            if item.status in {"produced", "passed"}
        }
        return [
            item.requirement_id for item in self.items
            if item.status == "pending"
            and set(item.depends_on).issubset(completed)
        ]

    def mark_planned(
        self,
        requirement_ids: list[str],
        stage: int,
        producer_mapping: dict[str, list[str]],
    ) -> None:
        self.current_stage = stage
        for requirement_id in requirement_ids:
            item = self.item(requirement_id)
            item.status = "planned"
            item.stage = stage
            item.attempts += 1
            item.producer_node_ids = list(dict.fromkeys(
                producer_mapping.get(requirement_id, [])
            ))
        self.stage_history.append({
            "stage": stage,
            "requirement_ids": list(requirement_ids),
            "producer_mapping": producer_mapping,
            "planned_at": datetime.now().isoformat(timespec="seconds"),
        })
        self.updated_at = datetime.now().isoformat(timespec="seconds")

    def mark_stage_blocked(
        self,
        requirement_ids: list[str],
        stage: int,
        reason: str,
    ) -> None:
        self.current_stage = stage
        for requirement_id in requirement_ids:
            item = self.item(requirement_id)
            item.status = "blocked"
            item.stage = stage
            item.attempts += 1
        self.stage_history.append({
            "stage": stage,
            "requirement_ids": list(requirement_ids),
            "status": "planning_blocked",
            "reason": reason,
            "planned_at": datetime.now().isoformat(timespec="seconds"),
        })
        self.updated_at = datetime.now().isoformat(timespec="seconds")

    def refresh_from_graph(self, graph: TaskGraph) -> None:
        by_id = {node.node_id: node for node in graph.nodes}
        for item in self.items:
            if not item.producer_node_ids:
                continue
            statuses = [
                by_id[node_id].status
                for node_id in item.producer_node_ids if node_id in by_id
            ]
            if not statuses:
                continue
            if any(status == NodeStatus.FAILED for status in statuses):
                item.status = "failed"
            elif any(status == NodeStatus.BLOCKED for status in statuses):
                item.status = "blocked"
            elif all(status == NodeStatus.COMPLETED for status in statuses):
                item.status = "produced"
            elif any(status == NodeStatus.RUNNING for status in statuses):
                item.status = "running"
        self.updated_at = datetime.now().isoformat(timespec="seconds")

    def apply_acceptance_ledger(self, requirement_ledger: object) -> None:
        """Promote produced requirements using independent acceptance results."""

        raw_entries = getattr(requirement_ledger, "entries", None)
        if raw_entries is None and isinstance(requirement_ledger, dict):
            raw_entries = requirement_ledger.get("entries", [])
        grouped: dict[str, list[str]] = {}
        for entry in raw_entries or []:
            if hasattr(entry, "requirement_id"):
                requirement_id = str(entry.requirement_id)
                status = str(entry.status)
                severity = str(entry.severity)
                mandatory = bool(entry.mandatory)
            else:
                requirement_id = str(entry.get("requirement_id", ""))
                status = str(entry.get("status", "unverified"))
                severity = str(entry.get("severity", "blocking"))
                mandatory = bool(entry.get("mandatory", True))
            if requirement_id and mandatory and severity == "blocking":
                grouped.setdefault(requirement_id, []).append(status)
        for requirement_id, statuses in grouped.items():
            try:
                item = self.item(requirement_id)
            except KeyError:
                continue
            # Acceptance over the complete global contract also contains
            # requirements that have not been planned yet.  Keep those
            # pending; otherwise a failed early stage makes unopened work look
            # as though it had executed and failed.
            if not item.producer_node_ids:
                continue
            if "failed" in statuses:
                item.status = "failed"
            elif "unverified" in statuses:
                item.status = "unverified"
            elif statuses and all(status == "passed" for status in statuses):
                item.status = "passed"
        self.updated_at = datetime.now().isoformat(timespec="seconds")


def build_requirement_progress_ledger(
    requirement_set: RequirementSet,
) -> RequirementProgressLedger:
    parent_ids = {
        item.parent_id for item in requirement_set.requirements if item.parent_id
    }
    actionable = [
        item for item in requirement_set.requirements
        if item.owner == "planner" and item.mandatory
        and (
            item.requirement_id not in parent_ids
            or any(
                criterion.severity == "blocking"
                for criterion in item.acceptance_criteria
            )
        )
    ]
    actionable_ids = {item.requirement_id for item in actionable}
    by_id = {
        item.requirement_id: item for item in requirement_set.requirements
    }
    children: dict[str, list[str]] = {
        item.requirement_id: [] for item in requirement_set.requirements
    }
    for item in requirement_set.requirements:
        if item.parent_id in children:
            children[item.parent_id].append(item.requirement_id)

    def is_ancestor(candidate: str, requirement_id: str) -> bool:
        current = by_id.get(requirement_id)
        while current and current.parent_id:
            if current.parent_id == candidate:
                return True
            current = by_id.get(current.parent_id)
        return False

    def actionable_descendants(requirement_id: str) -> list[str]:
        found: list[str] = []
        stack = list(children.get(requirement_id, []))
        while stack:
            current = stack.pop(0)
            if current in actionable_ids:
                found.append(current)
            else:
                stack.extend(children.get(current, []))
        return found

    def resolved_dependencies(item: RequirementItem) -> list[str]:
        resolved: list[str] = []
        for dependency in item.depends_on:
            if dependency in actionable_ids and dependency != item.requirement_id:
                resolved.append(dependency)
            elif is_ancestor(dependency, item.requirement_id):
                # A non-actionable semantic parent is not an execution state.
                continue
            else:
                resolved.extend(
                    candidate for candidate in actionable_descendants(dependency)
                    if candidate != item.requirement_id
                )
        return list(dict.fromkeys(resolved))

    return RequirementProgressLedger(
        contract_id=requirement_set.contract_id,
        items=[RequirementProgressItem(
            requirement_id=item.requirement_id,
            statement=item.statement,
            depends_on=resolved_dependencies(item),
            expected_outputs=list(item.expected_outputs),
        ) for item in actionable],
    )


def select_requirement_batch(
    ledger: RequirementProgressLedger,
    requirement_set: RequirementSet,
    *,
    max_requirements: int,
    max_prompt_tokens: int,
) -> list[str]:
    """Choose dependency-ready requirements under communication budgets."""

    by_id: dict[str, RequirementItem] = {
        item.requirement_id: item for item in requirement_set.requirements
    }
    order = {item.requirement_id: index for index, item in enumerate(ledger.items)}

    def is_code(requirement_id: str) -> bool:
        return requirement_needs_code(by_id[requirement_id])

    ready = sorted(
        ledger.ready_ids(), key=lambda requirement_id: (
            is_code(requirement_id), order[requirement_id],
        ),
    )
    selected: list[str] = []
    estimated_tokens = 0
    for requirement_id in ready:
        item = by_id[requirement_id]
        cost = max(32, len(json.dumps(
            item.model_dump(mode="json"), ensure_ascii=False,
        )) // 4)
        # Code requirements that are simultaneously ready form one physical
        # implementation stage.  Splitting them would create multiple writers
        # for the framework-owned code/result artifacts.  The configured
        # batch size remains a soft communication target in this case.
        extending_code_stage = bool(selected) and all(
            is_code(selected_id) for selected_id in selected
        ) and is_code(requirement_id)
        if selected and is_code(requirement_id) and not all(
            is_code(selected_id) for selected_id in selected
        ):
            break
        if selected and not extending_code_stage and (
            len(selected) >= max(1, max_requirements)
            or estimated_tokens + cost > max(256, max_prompt_tokens)
        ):
            break
        selected.append(requirement_id)
        estimated_tokens += cost
    return selected


def scoped_requirement_set(
    requirement_set: RequirementSet,
    requirement_ids: list[str],
) -> RequirementSet:
    selected = set(requirement_ids)
    by_id = {item.requirement_id: item for item in requirement_set.requirements}
    ancestors: set[str] = set()
    for requirement_id in selected:
        current = by_id.get(requirement_id)
        while current and current.parent_id and current.parent_id in by_id:
            ancestors.add(current.parent_id)
            current = by_id[current.parent_id]
    requirements = []
    for item in requirement_set.requirements:
        if item.requirement_id in selected:
            requirements.append(item)
        elif item.requirement_id in ancestors:
            requirements.append(item.model_copy(update={"mandatory": False}))
    return requirement_set.model_copy(update={"requirements": requirements})


def persist_requirement_progress(
    run_dir: str | Path,
    ledger: RequirementProgressLedger,
) -> str:
    root = Path(run_dir)
    path = root / "planning" / "requirement_progress.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ledger.model_dump_json(indent=2), encoding="utf-8")
    return path.relative_to(root).as_posix()
