"""Validation and deterministic fallback for language-model planned task graphs."""

from __future__ import annotations

from typing import Any

from orchestration.core.schemas import parse_model
from orchestration.graph.task_graph import GraphValidationError, TaskGraph, TaskNode
from orchestration.task.artifact_validator import intermediate_artifacts


FORBIDDEN_GRAPH_CAPABILITIES = {"review", "report", "final_validate", "finish"}
# Only ``code`` invokes the framework-owned generation/persistence/execution
# pipeline.  data_analysis/calculation are semantic capabilities and cannot
# claim filesystem artifacts in the current executor contract.
CODE_CAPABILITIES = {"code"}
SUPPORTED_CRITERIA = {
    "node_result", "artifact_exists", "execution_exit_code", "artifact_quality",
    "evidence_refs_nonempty",
}
EXTERNAL_EVIDENCE_CAPABILITIES = {
    "document_recovery", "document_conversion", "file_navigation",
    "web_research", "browser_navigation",
}
VERIFICATION_CAPABILITY = "evidence_verification"
TERMINAL_CAPABILITY = "terminal_execution"


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
    policy_retry_limit = int(
        task_spec.get("recovery_policy", {}).get("max_node_retries", 1)
    )
    for node in graph.nodes:
        # TaskSpec owns code retry count; scheduler retries would multiply calls.
        if node.capability in CODE_CAPABILITIES | {"artifact_validation"}:
            node.max_retries = 0
        else:
            node.max_retries = min(node.max_retries, policy_retry_limit)
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

    required_capabilities = list(
        task_spec.get("capability_contract", {}).get("required", [])
    )
    planned_capabilities = {node.capability for node in graph.nodes}
    missing_required_capabilities = sorted(
        set(required_capabilities) - planned_capabilities
    )
    if missing_required_capabilities:
        raise GraphValidationError(
            "TaskGraph 缺少 TaskSpec 强制能力节点: "
            + ", ".join(missing_required_capabilities)
        )

    if VERIFICATION_CAPABILITY in required_capabilities:
        verifier_nodes = [
            node for node in graph.nodes
            if node.capability == VERIFICATION_CAPABILITY
        ]
        if len(verifier_nodes) != 1:
            raise GraphValidationError(
                "TaskSpec 要求证据核验时，TaskGraph 必须且只能包含一个 evidence_verification 节点"
            )
        verifier = verifier_nodes[0]
        business_node_ids = {
            node.node_id
            for node in graph.nodes
            if node.node_id != verifier.node_id
            and node.capability != "artifact_validation"
        }
        missing_direct_inputs = sorted(
            business_node_ids - set(verifier.dependencies)
        )
        if missing_direct_inputs:
            raise GraphValidationError(
                "evidence_verification 必须直接接收每个业务节点的结构化结果；"
                "缺少直接依赖: " + ", ".join(missing_direct_inputs)
            )
        uncovered = sorted(
            node.node_id
            for node in graph.nodes
            if node.node_id != verifier.node_id
            and node.capability != "artifact_validation"
            and node.node_id not in _ancestors(graph, verifier.node_id)
        )
        if uncovered:
            raise GraphValidationError(
                "所有业务执行节点必须先汇入 evidence_verification；未核验节点: "
                + ", ".join(uncovered)
            )
    elif any(
        node.capability == VERIFICATION_CAPABILITY for node in graph.nodes
    ):
        raise GraphValidationError(
            "TaskSpec 未要求 evidence_verification，TaskGraph 不得自行增加事实核验节点"
        )

    # Merely adding an external-tool node to satisfy a capability count is not
    # useful. Every planned evidence-acquisition node must feed at least one
    # downstream semantic/code node before the deterministic validation sink.
    semantic_consumers = [
        node for node in graph.nodes
        if node.capability not in EXTERNAL_EVIDENCE_CAPABILITIES | {
            "artifact_validation", VERIFICATION_CAPABILITY, TERMINAL_CAPABILITY,
        }
    ]
    unconsumed_external_nodes = sorted(
        node.node_id
        for node in graph.nodes
        if node.capability in EXTERNAL_EVIDENCE_CAPABILITIES
        and not any(
            node.node_id in _ancestors(graph, consumer.node_id)
            for consumer in semantic_consumers
        )
    )
    if unconsumed_external_nodes:
        raise GraphValidationError(
            "外部能力节点没有进入下游分析/执行链路，疑似冗余凑数: "
            + ", ".join(unconsumed_external_nodes)
        )

    allowed_outputs = set(intermediate_artifacts(task_spec))
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

    terminal_nodes = [node for node in graph.nodes if node.capability == TERMINAL_CAPABILITY]
    if TERMINAL_CAPABILITY in required_capabilities:
        if len(terminal_nodes) != 1:
            raise GraphValidationError(
                "代码任务必须且只能包含一个 terminal_execution 节点"
            )
        if not code_nodes:
            raise GraphValidationError("terminal_execution 需要上游 code 节点")
        if code_nodes[0].node_id not in _ancestors(graph, terminal_nodes[0].node_id):
            raise GraphValidationError("terminal_execution 必须在 code 节点之后执行")
        if not any(
            criterion.get("type") == "execution_exit_code"
            and int(criterion.get("equals", 0)) == 0
            for criterion in terminal_nodes[0].success_criteria
        ):
            raise GraphValidationError(
                "terminal_execution 节点必须声明 execution_exit_code == 0"
            )
    elif terminal_nodes:
        raise GraphValidationError(
            "TaskSpec 未要求 terminal_execution，TaskGraph 不得自行增加终端节点"
        )

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

    recovery_retries = int(
        task_spec.get("recovery_policy", {}).get("max_node_retries", 1)
    )
    required_capabilities = list(
        task_spec.get("capability_contract", {}).get("required", [])
    )
    missing_runtime_capabilities = sorted(set(required_capabilities) - available)
    if missing_runtime_capabilities:
        raise GraphValidationError(
            "TaskSpec 强制能力没有可用执行器: "
            + ", ".join(missing_runtime_capabilities)
        )

    nodes: list[TaskNode] = []
    preprocessing_nodes: list[str] = []
    if "document_conversion" in required_capabilities or "document_recovery" in required_capabilities:
        input_artifacts = [
            f"inputs/{str(path).replace(chr(92), '/').rsplit('/', 1)[-1]}"
            for path in task_spec.get("input", {}).get("files", [])
        ]
        nodes.append(TaskNode(
            node_id="recover_documents",
            description="使用已注册的真实文档转换或文件浏览组件恢复无法可靠预览的输入",
            capability=(
                "document_conversion"
                if "document_conversion" in required_capabilities
                else "document_recovery"
            ),
            input_artifacts=input_artifacts,
            success_criteria=[{"type": "node_result"}],
            max_retries=recovery_retries,
        ))
        preprocessing_nodes.append("recover_documents")
    if "file_navigation" in required_capabilities:
        input_artifacts = [
            f"inputs/{str(path).replace(chr(92), '/').rsplit('/', 1)[-1]}"
            for path in task_spec.get("input", {}).get("files", [])
        ]
        nodes.append(TaskNode(
            node_id="navigate_documents",
            description="使用受控文件浏览 Agent 在授权输入中逐页定位和提取任务所需证据",
            capability="file_navigation",
            input_artifacts=input_artifacts,
            success_criteria=[{"type": "node_result"}],
            max_retries=recovery_retries,
        ))
        preprocessing_nodes.append("navigate_documents")
    if "web_research" in required_capabilities:
        nodes.append(TaskNode(
            node_id="research_public_web",
            description="使用受控浏览器检索任务明确要求的公开网页信息并记录来源URL",
            capability="web_research",
            success_criteria=[{"type": "node_result"}],
            max_retries=recovery_retries,
        ))
        preprocessing_nodes.append("research_public_web")

    nodes.append(TaskNode(
        node_id="analyze_task",
        description="分析任务目标、输入、约束与产物契约",
        capability="analysis",
        dependencies=preprocessing_nodes,
        success_criteria=[{"type": "node_result"}],
        max_retries=recovery_retries,
    ))
    previous = "analyze_task"
    if task_spec.get("code_policy", {}).get("mode", "none") != "none":
        if "code" not in available:
            raise GraphValidationError("确定性 fallback 需要 code 能力")
        outputs = intermediate_artifacts(task_spec)
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

    if TERMINAL_CAPABILITY in required_capabilities:
        nodes.append(TaskNode(
            node_id="verify_code_in_terminal",
            description="使用 AutoGen CodeExecutorAgent 在受控工作目录复核已落盘 Python 入口",
            capability=TERMINAL_CAPABILITY,
            dependencies=[previous],
            input_artifacts=[
                task_spec.get("artifacts", {}).get("code")
                or "artifacts/code_pipeline.py"
            ],
            success_criteria=[{"type": "execution_exit_code", "equals": 0}],
            max_retries=0,
        ))
        previous = "verify_code_in_terminal"

    if VERIFICATION_CAPABILITY in required_capabilities:
        verification_dependencies = [node.node_id for node in nodes]
        nodes.append(TaskNode(
            node_id="verify_evidence",
            description="用真实文件和来源目录逐条核验上游事实，再决定是否允许进入产物校验",
            capability=VERIFICATION_CAPABILITY,
            dependencies=verification_dependencies,
            success_criteria=[{"type": "node_result"}],
            max_retries=recovery_retries,
        ))
        previous = "verify_evidence"

    nodes.append(TaskNode(
        node_id="validate_artifacts",
        description="依据 TaskSpec 产物契约确定性校验全部中间产物",
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
