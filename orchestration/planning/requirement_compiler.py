"""Evidence-grounded requirement compilation for the planning frontend.

The compiler deliberately stays task-agnostic.  The model may propose atomic
requirements, while Python assigns stable IDs, preserves source references and
checks the resulting requirement graph before it reaches PlanningAgent.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

from orchestration.core.schemas import (
    RequirementAcceptance,
    RequirementCoverageResult,
    RequirementItem,
    RequirementSet,
    RequirementSourceCandidate,
    RequirementSourceRef,
)


def _slug(value: str, fallback: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").upper()
    return value[:48] or fallback


def _source_refs(task_spec: dict[str, Any]) -> list[RequirementSourceRef]:
    refs: list[RequirementSourceRef] = []
    for preview in task_spec.get("file_previews", []) or []:
        path = str(preview.get("path") or preview.get("file") or "").strip()
        if path:
            refs.append(RequirementSourceRef(
                artifact=path.replace("\\", "/"),
                locator="file_preview",
            ))
    if not refs:
        for path in task_spec.get("input", {}).get("files", []) or []:
            refs.append(RequirementSourceRef(
                artifact=str(path).replace("\\", "/"),
                locator="input_file",
            ))
    return refs[:16]


def _acceptance(requirement_id: str, statement: str, output: str = "") -> list[RequirementAcceptance]:
    target = output or requirement_id
    if output:
        criteria = [RequirementAcceptance(
            criterion_id=f"AC-{requirement_id}-EXISTS",
            method="artifact_exists",
            target=target,
            condition=f"交付物 {target} 必须真实存在",
            severity="blocking",
        )]
        if str(output).lower().endswith(".json"):
            criteria.append(RequirementAcceptance(
                criterion_id=f"AC-{requirement_id}-JSON",
                method="json_parseable",
                target=target,
                condition=f"交付物 {target} 必须是可严格解析的 JSON",
                severity="blocking",
            ))
        return criteria
    return [RequirementAcceptance(
        criterion_id=f"AC-{requirement_id}",
        method="evidence_supported",
        target=target,
        condition=f"能够对“{statement[:180]}”给出结构化结果和可追踪证据",
        params={},
        severity="blocking",
    )]


def _split_acceptance_target(target: str) -> tuple[str, str]:
    value = str(target or "").strip().replace("\\", "/")
    if "#" in value:
        path, fragment = value.split("#", 1)
        return path, "#" + fragment
    if "::" in value:
        path, fragment = value.split("::", 1)
        return path, "::" + fragment
    return value, ""


def _canonical_compare_params(
    criterion: RequirementAcceptance,
) -> RequirementAcceptance:
    """Make model-authored comparison prose executable without guessing data.

    Providers often put ``max_value`` or a comparison symbol in ``condition``
    while omitting the finite runner's ``operator`` field.  Converting those
    explicit signals is deterministic; when no signal exists the original
    criterion is retained for the contract compiler to mark unverified.
    """

    if criterion.method not in {"numeric_compare", "record_count_compare"}:
        return criterion
    params = dict(criterion.params)
    target_path, _ = _split_acceptance_target(criterion.target)

    def normalise_reference(value: Any) -> Any:
        if isinstance(value, str) and value.strip().startswith("/") and target_path:
            return f"{target_path}#{value.strip()}"
        return value

    def normalise_scalar(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", text):
            return value
        number = float(text)
        return int(number) if number.is_integer() else number

    if "left" in params:
        params["left"] = normalise_reference(params["left"])
    if "right" in params:
        right = normalise_reference(params["right"])
        scalar = normalise_scalar(right)
        if isinstance(scalar, (int, float)) and not isinstance(scalar, bool):
            params["value"] = scalar
            params.pop("right", None)
        else:
            params["right"] = right
    if "value" not in params:
        if "max_value" in params:
            params["value"] = params["max_value"]
        elif "min_value" in params:
            params["value"] = params["min_value"]
    if "operator" not in params:
        if "max_value" in params:
            params["operator"] = "le"
        elif "min_value" in params:
            params["operator"] = "ge"
        else:
            condition = str(criterion.condition or "")
            operator = None
            for pattern, candidate in [
                (r">=|≥|不小于", "ge"),
                (r"<=|≤|不超过|至多", "le"),
                (r">|大于", "gt"),
                (r"<|小于", "lt"),
                (r"==|等于|=", "eq"),
            ]:
                if re.search(pattern, condition):
                    operator = candidate
                    break
            if operator:
                params["operator"] = operator
    return criterion.model_copy(update={"params": params})


def _normalise_acceptance_artifacts(
    item: RequirementItem,
    *,
    allowed_outputs: set[str],
    primary_json_output: str | None,
) -> RequirementItem:
    """Route planner checks only to artifacts declared by the global TaskSpec.

    This prevents a requirement model from inventing a second business result
    such as ``validation_report.md`` after TaskSpec has frozen one authoritative
    JSON result.  The JSON fragment is preserved, so no field requirement is
    weakened or silently discarded.
    """

    if item.owner != "planner":
        return item
    expected_outputs: list[str] = []
    for output in item.expected_outputs:
        path, _ = _split_acceptance_target(output)
        replacement = (
            primary_json_output
            if path and path not in allowed_outputs and primary_json_output
            else output
        )
        if replacement not in expected_outputs:
            expected_outputs.append(replacement)
    criteria: list[RequirementAcceptance] = []
    for criterion in item.acceptance_criteria:
        path, fragment = _split_acceptance_target(criterion.target)
        target = criterion.target
        if (
            path
            and ("/" in path or PurePosixPath(path).suffix)
            and path not in allowed_outputs
            and primary_json_output
        ):
            target = primary_json_output + fragment
        method = criterion.method
        target_path, _ = _split_acceptance_target(target)
        if (
            method == "json_parseable"
            and target_path
            and not target_path.lower().endswith(".json")
        ):
            # JSON parsing cannot prove that a Python/text deliverable is
            # valid.  Preserve the existence obligation here; executable-code
            # validity remains independently enforced by terminal execution.
            method = "artifact_exists"
        criteria.append(_canonical_compare_params(
            criterion.model_copy(update={
                "target": target,
                "method": method,
            })
        ))
    return item.model_copy(update={
        "expected_outputs": expected_outputs,
        "acceptance_criteria": criteria,
    })


_OBLIGATION_PATTERN = re.compile(
    r"必须|务必|不得|不允许|仅允许|至少|不小于|不超过|分别|给出|输出|计算|"
    r"比较|对比|验证|目标(?:是|为)|建立.{0,24}(?:模型|方案)|"
    r"设计.{0,24}(?:算法|方案)|撰写.{0,12}报告|需要|要求"
)


def extract_requirement_source_candidates(
    task_spec: dict[str, Any],
) -> list[RequirementSourceCandidate]:
    """Locate likely obligation spans without interpreting domain semantics.

    Keywords only nominate source spans for LLM analysis; they never create a
    requirement by themselves.  Stable IDs let Python verify that the semantic
    compiler did not silently skip a task-bearing part of a long document.
    """

    candidates: list[RequirementSourceCandidate] = []
    seen: set[tuple[str, str]] = set()
    for file_index, preview in enumerate(
        task_spec.get("file_previews", []) or [], start=1,
    ):
        artifact = str(
            preview.get("path") or preview.get("file") or f"input-{file_index}"
        ).replace("\\", "/").rsplit("/", 1)[-1]
        text = str(preview.get("text_preview") or "")
        if not text.strip():
            continue
        # Preserve offsets while splitting paragraphs and Chinese sentences.
        for match in re.finditer(r"[^。！？；]+[。！？；]?", text):
            span = match.group(0).strip()
            semantic_text = re.sub(r"\s+", "", span)
            if len(semantic_text) < 8 or not _OBLIGATION_PATTERN.search(semantic_text):
                continue
            compact = re.sub(r"\s+", " ", span)[:600]
            key = (artifact, compact)
            if key in seen:
                continue
            seen.add(key)
            candidate_id = f"SRC-{file_index:02d}-{len(candidates) + 1:03d}"
            candidates.append(RequirementSourceCandidate(
                candidate_id=candidate_id,
                artifact=artifact,
                locator=f"preview:chars:{match.start()}-{match.end()}",
                text=compact,
            ))
            if len(candidates) >= 96:
                return candidates
    return candidates


def _normalise_model_requirements(
    raw: list[dict[str, Any]] | list[RequirementItem],
    refs: list[RequirementSourceRef],
    task_spec: dict[str, Any],
) -> list[RequirementItem]:
    items: list[RequirementItem] = []
    artifact_contract = task_spec.get("artifact_contract", {})
    graph_outputs = set(
        artifact_contract.get("intermediate_artifacts", []) or []
    )
    outer_outputs = set(artifact_contract.get("final_artifacts", []) or [])
    outer_outputs.update(artifact_contract.get("framework_artifacts", []) or [])
    allowed_outputs = graph_outputs | outer_outputs
    primary_json_output = next(
        (
            output for output in artifact_contract.get(
                "intermediate_artifacts", []
            ) or []
            if str(output).lower().endswith(".json")
        ),
        None,
    )
    for index, value in enumerate(raw, start=1):
        item = value if isinstance(value, RequirementItem) else RequirementItem.model_validate(value)
        expected = set(item.expected_outputs)
        statement = item.statement.lower()
        delivery_signal = bool(re.search(
            r"报告|文档|章节|附录|report|document|section|appendix",
            statement,
        ))
        # Ownership describes where the work is performed, not merely where
        # its prose is eventually displayed.  Any requirement that produces a
        # graph artifact, or a mandatory analytical/constraint obligation,
        # belongs to planning even if the model also points at final_report.md.
        if expected & graph_outputs:
            item = item.model_copy(update={"owner": "planner"})
        elif (
            expected
            and expected.issubset(outer_outputs)
            and (
                item.requirement_type == "deliverable"
                or delivery_signal
            )
        ):
            # Models often label a report-section obligation as a generic
            # ``goal``.  The explicit final-artifact target plus report wording
            # is sufficient to route it to the existing report/final-validation
            # phase.  Treating it as a planner leaf creates fake business nodes
            # and makes phrases such as "model performance analysis section"
            # look like cross-stage computation.
            item = item.model_copy(update={"owner": "outer_workflow"})
        elif (
            item.mandatory
            and item.requirement_type in {
                "goal", "scenario", "objective", "input", "constraint",
                "metric", "validation",
            }
        ):
            item = item.model_copy(update={"owner": "planner"})
        elif expected and expected.issubset(outer_outputs):
            item = item.model_copy(update={"owner": "outer_workflow"})
        item = _normalise_acceptance_artifacts(
            item,
            allowed_outputs=allowed_outputs,
            primary_json_output=primary_json_output,
        )
        # Materialise the generic fallback before applying a domain result
        # contract.  Refinement can introduce parent/compound requirements
        # without criteria; creating their fallback afterwards would leave an
        # empty evidence_supported check that bypasses the domain validator.
        if not item.acceptance_criteria:
            item = item.model_copy(update={
                "acceptance_criteria": _acceptance(
                    item.requirement_id, item.statement,
                    item.expected_outputs[0] if item.expected_outputs else "",
                )
            })
        domain_targets = (
            task_spec.get("domain_result_contract", {}).get(
                "allowed_acceptance_targets", {}
            ) or {}
        )
        if domain_targets:
            normalized_criteria = []
            for criterion in item.acceptance_criteria:
                base_target = str(criterion.target or "").split("#", 1)[0]
                allowed_fields = list(domain_targets.get(base_target, []))
                if allowed_fields and criterion.method in {
                    "required_fields", "numeric_compare", "record_count_compare",
                    "evidence_supported",
                }:
                    requested_fields = list(
                        (criterion.params or {}).get("fields", []) or []
                    )
                    invalid = (
                        criterion.method != "required_fields"
                        or not requested_fields
                        or any(field not in allowed_fields for field in requested_fields)
                    )
                    if invalid:
                        criterion = criterion.model_copy(update={
                            "method": "required_fields",
                            "target": base_target,
                            "params": {"fields": allowed_fields},
                            "condition": (
                                "领域合同字段存在；数值与语义正确性由独立领域验证器复算"
                            ),
                        })
                elif criterion.method == "evidence_supported" and item.mandatory:
                    validation_target = str(
                        task_spec["domain_result_contract"].get(
                            "semantic_validation_target", "",
                        )
                    )
                    validation_fields = list(
                        task_spec["domain_result_contract"].get(
                            "semantic_validation_fields", [],
                        )
                    )
                    if validation_target and validation_fields:
                        criterion = criterion.model_copy(update={
                            "method": "required_fields",
                            "target": validation_target,
                            "params": {"fields": validation_fields},
                            "condition": (
                                "领域独立验证日志完整；业务正确性由领域验证器复算"
                            ),
                        })
                normalized_criteria.append(criterion)
            item = item.model_copy(update={"acceptance_criteria": normalized_criteria})
        if not item.source_refs:
            item = item.model_copy(update={"source_refs": refs[:1]})
        if item.mandatory and item.owner == "planner" and not any(
            criterion.severity == "blocking"
            for criterion in item.acceptance_criteria
        ):
            # A mandatory planner obligation with warning-only acceptance is
            # not actually enforceable.  Promote one existing criterion while
            # preserving the model-authored method/target/condition.
            criteria = list(item.acceptance_criteria)
            criteria[0] = criteria[0].model_copy(
                update={"severity": "blocking"}
            )
            item = item.model_copy(update={"acceptance_criteria": criteria})
        items.append(item)
    # criterion_id is the machine key used by PlanIR mapping and the final
    # requirement ledger.  Refinement models may reuse a criterion ID for two
    # different child requirements.  Namespace every colliding occurrence by
    # its requirement ID so no acceptance obligation can silently overwrite
    # another in a dictionary lookup.
    criterion_owners: dict[str, list[str]] = defaultdict(list)
    for item in items:
        for criterion in item.acceptance_criteria:
            criterion_owners[criterion.criterion_id].append(
                item.requirement_id
            )
    normalised_items: list[RequirementItem] = []
    for item in items:
        criteria = [
            criterion.model_copy(update={
                "criterion_id": (
                    f"{criterion.criterion_id}--{item.requirement_id}"
                    if len(criterion_owners[criterion.criterion_id]) > 1
                    else criterion.criterion_id
                )
            })
            for criterion in item.acceptance_criteria
        ]
        normalised_items.append(item.model_copy(update={
            "acceptance_criteria": criteria,
        }))
    return normalised_items


def compile_requirement_set(
    task_spec: dict[str, Any],
    task_understanding: dict[str, Any] | None = None,
) -> RequirementSet:
    """Compile a frozen, generic requirement graph from understanding output.

    Explicit model requirements are preferred.  Older understanding models
    that only return goals/constraints remain supported through deterministic
    fallback requirements, so planning never receives an empty contract.
    """

    understanding = task_understanding or {}
    refs = _source_refs(task_spec)
    explicit = understanding.get("requirements") or []
    if explicit:
        items = _normalise_model_requirements(explicit, refs, task_spec)
    else:
        items = []
        root_id = "REQ-GLOBAL-GOAL"
        goal = (
            str(understanding.get("summary") or "").strip()
            or str(task_spec.get("task_name") or "").strip()
            or "完成 TaskSpec 定义的任务"
        )
        items.append(RequirementItem(
            requirement_id=root_id,
            requirement_type="goal",
            statement=goal,
            # No semantic requirement list was produced.  Preserve this as an
            # auditable derived goal, but do not pretend Python inferred a
            # machine-verifiable mandatory business obligation from a title.
            mandatory=False,
            source_refs=refs[:1],
            acceptance_criteria=_acceptance(root_id, goal),
            status="derived",
        ))
        for index, text in enumerate(understanding.get("goals", []) or [], start=1):
            statement = str(text).strip()
            if not statement:
                continue
            rid = f"REQ-GOAL-{index:03d}"
            items.append(RequirementItem(
                requirement_id=rid,
                parent_id=root_id,
                requirement_type="goal",
                statement=statement,
                mandatory=False,
                source_refs=refs[:1],
                acceptance_criteria=_acceptance(rid, statement),
                status="derived",
            ))
        for index, text in enumerate(understanding.get("constraints", []) or [], start=1):
            statement = str(text).strip()
            if not statement:
                continue
            rid = f"REQ-CONSTRAINT-{index:03d}"
            items.append(RequirementItem(
                requirement_id=rid,
                parent_id=root_id,
                requirement_type="constraint",
                statement=statement,
                mandatory=False,
                source_refs=refs[:1],
                acceptance_criteria=_acceptance(rid, statement),
                status="derived",
            ))
        for index, output in enumerate(
            task_spec.get("artifact_contract", {}).get("intermediate_artifacts", []) or [],
            start=1,
        ):
            rid = f"REQ-DELIVERABLE-{index:03d}"
            items.append(RequirementItem(
                requirement_id=rid,
                parent_id=root_id,
                requirement_type="deliverable",
                statement=f"生成交付物 {output}",
                mandatory=True,
                source_refs=refs[:1],
                expected_outputs=[str(output)],
                acceptance_criteria=_acceptance(rid, f"生成交付物 {output}", str(output)),
            ))

    # Add stable IDs for accidentally omitted model IDs and de-duplicate.
    normalised: list[RequirementItem] = []
    seen: set[str] = set()
    for index, item in enumerate(items, start=1):
        rid = item.requirement_id.strip() or f"REQ-DERIVED-{index:03d}"
        if rid in seen:
            rid = f"{rid}-{index}"
        seen.add(rid)
        update: dict[str, Any] = {"requirement_id": rid}
        # A requirement cannot contain itself.  Models occasionally emit a
        # self parent/dependency as a transport mistake; removing only that
        # impossible edge preserves the requirement and all acceptance terms.
        if item.parent_id == item.requirement_id:
            update["parent_id"] = None
        if item.requirement_id in item.depends_on:
            update["depends_on"] = [
                dependency for dependency in item.depends_on
                if dependency != item.requirement_id
            ]
        normalised.append(item.model_copy(update=update))

    return RequirementSet(
        contract_id=f"requirements-{task_spec.get('task_id', 'run')}",
        version=1,
        global_goal=str(
            understanding.get("summary")
            or task_spec.get("task_name")
            or "完成 TaskSpec 定义的任务"
        ),
        source_candidates=extract_requirement_source_candidates(task_spec),
        requirements=normalised,
    )


def validate_requirement_set(requirement_set: RequirementSet) -> RequirementCoverageResult:
    """Validate IDs, parent links, and acceptance closure without LLM guesses."""

    by_id = {item.requirement_id: item for item in requirement_set.requirements}
    result = RequirementCoverageResult()
    parent_ids = {
        item.parent_id for item in requirement_set.requirements if item.parent_id
    }
    def references_candidate(candidate: RequirementSourceCandidate) -> bool:
        candidate_artifact = PurePosixPath(
            candidate.artifact.replace("\\", "/")
        ).name
        for item in requirement_set.requirements:
            for ref in item.source_refs:
                # Both forms are lossless and auditable: a stable candidate
                # ID, or the exact file + source span from which that ID was
                # deterministically extracted.
                if candidate.candidate_id in ref.locator:
                    return True
                ref_artifact = PurePosixPath(
                    ref.artifact.replace("\\", "/")
                ).name
                if (
                    ref_artifact == candidate_artifact
                    and ref.locator == candidate.locator
                ):
                    return True
        return False

    referenced_candidates = {
        candidate.candidate_id
        for candidate in requirement_set.source_candidates
        if references_candidate(candidate)
    }
    result.uncovered_source_candidate_ids = [
        candidate.candidate_id
        for candidate in requirement_set.source_candidates
        if candidate.candidate_id not in referenced_candidates
    ]
    if result.uncovered_source_candidate_ids:
        result.revision_instructions.append(
            "分析未覆盖的 source candidate，并为其中真实业务要求补充原子需求；"
            "若候选片段仅为背景说明，也必须增加 status=derived、mandatory=false "
            "的追踪项说明排除理由"
        )
    if len(by_id) != len(requirement_set.requirements):
        result.invalid_requirement_ids.append("<duplicate-id>")
    for item in requirement_set.requirements:
        if item.parent_id and item.parent_id not in by_id:
            result.invalid_requirement_ids.append(item.requirement_id)
        invalid_dependencies = [
            dependency for dependency in item.depends_on
            if dependency not in by_id or dependency == item.requirement_id
        ]
        if invalid_dependencies:
            result.invalid_requirement_ids.append(item.requirement_id)
        if item.mandatory and not item.acceptance_criteria:
            result.missing_acceptance_ids.append(item.requirement_id)
        statement = item.statement.lower()
        split_signal = bool(re.search(
            r"(?:车型|场景|方案|目标)?\s*[一二三四五六七八九十1-9]+\s*/\s*"
            r"(?:车型|场景|方案|目标)?\s*[一二三四五六七八九十1-9]+"
            r"|单\s*/\s*多"
            r"|(?:和|与|、).{0,24}分别(?:建立|设计|求解|计算|输出|生成|验证|分析|比较)"
            r"|(?:不同|各|多种|两种).{0,24}分别(?:建立|设计|求解|计算|输出|生成|验证|分析|比较)"
            # Computing a value and emitting it is one executable state
            # transition.  Do not split every "计算并输出" requirement.  Keep
            # high-confidence cross-stage combinations such as designing a
            # model/algorithm and also producing solution output.
            r"|(?:设计|建立).{0,24}(?:算法|模型).{0,16}并输出"
            r"|并(?:对比|比较|验证|生成报告|撰写报告)",
            statement,
        ))
        responsibility_groups = [
            {"建模", "模型", "model"},
            {"算法", "求解策略", "algorithm", "solver"},
            {"代码", "实现", "code", "implement"},
            {"执行", "运行", "execute", "run"},
            {"验证", "校验", "verify", "validate"},
            {"报告", "撰写", "report"},
            {"分析", "对比", "比较", "analysis", "compare"},
        ]
        responsibility_count = sum(
            any(word in statement for word in words)
            for words in responsibility_groups
        )
        if (
            item.mandatory
            and item.owner == "planner"
            and item.requirement_id not in parent_ids
            and (
            split_signal or responsibility_count >= 3
            )
        ):
            result.oversized_requirement_ids.append(item.requirement_id)
            result.revision_instructions.append(
                f"{item.requirement_id} 同时包含可分别失败或验收的职责；"
                "保留为 compound 需求，并拆成具有独立输出和验收条件的子需求"
            )
    # A parent chain cycle would make closure impossible.
    for item in requirement_set.requirements:
        seen: set[str] = set()
        current = item
        while current.parent_id:
            if current.parent_id in seen or current.parent_id == item.requirement_id:
                result.invalid_requirement_ids.append(item.requirement_id)
                break
            seen.add(current.parent_id)
            current = by_id.get(current.parent_id)
            if current is None:
                break
    indegree = {
        item.requirement_id: 0 for item in requirement_set.requirements
    }
    children_by_dependency: dict[str, list[str]] = {
        item.requirement_id: [] for item in requirement_set.requirements
    }
    for item in requirement_set.requirements:
        for dependency in item.depends_on:
            if dependency in indegree and dependency != item.requirement_id:
                indegree[item.requirement_id] += 1
                children_by_dependency[dependency].append(item.requirement_id)
    dependency_ready = [
        requirement_id for requirement_id, degree in indegree.items()
        if degree == 0
    ]
    visited_dependencies = 0
    while dependency_ready:
        current_id = dependency_ready.pop()
        visited_dependencies += 1
        for child_id in children_by_dependency[current_id]:
            indegree[child_id] -= 1
            if indegree[child_id] == 0:
                dependency_ready.append(child_id)
    if visited_dependencies != len(indegree):
        result.invalid_requirement_ids.extend(
            requirement_id for requirement_id, degree in indegree.items()
            if degree > 0
        )
        result.revision_instructions.append(
            "修复 Requirement.depends_on 中的循环依赖"
        )
    result.invalid_requirement_ids = list(dict.fromkeys(result.invalid_requirement_ids))
    result.missing_acceptance_ids = list(dict.fromkeys(result.missing_acceptance_ids))
    result.oversized_requirement_ids = list(dict.fromkeys(
        result.oversized_requirement_ids
    ))
    result.passed = not (
        result.invalid_requirement_ids or result.missing_acceptance_ids
        or result.oversized_requirement_ids
        or result.uncovered_source_candidate_ids
    )
    return result


def add_derived_source_traces(requirement_set: RequirementSet) -> RequirementSet:
    """Preserve every extracted source span even when the model omits it.

    Background spans are explicitly marked derived/non-mandatory.  This keeps
    coverage auditable without silently turning a model omission into a fake
    executable business requirement.
    """
    referenced: set[str] = set()
    for item in requirement_set.requirements:
        for ref in item.source_refs:
            referenced.add(ref.locator.split("#", 1)[0])
    additions = []
    for candidate in requirement_set.source_candidates:
        if candidate.candidate_id in referenced or candidate.locator in referenced:
            continue
        additions.append(RequirementItem(
            requirement_id=f"TRACE-{candidate.candidate_id}",
            relation="optional", requirement_type="quality",
            statement=f"来源片段追踪（未判定为可执行业务要求）：{candidate.text}",
            mandatory=False, owner="compiler", status="derived",
            source_refs=[RequirementSourceRef(
                artifact=candidate.artifact,
                locator=f"{candidate.locator}#{candidate.candidate_id}",
                excerpt=candidate.text,
            )],
        ))
    if not additions:
        return requirement_set
    return requirement_set.model_copy(update={
        "requirements": [*requirement_set.requirements, *additions],
    })


def preserve_requirement_history(
    previous: RequirementSet,
    refined: RequirementSet,
) -> RequirementSet:
    """Keep every previously accepted requirement across a local refinement.

    A refinement model may accidentally return only its newly created
    children, even though the prompt asks for the complete RequirementSet.
    Losing an old requirement is more dangerous than retaining a coarse parent:
    append any omitted old item while preferring the refined form of IDs that
    were explicitly returned.
    """

    refined_ids = {
        item.requirement_id for item in refined.requirements
    }
    requirements = list(refined.requirements)
    requirements.extend(
        item for item in previous.requirements
        if item.requirement_id not in refined_ids
    )
    # Refinement models sometimes create an intermediate requirement and its
    # children in the same response but attach every new item to the original
    # coarse parent.  Recover only an unambiguous ID-prefix hierarchy (for
    # example REQ-005a-1 -> REQ-005a) so the intermediate item is not falsely
    # treated as an oversized leaf.  No requirement or acceptance condition is
    # invented or removed.
    repaired: list[RequirementItem] = []
    for item in requirements:
        candidates = [
            candidate for candidate in requirements
            if candidate.requirement_id != item.requirement_id
            and item.requirement_id.startswith(candidate.requirement_id + "-")
            and candidate.parent_id == item.parent_id
        ]
        if candidates:
            parent = max(candidates, key=lambda candidate: len(candidate.requirement_id))
            item = item.model_copy(update={"parent_id": parent.requirement_id})
        repaired.append(item)
    requirements = repaired
    return refined.model_copy(update={
        "contract_id": previous.contract_id,
        "version": max(previous.version + 1, refined.version),
        "global_goal": refined.global_goal or previous.global_goal,
        "source_candidates": previous.source_candidates,
        "requirements": requirements,
    })


def requirement_digest(requirement_set: RequirementSet) -> str:
    payload = requirement_set.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
