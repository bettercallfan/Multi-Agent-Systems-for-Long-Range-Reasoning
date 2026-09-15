"""Compile a model-facing SemanticGraph into the strict executable TaskGraph."""

from __future__ import annotations

from typing import Any

from orchestration.graph.semantic_graph import SemanticGraph, coerce_semantic_graph
from orchestration.graph.task_graph import TaskGraph, TaskNode
from orchestration.task.artifact_validator import intermediate_artifacts


GOVERNANCE = {"terminal_execution", "evidence_verification", "artifact_validation"}


def compile_semantic_graph(
    raw: Any,
    task_spec: dict,
    available_capabilities: list[str],
    *,
    include_governance: bool = True,
) -> tuple[TaskGraph, dict]:
    """Compile business nodes and add deterministic governance nodes.

    Only syntactic/contract fields are filled here.  Business dependencies and
    capability choices remain owned by the planner.
    """
    semantic = coerce_semantic_graph(raw)
    available = set(available_capabilities)
    if not semantic.nodes:
        raise ValueError("SemanticGraph 不能为空")
    business_semantic_nodes = [
        node for node in semantic.nodes if node.capability not in GOVERNANCE
    ]
    ids = {node.node_id for node in business_semantic_nodes}
    governance_ids = {
        node.node_id for node in semantic.nodes if node.capability in GOVERNANCE
    }
    legacy_governance_ids = {
        capability: next(
            (node.node_id for node in semantic.nodes if node.capability == capability),
            capability,
        )
        for capability in GOVERNANCE
    }
    nodes: list[TaskNode] = []
    repairs: list[dict] = []
    for node in semantic.nodes:
        if node.capability in GOVERNANCE:
            repairs.append({"action": "drop_planner_governance_node", "node_id": node.node_id})
            continue
        if node.capability not in available:
            raise ValueError(f"节点 {node.node_id} 使用未注册 capability: {node.capability}")
        # Old TaskGraph-shaped responses may point at planner-authored
        # governance nodes.  Those edges are discarded because the compiler
        # inserts trusted replacements below.
        unknown = sorted(set(node.dependencies) - ids - governance_ids)
        if unknown:
            raise ValueError(f"节点 {node.node_id} 依赖不存在的业务节点: {', '.join(unknown)}")
        dependencies = [dependency for dependency in node.dependencies if dependency in ids]
        removed_dependencies = sorted(set(node.dependencies) & governance_ids)
        if removed_dependencies:
            repairs.append({
                "action": "drop_governance_dependencies",
                "node_id": node.node_id,
                "dependencies": removed_dependencies,
            })
        outputs: list[str] = []
        criteria: list[dict[str, Any]] = [{"type": "node_result"}]
        # In a code task, the framework owns the exact output contract.
        if node.capability == "code":
            outputs = intermediate_artifacts(task_spec)
            criteria = [{"type": "execution_exit_code", "equals": 0}]
            repairs.append({"action": "bind_code_artifacts", "node_id": node.node_id, "artifacts": outputs})
        elif task_spec.get("code_policy", {}).get("mode", "none") == "none":
            # A hierarchical primitive may own a declared non-code artifact;
            # old SemanticGraph responses carry the same information in the
            # compatibility-only legacy field.
            requested_outputs = list(
                node.output_artifacts or node.legacy_output_artifacts
            )
            declared_outputs = set(intermediate_artifacts(task_spec))
            outputs = [
                output for output in requested_outputs
                if output in declared_outputs
            ]
            removed_outputs = [
                output for output in requested_outputs
                if output not in declared_outputs
            ]
            if removed_outputs:
                repairs.append({
                    "action": "convert_undeclared_artifact_to_logical_output",
                    "node_id": node.node_id,
                    "artifacts": removed_outputs,
                    "reason": (
                        "当前阶段未授予物理文件写入权；保留 expected_output "
                        "作为结构化 NodeResult"
                    ),
                })
        nodes.append(TaskNode(
            node_id=node.node_id,
            description=node.objective or node.node_id,
            capability=node.capability,
            dependencies=dependencies,
            dependency_fields=dict(node.required_inputs),
            input_artifacts=list(node.input_artifacts),
            output_artifacts=outputs,
            requirement_ids=list(node.metadata.get("requirement_ids", [])),
            acceptance_refs=list(node.metadata.get("acceptance_refs", [])),
            expected_output=node.expected_output,
            acceptance_criteria=list(node.acceptance_criteria),
            success_criteria=criteria,
            max_retries=0 if node.capability == "code" else 1,
        ))

    code_nodes = [n for n in nodes if n.capability == "code"]
    if task_spec.get("code_policy", {}).get("mode", "none") != "none" and len(code_nodes) != 1:
        raise ValueError("TaskSpec 要求代码执行时，SemanticGraph 必须且只能包含一个 code 节点")
    required = set(task_spec.get("capability_contract", {}).get("required", []))
    if include_governance and "terminal_execution" in required:
        if not code_nodes:
            raise ValueError("terminal_execution 需要上游 code 节点")
        code_id = code_nodes[0].node_id
        nodes.append(TaskNode(
            node_id=legacy_governance_ids["terminal_execution"],
            description="复核框架保存的代码入口并记录真实退出码",
            capability="terminal_execution",
            dependencies=[code_id],
            input_artifacts=["artifacts/code_pipeline.py"],
            success_criteria=[{"type": "execution_exit_code", "equals": 0}],
            max_retries=0,
        ))
        # Consumers of the code result must not run before terminal verification.
        terminal_id = legacy_governance_ids["terminal_execution"]
        for node in nodes:
            if node.node_id not in {code_id, terminal_id} and code_id in node.dependencies:
                if terminal_id not in node.dependencies:
                    node.dependencies.append(terminal_id)
    if include_governance and "evidence_verification" in required:
        business_ids = [n.node_id for n in nodes if n.capability not in GOVERNANCE]
        evidence_dependencies = list(business_ids)
        terminal_id = legacy_governance_ids["terminal_execution"]
        if any(n.capability == "terminal_execution" for n in nodes):
            evidence_dependencies.append(terminal_id)
        nodes.append(TaskNode(
            node_id=legacy_governance_ids["evidence_verification"],
            description="汇总上游结构化结果并核验事实证据",
            capability="evidence_verification",
            dependencies=list(dict.fromkeys(evidence_dependencies)),
            success_criteria=[{"type": "node_result"}],
            max_retries=1,
        ))
    evidence_id = legacy_governance_ids["evidence_verification"]
    sink = evidence_id if any(n.capability == "evidence_verification" for n in nodes) else None
    if include_governance and "artifact_validation" in available:
        if sink is None:
            leaves = {n.node_id for n in nodes} - {d for n in nodes for d in n.dependencies}
            deps = sorted(leaves)
        else:
            deps = [sink]
        nodes.append(TaskNode(
            node_id=legacy_governance_ids["artifact_validation"],
            description="按 TaskSpec 产物契约校验中间产物",
            capability="artifact_validation",
            dependencies=deps,
            success_criteria=[{"type": "artifact_quality", "equals": "passed"}],
            max_retries=0,
        ))
    graph = TaskGraph(
        graph_id=f"{task_spec['task_id']}_graph",
        version=semantic.version,
        goal=semantic.goal or task_spec.get("task_name", "完成任务"),
        nodes=nodes,
    )
    graph.validate_graph(available)
    report = {
        "status": "compiled",
        "source": "planning_agent",
        "semantic_node_count": len(semantic.nodes),
        "executable_node_count": len(nodes),
        "repairs": repairs,
        "inserted_governance_nodes": [
            n.node_id for n in nodes if n.capability in GOVERNANCE
        ],
        "warnings": [],
    }
    return graph, report
