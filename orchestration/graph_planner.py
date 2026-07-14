"""Validation and deterministic fallback for language-model planned task graphs."""

from __future__ import annotations

from typing import Any

from orchestration.schemas import parse_model
from orchestration.task_graph import GraphValidationError, TaskGraph, TaskNode


FORBIDDEN_GRAPH_CAPABILITIES = {"review", "report", "final_validate", "finish"}
# Only ``code`` invokes the framework-owned generation/persistence/execution
# pipeline.  data_analysis/calculation are semantic capabilities and cannot
# claim filesystem artifacts in the current executor contract.
CODE_CAPABILITIES = {"code"}
SUPPORTED_CRITERIA = {
    "node_result", "artifact_exists", "execution_exit_code", "artifact_quality",
}


def _intermediate_artifacts(task_spec: dict) -> list[str]:
    contract = task_spec.get("artifact_contract", {})
    if "intermediate_artifacts" in contract:
        return list(contract["intermediate_artifacts"])
    framework = {
        "task_spec.json", "task_graph.json", "agent_trace.md", "run_state.json",
        "file_previews.json", "normalized_input.json", "final_report.md",
    }
    return [path for path in task_spec.get("required_artifacts", []) if path not in framework]


def _ancestors(graph: TaskGraph, node_id: str) -> set[str]:
    found: set[str] = set()
    stack = list(graph.get_node(node_id).dependencies)
    while stack:
        current = stack.pop()
        if current in found:
            continue
        found.add(current)
        stack.extend(graph.get_node(current).dependencies)
    return found


def validate_planned_graph(
    raw: Any,
    task_spec: dict,
    available_capabilities: list[str],
) -> TaskGraph:
    """Parse an LLM graph and enforce framework/TaskSpec authority."""
    graph = parse_model(TaskGraph, raw)
    graph.graph_id = f"{task_spec['task_id']}_graph"
    graph.reset_runtime_state()
    for node in graph.nodes:
        # TaskSpec owns code retry count; scheduler retries would multiply calls.
        if node.capability in CODE_CAPABILITIES | {"artifact_validation"}:
            node.max_retries = 0
        else:
            node.max_retries = min(node.max_retries, 2)
    graph.validate_graph(set(available_capabilities))

    forbidden = [
        node.node_id for node in graph.nodes
        if node.capability in FORBIDDEN_GRAPH_CAPABILITIES
    ]
    if forbidden:
        raise GraphValidationError(
            "TaskGraph 不得包含外层 review/report/final_validate/finish 节点: "
            + ", ".join(forbidden)
        )

    allowed_outputs = set(_intermediate_artifacts(task_spec))
    undeclared = sorted({
        output
        for node in graph.nodes
        for output in node.output_artifacts
        if output not in allowed_outputs
    })
    if undeclared:
        raise GraphValidationError("TaskGraph 包含 TaskSpec 未声明的输出产物: " + ", ".join(undeclared))
    produced = {output for node in graph.nodes for output in node.output_artifacts}
    missing_contract_outputs = sorted(allowed_outputs - produced)
    if missing_contract_outputs:
        raise GraphValidationError(
            "TaskGraph 没有节点负责 TaskSpec 中间产物: " + ", ".join(missing_contract_outputs)
        )
    unsupported_criteria = sorted({
        str(criterion.get("type"))
        for node in graph.nodes for criterion in node.success_criteria
        if criterion.get("type") not in SUPPORTED_CRITERIA
    })
    if unsupported_criteria:
        raise GraphValidationError(
            "TaskGraph 使用框架不支持的 success_criteria.type: "
            + ", ".join(unsupported_criteria)
        )

    code_mode = task_spec.get("code_policy", {}).get("mode", "none")
    code_nodes = [node for node in graph.nodes if node.capability == "code"]
    if code_mode != "none" and len(code_nodes) != 1:
        raise GraphValidationError("TaskSpec 要求代码执行时，TaskGraph 必须且只能包含一个 code 节点")
    if code_mode == "none" and code_nodes:
        raise GraphValidationError("TaskSpec code_policy.mode=none，TaskGraph 不得请求代码能力")
    if code_mode != "none":
        code_node = code_nodes[0]
        other_writers = [
            node.node_id for node in graph.nodes
            if node.node_id != code_node.node_id and node.output_artifacts
        ]
        if other_writers:
            raise GraphValidationError(
                "当前代码流水线中只有 code 节点可以写 TaskSpec 中间产物；违规节点: "
                + ", ".join(other_writers)
            )
        code_outputs = set(code_node.output_artifacts)
        if code_outputs != allowed_outputs:
            missing = sorted(allowed_outputs - code_outputs)
            extra = sorted(code_outputs - allowed_outputs)
            details = []
            if missing:
                details.append("缺少 " + ", ".join(missing))
            if extra:
                details.append("多出 " + ", ".join(extra))
            raise GraphValidationError(
                "唯一 code 节点必须负责全部 TaskSpec 中间产物（" + "；".join(details) + "）"
            )
        if not any(
            criterion.get("type") == "execution_exit_code"
            and int(criterion.get("equals", 0)) == 0
            for criterion in code_node.success_criteria
        ):
            raise GraphValidationError("code 节点必须声明 execution_exit_code == 0 验收条件")

    validation_nodes = [node for node in graph.nodes if node.capability == "artifact_validation"]
    if len(validation_nodes) != 1:
        raise GraphValidationError("TaskGraph 必须且只能包含一个 artifact_validation 节点")
    validation_node = validation_nodes[0]
    ancestors = _ancestors(graph, validation_node.node_id)
    unreachable = sorted(
        node.node_id for node in graph.nodes
        if node.node_id != validation_node.node_id and node.node_id not in ancestors
    )
    if unreachable:
        raise GraphValidationError(
            "所有执行节点最终都必须汇入 artifact_validation；未连接节点: " + ", ".join(unreachable)
        )
    return graph


def build_fallback_graph(
    task_spec: dict,
    available_capabilities: list[str],
    reason: str,
) -> TaskGraph:
    """Build a safe analysis -> code? -> validation graph without LLM authority."""
    available = set(available_capabilities)
    if "analysis" not in available or "artifact_validation" not in available:
        raise GraphValidationError("确定性 fallback 需要 analysis 和 artifact_validation 能力")

    nodes = [TaskNode(
        node_id="analyze_task",
        description="分析任务目标、输入、约束与产物契约",
        capability="analysis",
        success_criteria=[{"type": "node_result"}],
        max_retries=0,
    )]
    previous = "analyze_task"
    if task_spec.get("code_policy", {}).get("mode", "none") != "none":
        if "code" not in available:
            raise GraphValidationError("确定性 fallback 需要 code 能力")
        outputs = _intermediate_artifacts(task_spec)
        nodes.append(TaskNode(
            node_id="execute_code",
            description="生成、保存并真实执行满足 TaskSpec 的代码",
            capability="code",
            dependencies=[previous],
            input_artifacts=["normalized_input.json"],
            output_artifacts=outputs,
            success_criteria=[{"type": "execution_exit_code", "equals": 0}],
            # CodePipelineExecutor owns TaskSpec.code_policy retries; avoid multiplication.
            max_retries=0,
        ))
        previous = "execute_code"

    nodes.append(TaskNode(
        node_id="validate_artifacts",
        description="用任务插件确定性校验全部中间产物",
        capability="artifact_validation",
        dependencies=[previous],
        success_criteria=[{"type": "artifact_quality", "equals": "passed"}],
        max_retries=0,
    ))
    graph = TaskGraph(
        graph_id=f"{task_spec['task_id']}_graph",
        version=1,
        goal=task_spec.get("task_name") or "完成任务契约并生成可验证产物",
        nodes=nodes,
    )
    graph.validate_graph(available)
    return graph
