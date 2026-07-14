"""Explicit, framework-controlled multi-agent workflow."""

from __future__ import annotations

import json
from pathlib import Path

from agents.planning_agent import create_planning_agent
from agents.report_agent import create_report_agent
from agents.review_agent import create_review_agent
from orchestration.default_executors import build_default_registry
from orchestration.graph_planner import build_fallback_graph, validate_planned_graph
from orchestration.graph_scheduler import GraphScheduler
from orchestration.prompt_builder import (
    build_planning_prompt,
    build_report_prompt,
    build_review_prompt,
)
from orchestration.model_calls import run_agent
from orchestration.run_state import RunState
from orchestration.schemas import ArtifactQualityResult, ExecutionResult, ReviewDecision, SemanticReviewResult, parse_model
from orchestration.task_graph import TaskGraph
from task_plugins import get_task_plugin
from task_plugins.base import load_strict_json
from utils.output import TraceEvent, content_to_text, save_agent_trace, save_final_report
from utils.review_artifacts import (
    final_validation,
    pre_report_review,
    write_validation_report,
)


_run_agent = run_agent  # Compatibility for integrations importing the old helper.


def _artifact_evidence(task_spec: dict, limit: int = 12000) -> dict:
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
                evidence[relative] = load_strict_json(path)
            except (OSError, ValueError) as exc:
                evidence[relative] = {"parse_error": str(exc)}
        elif path.suffix.lower() in {".md", ".txt", ".py"}:
            evidence[relative] = path.read_text(encoding="utf-8", errors="replace")[:limit]
        else:
            evidence[relative] = {"exists": True, "size": path.stat().st_size}
    return evidence


async def run_explicit_workflow(
    task_spec: dict,
    run_state: RunState,
    model_client,
    file_model_client=None,
) -> tuple[ReviewDecision, list]:
    run_dir = Path(task_spec["run_dir"])
    trace: list = [TraceEvent("Framework", {"stage": "prepare", "task_id": task_spec["task_id"]})]
    context = "\n当前任务由 Python 显式工作流控制；你只负责当前调用阶段。"
    plugin = get_task_plugin(task_spec.get("plugin_id"))

    # Framework-owned deterministic input normalization. Generated code consumes
    # this stable boundary instead of guessing heterogeneous source layouts.
    try:
        normalized_inputs = plugin.prepare_inputs(task_spec, run_dir)
        normalized_path = run_dir / "normalized_input.json"
        normalized_path.write_text(
            json.dumps(normalized_inputs, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        run_state.record_artifact("normalized_input.json")
        run_state.record_event("inputs_normalized", {
            "plugin_id": task_spec.get("plugin_id", "generic"),
            "path": "normalized_input.json",
        })
        trace.append(TraceEvent("InputNormalizer", {
            "plugin_id": task_spec.get("plugin_id", "generic"),
            "path": "normalized_input.json",
        }))
    except Exception as exc:
        normalized_inputs = {}
        run_state.set_failure("input_normalization_failure", "input", "prepare", [str(exc)])
        trace.append(TraceEvent("InputNormalizer", {"status": "failed", "error": str(exc)}))

    registry = build_default_registry(
        model_client=model_client,
        plugin=plugin,
        run_state=run_state,
        trace=trace,
        normalized_inputs=normalized_inputs,
    )
    available_capabilities = registry.list_capabilities()

    # PLAN: the model proposes a real graph; Python validates and owns runtime state.
    run_state.start_stage("plan")
    trace.append(TraceEvent("Framework", {"stage": "plan"}))
    planner = create_planning_agent(model_client, extra_context=context)
    task_graph: TaskGraph | None = None
    planning_errors: list[str] = []
    for planning_attempt in range(1, 3):
        try:
            raw_graph = await _run_agent(
                planner,
                build_planning_prompt(task_spec, available_capabilities, planning_errors),
                trace,
            )
            task_graph = validate_planned_graph(raw_graph, task_spec, available_capabilities)
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
    run_state.record_plan({
        "execution_mode": "task_graph",
        "graph_id": task_graph.graph_id,
        "graph_version": task_graph.version,
        "goal": task_graph.goal,
        "node_count": len(task_graph.nodes),
        "available_capabilities": available_capabilities,
    })
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
    task_graph = await GraphScheduler(registry, run_state, run_dir).run(task_graph, task_spec)

    state_after_graph = run_state.to_dict()
    execution_history = state_after_graph.get("execution", {}).get("history", [])
    execution_result = ExecutionResult.model_validate(execution_history[-1]) if execution_history else None
    quality_payload = state_after_graph.get("artifact_quality", {}).get("current")
    if quality_payload is None:
        artifact_quality = plugin.validate_intermediate(task_spec, run_dir)
        run_state.record_artifact_quality(artifact_quality)
    else:
        artifact_quality = ArtifactQualityResult.model_validate(quality_payload)

    analyses = {
        node.node_id: node.result
        for node in task_graph.nodes
        if node.result and node.capability in {
            "analysis", "research", "reasoning", "math_modeling", "document_extraction",
        }
    }

    run_state.refresh_artifacts()
    save_agent_trace(trace, run_dir / "agent_trace.md")
    run_state.record_artifact("agent_trace.md")

    # REVIEW
    run_state.start_stage("review")
    trace.append(TraceEvent("Framework", {"stage": "review"}))
    framework_review = pre_report_review(task_spec, run_state.to_dict(), artifact_quality)
    review_evidence = {
        "framework_review": framework_review.model_dump(),
        "execution": execution_result.model_dump() if execution_result else None,
        "artifacts": _artifact_evidence(task_spec),
    }
    semantic_review = SemanticReviewResult()
    if framework_review.can_generate_final_report:
        reviewer = create_review_agent(model_client, extra_context=context)
        try:
            raw_review = await _run_agent(reviewer, build_review_prompt(task_spec, review_evidence), trace)
            semantic_review = parse_model(SemanticReviewResult, raw_review)
        except Exception as exc:
            semantic_review.advisory_issues.append(f"语义审查输出解析失败：{exc}")
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
            "task_graph": task_graph.model_dump(mode="json"),
            "analysis": analyses,
            "execution": execution_result.model_dump() if execution_result else None,
            "artifact_quality": artifact_quality.model_dump(),
            "semantic_review": semantic_review.model_dump(),
            "artifacts": _artifact_evidence(task_spec),
        }
        reporter = create_report_agent(model_client, extra_context=context)
        try:
            report_content = content_to_text(await _run_agent(
                reporter, build_report_prompt(task_spec, report_evidence), trace
            ))
            if not report_content.strip():
                raise ValueError("ReportAgent 返回空内容")
            save_final_report(report_content, run_dir / "final_report.md")
            run_state.record_artifact("final_report.md")
        except Exception as exc:
            trace.append(TraceEvent("Framework", {"report_generation_failed": str(exc)}))
            run_state.set_failure("report_generation_failure", "report", "report", [str(exc)])

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
    validation_path = write_validation_report(run_dir, final_decision, run_state.to_dict())
    run_state.record_artifact(validation_path)
    trace.append(TraceEvent("Framework", {"final_validation": final_decision.model_dump()}))
    save_agent_trace(trace, run_dir / "agent_trace.md")

    outcome = {"passed": "success", "partial": "partial", "failed": "failed"}[final_decision.status]
    run_state.finish(outcome)
    return final_decision, trace
