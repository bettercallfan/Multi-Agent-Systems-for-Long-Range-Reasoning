"""Requirement-driven stage scoping and safe executable-graph growth."""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Iterable

from orchestration.core.schemas import RequirementSet
from orchestration.graph.graph_compiler import GOVERNANCE
from orchestration.graph.task_graph import TaskGraph, TaskNode
from orchestration.planning.requirement_progress import (
    requirement_needs_code,
    scoped_requirement_set,
)


class StageContractError(ValueError):
    """Raised before execution when a stage violates its scoped contract."""


def requirement_staging_enabled(
    task_spec: dict,
    requirement_set: RequirementSet,
    *,
    hierarchical_selected: bool,
) -> bool:
    """Use stages only for genuinely non-trivial hierarchical contracts."""

    policy = task_spec.get("horizon_policy") or {}
    if not hierarchical_selected or not policy.get("requirement_driven_expansion", True):
        return False
    parent_ids = {
        item.parent_id for item in requirement_set.requirements if item.parent_id
    }
    mandatory_leaves = [
        item for item in requirement_set.requirements
        if item.owner == "planner" and item.mandatory
        and item.requirement_id not in parent_ids
    ]
    return len(mandatory_leaves) >= max(
        2, int(policy.get("min_requirements_for_staging", 8)),
    )


def build_stage_task_spec(
    task_spec: dict,
    requirement_set: RequirementSet,
    requirement_ids: list[str],
    stage: int,
) -> dict:
    """Create a planning-only TaskSpec view for one requirement frontier.

    The global TaskSpec remains frozen.  Generated code and result artifacts
    are owned by the one code stage; analysis stages communicate through
    structured node results rather than competing for the same file path.
    """

    scoped = scoped_requirement_set(requirement_set, requirement_ids)
    selected = {
        item.requirement_id: item for item in scoped.requirements
        if item.requirement_id in set(requirement_ids)
    }
    code_stage = (
        task_spec.get("code_policy", {}).get("mode", "none") != "none"
        and any(requirement_needs_code(item) for item in selected.values())
    )
    stage_spec = deepcopy(task_spec)
    stage_spec["task_id"] = f"{task_spec['task_id']}_stage_{stage:03d}"
    stage_spec["task_name"] = (
        f"{task_spec.get('task_name', '任务')}：阶段 {stage}，完成需求 "
        + ", ".join(requirement_ids)
    )
    stage_spec["requirement_contract"] = scoped.model_dump(mode="json")
    stage_spec["stage_scope"] = {
        "stage": stage,
        "requirement_ids": list(requirement_ids),
        "global_task_id": task_spec["task_id"],
    }
    stage_spec.setdefault("planning_policy", {})["mode"] = "hierarchical"

    global_artifacts = list(
        task_spec.get("artifact_contract", {}).get("intermediate_artifacts", [])
    )
    stage_artifacts = global_artifacts if code_stage else []
    stage_spec["artifact_contract"] = {
        "intermediate_artifacts": stage_artifacts,
        "final_artifacts": [],
        "framework_artifacts": [],
    }
    stage_spec["required_artifacts"] = list(stage_artifacts)
    stage_spec["planning_contract"] = {
        "graph_deliverables": list(stage_artifacts),
        "postprocess_deliverables": [],
        "framework_artifacts": [],
        "validation_requirements": [],
        "requirement_contract": scoped.model_dump(mode="json"),
    }
    if not code_stage:
        stage_spec["code_policy"] = {
            **dict(stage_spec.get("code_policy") or {}),
            "mode": "none",
            "max_retries": 0,
        }
    stage_spec["capability_contract"] = deepcopy(
        task_spec.get("capability_contract") or {}
    )
    stage_spec["capability_contract"]["required"] = [
        capability
        for capability in stage_spec["capability_contract"].get("required", [])
        if capability not in GOVERNANCE
    ]
    return stage_spec


def enforce_stage_graph_contract(
    graph: TaskGraph,
    stage_spec: dict,
    available_capabilities: Iterable[str],
) -> tuple[TaskGraph, list[dict]]:
    """Align logical Agent results and physical artifacts before execution."""

    nodes = [node.model_copy(deep=True) for node in graph.nodes]
    code_nodes = [node for node in nodes if node.capability == "code"]
    code_mode = stage_spec.get("code_policy", {}).get("mode", "none")
    declared = set(
        stage_spec.get("artifact_contract", {}).get("intermediate_artifacts", [])
    )
    if code_mode == "none" and code_nodes:
        raise StageContractError(
            "非代码阶段出现 code 节点；必须局部重规划，不能把代码节点编译为空产物"
        )
    if code_mode != "none" and len(code_nodes) != 1:
        raise StageContractError("代码阶段必须且只能包含一个 code 节点")
    if code_nodes and set(code_nodes[0].output_artifacts) != declared:
        raise StageContractError(
            "代码节点没有独占阶段物理产物："
            f"expected={sorted(declared)}, actual={sorted(code_nodes[0].output_artifacts)}"
        )

    repairs: list[dict] = []
    removed_outputs: set[str] = set()
    for node in nodes:
        if node.capability == "code" or not node.output_artifacts:
            continue
        removed = list(node.output_artifacts)
        removed_outputs.update(removed)
        node.output_artifacts = []
        repairs.append({
            "action": "convert_agent_artifact_to_logical_output",
            "node_id": node.node_id,
            "removed_output_artifacts": removed,
            "reason": "该 capability 只返回结构化 NodeResult，不拥有文件写入权",
        })
    invalid_consumers = [
        {"node_id": node.node_id, "input_artifacts": sorted(
            set(node.input_artifacts) & removed_outputs
        )}
        for node in nodes
        if set(node.input_artifacts) & removed_outputs
    ]
    if invalid_consumers:
        raise StageContractError(
            "节点把逻辑输出错误声明为磁盘输入；应使用 node_output 依赖："
            + str(invalid_consumers)
        )
    normalized = TaskGraph(
        graph_id=graph.graph_id,
        version=graph.version,
        goal=graph.goal,
        nodes=nodes,
    )
    normalized.validate_graph(set(available_capabilities))
    return normalized, repairs


def _stage_node_id(stage: int, node_id: str) -> str:
    prefix = f"s{stage:03d}_"
    candidate = prefix + node_id
    if len(candidate) <= 64:
        return candidate
    return candidate[:64]


def namespace_stage_graph(graph: TaskGraph, stage: int) -> TaskGraph:
    """Make independently planned stage node IDs globally collision-free."""

    mapping = {node.node_id: _stage_node_id(stage, node.node_id) for node in graph.nodes}
    nodes = []
    for node in graph.nodes:
        dependencies = [mapping[item] for item in node.dependencies]
        dependency_fields = {
            mapping[key]: value for key, value in node.dependency_fields.items()
        }
        nodes.append(node.model_copy(update={
            "node_id": mapping[node.node_id],
            "dependencies": dependencies,
            "dependency_fields": dependency_fields,
        }))
    return TaskGraph(
        graph_id=graph.graph_id,
        version=graph.version,
        goal=graph.goal,
        nodes=nodes,
    )


def merge_stage_graph(
    current: TaskGraph | None,
    stage_graph: TaskGraph,
    *,
    stage: int,
    global_goal: str,
    available_capabilities: Iterable[str],
    framework_owned_artifacts: Iterable[str] = (),
) -> tuple[TaskGraph, dict[str, list[str]]]:
    """Append one namespaced stage while preserving completed runtime state."""

    incoming = namespace_stage_graph(stage_graph, stage)
    protected_outputs = set(framework_owned_artifacts)
    existing_code_nodes = [
        node for node in (current.nodes if current else [])
        if node.capability == "code"
    ]
    incoming_code_nodes = [
        node for node in incoming.nodes if node.capability == "code"
    ]
    if existing_code_nodes and incoming_code_nodes:
        raise StageContractError(
            "累计任务图已经存在 code 节点，不能追加第二个物理产物写入阶段"
        )
    for node in incoming.nodes:
        if node.capability != "code" and node.output_artifacts:
            raise StageContractError(
                f"非代码节点 {node.node_id} 仍声明物理产物：{node.output_artifacts}"
            )
        if node.capability == "code" and not set(node.output_artifacts).issubset(
            protected_outputs
        ):
            raise StageContractError(
                f"代码节点 {node.node_id} 写入了全局契约之外的产物"
            )
    existing = list(current.nodes) if current else []
    if current:
        depended_on = {dep for node in current.nodes for dep in node.dependencies}
        previous_leaves = [
            node.node_id for node in current.nodes
            if node.node_id not in depended_on and node.capability not in GOVERNANCE
        ]
        for node in incoming.nodes:
            if not node.dependencies:
                node.dependencies = list(previous_leaves)
    merged = TaskGraph(
        graph_id=(current.graph_id if current else f"staged_{stage_graph.graph_id}"),
        version=(current.version + 1 if current else 1),
        goal=global_goal,
        nodes=existing + incoming.nodes,
    )
    merged.validate_graph(set(available_capabilities))
    producer_mapping: dict[str, list[str]] = {}
    for node in incoming.nodes:
        for requirement_id in node.requirement_ids:
            producer_mapping.setdefault(requirement_id, []).append(node.node_id)
    return merged, producer_mapping


def append_governance_nodes(
    graph: TaskGraph,
    task_spec: dict,
    available_capabilities: Iterable[str],
) -> TaskGraph:
    """Append the trusted global governance chain exactly once."""

    if any(node.capability in GOVERNANCE for node in graph.nodes):
        return graph
    nodes = [node.model_copy(deep=True) for node in graph.nodes]
    required = set(task_spec.get("capability_contract", {}).get("required", []))
    available = set(available_capabilities)
    code_nodes = [node for node in nodes if node.capability == "code"]
    if not code_nodes and "terminal_execution" in required:
        declared_outputs = list(
            task_spec.get("artifact_contract", {}).get(
                "intermediate_artifacts", []
            )
        )
        if "code" not in available or not declared_outputs:
            raise ValueError(
                "分阶段图遗漏 code 节点，且框架无法按产物合同补全"
            )
        depended_on = {
            dependency for node in nodes for dependency in node.dependencies
        }
        leaves = [
            node.node_id for node in nodes
            if node.node_id not in depended_on
            and node.capability not in GOVERNANCE
        ]
        owned_requirements = []
        acceptance_refs = []
        declared_set = set(declared_outputs)
        contract = task_spec.get("requirement_contract", {})
        for item in contract.get("requirements", []):
            if item.get("owner", "planner") != "planner":
                continue
            if not declared_set.intersection(item.get("expected_outputs", [])):
                continue
            requirement_id = str(item.get("requirement_id") or "").strip()
            if requirement_id:
                owned_requirements.append(requirement_id)
            acceptance_refs.extend(
                str(criterion.get("criterion_id"))
                for criterion in item.get("acceptance_criteria", [])
                if criterion.get("criterion_id")
            )
        synthesized = TaskNode(
            node_id="framework_code_delivery",
            description="根据已完成分析生成、执行并交付全局代码产物",
            capability="code",
            dependencies=leaves,
            input_artifacts=["normalized_input.json"],
            output_artifacts=declared_outputs,
            requirement_ids=list(dict.fromkeys(owned_requirements)),
            acceptance_refs=list(dict.fromkeys(acceptance_refs)),
            success_criteria=[{"type": "execution_exit_code", "equals": 0}],
            max_retries=0,
        )
        nodes.append(synthesized)
        code_nodes = [synthesized]
    if len(code_nodes) > 1:
        # A staged planner can occasionally emit duplicate code primitives for
        # the same global artifact contract.  They are not independent
        # deliverables: running both would race on identical files and violate
        # the single terminal execution contract.  Keep the node that owns the
        # most declared outputs (stable first-node tie break), then redirect
        # dependencies from discarded duplicates to it.  This is a generic
        # graph normalization guard; it does not inspect any business domain.
        canonical = max(
            code_nodes,
            key=lambda node: (len(node.output_artifacts), -nodes.index(node)),
        )
        duplicate_ids = {
            node.node_id for node in code_nodes if node.node_id != canonical.node_id
        }
        for duplicate in code_nodes:
            if duplicate.node_id not in duplicate_ids:
                continue
            canonical.requirement_ids = list(dict.fromkeys(
                [*canonical.requirement_ids, *duplicate.requirement_ids]
            ))
            canonical.input_artifacts = list(dict.fromkeys(
                [*canonical.input_artifacts, *duplicate.input_artifacts]
            ))
            canonical.success_criteria = list({
                json.dumps(item, ensure_ascii=False, sort_keys=True, default=str): item
                for item in [*canonical.success_criteria, *duplicate.success_criteria]
            }.values())
        nodes = [node for node in nodes if node.node_id not in duplicate_ids]
        for node in nodes:
            node.dependencies = list(dict.fromkeys(
                canonical.node_id if dependency in duplicate_ids else dependency
                for dependency in node.dependencies
            ))
        code_nodes = [canonical]
    terminal_id = "terminal_execution"
    if "terminal_execution" in required:
        if len(code_nodes) != 1:
            raise ValueError("分阶段图要求 terminal_execution 时必须且只能存在一个 code 节点")
        code_id = code_nodes[0].node_id
        nodes.append(TaskNode(
            node_id=terminal_id,
            description="复核框架保存的代码入口并记录真实退出码",
            capability="terminal_execution",
            dependencies=[code_id],
            input_artifacts=["artifacts/code_pipeline.py"],
            success_criteria=[{"type": "execution_exit_code", "equals": 0}],
            max_retries=0,
        ))
        for node in nodes:
            if node.node_id not in {code_id, terminal_id} and code_id in node.dependencies:
                node.dependencies.append(terminal_id)
    evidence_id = "evidence_verification"
    if "evidence_verification" in required:
        dependencies = [
            node.node_id for node in nodes
            if node.capability not in GOVERNANCE
        ]
        if any(node.node_id == terminal_id for node in nodes):
            dependencies.append(terminal_id)
        nodes.append(TaskNode(
            node_id=evidence_id,
            description="汇总全部阶段结果并核验事实证据",
            capability="evidence_verification",
            dependencies=list(dict.fromkeys(dependencies)),
            success_criteria=[{"type": "node_result"}],
            max_retries=1,
        ))
    if "artifact_validation" in available:
        if any(node.node_id == evidence_id for node in nodes):
            dependencies = [evidence_id]
        else:
            depended_on = {dep for node in nodes for dep in node.dependencies}
            dependencies = [node.node_id for node in nodes if node.node_id not in depended_on]
        nodes.append(TaskNode(
            node_id="artifact_validation",
            description="按全局 TaskSpec 产物契约校验中间产物",
            capability="artifact_validation",
            dependencies=dependencies,
            success_criteria=[{"type": "artifact_quality", "equals": "passed"}],
            max_retries=0,
        ))
    result = TaskGraph(
        graph_id=graph.graph_id,
        version=graph.version + 1,
        goal=graph.goal,
        nodes=nodes,
    )
    result.validate_graph(available)
    return result
