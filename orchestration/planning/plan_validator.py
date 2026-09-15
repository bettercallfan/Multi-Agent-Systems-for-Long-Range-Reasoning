"""Deterministic quality checks for hierarchical plans."""

from __future__ import annotations

import json
import re
from collections import defaultdict

from orchestration.planning.plan_ir import (
    PlanIR,
    PlanNode,
    PlanNodeType,
    PlanValidationResult,
    PlanningContract,
    PlanningPolicy,
)
from orchestration.core.schemas import RequirementSet


_PHASE_WORDS = {
    # Generic output paths such as ``result.json``/“结果文件” must not make
    # an implementation primitive look like a document-processing phase.
    "document": {"文档", "附件", "提取", "document", "extract"},
    "analysis": {"分析", "清洗", "分类", "理解", "analysis", "clean"},
    "model": {"建模", "模型", "约束", "目标函数", "model", "constraint"},
    "code": {"代码", "编程", "实现", "code", "implement"},
    "execution": {"执行", "运行", "求解", "execute", "run", "solve"},
    "validation": {"验证", "校验", "审查", "核验", "validate", "verify", "review"},
    "report": {"报告", "交付", "report", "deliver"},
}
_PSEUDO_WORDS = {"进一步", "完善", "全面", "综合", "最终", "further", "complete", "overall"}
_FRAMEWORK_CAPABILITIES = {
    "terminal_execution", "test_execution", "artifact_validation",
    "evidence_verification", "report_generation",
}
_MODELLING_RESPONSIBILITIES = {
    "decision_variables": {
        "决策变量", "变量定义", "定义变量", "decision variable",
        "decision variables", "variable definition",
    },
    "objective_function": {
        "目标函数", "优化目标", "objective function", "optimization objective",
    },
    "constraints": {
        "约束设计", "物理约束", "业务约束", "全部约束", "多类约束",
        "constraint design", "physical constraints", "all constraints",
    },
    "assumptions": {
        "建模假设", "模型假设", "假设说明", "model assumption",
        "model assumptions",
    },
    "solution_strategy": {
        "求解策略", "求解方法", "算法设计", "算法选择", "solver strategy",
        "solution strategy", "algorithm design", "algorithm selection",
    },
    "model_validation": {
        "模型验证", "模型校验", "验证模型", "validate model",
        "model validation", "model verification",
    },
}
_CODE_ALGORITHM_DESIGN_WORDS = {
    "算法设计", "算法选择", "设计算法", "求解策略", "求解方法设计",
    "完整求解流程", "求解流程设计", "algorithm design",
    "algorithm selection", "solver strategy", "solution strategy",
}
_CODE_IMPLEMENTATION_WORDS = {
    "代码", "实现", "编程", "可执行", "code", "implement", "implementation",
    "executable",
}
_CODE_BUSINESS_VALIDATION_WORDS = {
    "业务验证", "结果验证", "业务结果校验", "结果合理性", "方案有效性",
    "验证业务结果", "business validation", "result validation",
    "validate business result", "solution validity",
}


def _all_edges(plan: PlanIR) -> set[tuple[str, str]]:
    edges = set(plan.edges)
    for node in plan.nodes:
        edges.update((dep, node.node_id) for dep in node.dependencies)
    return edges


def _is_cycle(node_ids: set[str], edges: set[tuple[str, str]]) -> bool:
    outgoing: dict[str, set[str]] = defaultdict(set)
    indegree = {node_id: 0 for node_id in node_ids}
    for source, target in edges:
        if source in node_ids and target in node_ids and target not in outgoing[source]:
            outgoing[source].add(target)
            indegree[target] += 1
    ready = [node_id for node_id, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        current = ready.pop()
        visited += 1
        for target in outgoing[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    return visited != len(node_ids)


def _phase_count(objective: str) -> int:
    text = objective.lower()
    return sum(any(word.lower() in text for word in words) for words in _PHASE_WORDS.values())


def _primitive_scope_text(node: PlanNode) -> str:
    """Collect model-authored responsibility signals without inventing work."""
    parts = [node.objective]
    if node.expected_output is not None:
        parts.append(node.expected_output.description)
    parts.extend(
        json.dumps(criterion, ensure_ascii=False, sort_keys=True)
        for criterion in node.acceptance_criteria
    )
    parts.extend(node.output_artifacts)
    return "\n".join(parts).lower()


def _matched_responsibilities(
    text: str,
    vocabulary: dict[str, set[str]],
) -> list[str]:
    return [
        responsibility
        for responsibility, words in vocabulary.items()
        if any(word.lower() in text for word in words)
    ]


def _primitive_scope_issue(node: PlanNode) -> tuple[str, list[str]] | None:
    """Return only high-confidence cross-responsibility primitive violations."""
    text = _primitive_scope_text(node)
    if node.primary_capability == "math_modeling":
        responsibilities = _matched_responsibilities(
            text, _MODELLING_RESPONSIBILITIES,
        )
        if len(responsibilities) >= 2:
            return "math_modeling", responsibilities

    if node.primary_capability == "code":
        implements_code = any(
            word.lower() in text for word in _CODE_IMPLEMENTATION_WORDS
        ) or any(path.endswith((".py", ".js", ".ts", ".java", ".cpp")) for path in node.output_artifacts)
        designs_algorithm = any(
            word.lower() in text for word in _CODE_ALGORITHM_DESIGN_WORDS
        )
        validates_business_result = any(
            word.lower() in text for word in _CODE_BUSINESS_VALIDATION_WORDS
        )
        responsibilities = []
        if designs_algorithm:
            responsibilities.append("algorithm_or_solution_strategy_design")
        if implements_code:
            responsibilities.append("code_implementation")
        if validates_business_result:
            responsibilities.append("business_result_validation")
        if implements_code and (designs_algorithm or validates_business_result):
            return "code", responsibilities
    return None


def _validate_requirement_coverage(
    plan: PlanIR,
    requirement_contract: dict,
    result: PlanValidationResult,
) -> None:
    """Require every mandatory requirement leaf to reach a PlanIR primitive."""
    if not requirement_contract:
        return
    try:
        requirement_set = RequirementSet.model_validate(requirement_contract)
    except Exception as exc:
        result.graph_errors.append(f"RequirementSet 无法解析: {exc}")
        result.revision_instructions.append("修复 requirement_contract 后再生成 PlanIR")
        return
    items = {item.requirement_id: item for item in requirement_set.requirements}
    children: dict[str, list[str]] = defaultdict(list)
    for item in requirement_set.requirements:
        if item.parent_id in items:
            children[item.parent_id].append(item.requirement_id)
    covered = {
        requirement_id
        for node in plan.nodes
        if node.node_type == PlanNodeType.PRIMITIVE
        for requirement_id in node.requirement_ids
        if requirement_id in items
    }
    result.covered_requirement_ids = sorted(covered)
    missing: set[str] = set()

    def closes(requirement_id: str) -> bool:
        item = items[requirement_id]
        if item.owner != "planner":
            return True
        child_ids = children.get(requirement_id, [])
        if not child_ids:
            ok = (not item.mandatory) or requirement_id in covered
            if not ok:
                missing.add(requirement_id)
            return ok
        states = [closes(child_id) for child_id in child_ids]
        if item.relation == "or":
            return any(states)
        if item.relation == "optional":
            return True
        return all(states)

    roots = [
        item.requirement_id for item in requirement_set.requirements
        if not item.parent_id or item.parent_id not in items
    ]
    for root in roots:
        closes(root)
    result.missing_requirement_ids = sorted(missing)
    for requirement_id in result.missing_requirement_ids:
        result.problem_node_ids.extend(
            node.node_id for node in plan.nodes
            if node.node_type == PlanNodeType.COMPOUND
        )
    required_acceptance = {
        criterion.criterion_id
        for item in requirement_set.requirements
        if item.requirement_id in covered and item.mandatory
        and item.owner == "planner"
        for criterion in item.acceptance_criteria
        if criterion.severity == "blocking"
    }
    declared_acceptance = {
        reference
        for node in plan.nodes if node.node_type == PlanNodeType.PRIMITIVE
        for reference in node.acceptance_refs
    }
    result.missing_acceptance_ids = sorted(required_acceptance - declared_acceptance)
    if result.missing_requirement_ids:
        result.revision_instructions.append(
            "为每个 mandatory requirement 补充 requirement_ids，并将其映射到可执行 primitive"
        )
    if result.missing_acceptance_ids:
        result.revision_instructions.append(
            "为已覆盖 requirement 补充对应的 acceptance_refs，确保每条强制要求可独立验收"
        )


def validate_plan_ir(
    plan: PlanIR,
    contract: PlanningContract,
    policy: PlanningPolicy,
    *,
    authoritative_goal: str | None = None,
    available_capabilities: set[str] | list[str] | None = None,
) -> PlanValidationResult:
    result = PlanValidationResult()
    nodes = plan.nodes
    by_id = {node.node_id: node for node in nodes}
    primitive = [node for node in nodes if node.node_type == PlanNodeType.PRIMITIVE]
    compounds = [node for node in nodes if node.node_type == PlanNodeType.COMPOUND]
    primitive_ids = {node.node_id for node in primitive}
    edges = _all_edges(plan)
    code_nodes = [
        node for node in primitive if node.primary_capability == "code"
    ]
    _validate_requirement_coverage(
        plan, contract.requirement_contract, result,
    )
    if len(code_nodes) > 1:
        code_ids = [node.node_id for node in code_nodes]
        result.graph_errors.append(
            "层级 PlanIR 必须且只能包含一个 code primitive，当前包含: "
            + ", ".join(code_ids)
        )
        result.problem_node_ids.extend(code_ids)
        result.revision_instructions.append(
            "保留唯一 code primitive；算法/求解策略由上游非 code 节点产出，"
            "不要在建模细化子树中新增第二个 code primitive"
        )

    if len(by_id) != len(nodes):
        result.graph_errors.append("PlanNode.node_id 必须唯一")

    def hierarchy_ancestors(node: PlanNode) -> set[str]:
        """Return containment ancestors; they are never data producers."""
        ancestors: set[str] = set()
        current_id = node.parent_node_id
        while current_id and current_id not in ancestors:
            ancestors.add(current_id)
            parent = by_id.get(current_id)
            current_id = parent.parent_node_id if parent is not None else None
        return ancestors

    def is_hierarchy_pair(source: str, target: str) -> bool:
        source_node = by_id.get(source)
        target_node = by_id.get(target)
        if source_node is None or target_node is None:
            return False
        return (
            source in hierarchy_ancestors(target_node)
            or target in hierarchy_ancestors(source_node)
        )

    for source, target in edges:
        missing_endpoints = [
            node_id for node_id in (source, target) if node_id not in by_id
        ]
        if missing_endpoints:
            result.dependency_errors.append(
                f"PlanIR 边引用不存在节点: {source}->{target}"
            )
            if target in by_id:
                result.problem_node_ids.append(target)
            elif source in by_id:
                result.problem_node_ids.append(source)
            continue
        if is_hierarchy_pair(source, target):
            result.dependency_errors.append(
                f"层级包含关系不能作为数据依赖: {source}->{target}；"
                "父子/祖先后代关系只能由 parent_node_id 表达"
            )
            # The target owns an incoming dependency when the pair came from
            # node.dependencies. Marking it makes local refinement actionable;
            # the adapter normally removes this defect before validation.
            result.problem_node_ids.append(target)
            result.revision_instructions.append(
                f"删除层级节点之间的执行边 {source}->{target}，"
                "仅保留跨独立任务节点的真实数据依赖"
            )
    if len(nodes) > policy.max_plan_nodes:
        result.oversized_nodes.append("__plan__")
        result.revision_instructions.append(
            f"计划节点数不能超过 {policy.max_plan_nodes}"
        )
    for node in nodes:
        # Compound nodes are removed before execution; their capability is
        # descriptive hierarchy metadata.  Only executable primitive leaves
        # must map to a registered runtime capability.
        if (
            node.node_type == PlanNodeType.PRIMITIVE
            and available_capabilities is not None
            and node.primary_capability not in set(available_capabilities)
        ):
            result.graph_errors.append(
                f"节点 {node.node_id} 使用未注册 capability: {node.primary_capability}"
            )
            result.problem_node_ids.append(node.node_id)
        if node.primary_capability in _FRAMEWORK_CAPABILITIES:
            result.graph_errors.append(
                f"节点 {node.node_id} 使用框架治理 capability，不能进入业务 PlanIR: "
                f"{node.primary_capability}"
            )
            result.problem_node_ids.append(node.node_id)
        if node.decomposition_depth > policy.max_decomposition_depth:
            result.oversized_nodes.append(node.node_id)
        if node.parent_node_id and node.parent_node_id not in by_id:
            result.graph_errors.append(
                f"节点 {node.node_id} 的 parent_node_id 不存在: {node.parent_node_id}"
            )
            # A missing parent is a local structural defect.  Mark the
            # referencing node so refinement never receives an empty target
            # set and falls back to an arbitrary model-generated ID.
            result.problem_node_ids.append(node.node_id)
            result.revision_instructions.append(
                f"为 {node.node_id} 补齐真实 compound 父节点 "
                f"{node.parent_node_id}，或移除无效 parent_node_id"
            )
        invalid_ancestor_dependencies = sorted(
            set(node.dependencies) & hierarchy_ancestors(node)
        )
        if invalid_ancestor_dependencies:
            result.dependency_errors.append(
                f"节点 {node.node_id} 不能把 parent_node_id/层级祖先 "
                f"{', '.join(invalid_ancestor_dependencies)} 作为数据依赖；"
                "父子包含关系只能由 parent_node_id 表达"
            )
            result.problem_node_ids.append(node.node_id)
            result.revision_instructions.append(
                f"修正 {node.node_id} 的局部依赖：删除对父 compound "
                f"{', '.join(invalid_ancestor_dependencies)} 的 dependencies，"
                "仅保留真实数据生产者"
            )
        for dependency in node.dependencies:
            if dependency not in by_id:
                result.dependency_errors.append(
                    f"节点 {node.node_id} 依赖不存在: {dependency}"
                )
                result.problem_node_ids.append(node.node_id)
        if (node.node_id, node.node_id) in edges:
            result.graph_errors.append(f"节点不能依赖自己: {node.node_id}")

    if _is_cycle(set(by_id), edges):
        result.graph_errors.append("PlanIR 存在循环依赖")

    children: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        if node.parent_node_id:
            children[node.parent_node_id].append(node.node_id)
    for parent_id, child_ids in children.items():
        parent = by_id.get(parent_id)
        if parent is not None and parent.node_type == PlanNodeType.PRIMITIVE:
            result.invalid_decompositions.append(
                f"primitive 节点 {parent_id} 不能作为层级父节点；"
                f"其子节点为: {', '.join(sorted(child_ids))}"
            )
            result.problem_node_ids.append(parent_id)
            result.revision_instructions.append(
                f"将 {parent_id} 转为 compound 并保留真实子任务，"
                "或移除子节点的 parent_node_id；primitive 必须是可直接执行的叶子"
            )
    for node in compounds:
        invalid_compound = False
        if not children.get(node.node_id):
            result.invalid_decompositions.append(
                f"compound 节点 {node.node_id} 没有子节点"
            )
            invalid_compound = True
        if len(children.get(node.node_id, [])) < 2:
            result.invalid_decompositions.append(
                f"compound 节点 {node.node_id} 至少需要两个实质子任务"
            )
            invalid_compound = True
        if node.output_artifacts:
            result.invalid_decompositions.append(
                f"compound 节点 {node.node_id} 不能直接声明物理产物"
            )
            invalid_compound = True
        if invalid_compound:
            result.problem_node_ids.append(node.node_id)

    for node in primitive:
        if node.expected_output is None:
            result.missing_outputs.append(node.node_id)
            result.problem_node_ids.append(node.node_id)
        if not node.acceptance_criteria:
            result.missing_acceptance_criteria.append(node.node_id)
            result.problem_node_ids.append(node.node_id)
        # Three vocabularies can occur naturally in one coherent transform
        # (for example: "clean extracted data for model input").  Four or more
        # distinct lifecycle vocabularies is a much stronger signal that a
        # primitive has collapsed modelling, coding, execution and reporting.
        if _phase_count(node.objective) >= 4:
            result.oversized_nodes.append(node.node_id)
            result.problem_node_ids.append(node.node_id)
            result.revision_instructions.append(
                f"将 {node.node_id} 拆成文档/分析/建模/代码/执行/验证等独立阶段，"
                "每个子节点只保留一个主要目标"
            )
        scope_issue = _primitive_scope_issue(node)
        if scope_issue is not None:
            issue_type, responsibilities = scope_issue
            result.oversized_nodes.append(node.node_id)
            result.problem_node_ids.append(node.node_id)
            if issue_type == "math_modeling":
                result.revision_instructions.append(
                    f"将 {node.node_id} 从 primitive 转为 compound 并定向细化；"
                    "当前节点同时包含多个可独立验收的建模职责："
                    + ", ".join(responsibilities)
                    + "。子节点必须具有不同输入、逻辑输出或验收责任"
                )
            else:
                result.revision_instructions.append(
                    f"将 {node.node_id} 从 primitive 转为 compound 并定向细化；"
                    "当前 code 节点跨越了："
                    + ", ".join(responsibilities)
                    + "。算法/求解策略由上游非 code 子节点产出，唯一 code primitive "
                    "只实现既定规格，业务结果验证由下游治理流程负责"
                )
        lower = node.objective.lower()
        if any(word in lower for word in _PSEUDO_WORDS) and node.parent_node_id:
            siblings = [item for item in children.get(node.parent_node_id, []) if item != node.node_id]
            if siblings and all(by_id[item].expected_output == node.expected_output for item in siblings):
                result.invalid_decompositions.append(
                    f"节点 {node.node_id} 疑似只是父节点的伪分解"
                )
        for input_ref in node.required_inputs:
            if input_ref.source_type == "node_output":
                if not input_ref.producer_node_id or input_ref.producer_node_id not in by_id:
                    result.dependency_errors.append(
                        f"节点 {node.node_id} 的 InputRef producer 不存在: {input_ref.producer_node_id}"
                    )
                elif input_ref.producer_node_id not in node.dependencies:
                    result.dependency_errors.append(
                        f"节点 {node.node_id} 使用 {input_ref.producer_node_id} 输出但未声明依赖"
                    )

    produced: set[str] = set()
    for node in primitive:
        if node.expected_output:
            produced.add(node.expected_output.output_id)
        produced.update(node.output_artifacts)
    for deliverable in contract.graph_deliverables:
        if deliverable not in produced:
            result.missing_deliverables.append(deliverable)
            candidates = [
                node.node_id for node in primitive
                if node.primary_capability == "code"
            ] if deliverable.endswith((".py", ".json", ".csv", ".xlsx")) else []
            result.problem_node_ids.extend(candidates)
            result.revision_instructions.append(
                f"让负责生产 {deliverable} 的 primitive 在 output_artifacts 中明确声明该交付物"
            )

    forbidden_outputs = set(contract.postprocess_deliverables) | set(contract.framework_artifacts)
    for node in nodes:
        illegal = sorted(set(node.output_artifacts) & forbidden_outputs)
        if illegal:
            result.graph_errors.append(
                f"节点 {node.node_id} 声明了外层流程产物: {', '.join(illegal)}"
            )
            result.problem_node_ids.append(node.node_id)

    for requirement in contract.validation_requirements:
        owner = requirement.get("owner")
        if owner not in {"planner", "compiler", "outer_workflow"}:
            result.missing_validation_steps.append(
                f"验证要求 owner 不受支持: {owner}"
            )
            continue
        if owner in {"compiler", "outer_workflow"}:
            # These are deliberately checked by GraphCompiler/Workflow after
            # flattening; PlanValidator only verifies that the responsibility
            # is declared and points at a graph or postprocess deliverable.
            target = str(requirement.get("target_output", ""))
            declared = set(contract.graph_deliverables) | set(contract.postprocess_deliverables)
            if target not in declared:
                result.missing_validation_steps.append(
                    f"验证目标未在 PlanningContract 声明: {target}"
                )
            continue
        if owner != "planner":
            continue
        target = str(requirement.get("target_output", ""))
        owners = [node for node in primitive if (
            (node.expected_output and node.expected_output.output_id == target)
            or target in node.output_artifacts
        )]
        if not owners:
            result.missing_validation_steps.append(
                f"验证目标 {target} 没有生产节点"
            )
            continue
        validator_type = str(requirement.get("validator_type", "")).lower()
        if not any(
            target in str(criteria)
            or validator_type in str(criteria).lower()
            or any(word in str(criteria).lower() for word in ("validate", "verify", "schema", "quality"))
            for node in primitive
            for criteria in node.acceptance_criteria
        ):
            result.missing_validation_steps.append(
                f"验证目标 {target} 缺少规划级验收步骤"
            )

    if authoritative_goal:
        goal = plan.global_goal.strip().lower()
        authority = authoritative_goal.strip().lower()
        filename_only = bool(re.fullmatch(r"[^/\\]+\.(docx|xlsx|pdf|txt|csv)", plan.global_goal.strip(), re.I))
        if filename_only or (goal and goal != authority and len(goal) < 12):
            result.goal_errors.append("PlanIR.global_goal 不能使用附件文件名或过短目标")

    result.oversized_nodes = list(dict.fromkeys(result.oversized_nodes))
    result.problem_node_ids = list(dict.fromkeys(result.problem_node_ids))
    result.passed = result.error_count == 0
    if not result.passed and not result.revision_instructions:
        result.revision_instructions.append("仅修改问题节点及其局部依赖，不要重新生成整张计划")
    return result
