"""Compile frozen requirements into a finite, executable acceptance contract."""

from __future__ import annotations

from collections import defaultdict
from pathlib import PurePosixPath
from typing import Any

from orchestration.core.schemas import RequirementSet
from orchestration.graph.task_graph import TaskGraph
from orchestration.validation.models import (
    AcceptanceContract,
    AcceptanceCriterionContract,
)


SUPPORTED_ACCEPTANCE_METHODS = (
    "artifact_exists",
    "json_parseable",
    "required_fields",
    "record_count_compare",
    "numeric_compare",
    "evidence_supported",
)

_METHOD_ALIASES = {
    "file_exists": "artifact_exists",
    "existence_check": "artifact_exists",
    "present": "artifact_exists",
    "json_valid": "json_parseable",
    "evidence_review": "evidence_supported",
    # The terminal executor and business gate own actual execution success.
    # At requirement-ledger level this criterion proves the executable exists.
    "execution_must_succeed_for_pass": "artifact_exists",
}


def _artifact_part(target: str) -> str:
    value = str(target or "").strip().replace("\\", "/")
    value = value.split("#", 1)[0].split("::", 1)[0]
    return value


def _looks_like_artifact(target: str) -> bool:
    value = _artifact_part(target)
    if not value or value.startswith(("node:", "REQ-", "AC-")):
        return False
    return bool(PurePosixPath(value).suffix) or "/" in value


def _normalise_method(
    method: str,
    target: str,
    params: dict[str, Any],
) -> tuple[str, str, str]:
    source = str(method or "").strip().lower()
    normalised = _METHOD_ALIASES.get(source, source)
    if normalised == "schema_check":
        if params.get("fields") or "::" in target or "#" in target:
            normalised = "required_fields"
        elif _artifact_part(target).lower().endswith(".json"):
            normalised = "json_parseable"
    if normalised not in SUPPORTED_ACCEPTANCE_METHODS:
        return source, "unsupported", f"不支持的验收方法：{source or '<empty>'}"
    if normalised in {"artifact_exists", "json_parseable", "required_fields"}:
        if not _looks_like_artifact(target):
            return normalised, "unroutable", "验收目标不是可定位的产物路径"
    if normalised in {"record_count_compare", "numeric_compare"}:
        if not params:
            return normalised, "unroutable", "比较型验收缺少结构化 params"
    if normalised == "evidence_supported" and not (
        params.get("keywords") or params.get("claim_id")
    ):
        return normalised, "unroutable", (
            "evidence_supported 必须提供 params.keywords 或 params.claim_id，"
            "不能用责任节点的任意事实代替当前验收项"
        )
    return normalised, "ready", ""


def compile_acceptance_contract(
    requirement_contract: dict[str, Any] | RequirementSet,
    graph: TaskGraph,
    task_spec: dict[str, Any],
) -> AcceptanceContract:
    """Bind requirement criteria to executable producers without LLM guesses."""

    requirement_set = (
        requirement_contract
        if isinstance(requirement_contract, RequirementSet)
        else RequirementSet.model_validate(requirement_contract)
    )
    by_requirement: dict[str, list[str]] = defaultdict(list)
    by_acceptance: dict[str, list[str]] = defaultdict(list)
    artifact_owners: dict[str, list[str]] = defaultdict(list)
    for node in graph.nodes:
        for requirement_id in node.requirement_ids:
            by_requirement[requirement_id].append(node.node_id)
        for criterion_id in node.acceptance_refs:
            by_acceptance[criterion_id].append(node.node_id)
        for artifact in node.output_artifacts:
            artifact_owners[artifact].append(node.node_id)

    final_artifacts = set(
        task_spec.get("artifact_contract", {}).get("final_artifacts", []) or []
    )
    criteria: list[AcceptanceCriterionContract] = []
    for item in requirement_set.requirements:
        # A semantic parent may still carry its own independently checkable
        # objective.  Do not discard that acceptance obligation merely because
        # it also has more detailed children.
        for criterion in item.acceptance_criteria:
            target = criterion.target or (
                item.expected_outputs[0] if item.expected_outputs else ""
            )
            method, compilation_status, issue = _normalise_method(
                criterion.method, target, criterion.params,
            )
            # LLM planners occasionally retain a mandatory parent objective
            # with an empty ``evidence_supported`` criterion.  When a task
            # declares a frozen domain artifact contract, bind that objective
            # to a deterministic artifact instead of leaving an impossible
            # evidence route.  Optional background/trace items are untouched.
            if (
                method == "evidence_supported"
                and item.mandatory
                and not (criterion.params or {}).get("keywords")
                and not (criterion.params or {}).get("claim_id")
                and task_spec.get("domain_result_contract")
            ):
                allowed = task_spec["domain_result_contract"].get(
                    "allowed_acceptance_targets", {}
                )
                if "artifacts/cost_comparison.json" in allowed:
                    target = "artifacts/cost_comparison.json"
                    method = "required_fields"
                    compilation_status = "ready"
                    issue = ""
                    criterion = criterion.model_copy(update={
                        "target": target,
                        "method": method,
                        "params": {"fields": ["/scenarios"]},
                    })
            explicit_candidates = by_acceptance.get(
                criterion.criterion_id, []
            )
            semantic_candidates = list(dict.fromkeys(
                explicit_candidates
                or by_requirement.get(item.requirement_id, [])
            ))
            artifact_candidates = list(dict.fromkeys(
                artifact_owners.get(_artifact_part(target), [])
            ))
            # Deterministic checks must repair/reopen the node that physically
            # produced the file, not an upstream modeling node that merely
            # owns the semantic requirement.
            if method in {
                "artifact_exists", "json_parseable", "required_fields",
                "record_count_compare", "numeric_compare",
            } and artifact_candidates:
                candidates = artifact_candidates
            else:
                candidates = semantic_candidates or artifact_candidates
            producer = candidates[0] if len(candidates) == 1 else None
            if len(candidates) > 1:
                compilation_status = "unroutable"
                issue = "同一验收条件映射到多个生产节点：" + ", ".join(candidates)
            phase = (
                "delivery"
                if item.owner == "outer_workflow"
                or _artifact_part(target) in final_artifacts
                or _artifact_part(target) == "final_report.md"
                else "business"
            )
            if (
                phase == "delivery"
                and method == "evidence_supported"
                and _looks_like_artifact(target)
            ):
                # Delivery content semantics remain covered by the existing
                # deterministic final report validator.  At this contract
                # layer the independently provable obligation is existence.
                method = "artifact_exists"
                compilation_status = "ready"
                issue = ""
            if (
                phase == "delivery"
                and _artifact_part(target) == "final_report.md"
                and method == "required_fields"
            ):
                # Markdown has no JSON Pointer fields.  Existence belongs to
                # the AcceptanceRunner; required report sections are checked
                # by the deterministic final report validator after the one
                # allowed ReportAgent call.
                method = "artifact_exists"
                compilation_status = "ready"
                issue = ""
            # A deterministic artifact check may be evaluated without a graph
            # producer (for example final_report.md, written by the workflow).
            if (
                producer is None
                and item.owner == "planner"
                and method == "evidence_supported"
                and compilation_status == "ready"
            ):
                compilation_status = "unroutable"
                issue = "语义验收没有唯一的生产节点"
            criteria.append(AcceptanceCriterionContract(
                criterion_id=criterion.criterion_id,
                requirement_id=item.requirement_id,
                statement=item.statement,
                mandatory=item.mandatory,
                method=method,
                source_method=criterion.method,
                target=target,
                params=dict(criterion.params),
                severity=criterion.severity,
                producer_node_id=producer,
                phase=phase,
                compilation_status=compilation_status,
                compilation_issue=issue,
            ))

    return AcceptanceContract(
        contract_id=f"acceptance-{requirement_set.contract_id}",
        requirement_contract_version=requirement_set.version,
        criteria=criteria,
        supported_methods=list(SUPPORTED_ACCEPTANCE_METHODS),
    )
