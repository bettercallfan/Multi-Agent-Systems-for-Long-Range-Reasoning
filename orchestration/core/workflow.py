"""Explicit, framework-controlled multi-agent workflow."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path

from agents.planning_agent import create_planning_agent
from agents.report_agent import create_report_agent
from agents.review_agent import create_review_agent
from agents.task_understanding_agent import create_task_understanding_agent
from orchestration.task.artifact_validator import (
    load_strict_json,
    validate_intermediate_artifacts,
)
from orchestration.execution.default_executors import build_default_registry
from orchestration.graph.graph_planner import build_fallback_graph, validate_planned_graph
from orchestration.graph.graph_compiler import compile_semantic_graph
from orchestration.graph.semantic_graph import coerce_semantic_graph
from orchestration.planning.hierarchical_planner import (
    PlanningValidationError,
    build_hierarchical_semantic_graph,
    resolve_planning_policy,
)
from orchestration.planning.requirement_compiler import (
    add_derived_source_traces,
    compile_requirement_set,
    preserve_requirement_history,
    requirement_digest,
    validate_requirement_set,
)
from orchestration.planning.requirement_progress import (
    build_requirement_progress_ledger,
    persist_requirement_progress,
    select_requirement_batch,
)
from orchestration.planning.staged_expansion import (
    append_governance_nodes,
    build_stage_task_spec,
    enforce_stage_graph_contract,
    merge_stage_graph,
    requirement_staging_enabled,
)
from orchestration.graph.graph_scheduler import GraphScheduler
from orchestration.graph.repair_loop import BusinessRepairLoop, BusinessRepairOutcome
from orchestration.task.input_preparation import prepare_normalized_input
from orchestration.core.prompt_builder import (
    build_planning_prompt,
    build_replanning_prompt,
    build_report_prompt,
    build_requirement_refinement_prompt,
    build_review_prompt,
    build_task_understanding_prompt,
)
from orchestration.core.model_calls import run_agent
from orchestration.core.run_state import RunState
from orchestration.core.schemas import (
    ArtifactQualityResult,
    ExecutionResult,
    ReviewDecision,
    SemanticReviewResult,
    TaskUnderstandingResult,
    RequirementSet,
    parse_model,
)
from orchestration.graph.task_graph import TaskGraph
from orchestration.memory.workflow_memory import (
    curate_workflow_memory,
    retrieve_workflow_memory,
)
from orchestration.validation.runner import run_business_validation
from orchestration.validation.acceptance_contract import (
    compile_acceptance_contract,
)
from orchestration.validation.acceptance_runner import (
    merge_requirement_ledgers,
    run_acceptance_contract,
    write_acceptance_outputs,
)
from utils.output import TraceEvent, content_to_text, save_agent_trace, save_final_report
from utils.review_artifacts import (
    final_validation,
    pre_report_review,
    write_validation_report,
)


def _finish_planning_blocked(
    task_spec: dict,
    run_state: RunState,
    trace: list,
    error: Exception,
    *,
    failure_type: str = "planning_provider_unavailable",
    recovery_action: str = "retry_planning_with_available_model",
    issue_prefix: str = "层级规划暂时不可用",
) -> tuple[ReviewDecision, list]:
    """Persist a normal failed delivery when planning infrastructure is down.

    Provider exhaustion is not a valid reason to fabricate a fallback business
    graph, but it is also not an uncaught programming error.  Preserve every
    artifact/checkpoint produced so far and emit the same framework-owned
    trace and validation files as other failed runs.
    """

    run_dir = Path(task_spec["run_dir"])
    issue = f"{issue_prefix}：{error}"
    run_state.set_failure(
        failure_type,
        "planning",
        "plan",
        [issue],
    )
    run_state.record_event("planning_recoverable_blocked", {
        "error_type": type(error).__name__,
        "error": str(error),
        "recovery_action": recovery_action,
    })
    trace.append(TraceEvent("HierarchicalPlanner", {
        "status": "blocked",
        "error_type": type(error).__name__,
        "error": str(error),
        "recovery_action": recovery_action,
    }))
    run_state.start_stage("final_validate")
    save_agent_trace(trace, run_dir / "agent_trace.md")
    run_state.record_artifact("agent_trace.md")
    run_state.refresh_artifacts()
    deterministic = final_validation(task_spec, run_state.to_dict())
    decision = ReviewDecision(
        status="failed",
        can_generate_final_report=False,
        issues=list(dict.fromkeys([issue, *deterministic.issues])),
        required_artifacts_checked=deterministic.required_artifacts_checked,
    )
    run_state.record_review(decision)
    run_state.record_delivery_outcome(
        "failed", ["final_report.md", "review/validation_report.md"],
    )
    validation_path = write_validation_report(
        run_dir, decision, run_state.to_dict(),
    )
    run_state.record_artifact(validation_path)
    trace.append(TraceEvent("Framework", {
        "final_validation": decision.model_dump(mode="json"),
    }))
    save_agent_trace(trace, run_dir / "agent_trace.md")
    run_state.finish("failed")
    return decision, trace


def _build_graph_plan(task_graph: TaskGraph, available_capabilities: list[str]) -> dict:
    """Derive audit metadata from the validated graph instead of model claims."""
    capabilities = [node.capability for node in task_graph.nodes]
    node_count = len(task_graph.nodes)
    edge_count = sum(len(node.dependencies) for node in task_graph.nodes)
    possible_full_connection_edges = node_count * max(node_count - 1, 0)
    return {
        "execution_mode": "task_graph",
        "graph_id": task_graph.graph_id,
        "graph_version": task_graph.version,
        "goal": task_graph.goal,
        "node_count": node_count,
        "edge_count": edge_count,
        "possible_full_connection_edges": possible_full_connection_edges,
        "topology_density": round(
            edge_count / possible_full_connection_edges, 6
        ) if possible_full_connection_edges else 0.0,
        "requires_code": "code" in capabilities,
        "use_research": "research" in capabilities,
        "use_reasoning": any(
            capability in {"reasoning", "math_modeling"}
            for capability in capabilities
        ),
        "node_capabilities": capabilities,
        "available_capabilities": available_capabilities,
    }


def _progressive_execution_enabled(
    task_spec: dict,
    task_graph: TaskGraph,
) -> bool:
    """Select bounded execution windows without forcing short tasks to expand."""

    policy = task_spec.get("horizon_policy") or {}
    if not policy.get("progressive_execution", True):
        return False
    mode = policy.get("mode", "auto")
    if mode == "one_shot":
        return False
    if mode == "progressive":
        return True
    # Auto mode is deliberately conservative.  Iterative scientific/data
    # tasks and graphs larger than one active window benefit from epoch
    # boundaries; small document tasks keep the simple one-shot path.
    return (
        task_spec.get("task_type") in {"math_modeling", "data_modeling"}
        or len(task_graph.nodes) > int(policy.get("max_active_nodes", 8))
    )


def _checkpoint_digest(payload: dict) -> str:
    """Return a stable digest for framework-owned checkpoint contents."""
    body = {key: value for key, value in payload.items() if key != "integrity"}
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _protected_task_digest(task_spec: dict) -> str:
    """Digest only immutable task contract fields used for resume safety."""
    contract = {
        "task_id": task_spec.get("task_id"),
        "task_type": task_spec.get("task_type"),
        "required_artifacts": list(task_spec.get("required_artifacts", [])),
        "success_criteria": task_spec.get("success_criteria", {}),
        "required_report_sections": list(task_spec.get("required_report_sections", [])),
    }
    encoded = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_horizon_checkpoint(
    run_dir: str | Path,
    *,
    checkpoint_ref: str | None = None,
    expected_goal: str | None = None,
    expected_task_spec: dict | None = None,
) -> tuple[TaskGraph, dict]:
    """Load and validate a persisted horizon checkpoint for process resume.

    Checkpoints are untrusted input: integrity, graph identity and protected
    task state are checked before a graph is handed to the scheduler.
    """
    root = Path(run_dir)
    if checkpoint_ref:
        reference = Path(checkpoint_ref)
        if reference.is_absolute() or ".." in reference.parts:
            raise ValueError("checkpoint 引用路径越界，拒绝恢复")
        path = root / reference
    else:
        candidates = sorted((root / "checkpoints").glob("epoch-*.json"))
        if not candidates:
            raise FileNotFoundError("未找到长程执行 checkpoint")
        path = candidates[-1]
    if not path.is_file():
        raise FileNotFoundError(f"checkpoint 不存在: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    integrity = payload.get("integrity") or {}
    expected_digest = integrity.get("sha256")
    actual_digest = _checkpoint_digest(payload)
    if not expected_digest or expected_digest != actual_digest:
        raise ValueError("checkpoint 完整性校验失败，拒绝恢复")
    graph_payload = payload.get("task_graph")
    if not isinstance(graph_payload, dict):
        raise ValueError("checkpoint 缺少完整 task_graph 快照")
    graph = TaskGraph.model_validate(graph_payload)
    protected = payload.get("protected_context") or {}
    if expected_goal is not None and protected.get("global_goal") != expected_goal:
        raise ValueError("checkpoint 的全局目标与当前任务不一致")
    if expected_task_spec is not None:
        expected_artifacts = list(expected_task_spec.get("required_artifacts", []))
        if list(protected.get("mandatory_artifacts", [])) != expected_artifacts:
            raise ValueError("checkpoint 的必需产物约束与当前 TaskSpec 不一致")
        if protected.get("task_contract_digest") != _protected_task_digest(expected_task_spec):
            raise ValueError("checkpoint 的任务契约与当前 TaskSpec 不一致")
    if protected.get("global_goal") != graph.goal:
        raise ValueError("checkpoint 的保护目标与任务图不一致")
    return graph, payload


async def _execute_graph_in_horizons(
    *,
    task_graph: TaskGraph,
    task_spec: dict,
    registry,
    run_state: RunState,
    run_dir: Path,
    replan_callback,
    trace: list,
    validate_task_contract: bool = True,
    expand_callback=None,
    resume_checkpoint: str | None = None,
) -> TaskGraph:
    """Run a graph in bounded ready-node windows using the existing scheduler."""

    policy = task_spec.get("horizon_policy") or {}
    max_active = max(1, int(policy.get("max_active_nodes", 8)))
    max_epochs = max(1, int(policy.get("max_epochs", 128)))
    persist_every = max(1, int(policy.get("persist_every_epochs", 1)))
    live_updates = bool(task_spec.get("accept_runtime_injections"))
    epoch = 0
    if resume_checkpoint:
        restored, checkpoint = load_horizon_checkpoint(
            run_dir,
            checkpoint_ref=resume_checkpoint,
            expected_goal=task_graph.goal,
            expected_task_spec=task_spec,
        )
        task_graph = restored
        epoch = int(checkpoint.get("epoch", 0))
        run_state.record_event("horizon_resumed", {
            "checkpoint_ref": resume_checkpoint,
            "epoch": epoch,
            "completed_node_count": sum(
                node.status.value == "completed" for node in task_graph.nodes
            ),
        })
    previous_completed: set[str] = {
        node.node_id for node in task_graph.nodes
        if node.status.value == "completed"
    }
    run_state.record_event("horizon_execution_started", {
        "mode": "progressive",
        "max_active_nodes": max_active,
        "max_epochs": max_epochs,
        "resume_supported": True,
    })
    scheduler = GraphScheduler(
        registry, run_state, run_dir,
        replan_callback=replan_callback,
        validate_task_contract=validate_task_contract,
        persist_graph_transitions=live_updates,
    )

    def next_action() -> str:
        if task_graph.has_failed_nodes() or task_graph.has_blocked_nodes():
            return "repair_or_stop_failed_frontier"
        if task_graph.is_finished():
            return (
                "expand_requirement_frontier"
                if expand_callback is not None else "finish"
            )
        return "execute_next_frontier"

    epoch_target_warning_recorded = False
    while True:
        if task_graph.is_finished():
            if expand_callback is None:
                break
            expanded = await expand_callback(task_graph)
            if expanded is None or len(expanded.nodes) <= len(task_graph.nodes):
                break
            task_graph = expanded
            run_state.record_event("requirement_stage_graph_expanded", {
                "graph_version": task_graph.version,
                "total_generated_nodes": len(task_graph.nodes),
            })
            trace.append(TraceEvent("RequirementStageController", {
                "status": "graph_expanded",
                "graph_version": task_graph.version,
                "total_generated_nodes": len(task_graph.nodes),
            }))
        ready = task_graph.ready_nodes()
        if not ready:
            # Let the normal scheduler produce its deterministic deadlock or
            # blocked outcome and diagnostics.
            return await scheduler.run(task_graph, task_spec)
        targets = {
            node.node_id for node in ready[:max_active]
        }
        epoch += 1
        if not live_updates:
            run_state.begin_save_batch()
        if epoch > max_epochs and not epoch_target_warning_recorded:
            run_state.record_event("soft_horizon_epoch_target_exceeded", {
                "epoch": epoch,
                "configured_target": max_epochs,
                "warning": "继续执行；max_epochs 是观测与成本目标，不是完成语义",
            })
            epoch_target_warning_recorded = True
        before = {
            node.node_id: node.status.value for node in task_graph.nodes
        }
        task_graph = await scheduler.run(
            task_graph,
            task_spec,
            stop_after_node_ids=targets,
        )
        completed_now = sorted(
            node.node_id for node in task_graph.nodes
            if node.status.value == "completed"
            and before.get(node.node_id) != "completed"
        )
        previous_completed.update(completed_now)
        checkpoint_ref = None
        if policy.get("checkpoint_each_epoch", True):
            checkpoint_dir = run_dir / "checkpoints"
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            checkpoint_path = checkpoint_dir / f"epoch-{epoch:04d}.json"
            checkpoint_payload = {
                    "epoch": epoch,
                    "graph_id": task_graph.graph_id,
                    "graph_version": task_graph.version,
                    "active_node_ids": sorted(targets),
                    "completed_node_ids": completed_now,
                    "node_statuses": {
                        node.node_id: node.status.value
                        for node in task_graph.nodes
                    },
                    "protected_context": {
                        "global_goal": task_graph.goal,
                        "mandatory_artifacts": list(
                            task_spec.get("required_artifacts", [])
                        ),
                        "task_contract_digest": _protected_task_digest(task_spec),
                        "completed_node_ids": sorted(previous_completed),
                        "open_issue_ids": [
                            str(issue.get("issue_id"))
                            for issue in run_state.get_unresolved_blocking_issues()
                        ],
                        "artifact_refs": list(
                            run_state.get("artifacts", {}).get("produced", [])
                        ),
                    },
                    "task_graph": task_graph.model_dump(mode="json"),
                    "next_action": next_action(),
                    "resume_supported": True,
                }
            checkpoint_payload["integrity"] = {
                "algorithm": "sha256",
                "sha256": _checkpoint_digest(checkpoint_payload),
            }
            checkpoint_path.write_text(
                json.dumps(checkpoint_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            checkpoint_ref = checkpoint_path.relative_to(run_dir).as_posix()
            run_state.record_artifact(checkpoint_ref, save=False)
        logical_steps = sum(
            1 for event in run_state.get("events", [])
            if event.get("type") in {
                "graph_node_started",
                "graph_node_result_recorded",
                "node_recovery_decision",
                "horizon_epoch_recorded",
            }
        )
        run_state.record_horizon_epoch(
            epoch=epoch,
            mode="progressive",
            active_node_ids=sorted(targets),
            completed_node_ids=completed_now,
            total_generated_nodes=len(task_graph.nodes),
            total_completed_nodes=len(previous_completed),
            logical_step_count=logical_steps,
            next_action=next_action(),
            checkpoint_ref=checkpoint_ref,
        )
        if epoch % persist_every == 0 or task_graph.is_finished() or task_graph.has_failed_nodes() or task_graph.has_blocked_nodes():
            scheduler.persist_graph_transitions = True
            scheduler._save_graph(task_graph)
            scheduler.persist_graph_transitions = live_updates
            if not live_updates:
                run_state.end_save_batch(flush=True)
        elif not live_updates:
            run_state.end_save_batch(flush=False)
        trace.append(TraceEvent("HorizonController", {
            "epoch": epoch,
            "active_node_ids": sorted(targets),
            "completed_node_ids": completed_now,
            "total_completed_nodes": len(previous_completed),
            "checkpoint_ref": checkpoint_ref,
        }))
        if task_graph.has_failed_nodes() or task_graph.has_blocked_nodes():
            break
        if not completed_now and not task_graph.is_finished():
            run_state.record_event("horizon_no_progress", {
                "epoch": epoch,
                "target_node_ids": sorted(targets),
            })
            break

    return task_graph


def _control_prompt_budget(task_spec: dict) -> int:
    policy = task_spec.get("communication_policy", {})
    nested_budget = policy.get("budget", {}) if isinstance(policy, dict) else {}
    return int(
        policy.get(
            "max_control_prompt_tokens",
            nested_budget.get("max_control_prompt_tokens", 8_000),
        )
    )


def _bounded_json_value(value: object, limit: int) -> object:
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded) <= limit:
        return value
    return {
        "$truncated": True,
        "original_chars": len(encoded),
        "preview": encoded[:limit],
    }


def _artifact_evidence(task_spec: dict, limit: int = 4000) -> dict:
    run_dir = Path(task_spec["run_dir"])
    evidence = {}
    intermediate = task_spec.get("artifact_contract", {}).get(
        "intermediate_artifacts", task_spec.get("required_artifacts", [])
    )
    for relative in intermediate:
        path = run_dir / relative
        if not path.is_file() or relative in {"agent_trace.md", "final_report.md"}:
            continue
        if path.suffix.lower() == ".json":
            try:
                evidence[relative] = _bounded_json_value(
                    load_strict_json(path), limit,
                )
            except (OSError, ValueError) as exc:
                evidence[relative] = {"parse_error": str(exc)}
        elif path.suffix.lower() == ".py":
            evidence[relative] = {
                "exists": True,
                "size": path.stat().st_size,
                "entrypoint": relative,
            }
        elif path.suffix.lower() in {".md", ".txt"}:
            evidence[relative] = path.read_text(encoding="utf-8", errors="replace")[:limit]
        else:
            evidence[relative] = {"exists": True, "size": path.stat().st_size}
    return evidence


def _build_verified_report_fallback(task_spec: dict, evidence: dict) -> str:
    """Render a conservative report from evidence that passed the hard gates.

    This function is called only after ``pre_report_review`` permits report
    generation.  It does not infer missing business facts or turn a failed run
    into success; it only prevents a final prose-model outage from discarding
    an otherwise verified delivery.
    """
    sections = list(task_spec.get("required_report_sections") or [
        "任务理解", "任务拆解", "方法或模型设计",
        "代码实现或执行过程", "结果分析", "风险与改进建议",
    ])
    artifacts = evidence.get("artifacts") or {}
    graph = evidence.get("task_graph") or {}
    execution = evidence.get("execution") or {}
    business = evidence.get("business_validation") or {}
    artifact_lines = [f"- `{path}`：已由框架读取并纳入验收。" for path in artifacts]
    node_lines = [
        f"- {node.get('description') or node.get('node_id')}："
        f"{node.get('capability', 'unknown')}，状态 {node.get('status', 'unknown')}。"
        for node in graph.get("nodes", [])
    ]
    observed = []
    for path, value in artifacts.items():
        if not isinstance(value, dict) or value.get("$truncated"):
            continue
        metrics = []
        for key, item in value.items():
            if isinstance(item, (str, int, float, bool)) or item is None:
                metrics.append(f"{key}={item}")
            elif isinstance(item, list):
                metrics.append(f"{key} 条目数={len(item)}")
        if metrics:
            observed.append(f"- `{path}`：" + "，".join(metrics[:12]) + "。")
    content_by_section = {
        "任务理解": (
            f"本次任务为“{task_spec.get('task_name') or task_spec.get('task_type', '复杂任务')}”。"
            "系统以冻结任务契约为边界，只汇总已经执行并通过框架验收的结果。"
        ),
        "任务拆解": "任务采用依赖图逐步完成，主要节点如下：\n" + "\n".join(node_lines),
        "方法或模型设计": (
            "方法由结构化任务图、能力路由、真实工具执行和独立证据校验组成。"
            "业务结论以持久化产物为准，中间推导仅作为方法说明。"
        ),
        "代码实现或执行过程": (
            f"代码执行退出码为 {execution.get('exit_code', '不适用')}，"
            f"执行阶段为 {execution.get('phase', '已记录')}。已验收产物如下：\n"
            + "\n".join(artifact_lines)
        ),
        "结果分析": (
            f"独立业务验证状态为 {business.get('status', '已记录')}。"
            "以下仅列出产物中可直接读取的结果，不补造缺失数值：\n"
            + ("\n".join(observed) if observed else "- 详细结果见上述已验收产物。")
            + "\n参数变化对装载率与运输成本的影响应以成本比较和约束审计产物中的实际场景为依据。"
        ),
        "风险与改进建议": (
            "当前结果是通过确定性约束与证据检查的可复现实验结果。进一步优化应保留"
            "同一验收合同并扩大测试规模。面向物流企业管理决策时，可落地方案仍应结合"
            "实际车辆、道路和作业条件复核。"
        ),
    }
    lines = [f"# {task_spec.get('task_name') or '任务执行报告'}", ""]
    for section in sections:
        lines.extend([
            f"## {section}", "",
            content_by_section.get(
                section,
                "本节依据已验证任务图、执行记录和产物生成；详细事实见已验收产物。",
            ), "",
        ])
    return "\n".join(lines).strip() + "\n"


def _compact_task_graph(task_graph: TaskGraph) -> dict:
    return {
        "graph_id": task_graph.graph_id,
        "goal": task_graph.goal,
        "nodes": [
            {
                "node_id": node.node_id,
                "description": node.description,
                "capability": node.capability,
                "dependencies": list(node.dependencies),
                "status": node.status,
                "output_artifacts": list(node.output_artifacts),
            }
            for node in task_graph.nodes
        ],
    }


def _compact_analyses(analyses: dict) -> dict:
    compact = {}
    for node_id, result in analyses.items():
        if not isinstance(result, dict):
            compact[node_id] = str(result)[:1200]
            continue
        structured = result.get("structured_output", {})
        compact[node_id] = {
            "summary": str(result.get("summary", ""))[:1200],
            "findings": list(structured.get("findings", []))[:8]
            if isinstance(structured, dict) else [],
            "evidence_refs": list(result.get("evidence_refs", []))[:12],
            "status": result.get("status"),
        }
    return compact


async def run_explicit_workflow(
    task_spec: dict,
    run_state: RunState,
    model_client,
    file_model_client=None,
    web_model_client=None,
    resume_checkpoint: str | None = None,
) -> tuple[ReviewDecision, list]:
    run_dir = Path(task_spec["run_dir"])
    trace: list = [TraceEvent("Framework", {"stage": "prepare", "task_id": task_spec["task_id"]})]
    context = "\n当前任务由 Python 显式工作流控制；你只负责当前调用阶段。"

    # Framework-owned, task-independent input boundary. It preserves previews
    # without applying hidden domain transforms or selecting a task plugin.
    try:
        normalized_inputs = prepare_normalized_input(task_spec)
        normalized_path = run_dir / "normalized_input.json"
        normalized_path.write_text(
            json.dumps(normalized_inputs, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        run_state.record_artifact("normalized_input.json")
        run_state.record_event("inputs_normalized", {
            "normalizer": "generic_input_preparation",
            "path": "normalized_input.json",
        })
        trace.append(TraceEvent("InputNormalizer", {
            "normalizer": "generic_input_preparation",
            "path": "normalized_input.json",
        }))
    except Exception as exc:
        normalized_inputs = {}
        run_state.set_failure("input_normalization_failure", "input", "prepare", [str(exc)])
        trace.append(TraceEvent("InputNormalizer", {"status": "failed", "error": str(exc)}))

    registry = build_default_registry(
        model_client=model_client,
        run_state=run_state,
        trace=trace,
        normalized_inputs=normalized_inputs,
        task_spec=task_spec,
        file_model_client=file_model_client,
        web_model_client=web_model_client,
    )
    available_capabilities = registry.list_capabilities()

    # PLAN: the model proposes a real graph; Python validates and owns runtime state.
    run_state.start_stage("plan")
    trace.append(TraceEvent("Framework", {"stage": "plan"}))
    task_understanding: dict = {
        "status": "disabled",
        "summary": "",
        "goals": [],
        "constraints": [],
        "ambiguities": [],
        "risk_flags": [],
        "recommended_capabilities": [],
        "requirements": [],
        "confidence": 0.0,
    }
    if task_spec.get("task_understanding_policy", {}).get("enabled", False):
        understanding_error: Exception | None = None
        max_understanding_attempts = max(1, int(
            task_spec.get("task_understanding_policy", {}).get(
                "max_understanding_attempts", 2,
            )
        ))
        for understanding_attempt in range(1, max_understanding_attempts + 1):
            try:
                understanding_agent = create_task_understanding_agent(
                    model_client, extra_context=context,
                )
                understanding_prompt = build_task_understanding_prompt(
                    task_spec, available_capabilities,
                )
                if understanding_error is not None:
                    understanding_prompt += (
                        "\n\n上一轮结构化输出未通过解析："
                        f"{understanding_error}\n请重新返回完整、严格合法的 JSON；"
                        "不得删除来源需求或降低验收条件。"
                    )
                raw_understanding = await run_agent(
                    understanding_agent,
                    understanding_prompt,
                    trace,
                    run_state=run_state,
                    stage=(
                        "plan" if understanding_attempt == 1
                        else f"task_understanding_retry_{understanding_attempt}"
                    ),
                    prompt_budget_tokens=_control_prompt_budget(task_spec),
                )
                parsed_understanding = parse_model(
                    TaskUnderstandingResult, raw_understanding,
                )
                unavailable = sorted(
                    set(parsed_understanding.recommended_capabilities)
                    - set(available_capabilities)
                )
                if unavailable:
                    raise ValueError(
                        "TaskUnderstandingAgent 建议了不可用 capability: "
                        + ", ".join(unavailable)
                    )
                task_understanding = {
                    "status": "completed",
                    **parsed_understanding.model_dump(mode="json"),
                }
                understanding_path = run_dir / "planning" / "task_understanding.json"
                understanding_path.parent.mkdir(parents=True, exist_ok=True)
                understanding_path.write_text(
                    json.dumps(
                        task_understanding, ensure_ascii=False, indent=2,
                        allow_nan=False,
                    ),
                    encoding="utf-8",
                )
                run_state.record_artifact("planning/task_understanding.json")
                run_state.record_event(
                    "task_understanding_recorded", {
                        **task_understanding,
                        "attempt": understanding_attempt,
                    },
                )
                trace.append(TraceEvent(
                    "TaskUnderstandingAgent", task_understanding,
                ))
                understanding_error = None
                break
            except Exception as exc:
                understanding_error = exc
                run_state.record_event(
                    "task_understanding_attempt_failed", {
                        "attempt": understanding_attempt,
                        "max_attempts": max_understanding_attempts,
                        "error": str(exc),
                    },
                )
        if understanding_error is not None:
            task_understanding = {
                **task_understanding,
                "status": "failed",
                "ambiguities": [f"任务理解 Agent 不可用：{understanding_error}"],
            }
            run_state.record_event(
                "task_understanding_failed", {
                    "error": str(understanding_error),
                    "attempts": max_understanding_attempts,
                },
            )
            trace.append(TraceEvent("TaskUnderstandingAgent", {
                "status": "failed", "error": str(understanding_error),
            }))

    # REQUIREMENTS: compile an immutable, evidence-grounded contract before
    # PlanningAgent sees the task.  This is intentionally generic; it does not
    # encode any domain fields or create execution nodes.
    requirement_set = compile_requirement_set(task_spec, task_understanding)
    requirement_set = add_derived_source_traces(requirement_set)
    requirement_validation = validate_requirement_set(requirement_set)
    requirement_dir = run_dir / "planning"
    requirement_dir.mkdir(parents=True, exist_ok=True)

    def _write_requirement_round(round_number: int) -> None:
        (requirement_dir / f"requirement_contract_round_{round_number}.json").write_text(
            requirement_set.model_dump_json(indent=2),
            encoding="utf-8",
        )
        (requirement_dir / f"requirement_validation_round_{round_number}.json").write_text(
            requirement_validation.model_dump_json(indent=2),
            encoding="utf-8",
        )

    _write_requirement_round(0)
    max_requirement_rounds = max(0, int(
        task_spec.get("task_understanding_policy", {}).get(
            "max_requirement_revision_rounds", 2,
        )
    ))
    for round_number in range(1, max_requirement_rounds + 1):
        if (
            task_understanding.get("status") != "completed"
            or requirement_validation.passed
        ):
            break
        try:
            refinement_prompt = build_requirement_refinement_prompt(
                requirement_set.model_dump(mode="json"),
                requirement_validation.model_dump(mode="json"),
            )
            refined = None
            parse_error: Exception | None = None
            for format_attempt in range(1, 3):
                requirement_refiner = create_task_understanding_agent(
                    model_client, extra_context=context,
                )
                prompt = refinement_prompt
                if parse_error is not None:
                    prompt += (
                        "\n\n上一版细化内容未通过结构化解析，错误为："
                        f"{parse_error}\n只修复 JSON 结构并返回完整 RequirementSet；"
                        "不得删除已有需求、来源引用或降低验收条件。"
                    )
                raw_refinement = await run_agent(
                    requirement_refiner,
                    prompt,
                    trace,
                    run_state=run_state,
                    stage=(
                        f"requirement_refinement_round_{round_number}"
                        if format_attempt == 1
                        else (
                            f"requirement_refinement_round_{round_number}"
                            "_format_retry"
                        )
                    ),
                    prompt_budget_tokens=_control_prompt_budget(task_spec),
                )
                try:
                    refined = parse_model(RequirementSet, raw_refinement)
                    break
                except Exception as exc:
                    parse_error = exc
                    run_state.record_event(
                        "requirement_contract_format_retry", {
                            "round": round_number,
                            "format_attempt": format_attempt,
                            "max_format_attempts": 2,
                            "error": str(exc),
                        },
                    )
            if refined is None:
                raise parse_error or ValueError(
                    "RequirementSet 细化没有返回可解析结果"
                )
            refined = preserve_requirement_history(requirement_set, refined)
            requirement_set = compile_requirement_set(task_spec, {
                "summary": refined.global_goal,
                "requirements": [
                    item.model_dump(mode="json")
                    for item in refined.requirements
                ],
            })
            requirement_set = add_derived_source_traces(requirement_set)
            requirement_validation = validate_requirement_set(requirement_set)
            _write_requirement_round(round_number)
            run_state.record_event("requirement_contract_refined", {
                "round": round_number,
                "requirement_count": len(requirement_set.requirements),
                "validation": requirement_validation.model_dump(mode="json"),
            })
            trace.append(TraceEvent("RequirementCompiler", {
                "stage": "refinement",
                "round": round_number,
                "requirement_count": len(requirement_set.requirements),
                "validation": requirement_validation.model_dump(mode="json"),
            }))
        except Exception as exc:
            run_state.record_event(
                "requirement_contract_refinement_failed", {
                    "round": round_number, "error": str(exc),
                },
            )
            trace.append(TraceEvent("RequirementCompiler", {
                "stage": "refinement", "round": round_number,
                "status": "failed", "error": str(exc),
            }))
            # A malformed or transient model response must not discard the
            # remaining bounded refinement opportunities.  Keep the last
            # deterministically validated contract unchanged and retry the
            # next configured round.  The loop remains strictly bounded by
            # max_requirement_revision_rounds.
            continue
    requirement_payload = requirement_set.model_dump(mode="json")
    task_spec["requirement_contract"] = requirement_payload
    task_spec["requirement_contract_digest"] = requirement_digest(requirement_set)
    if any(
        item.mandatory and item.owner == "planner"
        for item in requirement_set.requirements
    ):
        task_spec.setdefault("business_validation", {})["mode"] = "required"
    (requirement_dir / "requirement_contract.json").write_text(
        json.dumps(requirement_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (requirement_dir / "requirement_validation.json").write_text(
        requirement_validation.model_dump_json(indent=2),
        encoding="utf-8",
    )
    # Keep the run's TaskSpec authoritative after the contract is frozen.
    (run_dir / "task_spec.json").write_text(
        json.dumps(task_spec, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    run_state.configure(task_spec)
    run_state.record_artifact("planning/requirement_contract.json")
    run_state.record_artifact("planning/requirement_validation.json")
    run_state.record_event("requirement_contract_compiled", {
        "requirement_count": len(requirement_set.requirements),
        "digest": task_spec["requirement_contract_digest"],
        "validation": requirement_validation.model_dump(mode="json"),
    })
    trace.append(TraceEvent("RequirementCompiler", {
        "requirement_count": len(requirement_set.requirements),
        "digest": task_spec["requirement_contract_digest"],
        "validation": requirement_validation.model_dump(mode="json"),
    }))
    if not requirement_validation.passed:
        run_state.record_event("requirement_contract_blocked", {
            "validation": requirement_validation.model_dump(mode="json"),
            "recovery_action": "refine_requirement_contract",
        })
        return _finish_planning_blocked(
            task_spec,
            run_state,
            trace,
            ValueError(
                "RequirementSet 在允许的细化轮次后仍未通过确定性校验："
                + "; ".join(
                    requirement_validation.revision_instructions
                    or [
                        "invalid=" + ",".join(
                            requirement_validation.invalid_requirement_ids
                        )
                    ]
                )
            ),
            failure_type="requirement_contract_invalid",
            recovery_action="refine_requirement_contract",
            issue_prefix="需求合同未通过校验",
        )
    workflow_memory: dict = {
        "selected_skill_ids": [],
        "selected_skills": [],
        "planning_guidance": [],
    }
    if task_spec.get("memory_policy", {}).get("enabled", False):
        try:
            workflow_memory = await retrieve_workflow_memory(
                task_spec, run_state, model_client, trace,
            )
            trace.append(TraceEvent("WorkflowMemory", {
                "stage": "retrieval",
                "selected_skill_ids": workflow_memory.get(
                    "selected_skill_ids", []
                ),
            }))
        except Exception as exc:
            # Memory is advisory. A malformed or unavailable store must not
            # take authority away from the current TaskSpec and planner.
            payload = {"stage": "retrieval", "error": str(exc)}
            run_state.record_event("workflow_memory_retrieval_failed", payload)
            trace.append(TraceEvent("WorkflowMemory", payload))
    task_graph: TaskGraph | None = None
    planning_errors: list[str] = []
    planning_policy, planning_complexity_score = resolve_planning_policy(
        task_spec, task_understanding,
    )
    hierarchical_selected = (
        planning_policy.mode == "hierarchical"
        or (
            planning_policy.mode == "auto"
            and planning_policy.hierarchical_validation_enabled
            and planning_complexity_score >= 4
        )
    )
    staged_expansion = requirement_staging_enabled(
        task_spec,
        requirement_set,
        hierarchical_selected=hierarchical_selected,
    )
    requirement_progress = (
        build_requirement_progress_ledger(requirement_set)
        if staged_expansion else None
    )
    stage_counter = 0
    governance_attached = False
    framework_owned_outputs = list(
        task_spec.get("artifact_contract", {}).get("intermediate_artifacts", [])
    )

    async def plan_requirement_stage(
        requirement_ids: list[str],
        stage: int,
        current_graph: TaskGraph | None,
    ) -> TaskGraph:
        """Plan, compile and append one dependency-ready requirement batch."""

        stage_spec = build_stage_task_spec(
            task_spec, requirement_set, requirement_ids, stage,
        )
        stage_subdir = f"planning/stages/stage-{stage:03d}"
        completed_stage_context_ref: str | None = None
        if current_graph is not None:
            stage_spec["completed_stage_context"] = [
                {
                    "node_id": node.node_id,
                    "description": node.description,
                    "capability": node.capability,
                    "requirement_ids": list(node.requirement_ids),
                    "expected_output": node.expected_output,
                    "structured_result": _bounded_json_value(
                        node.result or {}, 2_000,
                    ),
                    "output_artifacts": list(node.output_artifacts),
                }
                for node in current_graph.nodes[-24:]
                if node.status.value == "completed"
                and node.capability not in {
                    "terminal_execution", "evidence_verification",
                    "artifact_validation",
                }
            ]
            if stage_spec.get("code_policy", {}).get("mode") != "none":
                context_path = (
                    run_dir / stage_subdir / "completed_stage_context.json"
                )
                context_path.parent.mkdir(parents=True, exist_ok=True)
                context_path.write_text(
                    json.dumps(
                        stage_spec["completed_stage_context"],
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                completed_stage_context_ref = context_path.relative_to(
                    run_dir
                ).as_posix()
                run_state.record_artifact(completed_stage_context_ref)
        stage_available_capabilities = list(available_capabilities)
        if stage_spec.get("code_policy", {}).get("mode") == "none":
            stage_available_capabilities = [
                capability for capability in stage_available_capabilities
                if capability != "code"
            ]
        semantic_graph, metrics = await build_hierarchical_semantic_graph(
            task_spec=stage_spec,
            available_capabilities=stage_available_capabilities,
            model_client=model_client,
            run_dir=run_dir,
            trace=trace,
            run_state=run_state,
            task_understanding={
                **task_understanding,
                "summary": (
                    f"全局目标：{requirement_set.global_goal}\n"
                    f"当前阶段只闭合需求：{', '.join(requirement_ids)}"
                ),
            },
            extra_context=(
                context
                + "\n这是需求驱动的局部规划阶段。只规划 stage_scope 中的需求；"
                "已完成阶段通过结构化结果提供，不得重复规划。"
            ),
            planning_subdir=stage_subdir,
        )
        stage_dir = run_dir / stage_subdir
        semantic_path = stage_dir / "semantic_graph.json"
        semantic_path.write_text(
            semantic_graph.model_dump_json(indent=2), encoding="utf-8",
        )
        run_state.record_artifact(
            semantic_path.relative_to(run_dir).as_posix(),
        )
        compiled_stage, compile_report = compile_semantic_graph(
            semantic_graph,
            stage_spec,
            stage_available_capabilities,
            include_governance=False,
        )
        if completed_stage_context_ref:
            for node in compiled_stage.nodes:
                if node.capability == "code":
                    node.input_artifacts = list(dict.fromkeys([
                        *node.input_artifacts,
                        completed_stage_context_ref,
                    ]))
        compiled_stage, stage_repairs = enforce_stage_graph_contract(
            compiled_stage, stage_spec, stage_available_capabilities,
        )
        compile_report["repairs"].extend(stage_repairs)
        compile_report["stage_contract"] = {
            "stage": stage,
            "code_mode": stage_spec.get("code_policy", {}).get("mode"),
            "allowed_capabilities": stage_available_capabilities,
            "physical_artifacts": list(
                stage_spec.get("artifact_contract", {}).get(
                    "intermediate_artifacts", []
                )
            ),
            "completed_stage_context": completed_stage_context_ref,
        }
        compile_path = stage_dir / "graph_compile_report.json"
        compile_path.write_text(
            json.dumps(compile_report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        run_state.record_artifact(
            compile_path.relative_to(run_dir).as_posix(),
        )
        merged, producer_mapping = merge_stage_graph(
            current_graph,
            compiled_stage,
            stage=stage,
            global_goal=requirement_set.global_goal,
            available_capabilities=available_capabilities,
            framework_owned_artifacts=framework_owned_outputs,
        )
        requirement_progress.mark_planned(
            requirement_ids, stage, producer_mapping,
        )
        progress_ref = persist_requirement_progress(run_dir, requirement_progress)
        run_state.record_artifact(progress_ref)
        run_state.record_event("requirement_stage_planned", {
            "stage": stage,
            "requirement_ids": requirement_ids,
            "producer_mapping": producer_mapping,
            "added_node_count": len(compiled_stage.nodes),
            "total_generated_nodes": len(merged.nodes),
            "planning_metrics": metrics,
        })
        trace.append(TraceEvent("RequirementStagePlanner", {
            "stage": stage,
            "requirement_ids": requirement_ids,
            "producer_mapping": producer_mapping,
            "added_node_count": len(compiled_stage.nodes),
            "total_generated_nodes": len(merged.nodes),
        }))
        return merged

    run_state.record_event("planning_policy_resolved", {
        "mode": planning_policy.mode,
        "selected_mode": "hierarchical" if hierarchical_selected else "legacy",
        "complexity_score": planning_complexity_score,
        "requirement_driven_expansion": staged_expansion,
    })
    if hierarchical_selected:
        try:
            if staged_expansion:
                horizon_policy = task_spec.get("horizon_policy") or {}
                initial_batch = select_requirement_batch(
                    requirement_progress,
                    requirement_set,
                    max_requirements=int(
                        horizon_policy.get("max_requirements_per_stage", 6)
                    ),
                    max_prompt_tokens=int(
                        horizon_policy.get("max_stage_prompt_tokens", 6_000)
                    ),
                )
                if not initial_batch:
                    raise PlanningValidationError(
                        "Requirement Progress Ledger 没有可规划的初始需求"
                    )
                stage_counter = 1
                task_graph = await plan_requirement_stage(
                    initial_batch, stage_counter, None,
                )
                run_state.record_event("requirement_driven_expansion_started", {
                    "leaf_requirement_count": len(requirement_progress.items),
                    "initial_requirement_ids": initial_batch,
                    "progress_path": "planning/requirement_progress.json",
                })
            else:
                semantic_graph, planning_metrics = await build_hierarchical_semantic_graph(
                    task_spec=task_spec,
                    available_capabilities=available_capabilities,
                    model_client=model_client,
                    run_dir=run_dir,
                    trace=trace,
                    run_state=run_state,
                    task_understanding=task_understanding,
                    extra_context=context,
                )
                semantic_path = run_dir / "planning" / "semantic_graph.json"
                semantic_path.write_text(
                    semantic_graph.model_dump_json(indent=2), encoding="utf-8",
                )
                run_state.record_artifact("planning/semantic_graph.json")
                compiled_graph, compile_report = compile_semantic_graph(
                    semantic_graph, task_spec, available_capabilities,
                )
                compile_path = run_dir / "planning" / "graph_compile_report.json"
                compile_path.write_text(
                    json.dumps(compile_report, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                run_state.record_artifact("planning/graph_compile_report.json")
                run_state.record_event("hierarchical_planning_metrics", planning_metrics)
                trace.append(TraceEvent("HierarchicalPlanner", planning_metrics))
                trace.append(TraceEvent("GraphCompiler", compile_report))
                task_graph = validate_planned_graph(
                    compiled_graph.model_dump(mode="json"),
                    task_spec,
                    available_capabilities,
                )
        except PlanningValidationError as exc:
            planning_errors = [str(exc)]
            run_state.record_event("hierarchical_planning_failed", {
                "error": str(exc),
                "validation": exc.validation.model_dump(mode="json") if exc.validation else {},
                "fallback_allowed": planning_policy.fallback_to_legacy_plan,
            })
            trace.append(TraceEvent("HierarchicalPlanner", {
                "status": "failed", "error": str(exc),
            }))
            if not planning_policy.fallback_to_legacy_plan:
                return _finish_planning_blocked(
                    task_spec, run_state, trace, exc,
                )
            hierarchical_selected = False
            staged_expansion = False
            requirement_progress = None
        except Exception as exc:
            planning_errors = [str(exc)]
            run_state.record_event("hierarchical_planning_failed", {
                "error": str(exc),
                "fallback_allowed": planning_policy.fallback_to_legacy_plan,
            })
            if not planning_policy.fallback_to_legacy_plan:
                return _finish_planning_blocked(
                    task_spec, run_state, trace, exc,
                )
            hierarchical_selected = False
            staged_expansion = False
            requirement_progress = None

    planning_attempts = range(1, 3) if not hierarchical_selected else range(0)
    for planning_attempt in planning_attempts:
        try:
            # A repair prompt already carries the previous validation errors.
            # Use a fresh planner so the first full prompt/response is not sent
            # again as hidden chat history on the second attempt.
            planner = create_planning_agent(model_client, extra_context=context)
            raw_graph = await run_agent(
                planner,
                build_planning_prompt(
                    task_spec,
                    available_capabilities,
                    planning_errors,
                    workflow_memory=workflow_memory,
                    task_understanding=task_understanding,
                ),
                trace,
                run_state=run_state,
                stage="plan",
                prompt_budget_tokens=_control_prompt_budget(task_spec),
            )
            # The planner owns only the semantic/business graph.  Compile it
            # into the strict executable graph before scheduler validation.
            semantic_path = run_dir / "planning" / "semantic_graph.raw.json"
            semantic_path.parent.mkdir(parents=True, exist_ok=True)
            semantic_path.write_text(
                json.dumps(raw_graph, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            run_state.record_artifact("planning/semantic_graph.raw.json")
            semantic_graph = coerce_semantic_graph(raw_graph)
            normalized_semantic_path = run_dir / "planning" / "semantic_graph.json"
            normalized_semantic_path.write_text(
                semantic_graph.model_dump_json(indent=2), encoding="utf-8",
            )
            run_state.record_artifact("planning/semantic_graph.json")
            compiled_graph, compile_report = compile_semantic_graph(
                semantic_graph, task_spec, available_capabilities,
            )
            compile_path = run_dir / "planning" / "graph_compile_report.json"
            compile_path.write_text(
                json.dumps(compile_report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            run_state.record_artifact("planning/graph_compile_report.json")
            task_graph = validate_planned_graph(
                compiled_graph.model_dump(mode="json"),
                task_spec,
                available_capabilities,
            )
            trace.append(TraceEvent("GraphCompiler", compile_report))
            break
        except Exception as exc:
            planning_errors = [str(exc)]
            payload = {
                "attempt": planning_attempt,
                "max_attempts": 2,
                "issues": planning_errors,
            }
            run_state.record_event("task_graph_validation_failed", payload)
            trace.append(TraceEvent("GraphPlanner", {"status": "invalid", **payload}))
    if task_graph is None:
        reason = planning_errors[-1] if planning_errors else "规划模型没有返回任务图"
        task_graph = build_fallback_graph(task_spec, available_capabilities, reason)
        run_state.trigger_fallback(f"TaskGraph 规划失败，使用确定性图：{reason}")
        trace.append(TraceEvent("GraphPlanner", {
            "status": "fallback", "reason": reason,
            "graph": task_graph.model_dump(mode="json"),
        }))

    task_graph_path = run_dir / "task_graph.json"
    task_graph_path.write_text(task_graph.model_dump_json(indent=2), encoding="utf-8")
    run_state.record_plan(_build_graph_plan(task_graph, available_capabilities))
    run_state.record_task_graph(task_graph)
    run_state.record_artifact("task_graph.json")
    trace.append(TraceEvent("GraphPlanner", {
        "status": "validated",
        "graph_id": task_graph.graph_id,
        "topological_order": task_graph.topological_order(),
    }))

    # EXECUTE GRAPH: single-thread scheduling, dynamic capability routing.
    run_state.start_stage("execute")
    trace.append(TraceEvent("Framework", {
        "stage": "execute", "execution_mode": "task_graph",
        "graph_id": task_graph.graph_id,
    }))
    replan_callback = None
    if (
        not staged_expansion
        and int(task_spec.get("recovery_policy", {}).get("max_replans", 0)) > 0
    ):
        async def replan_callback(current_graph, failed_node, failure_result, attempt):
            """Ask for one full replacement graph; Scheduler owns the safe merge."""
            trace.append(TraceEvent("GraphReplanner", {
                "status": "requested",
                "attempt": attempt,
                "failed_node_id": failed_node.node_id,
                "error_type": failure_result.error_type,
            }))
            replanner = create_planning_agent(
                model_client,
                extra_context=(
                    context
                    + "\n这是执行期重规划。只返回完整替代 TaskGraph，已完成节点不可修改。"
                ),
            )
            raw_replacement = await run_agent(
                replanner,
                build_replanning_prompt(
                    task_spec,
                    current_graph.model_dump(mode="json"),
                    failure_result.model_dump(mode="json"),
                    available_capabilities,
                    task_understanding=task_understanding,
                ),
                trace,
                run_state=run_state,
                stage="execute",
                node_id=failed_node.node_id,
                prompt_budget_tokens=_control_prompt_budget(task_spec),
            )
            replacement_semantic = coerce_semantic_graph(raw_replacement)
            replacement, compile_report = compile_semantic_graph(
                replacement_semantic,
                task_spec,
                available_capabilities,
            )
            replacement = validate_planned_graph(
                replacement.model_dump(mode="json"),
                task_spec,
                available_capabilities,
            )
            trace.append(TraceEvent("GraphReplanner", {
                "status": "validated",
                "attempt": attempt,
                "node_count": len(replacement.nodes),
                "compile_report": compile_report,
            }))
            return replacement

    async def expand_requirement_frontier(
        current_graph: TaskGraph,
    ) -> TaskGraph | None:
        """Turn newly ready requirements into the next executable subgraph."""

        nonlocal stage_counter, governance_attached
        requirement_progress.refresh_from_graph(current_graph)
        progress_ref = persist_requirement_progress(run_dir, requirement_progress)
        run_state.record_artifact(progress_ref)
        failed_items = [
            item.requirement_id for item in requirement_progress.items
            if item.status in {"failed", "blocked"}
        ]
        if failed_items:
            run_state.record_event("requirement_expansion_paused", {
                "reason": "producer_failed_or_blocked",
                "requirement_ids": failed_items,
            })
            return None
        pending = requirement_progress.pending_ids()
        if not pending:
            if governance_attached:
                return None
            governed = append_governance_nodes(
                current_graph, task_spec, available_capabilities,
            )
            governance_attached = True
            governed_path = run_dir / "task_graph.json"
            governed_path.write_text(
                governed.model_dump_json(indent=2), encoding="utf-8",
            )
            run_state.record_event("global_governance_appended", {
                "graph_version": governed.version,
                "governance_node_ids": [
                    node.node_id for node in governed.nodes
                    if node.capability in {
                        "terminal_execution", "evidence_verification",
                        "artifact_validation",
                    }
                ],
            })
            return governed
        policy = task_spec.get("horizon_policy") or {}
        batch = select_requirement_batch(
            requirement_progress,
            requirement_set,
            max_requirements=int(policy.get("max_requirements_per_stage", 6)),
            max_prompt_tokens=int(policy.get("max_stage_prompt_tokens", 6_000)),
        )
        if not batch:
            for requirement_id in pending:
                requirement_progress.item(requirement_id).status = "blocked"
            persist_requirement_progress(run_dir, requirement_progress)
            run_state.record_event("requirement_expansion_paused", {
                "reason": "requirement_dependency_deadlock",
                "pending_requirement_ids": pending,
            })
            return None
        stage_counter += 1
        configured_target = int(policy.get("max_stages", 64))
        if stage_counter > configured_target:
            run_state.record_event("soft_stage_target_exceeded", {
                "stage": stage_counter,
                "configured_target": configured_target,
                "warning": "继续展开；max_stages 是成本观测目标，不是完成上限",
            })
        try:
            expanded = await plan_requirement_stage(
                batch, stage_counter, current_graph,
            )
        except Exception as exc:
            reason = str(exc)
            requirement_progress.mark_stage_blocked(
                batch, stage_counter, reason,
            )
            persist_requirement_progress(run_dir, requirement_progress)
            run_state.record_event("requirement_stage_planning_blocked", {
                "stage": stage_counter,
                "requirement_ids": batch,
                "error": reason,
                "recoverable": True,
                "recovery_action": "revise_or_split_requirement_stage",
                "preserved_completed_node_ids": [
                    node.node_id for node in current_graph.nodes
                    if node.status.value == "completed"
                ],
            })
            trace.append(TraceEvent("RequirementStagePlanner", {
                "stage": stage_counter,
                "status": "blocked",
                "requirement_ids": batch,
                "error": reason,
                "recovery_action": "revise_or_split_requirement_stage",
            }))
            return None
        (run_dir / "task_graph.json").write_text(
            expanded.model_dump_json(indent=2), encoding="utf-8",
        )
        return expanded

    if staged_expansion or _progressive_execution_enabled(task_spec, task_graph):
        task_graph = await _execute_graph_in_horizons(
            task_graph=task_graph,
            task_spec=task_spec,
            registry=registry,
            run_state=run_state,
            run_dir=run_dir,
            replan_callback=replan_callback,
            trace=trace,
            validate_task_contract=not staged_expansion,
            expand_callback=(
                expand_requirement_frontier if staged_expansion else None
            ),
            resume_checkpoint=resume_checkpoint,
        )
    else:
        task_graph = await GraphScheduler(
            registry, run_state, run_dir, replan_callback=replan_callback,
        ).run(task_graph, task_spec)

    def current_execution_result() -> ExecutionResult | None:
        history = run_state.get("execution", {}).get("history", [])
        return ExecutionResult.model_validate(history[-1]) if history else None

    def validate_current_business():
        latest_execution = current_execution_result()
        return run_business_validation(
            run_dir,
            task_spec,
            latest_execution.model_dump() if latest_execution else None,
            task_graph=task_graph,
        )

    business_validation = validate_current_business()
    run_state.record_artifact("review/business_validation.json")
    run_state.record_artifact("review/business_validation.md")
    if business_validation.requirement_ledger is not None:
        for relative in [
            "review/acceptance_contract.json",
            "review/requirement_ledger.json",
            "review/requirement_ledger.md",
        ]:
            run_state.record_artifact(relative)
        run_state.record_requirement_ledger(
            business_validation.requirement_ledger.model_dump(mode="json"),
            "review/requirement_ledger.json",
        )
    run_state.record_business_validation(
        business_validation.model_dump(mode="json"),
        "review/business_validation.json",
    )
    trace.append(TraceEvent("FrameworkBusinessValidation", {
        "status": business_validation.status,
        "mode": business_validation.mode,
        "failed_required_checks": (
            business_validation.failed_required_checks
        ),
        "unresolved_validators": business_validation.unresolved_validators,
    }))

    async def replay_repaired_graph(reopened_graph: TaskGraph) -> TaskGraph:
        trace.append(TraceEvent("BusinessRepairLoop", {
            "status": "replay_started",
            "pending_node_ids": [
                node.node_id for node in reopened_graph.nodes
                if node.status.value == "pending"
            ],
        }))
        return await GraphScheduler(
            registry,
            run_state,
            run_dir,
            replan_callback=replan_callback,
        ).run(reopened_graph, task_spec)

    incomplete_requirement_ids = (
        [
            item.requirement_id for item in requirement_progress.items
            if item.status in {"pending", "planned", "failed", "blocked"}
        ]
        if requirement_progress is not None else []
    )
    if incomplete_requirement_ids and business_validation.status == "passed":
        # A missing future stage is not a defect in a completed producer.
        # Reopening earlier nodes cannot create that stage and would discard
        # the exact checkpoint semantics staged execution is meant to retain.
        run_state.record_event("business_repair_skipped", {
            "reason": "requirement_expansion_incomplete",
            "requirement_ids": incomplete_requirement_ids,
            "recovery_action": "revise_or_split_requirement_stage",
            "preserved_completed_node_ids": [
                node.node_id for node in task_graph.nodes
                if node.status.value == "completed"
            ],
        })
        repair_outcome = BusinessRepairOutcome(
            status="blocked",
            graph=task_graph,
            validation=business_validation,
            remaining_issue_ids=list(
                business_validation.failed_required_checks
            ),
        )
    else:
        repair_loop = BusinessRepairLoop(
            run_state,
            max_rounds=int(
                task_spec.get("recovery_policy", {}).get(
                    "max_business_repair_rounds", 0,
                )
            ),
        )
        repair_outcome = await repair_loop.run(
            task_graph,
            business_validation,
            replay=replay_repaired_graph,
            validate=validate_current_business,
        )
    task_graph = repair_outcome.graph
    business_validation = repair_outcome.validation
    if business_validation.requirement_ledger is not None:
        run_state.record_requirement_ledger(
            business_validation.requirement_ledger.model_dump(mode="json"),
            "review/requirement_ledger.json",
        )
        if requirement_progress is not None:
            requirement_progress.apply_acceptance_ledger(
                business_validation.requirement_ledger,
            )
            persist_requirement_progress(run_dir, requirement_progress)
    trace.append(TraceEvent("BusinessRepairLoop", {
        "status": repair_outcome.status,
        "rounds_used": repair_outcome.rounds_used,
        "target_node_ids": repair_outcome.target_node_ids,
        "affected_node_ids": repair_outcome.affected_node_ids,
        "resolved_issue_ids": repair_outcome.resolved_issue_ids,
        "remaining_issue_ids": repair_outcome.remaining_issue_ids,
        "final_business_validation": business_validation.status,
    }))

    # A repair replay may have produced a newer execution and quality result.
    execution_result = current_execution_result()
    quality_payload = run_state.get("artifact_quality", {}).get("current")
    if quality_payload is None:
        artifact_quality = validate_intermediate_artifacts(task_spec, run_dir)
        run_state.record_artifact_quality(artifact_quality)
    else:
        artifact_quality = ArtifactQualityResult.model_validate(quality_payload)

    analyses = {
        node.node_id: node.result
        for node in task_graph.nodes
        if node.result and node.capability in {
            "analysis", "research", "reasoning", "math_modeling", "document_extraction",
            "document_recovery", "document_conversion", "file_navigation",
            "web_research", "browser_navigation", "evidence_verification",
        }
    }

    run_state.refresh_artifacts()
    save_agent_trace(trace, run_dir / "agent_trace.md")
    run_state.record_artifact("agent_trace.md")

    if task_spec.get("accept_runtime_injections"):
        from utils.runtime_injection_queue import close_injection_queue
        unconsumed = close_injection_queue(run_dir)
        if unconsumed:
            run_state.record_blocking_issues([
                {"issue_id": item["injection_id"],
                 "description": "运行结束时仍有未执行的动态注入，不能宣称完成恢复"}
                for item in unconsumed
            ], source="runtime_injection_controller")
    injection_runtime = run_state.get("runtime_injections", {})
    if injection_runtime.get("enabled"):
        unresolved_injections = [
            {
                "issue_id": f"runtime-injection-{injection_id}",
                "description": (
                    f"运行期注入 {injection_id} 未完成恢复："
                    f"{item.get('status', 'unknown')}"
                ),
                "injection_id": injection_id,
                "injection_type": item.get("type"),
            }
            for injection_id, item in injection_runtime.get("items", {}).items()
            if item.get("status") != "recovered"
        ]
        if unresolved_injections:
            run_state.record_blocking_issues(
                unresolved_injections, source="runtime_injection_controller",
            )
        else:
            run_state.record_event("runtime_injection_demo_completed", {
                "requested_types": injection_runtime.get("requested_types", []),
                "recovered_count": injection_runtime.get("recovered_count", 0),
                "status": "passed",
            })

    # A business-repair replay can recover a graph after an earlier failed
    # attempt.  Refresh the outcome before review so stale failure state does
    # not block report generation or delivery acceptance.
    run_state.finish_graph(
        "failed" if task_graph.has_failed_nodes()
        else "blocked" if task_graph.has_blocked_nodes()
        else "success"
    )

    # REVIEW
    run_state.start_stage("review")
    trace.append(TraceEvent("Framework", {"stage": "review"}))
    framework_review = pre_report_review(task_spec, run_state.to_dict(), artifact_quality)
    review_evidence = {
        "framework_review": framework_review.model_dump(),
        "execution": execution_result.model_dump() if execution_result else None,
        "artifacts": _artifact_evidence(task_spec),
        "business_validation": business_validation.model_dump(mode="json"),
    }
    semantic_review = SemanticReviewResult()
    if framework_review.can_generate_final_report:
        reviewer = create_review_agent(model_client, extra_context=context)
        try:
            raw_review = await run_agent(
                reviewer, build_review_prompt(task_spec, review_evidence), trace,
                run_state=run_state, stage="review",
                prompt_budget_tokens=_control_prompt_budget(task_spec),
            )
            semantic_review = parse_model(SemanticReviewResult, raw_review)
        except Exception as exc:
            semantic_review.advisory_issues.append(f"语义审查输出解析失败：{exc}")
    if semantic_review.blocking_issues:
        run_state.record_blocking_issues(
            semantic_review.blocking_issues,
            source="review_agent",
        )
    run_state.record_event("semantic_review_recorded", semantic_review.model_dump())
    # Only the deterministic technical gate controls the report transition.
    review = framework_review
    run_state.record_review(review)

    # REPORT -- exactly one call, only after review allows it.
    if run_state.allow_report():
        run_state.start_stage("report")
        trace.append(TraceEvent("Framework", {"stage": "report", "single_call": True}))
        report_evidence = {
            "review_status": review.status,
            "review_issues": review.issues,
            "task_graph": _compact_task_graph(task_graph),
            "analysis": _compact_analyses(analyses),
            "execution": execution_result.model_dump() if execution_result else None,
            "artifact_quality": artifact_quality.model_dump(),
            "business_validation": business_validation.model_dump(mode="json"),
            "semantic_review": semantic_review.model_dump(),
            "artifacts": _artifact_evidence(task_spec),
        }
        reporter = create_report_agent(model_client, extra_context=context)
        try:
            report_content = content_to_text(await run_agent(
                reporter, build_report_prompt(task_spec, report_evidence), trace,
                run_state=run_state, stage="report",
                prompt_budget_tokens=_control_prompt_budget(task_spec),
            ))
            if not report_content.strip():
                raise ValueError("ReportAgent 返回空内容")
            save_final_report(report_content, run_dir / "final_report.md")
            run_state.record_artifact("final_report.md")
        except Exception as exc:
            report_content = _build_verified_report_fallback(
                task_spec, report_evidence,
            )
            save_final_report(report_content, run_dir / "final_report.md")
            run_state.record_artifact("final_report.md")
            run_state.record_event("verified_report_fallback_used", {
                "reason": str(exc),
                "source": "framework_verified_evidence",
                "report_path": "final_report.md",
            })
            trace.append(TraceEvent("Framework", {
                "report_generation_failed": str(exc),
                "verified_report_fallback_used": True,
            }))

    # Complete delivery-phase requirement checks only after the one permitted
    # ReportAgent call.  The merged ledger is the authoritative final closure
    # gate; deterministic report-content checks still run below.
    acceptance_contract = compile_acceptance_contract(
        task_spec["requirement_contract"], task_graph, task_spec,
    )
    delivery_ledger = run_acceptance_contract(
        run_dir, acceptance_contract, phase="delivery",
    )
    final_requirement_ledger = merge_requirement_ledgers(
        business_validation.requirement_ledger,
        delivery_ledger if delivery_ledger.entries else None,
    )
    write_acceptance_outputs(
        run_dir, acceptance_contract, final_requirement_ledger,
    )
    run_state.record_requirement_ledger(
        final_requirement_ledger.model_dump(mode="json"),
        "review/requirement_ledger.json",
    )
    if requirement_progress is not None:
        requirement_progress.apply_acceptance_ledger(final_requirement_ledger)
        persist_requirement_progress(run_dir, requirement_progress)
    for relative in [
        "review/acceptance_contract.json",
        "review/requirement_ledger.json",
        "review/requirement_ledger.md",
    ]:
        run_state.record_artifact(relative)

    # Persist the fully expanded graph with final runtime states.  Earlier
    # stage files remain immutable planning evidence.
    task_graph_path.write_text(
        task_graph.model_dump_json(indent=2), encoding="utf-8",
    )
    run_state.record_plan(_build_graph_plan(task_graph, available_capabilities))
    run_state.record_task_graph(task_graph)
    run_state.finish_graph(
        "failed" if task_graph.has_failed_nodes()
        else "blocked" if task_graph.has_blocked_nodes()
        else "success"
    )

    # FINAL VALIDATE
    run_state.start_stage("final_validate")
    trace.append(TraceEvent("Framework", {"stage": "final_validate"}))
    save_agent_trace(trace, run_dir / "agent_trace.md")
    run_state.refresh_artifacts()
    final_decision = final_validation(task_spec, run_state.to_dict())
    if final_decision.status == "failed":
        existing_failure = run_state.to_dict().get("failure")
        if existing_failure:
            run_state.record_event("final_validation_failed", {
                "issues": final_decision.issues,
                "root_failure_preserved": existing_failure,
            })
        else:
            run_state.set_failure("final_validation_failure", "report", "report", final_decision.issues)
    run_state.record_review(final_decision)
    run_state.record_delivery_outcome(
        final_decision.status,
        ["final_report.md", "review/validation_report.md"],
    )
    validation_path = write_validation_report(run_dir, final_decision, run_state.to_dict())
    run_state.record_artifact(validation_path)
    trace.append(TraceEvent("Framework", {"final_validation": final_decision.model_dump()}))
    save_agent_trace(trace, run_dir / "agent_trace.md")

    # Only after deterministic final validation may the memory Agent propose
    # reusable guidance. Python filters and owns the persistent store.
    if task_spec.get("memory_policy", {}).get("enabled", False):
        try:
            added_skill_ids = await curate_workflow_memory(
                task_spec,
                task_graph.model_dump(mode="json"),
                final_decision.model_dump(mode="json"),
                run_state,
                model_client,
                trace,
            )
            trace.append(TraceEvent("WorkflowMemory", {
                "stage": "curation",
                "added_skill_ids": added_skill_ids,
            }))
        except Exception as exc:
            payload = {"stage": "curation", "error": str(exc)}
            run_state.record_event("workflow_memory_curation_failed", payload)
            trace.append(TraceEvent("WorkflowMemory", payload))
        save_agent_trace(trace, run_dir / "agent_trace.md")

    outcome = {"passed": "success", "partial": "partial", "failed": "failed"}[final_decision.status]
    run_state.finish(outcome)
    return final_decision, trace
