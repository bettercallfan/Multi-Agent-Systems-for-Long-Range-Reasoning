"""Verifier-guided hierarchical planning frontend.

This module owns planning only.  It produces the existing SemanticGraph and
leaves execution, routing, and runtime recovery to the current framework.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from agents.planning_agent import create_planning_agent
from orchestration.core.model_calls import run_agent
from orchestration.graph.semantic_graph import SemanticGraph
from orchestration.planning.plan_format_adapter import adapt_plan_ir, adapt_plan_patch
from orchestration.planning.plan_flattener import flatten_plan_ir_to_semantic_graph
from orchestration.planning.plan_ir import (
    PlanIR,
    PlanNode,
    PlanNodeType,
    PlanPatch,
    PlanValidationResult,
    PlanningContract,
    PlanningPolicy,
)
from orchestration.planning.plan_validator import validate_plan_ir


class PlanningValidationError(RuntimeError):
    """Raised when hierarchical planning cannot produce a verified plan."""

    def __init__(self, message: str, validation: PlanValidationResult | None = None):
        super().__init__(message)
        self.validation = validation


def build_planning_contract(task_spec: dict[str, Any]) -> PlanningContract:
    explicit = task_spec.get("planning_contract")
    if isinstance(explicit, dict):
        payload = dict(explicit)
        payload.setdefault(
            "requirement_contract",
            dict(task_spec.get("requirement_contract") or {}),
        )
        return PlanningContract.model_validate(payload)
    artifact_contract = task_spec.get("artifact_contract", {})
    return PlanningContract(
        graph_deliverables=list(artifact_contract.get("intermediate_artifacts", [])),
        postprocess_deliverables=list(artifact_contract.get("final_artifacts", [])),
        framework_artifacts=list(artifact_contract.get("framework_artifacts", [])),
        requirement_contract=dict(task_spec.get("requirement_contract") or {}),
    )


def resolve_planning_policy(task_spec: dict[str, Any], task_understanding: dict | None = None) -> tuple[PlanningPolicy, int]:
    raw = task_spec.get("planning_policy", {})
    policy = PlanningPolicy.model_validate(raw if isinstance(raw, dict) else {})
    if policy.mode != "auto":
        return policy, 0
    if not policy.hierarchical_validation_enabled:
        return policy, 0
    score = 0
    task_type = str(task_spec.get("task_type", "")).lower()
    if task_type in {"math_modeling", "software_engineering", "complex_research"}:
        score += 2
    if task_spec.get("code_policy", {}).get("mode", "none") != "none":
        score += 2
    contract = build_planning_contract(task_spec)
    if len(contract.graph_deliverables) >= 2:
        score += 1
    if len(task_spec.get("input", {}).get("files", [])) >= 2:
        score += 1
    recommended = (task_understanding or {}).get("recommended_capabilities", [])
    if len(set(recommended)) >= 3:
        score += 1
    constraints = (task_understanding or {}).get("constraints", [])
    if len(constraints) >= 5:
        score += 1
    if task_spec.get("requirements", {}).get("must_review_artifacts"):
        score += 1
    return policy, score


def _goal(task_spec: dict[str, Any], task_understanding: dict | None) -> str:
    understanding = task_understanding or {}
    summary = str(understanding.get("summary") or "").strip()
    if summary:
        return summary
    explicit = str(task_spec.get("task_goal") or "").strip()
    if explicit:
        return explicit
    text = str(task_spec.get("input", {}).get("text") or "").strip()
    return text or "完成 TaskSpec 定义的任务"


def _planning_input_view(task_spec: dict[str, Any]) -> dict[str, Any]:
    task_input = task_spec.get("input", {})
    files = [Path(str(path)).name for path in task_input.get("files", [])]
    if len(files) <= 50:
        file_view: Any = files
    else:
        counts = Counter(Path(name).suffix.lower() or "<none>" for name in files)
        representatives = []
        seen = set()
        for name in files:
            suffix = Path(name).suffix.lower() or "<none>"
            if counts[suffix] <= 10 or suffix not in seen:
                representatives.append(name)
                seen.add(suffix)
        file_view = [{
            "manifest": "large_input_collection",
            "total_file_count": len(files),
            "counts_by_suffix": dict(sorted(counts.items())),
            "representative_files": representatives[:20],
        }]
    return {"type": task_input.get("type"), "files": file_view,
            "text": task_input.get("text", "")}


def _plan_prompt(
    task_spec: dict[str, Any],
    contract: PlanningContract,
    available_capabilities: list[str],
    goal: str,
    task_understanding: dict | None,
    policy: PlanningPolicy,
) -> str:
    return f"""
你是 Verifier-Guided Hierarchical Planning Frontend。请只生成层级 PlanIR，不要生成
SemanticGraph、TaskGraph、执行命令、Agent ID 或运行时状态。

权威全局目标：{goal}
TaskSpec 摘要：
{json.dumps({
    'task_type': task_spec.get('task_type'),
    'code_policy': task_spec.get('code_policy'),
    'input': _planning_input_view(task_spec),
    'success_criteria': task_spec.get('success_criteria'),
    'stage_scope': task_spec.get('stage_scope'),
    'completed_stage_context': task_spec.get('completed_stage_context'),
}, ensure_ascii=False, indent=2)}
图内交付物（必须被业务节点明确生产）：
{json.dumps(contract.graph_deliverables, ensure_ascii=False)}
后处理交付物（不要生成节点）：
{json.dumps(contract.postprocess_deliverables, ensure_ascii=False)}
框架产物（不要生成节点）：
{json.dumps(contract.framework_artifacts, ensure_ascii=False)}
可用能力：{json.dumps(available_capabilities, ensure_ascii=False)}
任务理解摘要：{json.dumps(task_understanding or {}, ensure_ascii=False, indent=2)}
冻结需求合同（当前阶段每条 owner=planner 的 mandatory 可执行需求必须被 primitive 的
requirement_ids 覆盖；outer_workflow 要求不要生成业务节点）：
{json.dumps(contract.requirement_contract, ensure_ascii=False, indent=2)}

限制：最大深度 {policy.max_decomposition_depth}，最大节点 {policy.max_plan_nodes}。
compound 只能表示需要细化的复合任务，primitive 必须只有一个主要目标、一个逻辑输出、
明确输入和可检查验收条件。模型、代码、运行、验证、报告是不同生命周期，不能合并为一个
primitive。不要生成 report、finish、task_spec.json、run_state.json 等外层节点。
primitive 表示一次独立状态转换：一个执行者消费明确输入，产生一个可独立验收的逻辑结果。
如果一个节点的验收条件包含多个可以分别失败、分别验证的成果，该节点必须是 compound。
每个 primitive 必须在 requirement_ids 中列出它实际覆盖的 requirement_id，并在
acceptance_refs 中列出对应的 blocking criterion_id；不能声称覆盖未生产的要求。
例如决策变量、目标函数、多类约束、建模假设、求解策略和模型验证属于可独立验收的职责，
不得把其中多个职责压缩成一个 math_modeling primitive。
只能使用上面“可用能力”中列出的 capability。不要生成 terminal_execution、test_execution、
artifact_validation、evidence_verification 节点，它们由 GraphCompiler 自动插入。不要生成
final_report.md、normalized_input.json 或任何 framework_artifacts 的生产节点。
如果 code_policy 需要代码，必须且只能有一个 primary_capability="code" 的 primitive；该节点
用一个逻辑输出表示“可执行求解包”，并在 output_artifacts 中同时声明代码入口和计算结果。
“只能有一个 code primitive”不表示算法设计、代码实现、求解和业务验证都应压入该节点。
算法或求解策略以及输入输出契约必须由上游非 code primitive 明确产出；唯一 code primitive
只负责将既定规格实现为现有执行器可运行的代码包并产生结构化结果。代码的真实执行、重试
和修复是现有执行器内部机制，业务结果验证由下游框架治理流程负责。
如果 code_policy.mode="none" 或可用能力中没有 code，绝对不能生成 code primitive；当前阶段
只产生结构化逻辑结果，不得为了保存中间模型而虚构 JSON/Markdown 物理文件。后续阶段通过
completed_stage_context 和 node_output 消费这些结果。
completed_stage_context 中的 node_id 属于已经完成的旧子图，不属于当前 PlanIR：不得把这些
ID 写入 dependencies、edges 或 InputRef.producer_node_id。它们是框架注入的只读阶段输入；
需要时使用 source_type="user_input"、name="completed_stage_context"、producer_node_id=null，
阶段合并器会在编译后添加真实跨阶段依赖。
compound 只负责层级分组，不得直接声明 output_artifacts；运行/求解不要另建治理节点。
每个 compound 至少包含两个实质子节点，且 compound 不得声明任何物理产物。
compound 不得承担 requirement_ids、acceptance_refs 或 acceptance_criteria；这些验收责任必须
落到实际执行的 primitive，且每个 primitive 的 acceptance_criteria 至少有一项。
代码任务的 code primitive 必须是唯一的代码产物所有者，并同时声明
artifacts/code_pipeline.py 与 artifacts/result.json；不要把这两个产物转移给 calculation、
analysis 或其他非 code 节点。不要额外创建“运行代码”“生成结果”“结果交付”业务节点，
也不要创建 framework_execution、validate_outputs 或其他执行/验证治理节点；
真实执行、重试、结果文件落盘由现有 code executor 和 GraphCompiler 治理节点完成。

required_inputs 的每一项必须是对象，禁止使用裸字符串。仅允许以下三种形状：
{{"source_type":"user_input","name":"附件路径","producer_node_id":null,"fields":[]}}
{{"source_type":"artifact","name":"normalized_input.json","producer_node_id":null,"fields":["items"]}}
{{"source_type":"node_output","name":"model_spec","producer_node_id":"build_model","fields":["variables"]}}
edges 必须是二元数组，例如 [["extract_data","build_model"]]；不要输出 from_node、to_node、
relationship 边对象。父子层级只用 parent_node_id 表达，不要放进 edges。
parent_node_id 只是层级元数据：绝对不要把父节点写入子节点的 dependencies，也不要把
compound→child 的父子包含关系写入 edges。dependencies 和 edges 只能表示真实的数据生产/
消费关系；一个节点只有在实际消费另一个节点的结构化输出时才声明依赖。
dependencies 必须始终是节点 ID 字符串数组，例如
["extract_data","build_model"]；禁止把 Python 列表序列化成一个字符串（例如
"['extract_data','build_model']"），也禁止把整个依赖列表嵌套成数组元素。
如果上游 expected_output 没有 schema_ref，required_inputs.fields 必须为空数组；不要猜测
variables、constraints 等字段名。框架会把无 schema 的直接依赖作为完整结构化结果传递。

严格返回 JSON：
{{
  "global_goal": "{goal}",
  "deliverables": {json.dumps(contract.graph_deliverables, ensure_ascii=False)},
  "version": 1,
  "nodes": [
    {{
      "node_id": "唯一ID",
      "objective": "一个主要目标",
      "node_type": "compound 或 primitive",
      "parent_node_id": null,
      "decomposition_depth": 0,
      "dependencies": [],
      "primary_capability": "从可用能力中选择",
      "supporting_capabilities": [],
      "required_inputs": [],
      "expected_output": null,
      "output_artifacts": [],
      "requirement_ids": [],
      "acceptance_refs": [],
      "acceptance_criteria": [{{"type":"明确可检查条件"}}]
    }}
  ],
  "edges": []
}}
""".strip()


def _refine_prompt(
    plan: PlanIR,
    validation: PlanValidationResult,
    target_node_ids: list[str],
    contract: PlanningContract,
    policy: PlanningPolicy,
    available_capabilities: list[str],
) -> str:
    selected = [node.model_dump(mode="json") for node in plan.nodes if node.node_id in target_node_ids]
    existing_code_nodes = [
        node.node_id for node in plan.nodes
        if node.node_type == PlanNodeType.PRIMITIVE and node.primary_capability == "code"
    ]
    return f"""
请只修订当前 PlanIR 中的问题节点，不要重新生成整张计划，不要输出运行时状态。
权威目标：{plan.global_goal}
图内交付物：{json.dumps(contract.graph_deliverables, ensure_ascii=False)}
最大新增节点数：{policy.max_new_nodes_per_refinement}
允许使用的业务能力：{json.dumps(available_capabilities, ensure_ascii=False)}
需求合同：{json.dumps(contract.requirement_contract, ensure_ascii=False, indent=2)}
剩余深度：{policy.max_decomposition_depth}
问题节点：{json.dumps(selected, ensure_ascii=False, indent=2)}
结构化验证错误：{validation.model_dump_json(indent=2)}

返回一个局部 PlanPatch：target_node_id 为待替换节点；replacement_nodes 只包含该节点和
新增子节点；replacement_edges 只包含这些节点之间以及必要的局部依赖。每个 primitive 只能
有一个主要目标、一个逻辑输出和明确 acceptance_criteria。不要用“进一步/综合/全面/最终”
等措辞伪造分解。不得使用 terminal_execution、test_execution、artifact_validation、
evidence_verification 或 report_generation；这些阶段由现有外层框架负责。
被验证器判定过粗的 primitive 必须转换为 compound，并用具有不同输入、逻辑输出或验收责任
的实质子节点替换；不得只改写名称。math_modeling 的变量、目标、约束、假设、求解策略与
模型验证应按实际独立职责细化，而不是重新合并为“完整模型”。如果允许使用的业务能力不含
code，本轮不得新增或保留 code primitive；如果允许 code 且当前阶段要求代码，则全图必须且
只能保留一个 code primitive：算法/求解策略由其上游非 code 子节点产出，code 子节点只实现
既定规格；执行重试由现有执行器内部完成，业务验证仍由外层治理流程完成。
本次只修订“问题节点”字段中指定的一个 target_node_id，不要替换其他节点。严格返回：
{{"target_node_id":"问题节点ID","replacement_nodes":[...],"replacement_edges":[["节点A","节点B"]]}}。
replacement_edges 必须是二元字符串数组，禁止使用 from_node_id、to_node_id、relationship
或其他边对象形式。不要把 artifacts/code_pipeline.py 或 artifacts/result.json 分配给
非 code 节点。replacement_nodes 的 parent_node_id 只表达包含关系；任何子节点都不得把
自己的 parent_node_id 或其他 compound 祖先写入 dependencies，dependencies 只能指向真实
数据生产者。dependencies 必须是节点 ID 字符串数组，不能是字符串化列表或嵌套列表。
dependencies、replacement_edges 和 InputRef.producer_node_id 只能引用当前 PlanIR 或本次
replacement_nodes 中存在的节点；不得引用 completed_stage_context 中的旧阶段 node_id。
compound 必须至少保留两个实质子节点；compound 自身不得持有 requirement_ids、
acceptance_refs 或 acceptance_criteria，必须分配给带有非空 acceptance_criteria 的 primitive。
当前计划中已有的 code primitive：{json.dumps(existing_code_nodes, ensure_ascii=False)}。
除非问题节点本身就是其中唯一的 code primitive，否则 replacement_nodes 不得新增 code
primitive；全图始终只能保留一个 code primitive。若本次问题节点就是过粗的 code primitive，
必须将 target_node_id 作为 compound，并只建立一个非 code 的“既定算法/输入输出规格”
子节点和一个 code 实现子节点；code 子节点必须继承并唯一声明
artifacts/code_pipeline.py、artifacts/result.json，非 code 子节点不得声明这两个产物。
compound 的 primary_capability 和所有子节点 capability 必须来自允许使用的业务能力列表，
禁止使用 management、coordination 等未列出的能力。
replacement primitive 必须保留或重新分配 requirement_ids 与 acceptance_refs，不能让已覆盖
的 mandatory requirement 在局部修订后消失。
""".strip()


def _merge_patch(plan: PlanIR, patch: PlanPatch) -> PlanIR:
    replacements = {node.node_id: node for node in patch.replacement_nodes}
    if patch.target_node_id not in replacements:
        original = next(
            (node for node in plan.nodes if node.node_id == patch.target_node_id), None,
        )
        if original is None or not replacements:
            raise ValueError("PlanPatch 目标不存在或 replacement_nodes 为空")
        # Models often return only the refined children.  Preserve the target
        # ID as a compound boundary so external predecessors/successors remain
        # stable while the new children become its executable leaves.
        for node in replacements.values():
            if not node.parent_node_id:
                node.parent_node_id = patch.target_node_id
                node.decomposition_depth = original.decomposition_depth + 1
        replacements[patch.target_node_id] = PlanNode(
            node_id=original.node_id,
            objective=original.objective,
            node_type=PlanNodeType.COMPOUND,
            parent_node_id=original.parent_node_id,
            decomposition_depth=original.decomposition_depth,
            dependencies=list(original.dependencies),
            primary_capability=original.primary_capability,
            supporting_capabilities=list(original.supporting_capabilities),
            required_inputs=list(original.required_inputs),
            expected_output=None,
            output_artifacts=[],
            acceptance_criteria=[],
            metadata={**original.metadata, "refined_from_primitive": True},
        )
    replacement_root = replacements[patch.target_node_id]
    if replacement_root.node_type == PlanNodeType.COMPOUND:
        # replacement_nodes is defined as the rejected target plus its local
        # children.  Restore omitted containment metadata without inventing
        # business nodes or data dependencies.
        for replacement in replacements.values():
            if (
                replacement.node_id != patch.target_node_id
                and replacement.parent_node_id is None
            ):
                replacement.parent_node_id = patch.target_node_id
                replacement.decomposition_depth = max(
                    replacement.decomposition_depth,
                    replacement_root.decomposition_depth + 1,
                )
    # Local refinement occasionally returns semantically useful primitive
    # children with a typed expected_output but omits their node-local check.
    # Do not spend another model round repairing this structural omission.
    # A non-empty logical-output check only validates the intermediate state
    # transition; it never replaces RequirementAcceptance or final business
    # validation.
    for replacement in replacements.values():
        if (
            replacement.node_type == PlanNodeType.PRIMITIVE
            and replacement.expected_output is not None
            and not replacement.acceptance_criteria
        ):
            replacement.acceptance_criteria = [{
                "type": "logical_output_present",
                "target": f"node:{replacement.node_id}",
                "condition": (
                    f"逻辑输出 {replacement.expected_output.output_id} "
                    "必须存在且非空"
                ),
                "params": {
                    "output_id": replacement.expected_output.output_id,
                },
                "severity": "blocking",
                "framework_generated": True,
            }]
            replacement.metadata["framework_local_acceptance_repaired"] = True
    def is_descendant(node_id: str, ancestor_id: str) -> bool:
        current = next((item for item in plan.nodes if item.node_id == node_id), None)
        seen: set[str] = set()
        while current and current.parent_node_id and current.parent_node_id not in seen:
            if current.parent_node_id == ancestor_id:
                return True
            seen.add(current.parent_node_id)
            current = next(
                (item for item in plan.nodes if item.node_id == current.parent_node_id),
                None,
            )
        return False

    # Refining an existing compound replaces its old local subtree. Keeping
    # stale children alongside the replacement would create duplicate leaves
    # and could reintroduce cycles during deterministic flattening.
    obsolete_descendants = {
        node.node_id for node in plan.nodes
        if node.node_id not in replacements
        and is_descendant(node.node_id, patch.target_node_id)
    }
    # A model patch can echo dependencies on children from the subtree it is
    # replacing.  Those children are about to disappear; retaining the
    # references manufactures dangling edges and wastes the next refinement
    # round on merge debris rather than on plan quality.
    for replacement in replacements.values():
        replacement.dependencies = [
            dependency
            for dependency in replacement.dependencies
            if dependency not in obsolete_descendants
        ]
    replaced_ids = set(replacements) | obsolete_descendants
    nodes = [
        replacements.get(node.node_id, node)
        for node in plan.nodes
        if node.node_id not in replaced_ids
    ]
    nodes.extend(replacements.values())
    old_edges = set(plan.edges) | {
        (dep, node.node_id) for node in plan.nodes for dep in node.dependencies
    }
    known_old_ids = {node.node_id for node in plan.nodes}
    # Preserve the target's external boundary.  A local refinement is not
    # allowed to silently disconnect the rest of the plan merely because the
    # replacement prompt only contains the problematic subtree.
    incoming = {
        (source, patch.target_node_id)
        for source, target in old_edges
        if target in replaced_ids and source not in replaced_ids
        and source in known_old_ids
    }
    outgoing = {
        (patch.target_node_id, target)
        for source, target in old_edges
        if source in replaced_ids and target not in replaced_ids
        and target in known_old_ids
    }
    edges = {
        edge for edge in old_edges
        if edge[0] not in replaced_ids and edge[1] not in replaced_ids
    }
    edges.update(incoming)
    edges.update(outgoing)
    edges.update(patch.replacement_edges)
    # Replacement nodes' dependencies are authoritative for their local edges.
    edges.update((dep, node.node_id) for node in replacements.values() for dep in node.dependencies)
    return PlanIR(
        global_goal=plan.global_goal,
        deliverables=list(plan.deliverables),
        nodes=nodes,
        edges=sorted(edges),
        version=plan.version,
    )


def _ensure_parent_compounds(plan: PlanIR) -> tuple[PlanIR, list[str]]:
    """Create deterministic compound shells for model-emitted orphan children.

    Planning models occasionally emit the children of a hierarchy while
    omitting the corresponding compound objects.  This is a structural
    normalization, not a task-specific decomposition: the shell only
    restores the declared containment boundary and leaves refinement to the
    validator/agent.
    """
    by_id = {node.node_id: node for node in plan.nodes}
    missing: list[str] = []
    for node in plan.nodes:
        parent = node.parent_node_id
        if parent and parent not in by_id and parent not in missing:
            missing.append(parent)
    if not missing:
        return _normalize_containment_dependencies(plan), []

    children_by_parent: dict[str, list[PlanNode]] = defaultdict(list)
    for node in plan.nodes:
        if node.parent_node_id in missing:
            children_by_parent[node.parent_node_id].append(node)
    shells: list[PlanNode] = []
    for parent_id in missing:
        children = children_by_parent[parent_id]
        capability = children[0].primary_capability if children else "reasoning"
        dependencies = sorted({
            dependency
            for child in children
            for dependency in child.dependencies
            if dependency != parent_id
            and dependency not in {item.node_id for item in children}
        })
        shells.append(PlanNode(
            node_id=parent_id,
            objective=f"组织并验收阶段：{parent_id.replace('_', ' ')}",
            node_type=PlanNodeType.COMPOUND,
            decomposition_depth=max(
                0, min((child.decomposition_depth for child in children), default=1) - 1
            ),
            dependencies=dependencies,
            primary_capability=capability,
            metadata={"framework_repaired_parent": True},
        ))
        for child in children:
            child.decomposition_depth = max(child.decomposition_depth, 1)
    return _normalize_containment_dependencies(
        plan.model_copy(update={"nodes": plan.nodes + shells})
    ), missing


def _normalize_containment_dependencies(plan: PlanIR) -> PlanIR:
    """Remove parent-containment links from data dependencies and graph edges."""
    by_id = {node.node_id: node for node in plan.nodes}
    ancestors: dict[str, set[str]] = {}
    for node in plan.nodes:
        lineage: set[str] = set()
        parent_id = node.parent_node_id
        while parent_id and parent_id in by_id and parent_id not in lineage:
            lineage.add(parent_id)
            parent_id = by_id[parent_id].parent_node_id
        ancestors[node.node_id] = lineage
    def hierarchy_pair(left: str, right: str) -> bool:
        """Return true when either endpoint contains the other."""

        return (
            left in ancestors.get(right, set())
            or right in ancestors.get(left, set())
        )

    for node in plan.nodes:
        node.dependencies = [
            dependency for dependency in node.dependencies
            if not hierarchy_pair(dependency, node.node_id)
        ]
        node.required_inputs = [
            item for item in node.required_inputs
            if item.producer_node_id is None
            or not hierarchy_pair(str(item.producer_node_id), node.node_id)
        ]
    edges = {
        edge for edge in (set(plan.edges) | {
            (dependency, node.node_id)
            for node in plan.nodes for dependency in node.dependencies
        })
        if not hierarchy_pair(edge[0], edge[1])
    }
    return plan.model_copy(update={"edges": sorted(edges)})


def _normalize_external_stage_references(
    plan: PlanIR,
    task_spec: dict[str, Any],
) -> PlanIR:
    """Remove old-stage IDs from a local PlanIR before deterministic bridging."""

    external_ids = {
        str(item.get("node_id"))
        for item in task_spec.get("completed_stage_context", []) or []
        if item.get("node_id")
    }
    if not external_ids:
        return plan
    known_ids = {node.node_id for node in plan.nodes}
    for node in plan.nodes:
        # Model-authored aliases such as ``input_normalization`` cannot be
        # valid local producers.  In a staged plan the framework already
        # supplies a bounded completed_stage_context and later bridges local
        # roots to the real previous leaves, so normalize both exact old IDs
        # and unknown external aliases to that trusted boundary.
        unknown_ids = {
            dependency for dependency in node.dependencies
            if dependency not in known_ids
        }
        removed = sorted(
            (set(node.dependencies) & external_ids) | unknown_ids
        )
        node.dependencies = [
            dependency for dependency in node.dependencies
            if dependency not in set(removed)
        ]
        removed_input_producers = {
            str(item.producer_node_id)
            for item in node.required_inputs
            if item.producer_node_id in external_ids
            or (
                item.producer_node_id is not None
                and item.producer_node_id not in known_ids
            )
        }
        node.required_inputs = [
            item for item in node.required_inputs
            if str(item.producer_node_id) not in removed_input_producers
        ]
        all_removed = sorted(set(removed) | removed_input_producers)
        if all_removed:
            node.metadata["framework_external_stage_inputs"] = all_removed
    return plan.model_copy(update={
        "edges": [
            edge for edge in plan.edges
            if edge[0] in known_ids and edge[1] in known_ids
        ],
    })


def _normalize_acceptance_ownership(
    plan: PlanIR,
    contract: PlanningContract,
) -> PlanIR:
    """Keep frozen acceptance duties on executable primitives only."""

    requirement_payload = contract.requirement_contract or {}
    criteria_by_id = {
        str(criterion.get("criterion_id")): dict(criterion)
        for requirement in requirement_payload.get("requirements", [])
        for criterion in requirement.get("acceptance_criteria", [])
        if criterion.get("criterion_id")
    }
    for node in plan.nodes:
        if node.node_type == PlanNodeType.COMPOUND:
            node.requirement_ids = []
            node.acceptance_refs = []
            node.acceptance_criteria = []
    primitive_owners: dict[str, list[PlanNode]] = defaultdict(list)
    requirement_criteria: dict[str, list[str]] = defaultdict(list)
    for requirement in requirement_payload.get("requirements", []):
        for criterion in requirement.get("acceptance_criteria", []):
            if criterion.get("severity", "blocking") == "blocking":
                requirement_criteria[str(requirement.get("requirement_id"))].append(
                    str(criterion.get("criterion_id"))
                )
    for node in plan.nodes:
        if node.node_type == PlanNodeType.PRIMITIVE:
            for requirement_id in node.requirement_ids:
                primitive_owners[requirement_id].append(node)
    # When exactly one primitive owns a requirement, there is no ambiguity:
    # bind every frozen blocking criterion to that primitive. This repairs
    # harmless LLM omissions without inventing acceptance semantics.
    for requirement_id, owners in primitive_owners.items():
        if len(owners) == 1:
            owner = owners[0]
            owner.acceptance_refs = list(dict.fromkeys([
                *owner.acceptance_refs,
                *requirement_criteria.get(requirement_id, []),
            ]))
        elif owners:
            criterion_ids = set(requirement_criteria.get(requirement_id, []))
            explicit = [
                owner for owner in owners
                if criterion_ids & set(owner.acceptance_refs)
            ]
            code_owners = [
                owner for owner in owners
                if owner.primary_capability == "code"
            ]
            artifact_owners = [
                owner for owner in owners
                if any(
                    str(criteria_by_id[criterion_id].get("target", ""))
                    in owner.output_artifacts
                    for criterion_id in criterion_ids
                    if criterion_id in criteria_by_id
                )
            ]
            # Requirement ownership must be unambiguous for repair routing.
            # Prefer an explicit mapping, then the sole physical code writer,
            # then an artifact owner.  The final fallback is deterministic and
            # only resolves duplicate claims; it does not invent criteria.
            owner = (explicit or code_owners or artifact_owners or owners)[-1]
            owner.acceptance_refs = list(dict.fromkeys([
                *owner.acceptance_refs,
                *requirement_criteria.get(requirement_id, []),
            ]))
    covered_with_refs = {
        requirement_id
        for node in plan.nodes if node.node_type == PlanNodeType.PRIMITIVE
        and node.acceptance_refs
        for requirement_id in node.requirement_ids
    }
    for node in plan.nodes:
        if node.node_type != PlanNodeType.PRIMITIVE:
            continue
        existing_ids = {
            str(item.get("criterion_id"))
            for item in node.acceptance_criteria if item.get("criterion_id")
        }
        node.acceptance_criteria.extend(
            criteria_by_id[criterion_id]
            for criterion_id in node.acceptance_refs
            if criterion_id in criteria_by_id and criterion_id not in existing_ids
        )
        if not node.acceptance_refs:
            node.requirement_ids = [
                requirement_id for requirement_id in node.requirement_ids
                if requirement_id not in covered_with_refs
            ]
    return plan


def _collapse_single_child_compounds(plan: PlanIR) -> PlanIR:
    """Remove meaningless one-child wrappers without inventing new work."""

    nodes = list(plan.nodes)
    edges = set(plan.edges)
    changed = True
    while changed:
        changed = False
        by_id = {node.node_id: node for node in nodes}
        children: dict[str, list[PlanNode]] = defaultdict(list)
        for node in nodes:
            if node.parent_node_id in by_id:
                children[node.parent_node_id].append(node)
        for compound in list(nodes):
            if compound.node_type != PlanNodeType.COMPOUND:
                continue
            direct = children.get(compound.node_id, [])
            if not direct:
                referenced_as_output = any(
                    item.producer_node_id == compound.node_id
                    for node in nodes if node.node_id != compound.node_id
                    for item in node.required_inputs
                )
                if referenced_as_output:
                    # A consumer still expects this logical output, so this is
                    # a real missing decomposition rather than an obsolete
                    # shell.  Keep it visible to PlanValidator.
                    continue
                # A compound is containment metadata, never an executable
                # producer.  Local refinement can move its children to real
                # primitives and leave the old shell behind.  Rewire any data
                # boundary to the shell's own dependencies, then remove this
                # semantic no-op instead of spending another model round.
                for node in nodes:
                    if node.node_id == compound.node_id:
                        continue
                    rewritten_dependencies: list[str] = []
                    for dependency in node.dependencies:
                        if dependency == compound.node_id:
                            rewritten_dependencies.extend(compound.dependencies)
                        else:
                            rewritten_dependencies.append(dependency)
                    node.dependencies = list(dict.fromkeys(
                        rewritten_dependencies
                    ))
                incoming = {
                    source for source, target in edges
                    if target == compound.node_id
                } | set(compound.dependencies)
                outgoing = {
                    target for source, target in edges
                    if source == compound.node_id
                }
                edges = {
                    (source, target) for source, target in edges
                    if source != compound.node_id and target != compound.node_id
                }
                edges.update(
                    (source, target)
                    for source in incoming for target in outgoing
                    if source != target
                )
                nodes.remove(compound)
                changed = True
                break
            if len(direct) != 1:
                continue
            child = direct[0]
            child.parent_node_id = compound.parent_node_id
            child.decomposition_depth = compound.decomposition_depth
            child.dependencies = list(dict.fromkeys([
                *compound.dependencies,
                *[
                    dependency for dependency in child.dependencies
                    if dependency != compound.node_id
                ],
            ]))
            for node in nodes:
                if node.node_id in {compound.node_id, child.node_id}:
                    continue
                node.dependencies = [
                    child.node_id if dependency == compound.node_id else dependency
                    for dependency in node.dependencies
                ]
            rewritten: set[tuple[str, str]] = set()
            for source, target in edges:
                source = child.node_id if source == compound.node_id else source
                target = child.node_id if target == compound.node_id else target
                if source != target:
                    rewritten.add((source, target))
            edges = rewritten
            nodes.remove(compound)
            changed = True
            break
    return _normalize_containment_dependencies(plan.model_copy(update={
        "nodes": nodes,
        "edges": sorted(edges),
    }))


def _normalize_stage_plan(
    plan: PlanIR,
    task_spec: dict[str, Any],
    contract: PlanningContract,
) -> PlanIR:
    plan = _normalize_containment_dependencies(plan)
    plan = _normalize_external_stage_references(plan, task_spec)
    plan = _normalize_acceptance_ownership(plan, contract)
    return _collapse_single_child_compounds(plan)


async def build_hierarchical_semantic_graph(
    *,
    task_spec: dict[str, Any],
    available_capabilities: list[str],
    model_client: Any,
    run_dir: str | Path,
    trace: list,
    run_state: Any,
    task_understanding: dict | None = None,
    extra_context: str = "",
    planning_subdir: str = "planning",
) -> tuple[SemanticGraph, dict]:
    policy, complexity_score = resolve_planning_policy(task_spec, task_understanding)
    contract = build_planning_contract(task_spec)
    goal = _goal(task_spec, task_understanding)
    governance_capabilities = {
        "terminal_execution", "test_execution", "artifact_validation",
        "evidence_verification",
    }
    planning_capabilities = [
        capability for capability in available_capabilities
        if capability not in governance_capabilities
    ]
    if policy.mode == "legacy" or (policy.mode == "auto" and complexity_score < 4):
        raise PlanningValidationError("当前任务策略选择 legacy，不应调用 hierarchical frontend")
    if not policy.hierarchical_validation_enabled:
        raise PlanningValidationError("hierarchical_validation_enabled=false")
    run_path = Path(run_dir)
    relative_plan_dir = Path(planning_subdir)
    if relative_plan_dir.is_absolute() or ".." in relative_plan_dir.parts:
        raise ValueError("planning_subdir 必须位于 run_dir 内")
    plan_dir = run_path / relative_plan_dir
    plan_dir.mkdir(parents=True, exist_ok=True)
    metrics = {
        "planning_mode": "hierarchical",
        "complexity_score": complexity_score,
        "planning_profile": "deep" if policy.max_plan_nodes > 32 else "standard",
        "initial_plan_node_count": 0,
        "initial_compound_node_count": 0,
        "initial_primitive_node_count": 0,
        "validation_rounds": 0,
        "refined_node_count": 0,
        "final_primitive_node_count": 0,
        "max_decomposition_depth": 0,
        "planning_token_usage": 0,
        "refinement_token_usage": 0,
        "fallback_used": False,
        "fallback_reason": "",
        "format_repair_count": 0,
        "graph_deliverable_coverage": 0.0,
        "primitive_output_coverage": 0.0,
        "primitive_acceptance_coverage": 0.0,
        "validation_requirement_coverage": 0.0,
        "dependency_error_count": 0,
        "invalid_decomposition_count": 0,
        "validation_closure_error_count": 0,
    }

    async def call(prompt: str, stage: str, budget: int) -> str:
        planner = create_planning_agent(
            model_client,
            extra_context=(extra_context + "\n当前输出为 PlanIR/PlanPatch，仅用于规划前端。"),
        )
        result = await run_agent(
            planner, prompt, trace, run_state=run_state, stage=stage,
            prompt_budget_tokens=budget,
        )
        return str(result)

    raw = await call(
        _plan_prompt(task_spec, contract, planning_capabilities, goal, task_understanding, policy),
        "plan",
        _planning_budget(task_spec),
    )
    raw_path = plan_dir / "plan_ir_raw_round_0.txt"
    raw_path.write_text(raw, encoding="utf-8")
    def artifact_ref(filename: str) -> str:
        return (relative_plan_dir / filename).as_posix()

    run_state.record_artifact(artifact_ref("plan_ir_raw_round_0.txt"))
    plan, format_report, normalised_payload = adapt_plan_ir(raw, task_spec)
    plan, repaired_parent_ids = _ensure_parent_compounds(plan)
    plan = _normalize_stage_plan(plan, task_spec, contract)
    metrics["format_repair_count"] += format_report.repair_count
    (plan_dir / "plan_ir_normalized_round_0.json").write_text(
        json.dumps(normalised_payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (plan_dir / "plan_format_report_round_0.json").write_text(
        format_report.model_dump_json(indent=2), encoding="utf-8",
    )
    run_state.record_artifact(artifact_ref("plan_ir_normalized_round_0.json"))
    run_state.record_artifact(artifact_ref("plan_format_report_round_0.json"))
    run_state.record_event("plan_ir_format_adapted", {
        "round": 0, "repair_count": format_report.repair_count,
        "repaired_parent_compounds": repaired_parent_ids,
    })
    metrics["initial_plan_node_count"] = len(plan.nodes)
    metrics["initial_compound_node_count"] = sum(node.node_type.value == "compound" for node in plan.nodes)
    metrics["initial_primitive_node_count"] = sum(node.node_type.value == "primitive" for node in plan.nodes)
    (plan_dir / "plan_ir_round_0.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    for round_number in range(policy.max_revision_rounds + 1):
        validation = validate_plan_ir(
            plan, contract, policy,
            authoritative_goal=goal,
            available_capabilities=set(planning_capabilities),
        )
        metrics["validation_rounds"] = round_number + 1
        (plan_dir / f"plan_validation_round_{round_number}.json").write_text(
            validation.model_dump_json(indent=2), encoding="utf-8",
        )
        run_state.record_event("plan_ir_validated", {
            "round": round_number,
            "passed": validation.passed,
            "problem_node_ids": validation.problem_node_ids,
            "error_count": validation.error_count,
        })
        primitive_count = max(1, len([n for n in plan.nodes if n.node_type.value == "primitive"]))
        produced = {
            item
            for node in plan.nodes
            if node.node_type.value == "primitive"
            for item in ([node.expected_output.output_id] if node.expected_output else []) + list(node.output_artifacts)
        }
        metrics["graph_deliverable_coverage"] = (
            len(set(contract.graph_deliverables) & produced) / max(1, len(set(contract.graph_deliverables)))
        )
        metrics["primitive_output_coverage"] = len([n for n in plan.nodes if n.node_type.value == "primitive" and n.expected_output]) / primitive_count
        metrics["primitive_acceptance_coverage"] = len([n for n in plan.nodes if n.node_type.value == "primitive" and n.acceptance_criteria]) / primitive_count
        metrics["validation_requirement_coverage"] = 0.0 if validation.missing_validation_steps else 1.0
        metrics["dependency_error_count"] = len(validation.dependency_errors)
        metrics["invalid_decomposition_count"] = len(validation.invalid_decompositions)
        metrics["validation_closure_error_count"] = len(validation.missing_validation_steps)
        if validation.passed:
            flattened = flatten_plan_ir_to_semantic_graph(
                plan, preserve_metadata=policy.preserve_hierarchy_metadata,
            )
            semantic = flattened.semantic_graph
            metrics["final_primitive_node_count"] = len(semantic.nodes)
            metrics["max_decomposition_depth"] = max((node.decomposition_depth for node in plan.nodes), default=0)
            (plan_dir / "plan_ir_final.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
            (plan_dir / "planning_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
            run_state.record_artifact(artifact_ref("plan_ir_final.json"))
            run_state.record_artifact(artifact_ref("planning_metrics.json"))
            return semantic, metrics
        if round_number >= policy.max_revision_rounds:
            message = "层级计划在最大修订轮次后仍未通过：" + "; ".join(validation.revision_instructions)
            run_state.record_event("plan_ir_failed", {"message": message, "validation": validation.model_dump(mode="json")})
            raise PlanningValidationError(message, validation)
        # High-confidence oversized primitives are the real semantic defect.
        # Repair them before structural parent shells or missing-deliverable
        # nodes, otherwise a round can be consumed by a harmless containment
        # repair while the coarse business node remains unchanged.
        target_ids = (
            validation.oversized_nodes
            or validation.problem_node_ids
            or [
            node.node_id for node in plan.nodes if node.node_type == "compound"
            ]
        )
        # PlanPatch has one target_node_id. Passing several rejected nodes in
        # one prompt lets the model choose an arbitrary target and weakens
        # local refinement. Handle one deterministic problem per round.
        target_ids = list(dict.fromkeys(target_ids))[:1]
        patch_raw = await call(
            _refine_prompt(
                plan, validation, target_ids, contract, policy,
                planning_capabilities,
            ),
            "plan_refinement",
            policy.max_refinement_prompt_tokens,
        )
        patch_raw_path = plan_dir / f"plan_patch_raw_round_{round_number + 1}.txt"
        patch_raw_path.write_text(patch_raw, encoding="utf-8")
        try:
            patch, patch_format_report, patch_payload = adapt_plan_patch(patch_raw, task_spec)
        except Exception as exc:
            # A malformed local patch must not abort the whole planning
            # frontend. Give the same target one bounded, schema-focused retry.
            repair_prompt = _refine_prompt(
                plan, validation, target_ids, contract, policy,
                planning_capabilities,
            ) + (
                "\n上一版 Patch 无法通过结构化解析，错误为："
                + str(exc)
                + "\n请只返回完整 JSON；replacement_nodes 的每个对象必须包含 "
                "node_id、objective、node_type、primary_capability、dependencies、"
                "expected_output、acceptance_criteria，不能省略 objective。"
            )
            patch_raw = await call(
                repair_prompt,
                "plan_refinement_retry",
                policy.max_refinement_prompt_tokens,
            )
            retry_path = plan_dir / f"plan_patch_raw_round_{round_number + 1}_retry.txt"
            retry_path.write_text(patch_raw, encoding="utf-8")
            patch, patch_format_report, patch_payload = adapt_plan_patch(patch_raw, task_spec)
        metrics["format_repair_count"] += patch_format_report.repair_count
        (plan_dir / f"plan_patch_normalized_round_{round_number + 1}.json").write_text(
            json.dumps(patch_payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        (plan_dir / f"plan_patch_format_report_round_{round_number + 1}.json").write_text(
            patch_format_report.model_dump_json(indent=2), encoding="utf-8",
        )
        plan = _normalize_stage_plan(
            _merge_patch(plan, patch), task_spec, contract,
        )
        metrics["refined_node_count"] += len(patch.replacement_nodes)
        (plan_dir / f"plan_ir_round_{round_number + 1}.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    raise PlanningValidationError("层级规划未生成可执行计划")


def _planning_budget(task_spec: dict[str, Any]) -> int:
    policy = task_spec.get("communication_policy", {})
    nested = policy.get("budget", {}) if isinstance(policy, dict) else {}
    return int(policy.get("max_control_prompt_tokens", nested.get("max_control_prompt_tokens", 8000)))
