"""Adapters that expose existing agents/tools through the NodeExecutor contract."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import re
import time

from agents.code_agents import (
    changed_run_files,
    create_code_modeling_agent,
    execute_code_file,
    save_generated_code,
    snapshot_run_files,
)
from agents.error_agent import create_error_agent
from agents.analysis_agent import create_analysis_agent
from agents.reasoning_agent import create_reasoning_agent
from agents.research_agent import create_research_agent
from orchestration.task.artifact_validator import (
    intermediate_artifacts,
    validate_intermediate_artifacts,
)
from orchestration.execution.capability_registry import CapabilityRegistry
from orchestration.integrations.external_agent_executors import (
    FileSurferNodeExecutor,
    MarkItDownDocumentExecutor,
    WebSurferNodeExecutor,
)
from orchestration.integrations.evidence_verifier import EvidenceVerificationExecutor
from orchestration.core.model_calls import (
    PromptBudgetExceededError,
    PromptContextBlockedError,
    compact_prompt_for_overflow,
    estimate_tokens,
    run_agent,
)
from orchestration.execution.node_executor import ExecutorDescriptor, NodeExecutionContext, NodeExecutionResult
from orchestration.core.prompt_builder import build_code_prompt, build_error_prompt
from orchestration.core.run_state import RunState
from orchestration.integrations.terminal_executor import SandboxedTerminalNodeExecutor
from orchestration.core.schemas import (
    CodeGenerationResult,
    ErrorDecision,
    ExecutionResult,
    NodeAnalysisResult,
    parse_model,
)
from utils.output import TraceEvent, content_to_text


def _parse_code_output(raw: object, task_spec: dict) -> CodeGenerationResult:
    try:
        return parse_model(CodeGenerationResult, raw)
    except ValueError:
        text = content_to_text(raw)
        match = re.search(r"```python\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
        if not match:
            raise
        code_path = task_spec.get("artifacts", {}).get("code") or "artifacts/code_pipeline.py"
        outputs = [path for path in intermediate_artifacts(task_spec) if path != code_path]
        return CodeGenerationResult(
            code=match.group(1).strip(), entrypoint=code_path, expected_outputs=outputs,
            notes="模型未返回有效 JSON，框架从 Python 代码块安全恢复",
        )


def _task_view(task_spec: dict) -> dict:
    """Exclude file previews and run history from ordinary analysis contexts."""
    return {
        "task_id": task_spec.get("task_id"),
        "task_name": task_spec.get("task_name"),
        "task_type": task_spec.get("task_type"),
        "input": {
            "type": task_spec.get("input", {}).get("type"),
            "files": [Path(path).name for path in task_spec.get("input", {}).get("files", [])],
            "text": task_spec.get("input", {}).get("text", ""),
        },
        "code_policy": task_spec.get("code_policy", {}),
        "artifact_contract": task_spec.get("artifact_contract", {}),
        "success_criteria": task_spec.get("success_criteria", {}),
    }


def _total_recorded_tokens(run_state: RunState) -> int:
    return int(run_state.get("model_calls", {}).get("total_tokens", 0) or 0)


def _failed_artifact_diagnostics(
    run_dir: Path,
    declared_outputs: list[str],
    *,
    max_chars: int = 4000,
) -> dict[str, object]:
    """Read bounded failure evidence produced by the executed program.

    A program may persist a structured error and then exit non-zero without
    flushing stderr.  The artifact remains untrusted as a result, but its
    error fields are useful repair evidence.  Only declared JSON outputs
    inside the run directory are inspected.
    """

    diagnostics: dict[str, object] = {}
    root = run_dir.resolve()
    remaining = max(0, int(max_chars))
    for relative in declared_outputs:
        if remaining <= 0 or not str(relative).lower().endswith(".json"):
            continue
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
            raw = candidate.read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        try:
            value: object = json.loads(raw)
        except json.JSONDecodeError:
            value = {
                "raw_excerpt": raw[:remaining],
                "truncated": len(raw) > remaining,
            }
        encoded = json.dumps(value, ensure_ascii=False)
        if len(encoded) > remaining:
            value = {"raw_excerpt": encoded[:remaining], "truncated": True}
            encoded = json.dumps(value, ensure_ascii=False)
        diagnostics[relative] = value
        remaining -= len(encoded)
    return diagnostics


def _node_prompt_budget(task_spec: dict) -> int:
    return int(
        task_spec.get("communication_policy", {}).get(
            "max_node_context_tokens", 5000,
        )
    )


def _model_input_limit(task_spec: dict) -> int | None:
    value = task_spec.get("communication_policy", {}).get("model_input_limit_tokens")
    if value in (None, "", 0):
        return None
    return max(1, int(value))


def _prompt_data_preview(value, *, depth: int = 0):
    """Preserve schema and representative values without copying a full dataset."""
    # A normalized workbook/document reaches cell values at depth 6-7:
    # sources -> source -> sheets/tables -> rows -> cells.  Stopping at depth
    # five hid the row representation and caused code generators to invent a
    # dict schema for framework-owned 2-D arrays.
    if depth >= 8:
        return {"$summary": f"{type(value).__name__} omitted below depth limit"}
    if isinstance(value, dict):
        return {
            str(key): _prompt_data_preview(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        # Keep a complete small table/list visible to the coder.  Showing only
        # two rows can hide the header-to-record shape and encourages a model
        # to invent fallback records.  The cap remains bounded for genuinely
        # large inputs, so this is still a preview rather than a second input
        # channel.
        preview_limit = 8
        preview = [
            _prompt_data_preview(item, depth=depth + 1)
            for item in value[:preview_limit]
        ]
        if len(value) > preview_limit:
            preview.append({"$remaining_items": len(value) - preview_limit})
        return preview
    if isinstance(value, str):
        return value[:500]
    return value


class PreparedInputExecutor:
    descriptor = ExecutorDescriptor(
        executor_id="prepared_input_executor",
        executor_type="framework_executor",
        capabilities=["document_extraction", "input_normalization"],
        quality_score=1.0, cost_score=0.0, latency_score=0.0, resource_location="local",
    )

    async def execute(self, context: NodeExecutionContext) -> NodeExecutionResult:
        path = Path(context.run_dir) / "normalized_input.json"
        if not path.is_file():
            return NodeExecutionResult(
                node_id=context.node.node_id, executor_id=self.descriptor.executor_id,
                status="failed", error_type="normalized_input_missing",
                error_message="框架标准化输入 normalized_input.json 不存在",
            )
        return NodeExecutionResult(
            node_id=context.node.node_id, executor_id=self.descriptor.executor_id,
            status="completed", summary="框架已建立任务无关的稳定输入边界",
            output_artifacts=["normalized_input.json"], evidence_refs=["normalized_input.json"],
            structured_output={"normalized_input_ref": "normalized_input.json"},
        )


class AgentAnalysisExecutor:
    """Create a fresh agent per node so unrelated node history cannot leak."""

    def __init__(
        self, executor_id, capabilities, agent_factory, model_client, trace: list,
        run_state: RunState, normalized_inputs: dict,
        *, quality_score: float, cost_score: float, latency_score: float,
    ):
        self.descriptor = ExecutorDescriptor(
            executor_id=executor_id, executor_type="agent", capabilities=capabilities,
            quality_score=quality_score,
            cost_score=cost_score,
            latency_score=latency_score,
        )
        self.agent_factory = agent_factory
        self.model_client = model_client
        self.trace = trace
        self.run_state = run_state
        self.normalized_inputs = normalized_inputs

    @staticmethod
    def _allowed_evidence(context: NodeExecutionContext) -> list[str]:
        allowed = list(context.input_artifacts)
        if (Path(context.run_dir) / "normalized_input.json").is_file():
            allowed.append("normalized_input.json")
        for result in context.dependency_results.values():
            allowed.extend(result.get("evidence_refs", []))
            allowed.extend(result.get("output_artifacts", []))
        return list(dict.fromkeys(str(item) for item in allowed if item))

    async def execute(self, context: NodeExecutionContext) -> NodeExecutionResult:
        start = time.monotonic()
        tokens_before = _total_recorded_tokens(self.run_state)
        agent = self.agent_factory(
            self.model_client,
            extra_context="\n你是动态图中的单节点执行者，只能消费调用方提供的直接依赖结果。",
        )
        allowed_evidence = self._allowed_evidence(context)
        normalized_preview = _prompt_data_preview(self.normalized_inputs)
        prompt = f"""
只完成下面一个任务图节点，不调度其他节点，不写 RunState，不声称外层任务完成。

任务概要：
{json.dumps(_task_view(context.task_spec), ensure_ascii=False, indent=2)}

当前节点：
{json.dumps(context.node.model_dump(mode='json', exclude={'result', 'error'}), ensure_ascii=False, indent=2)}

直接依赖节点结果（仅这些节点被任务图授权通信）：
{json.dumps(context.dependency_results, ensure_ascii=False, indent=2)}

框架生成的长程记忆上下文胶囊（只读；关键目标和约束由框架保护）：
{json.dumps(context.memory_capsule, ensure_ascii=False, indent=2)}

输入产物引用：
{json.dumps(context.input_artifacts, ensure_ascii=False)}

框架标准化输入的受预算预览（只允许根据这里出现的事实分析；完整数据仍以引用文件为准）：
{json.dumps(normalized_preview, ensure_ascii=False, indent=2)}

允许使用的证据引用（evidence_refs 只能从此列表选择）：
{json.dumps(allowed_evidence, ensure_ascii=False, indent=2)}

严格返回一个 JSON 对象，不使用 Markdown：
{{
  "summary": "简洁节点结论，不重复整个 findings",
  "findings": ["基于证据的发现"],
  "evidence_refs": ["从允许列表精确选择"],
  "risks": ["证据不足或不确定性；没有则为空数组"],
  "confidence": 0.0
}}
不得输出整个历史、无关节点内容或预览中不存在的事实。
""".strip()
        try:
            raw = await run_agent(
                agent, prompt, self.trace,
                run_state=self.run_state, stage="execute", node_id=context.node.node_id,
                prompt_budget_tokens=_node_prompt_budget(context.task_spec),
                model_input_limit_tokens=_model_input_limit(context.task_spec),
                prompt_reducer=(
                    (
                        lambda value: compact_prompt_for_overflow(
                            value, max_chars=_model_input_limit(context.task_spec),
                        )
                    )
                    if _model_input_limit(context.task_spec) is not None
                    else None
                ),
            )
            structured_quality = True
            try:
                analysis = parse_model(NodeAnalysisResult, raw)
            except ValueError as exc:
                # Keep the general framework recoverable with providers that
                # occasionally ignore JSON mode, but never reward the result as
                # a high-quality DyLAN observation.
                text = content_to_text(raw).strip()
                if not text or not allowed_evidence:
                    raise
                structured_quality = False
                analysis = NodeAnalysisResult(
                    summary=text[:4_000],
                    findings=[text[:4_000]],
                    evidence_refs=[allowed_evidence[0]],
                    risks=[f"模型未遵循结构化输出协议：{exc}"],
                    confidence=0.3,
                )
            invalid_refs = sorted(set(analysis.evidence_refs) - set(allowed_evidence))
            if invalid_refs:
                raise ValueError(
                    "Agent 使用未授权证据引用: " + ", ".join(invalid_refs)
                )
            if any(not item.strip() for item in analysis.findings):
                raise ValueError("findings 不能包含空结论")
            return NodeExecutionResult(
                node_id=context.node.node_id, executor_id=self.descriptor.executor_id,
                status="completed", summary=analysis.summary.strip(),
                structured_output={
                    "findings": analysis.findings,
                    "risks": analysis.risks,
                    "confidence": analysis.confidence,
                    "quality_signal": {
                        "status": "passed" if structured_quality else "failed",
                        "checks": [
                            (
                                "structured_output_valid"
                                if structured_quality else "structured_output_recovered"
                            ),
                            "evidence_refs_authorized",
                            "normalized_preview_supplied",
                        ],
                    },
                },
                evidence_refs=analysis.evidence_refs,
                token_usage={
                    "total_tokens": max(
                        0, _total_recorded_tokens(self.run_state) - tokens_before,
                    )
                },
                duration_seconds=time.monotonic() - start,
            )
        except PromptContextBlockedError as exc:
            return NodeExecutionResult(
                node_id=context.node.node_id, executor_id=self.descriptor.executor_id,
                status="blocked",
                summary="分析节点上下文超出模型真实上限，等待恢复",
                structured_output={
                    "recoverable": True,
                    "recovery_action": "reduce_context_or_split_node",
                },
                error_type="prompt_context_blocked",
                error_message=str(exc),
                token_usage={"total_tokens": max(
                    0, _total_recorded_tokens(self.run_state) - tokens_before,
                )},
                duration_seconds=time.monotonic() - start,
            )
        except Exception as exc:
            return NodeExecutionResult(
                node_id=context.node.node_id, executor_id=self.descriptor.executor_id,
                status="failed",
                error_type=(
                    "model_prompt_budget_exceeded"
                    if isinstance(exc, PromptBudgetExceededError)
                    else "agent_execution_failure"
                ),
                error_message=str(exc),
                token_usage={
                    "total_tokens": max(
                        0, _total_recorded_tokens(self.run_state) - tokens_before,
                    )
                },
                duration_seconds=time.monotonic() - start,
            )


class CodePipelineNodeExecutor:
    """Own code generation/execution retries exactly as declared by TaskSpec."""

    descriptor = ExecutorDescriptor(
        executor_id="code_pipeline_executor",
        executor_type="framework_executor",
        # This executor always generates, persists and runs the complete code
        # pipeline.  Semantic analysis/calculation nodes must not accidentally
        # trigger a second full pipeline execution.
        capabilities=["code"],
        quality_score=0.9, cost_score=0.7, latency_score=0.7,
    )

    def __init__(
        self,
        model_client,
        run_state: RunState,
        trace: list,
        normalized_inputs: dict,
    ) -> None:
        self.model_client = model_client
        self.run_state = run_state
        self.trace = trace
        self.normalized_inputs = normalized_inputs

    async def execute(self, context: NodeExecutionContext) -> NodeExecutionResult:
        start = time.monotonic()
        tokens_before = _total_recorded_tokens(self.run_state)
        task_spec = context.task_spec
        run_dir = Path(context.run_dir)
        max_retries = int(task_spec.get("code_policy", {}).get("max_retries", 0))
        recovery_context = dict(context.node.recovery_context or {})
        repair_instruction = str(
            recovery_context.get("repair_instruction") or ""
        ).strip()
        previous_code_candidate = ""
        # A post-validation replay creates a fresh executor call.  Preserve
        # the last runnable implementation as the repair baseline instead of
        # asking the model to regenerate an unrelated program from scratch.
        # The path is framework-owned and constrained to the current run.
        if recovery_context:
            previous_entrypoint = str(
                task_spec.get("artifacts", {}).get("code")
                or "artifacts/code_pipeline.py"
            )
            previous_path = (run_dir / previous_entrypoint).resolve()
            try:
                previous_path.relative_to(run_dir.resolve())
                previous_code_candidate = previous_path.read_text(encoding="utf-8")
            except (OSError, ValueError):
                previous_code_candidate = ""

        # A domain validator can reject a syntactically valid model result.
        # On that post-validation replay, a TaskSpec-declared plugin is the
        # deterministic repair strategy.  Use it before sampling another
        # structurally valid but semantically wrong model program.  The plugin
        # remains task-owned; generic tasks never enter this branch.
        declared_fallback = task_spec.get("code_fallback_plugin")
        if recovery_context and isinstance(declared_fallback, dict) \
                and declared_fallback.get("module") and declared_fallback.get("function"):
            try:
                module = importlib.import_module(str(declared_fallback["module"]))
                builder = getattr(module, str(declared_fallback["function"]))
                fallback_code = builder(task_spec)
                entrypoint = task_spec.get("artifacts", {}).get(
                    "code", "artifacts/code_pipeline.py"
                )
                generated = CodeGenerationResult(
                    language="python", code=fallback_code, entrypoint=entrypoint,
                    expected_outputs=[path for path in intermediate_artifacts(task_spec)
                                      if path != entrypoint],
                    notes="TaskSpec-declared deterministic fallback after business validation failure",
                )
                saved_path = save_generated_code(
                    generated, run_dir,
                    int(task_spec.get("code_policy", {}).get("max_lines", 0)),
                    allowed_outputs=[path for path in intermediate_artifacts(task_spec)
                                     if path != entrypoint],
                )
                self.run_state.record_artifact(saved_path)
                fallback_attempt = len(
                    self.run_state.get("execution", {}).get("history", [])
                ) + 1
                fallback_execution = await execute_code_file(
                    run_dir, saved_path, fallback_attempt,
                    timeout_seconds=max(1, int(task_spec.get("terminal_policy", {})
                                               .get("timeout_seconds", 30))),
                )
                self.run_state.record_execution(fallback_execution)
                fallback_quality = validate_intermediate_artifacts(task_spec, run_dir)
                self.run_state.record_artifact_quality(fallback_quality)
                self.run_state.record_event("code_business_repair_fallback_executed", {
                    "node_id": context.node.node_id,
                    "module": declared_fallback["module"],
                    "function": declared_fallback["function"],
                    "exit_code": fallback_execution.exit_code,
                    "artifact_quality": fallback_quality.status,
                })
                if fallback_execution.succeeded and fallback_quality.status == "passed":
                    produced = [path for path in intermediate_artifacts(task_spec)
                                if (run_dir / path).is_file()]
                    return NodeExecutionResult(
                        node_id=context.node.node_id,
                        executor_id=self.descriptor.executor_id,
                        status="completed",
                        summary="业务验收失败后执行 TaskSpec 领域 fallback 并通过产物校验",
                        output_artifacts=produced,
                        structured_output={
                            "execution_result": fallback_execution.model_dump(mode="json"),
                            "artifact_quality": fallback_quality.model_dump(mode="json"),
                            "fallback_plugin": declared_fallback,
                        },
                        evidence_refs=produced,
                        token_usage={"total_tokens": max(
                            0, _total_recorded_tokens(self.run_state) - tokens_before,
                        )},
                        duration_seconds=time.monotonic() - start,
                    )
            except Exception as exc:
                self.run_state.record_event("code_business_repair_fallback_failed", {
                    "node_id": context.node.node_id,
                    "plugin": declared_fallback,
                    "error": f"{type(exc).__name__}: {exc}",
                })
        last_execution: ExecutionResult | None = None
        last_quality = None
        dependency_context = {
            dependency_id: {
                "summary": result.get("summary", ""),
                "structured_output": result.get("structured_output", {}),
                "output_artifacts": result.get("output_artifacts", []),
                "evidence_refs": result.get("evidence_refs", []),
            }
            for dependency_id, result in context.dependency_results.items()
        }
        # Runtime memory is a bounded, read-only capsule.  It is included in
        # the code prompt through the existing structured analysis_context
        # channel rather than giving the coder access to the memory store.
        dependency_context["__memory_capsule__"] = context.memory_capsule
        # Requirement-driven code stages receive one framework-written,
        # bounded snapshot of prior stage outputs.  This keeps old node IDs out
        # of the local PlanIR while ensuring the coder implements the already
        # accepted model/algorithm specifications instead of reinventing them
        # from a title or a lossy final summary.
        for relative in context.input_artifacts:
            if not relative.endswith("/completed_stage_context.json"):
                continue
            candidate = (run_dir / relative).resolve()
            try:
                candidate.relative_to(run_dir.resolve())
                completed_context = json.loads(
                    candidate.read_text(encoding="utf-8")
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                return NodeExecutionResult(
                    node_id=context.node.node_id,
                    executor_id=self.descriptor.executor_id,
                    status="blocked",
                    summary="已完成阶段上下文不可读取，等待恢复",
                    error_type="completed_stage_context_invalid",
                    error_message=f"{relative}: {exc}",
                    structured_output={
                        "recoverable": True,
                        "recovery_action": "rebuild_completed_stage_context",
                    },
                    duration_seconds=time.monotonic() - start,
                )
            dependency_context["__completed_stage_context__"] = completed_context
            break
        if recovery_context:
            # This is framework-verified feedback, not free-form chat history.
            dependency_context["__business_repair__"] = recovery_context
        plan_view = {
            "execution_mode": "task_graph",
            "graph_id": context.graph_id,
            "current_node": context.node.model_dump(mode="json", exclude={"result", "error"}),
            "direct_dependency_ids": list(context.dependency_results),
        }
        declared_outputs = intermediate_artifacts(task_spec)
        node_files_before = snapshot_run_files(run_dir)

        business_attempts = 0
        generation_format_retries = 0
        while business_attempts < max_retries + 1:
            # Each repair receives the current prompt plus explicit verified
            # failure context.  Reusing an AssistantAgent would also resend
            # every earlier code draft and response, causing prompt growth and
            # attention dilution across retries.
            coder = create_code_modeling_agent(
                self.model_client,
                extra_context=(
                    "\n你只负责当前 TaskGraph 代码节点；框架拥有保存、执行和状态控制权。"
                ),
            )
            attempt = len(self.run_state.get("execution", {}).get("history", [])) + 1
            attempt_files_before = snapshot_run_files(run_dir)
            try:
                normalized_preview = _prompt_data_preview(self.normalized_inputs)
                attempt_context = dict(dependency_context)
                if previous_code_candidate:
                    # Once a real candidate exists, retries are code repair
                    # turns, not fresh planning turns.  Keep the verified
                    # repair context and the bounded stage snapshot, but drop
                    # repeated summaries from unrelated dependencies so the
                    # model can attend to the actual traceback.
                    attempt_context = {
                        key: value
                        for key, value in attempt_context.items()
                        if key in {
                            "__business_repair__",
                            "__completed_stage_context__",
                            "__memory_capsule__",
                        }
                    }
                    attempt_context["__previous_code_candidate__"] = {
                        "instruction": (
                            "这是上一轮真实失败源码。必须保留其输入解析、算法和输出结构，"
                            "只根据下面的真实 traceback 做最小局部修复；禁止重新设计、"
                            "删减验收字段或重新生成无关实现。"
                        ),
                        "code": previous_code_candidate,
                    }
                code_prompt = build_code_prompt(
                    task_spec, plan_view, repair_instruction,
                    analysis_context=attempt_context,
                    normalized_inputs=normalized_preview,
                )
                raw_code_prompt = build_code_prompt(
                    task_spec, plan_view, repair_instruction,
                    analysis_context=attempt_context,
                    normalized_inputs=self.normalized_inputs,
                )
                raw_code = await run_agent(
                    coder,
                    code_prompt,
                    self.trace,
                    run_state=self.run_state,
                    stage="execute",
                    node_id=context.node.node_id,
                    prompt_budget_tokens=_node_prompt_budget(task_spec),
                    raw_prompt_tokens_estimated=estimate_tokens(raw_code_prompt),
                    model_input_limit_tokens=_model_input_limit(task_spec),
                    prompt_reducer=(
                        (
                            lambda value: compact_prompt_for_overflow(
                                value,
                                # One character is the conservative upper
                                # bound for mixed Chinese/English input.
                                max_chars=_model_input_limit(task_spec),
                            )
                        )
                        if _model_input_limit(task_spec) is not None
                        else None
                    ),
                )
                generated = _parse_code_output(raw_code, task_spec)
                # Retain the exact candidate even when deterministic saving or
                # compilation validation rejects it.  A fresh Coder instance
                # in the next retry otherwise cannot act on line-specific
                # diagnostics and merely samples an unrelated replacement.
                previous_code_candidate = generated.code
                expected_entrypoint = task_spec.get("artifacts", {}).get("code")
                if expected_entrypoint:
                    generated.entrypoint = expected_entrypoint
                allowed_outputs = [
                    path for path in intermediate_artifacts(task_spec)
                    if path.startswith("artifacts/") and path != generated.entrypoint
                ]
                saved_path = save_generated_code(
                    generated, run_dir,
                    int(task_spec.get("code_policy", {}).get("max_lines", 0)),
                    allowed_outputs=allowed_outputs,
                )
                self.run_state.record_artifact(saved_path)
                last_execution = await execute_code_file(
                    run_dir,
                    saved_path,
                    attempt,
                    timeout_seconds=max(
                        1,
                        int(task_spec.get("terminal_policy", {}).get(
                            "timeout_seconds", 30
                        )),
                    ),
                )
                generated_files = changed_run_files(
                    run_dir,
                    attempt_files_before,
                    allowed_paths=[saved_path],
                )
                last_execution.produced_files = sorted(set(
                    generated_files + last_execution.produced_files
                ))
            except PromptContextBlockedError as exc:
                return NodeExecutionResult(
                    node_id=context.node.node_id,
                    executor_id=self.descriptor.executor_id,
                    status="blocked",
                    summary="模型真实上下文上限仍无法容纳当前节点，等待缩减上下文或拆分节点",
                    error_type="prompt_context_blocked",
                    error_message=str(exc),
                    structured_output={
                        "recoverable": True,
                        "recovery_action": "reduce_context_or_split_node",
                    },
                    token_usage={"total_tokens": 0},
                    duration_seconds=time.monotonic() - start,
                )
            except PromptBudgetExceededError as exc:
                return NodeExecutionResult(
                    node_id=context.node.node_id,
                    executor_id=self.descriptor.executor_id,
                    status="blocked",
                    summary="模型上下文暂不可用，等待恢复",
                    error_type="prompt_context_blocked",
                    error_message=str(exc),
                    structured_output={
                        "recoverable": True,
                        "recovery_action": "reduce_context_or_split_node",
                    },
                    token_usage={"total_tokens": 0},
                    duration_seconds=time.monotonic() - start,
                )
            except Exception as exc:
                last_execution = ExecutionResult(
                    attempt=attempt, phase="generation_validation",
                    command=["python", task_spec.get("artifacts", {}).get("code", "artifacts/code_pipeline.py")],
                    exit_code=65, stderr=f"代码生成或预检失败：{exc}",
                    produced_files=changed_run_files(
                        run_dir,
                        attempt_files_before,
                        allowed_paths=declared_outputs,
                    ),
                )

            self.run_state.record_execution(last_execution)
            self.trace.append(TraceEvent("GraphCodeExecutor", {
                "node_id": context.node.node_id, **last_execution.model_dump(),
            }))
            if last_execution.succeeded:
                last_quality = validate_intermediate_artifacts(task_spec, run_dir)
                self.run_state.record_artifact_quality(last_quality)
                self.trace.append(TraceEvent("GraphArtifactValidator", {
                    "node_id": context.node.node_id, **last_quality.model_dump(),
                }))
                if last_quality.status == "passed":
                    return NodeExecutionResult(
                        node_id=context.node.node_id, executor_id=self.descriptor.executor_id,
                        status="completed", summary="代码已真实执行且中间产物通过确定性校验",
                        output_artifacts=[
                            path for path in intermediate_artifacts(task_spec)
                            if (run_dir / path).is_file()
                        ],
                        structured_output={
                            "execution_result": last_execution.model_dump(mode="json"),
                            "artifact_quality": last_quality.model_dump(mode="json"),
                        },
                        evidence_refs=[
                            path for path in intermediate_artifacts(task_spec)
                            if (run_dir / path).is_file()
                        ],
                        token_usage={
                            "total_tokens": max(
                                0,
                                _total_recorded_tokens(self.run_state) - tokens_before,
                            )
                        },
                        duration_seconds=time.monotonic() - start,
                    )

            if (
                last_execution.phase == "generation_validation"
                and generation_format_retries < 1
            ):
                # A line-count/schema/syntax rejection means no business code
                # ran.  Permit one tightly scoped format repair inside the
                # current business attempt; do not consume execution retries.
                generation_format_retries += 1
                repair_instruction = (
                    "上一版代码尚未执行，静态预检失败。只修复该格式/语法问题，"
                    "保留既定算法、输入输出契约和产物路径：\n"
                    + last_execution.stderr
                )
                self.run_state.record_event(
                    "code_generation_format_retry_scheduled", {
                        "node_id": context.node.node_id,
                        "business_attempt": business_attempts + 1,
                        "format_retry": generation_format_retries,
                        "business_retry_consumed": False,
                        "error": last_execution.stderr,
                    },
                )
                continue

            business_attempts += 1
            generation_format_retries = 0
            retries_remaining = max_retries + 1 - business_attempts
            if retries_remaining <= 0:
                break
            if last_execution.succeeded and last_quality is not None:
                repair_instruction = (
                    "上一次代码执行成功，但声明的产物未通过确定性校验。只修复下列问题：\n- "
                    + "\n- ".join(last_quality.issues)
                )
                self.run_state.record_event("retry_scheduled", {
                    "node_id": context.node.node_id,
                    "failure_type": last_quality.failure_type,
                    "retries_remaining": retries_remaining,
                })
                continue

            self.run_state.set_failure(
                "code_generation_failure" if last_execution.phase == "generation_validation" else "execution_failure",
                "code", "execute", [last_execution.stderr],
            )
            failure_payload = last_execution.model_dump(mode="json")
            artifact_diagnostics = _failed_artifact_diagnostics(
                run_dir, declared_outputs,
            )
            if artifact_diagnostics:
                failure_payload["failed_artifact_diagnostics"] = artifact_diagnostics
            deterministic_context = (
                "\n失败运行产生的声明内 JSON 诊断（仅作错误证据，不代表业务成功）：\n"
                + json.dumps(artifact_diagnostics, ensure_ascii=False, indent=2)
                if artifact_diagnostics else ""
            )
            try:
                error_agent = create_error_agent(self.model_client)
                raw_error = await run_agent(
                    error_agent,
                    build_error_prompt(task_spec, failure_payload, retries_remaining),
                    self.trace,
                    run_state=self.run_state,
                    stage="execute",
                    node_id=context.node.node_id,
                    prompt_budget_tokens=_node_prompt_budget(task_spec),
                )
                error_decision = parse_model(ErrorDecision, raw_error)
            except Exception as exc:
                error_decision = ErrorDecision(
                    error_type="code", retry_recommended=True,
                    retry_count_remaining=retries_remaining,
                    repair_instruction=(
                        "根据真实退出码、stderr 和失败产物诊断修复上一次失败。"
                        f"结构化归因解析失败：{exc}{deterministic_context}"
                    ),
                )
            self.run_state.record_event("error_attribution_recorded", error_decision.model_dump())
            if not error_decision.retry_recommended:
                break
            repair_instruction = error_decision.repair_instruction + deterministic_context

        fallback = task_spec.get("code_fallback_plugin")
        if isinstance(fallback, dict) and fallback.get("module") and fallback.get("function"):
            fallback_attempt = len(self.run_state.get("execution", {}).get("history", [])) + 1
            try:
                module = importlib.import_module(str(fallback["module"]))
                builder = getattr(module, str(fallback["function"]))
                fallback_code = builder(task_spec)
                generated = CodeGenerationResult(
                    language="python", code=fallback_code,
                    entrypoint=task_spec.get("artifacts", {}).get("code", "artifacts/code_pipeline.py"),
                    expected_outputs=[path for path in declared_outputs
                                      if path != task_spec.get("artifacts", {}).get("code")],
                    notes="TaskSpec-declared deterministic fallback after model repair exhaustion",
                )
                saved_path = save_generated_code(
                    generated, run_dir,
                    int(task_spec.get("code_policy", {}).get("max_lines", 0)),
                    allowed_outputs=[path for path in declared_outputs if path != generated.entrypoint],
                )
                self.run_state.record_artifact(saved_path)
                last_execution = await execute_code_file(
                    run_dir, saved_path, fallback_attempt,
                    timeout_seconds=max(1, int(task_spec.get("terminal_policy", {}).get("timeout_seconds", 30))),
                )
                self.run_state.record_execution(last_execution)
                last_quality = validate_intermediate_artifacts(task_spec, run_dir)
                self.run_state.record_artifact_quality(last_quality)
                self.run_state.record_event("code_fallback_plugin_executed", {
                    "node_id": context.node.node_id,
                    "module": fallback["module"],
                    "function": fallback["function"],
                    "exit_code": last_execution.exit_code,
                    "artifact_quality": last_quality.status,
                })
                if last_execution.succeeded and last_quality.status == "passed":
                    produced = [path for path in declared_outputs if (run_dir / path).is_file()]
                    return NodeExecutionResult(
                        node_id=context.node.node_id, executor_id=self.descriptor.executor_id,
                        status="completed",
                        summary="模型修复耗尽后，TaskSpec 领域基线已真实执行并通过产物校验",
                        output_artifacts=produced,
                        structured_output={"execution_result": last_execution.model_dump(mode="json"),
                                           "artifact_quality": last_quality.model_dump(mode="json"),
                                           "fallback_plugin": fallback},
                        evidence_refs=produced,
                        token_usage={"total_tokens": max(0, _total_recorded_tokens(self.run_state) - tokens_before)},
                        duration_seconds=time.monotonic() - start,
                    )
            except Exception as exc:
                self.run_state.record_event("code_fallback_plugin_failed", {
                    "node_id": context.node.node_id, "plugin": fallback,
                    "error": f"{type(exc).__name__}: {exc}",
                })

        error_message = (
            "; ".join(last_quality.issues)
            if last_execution and last_execution.succeeded and last_quality is not None
            else (last_execution.stderr if last_execution else "代码节点没有执行结果")
        )
        return NodeExecutionResult(
            node_id=context.node.node_id, executor_id=self.descriptor.executor_id,
            status="failed",
            summary="代码节点达到 TaskSpec 重试上限",
            output_artifacts=changed_run_files(
                run_dir, node_files_before, allowed_paths=declared_outputs,
            ),
            structured_output={
                "execution_result": last_execution.model_dump(mode="json") if last_execution else {},
                "artifact_quality": last_quality.model_dump(mode="json") if last_quality else {},
            },
            error_type="artifact_quality_failure" if last_execution and last_execution.succeeded else "execution_failure",
            error_message=error_message,
            token_usage={
                "total_tokens": max(
                    0, _total_recorded_tokens(self.run_state) - tokens_before,
                )
            },
            duration_seconds=time.monotonic() - start,
        )


class ArtifactValidationExecutor:
    descriptor = ExecutorDescriptor(
        executor_id="artifact_validation_executor",
        executor_type="framework_executor",
        capabilities=["artifact_validation"],
        quality_score=1.0, cost_score=0.0, latency_score=0.0, resource_location="local",
    )

    def __init__(self, run_state: RunState):
        self.run_state = run_state

    async def execute(self, context: NodeExecutionContext) -> NodeExecutionResult:
        quality = validate_intermediate_artifacts(
            context.task_spec, Path(context.run_dir),
        )
        current = self.run_state.get("artifact_quality", {}).get("current")
        if current != quality.model_dump(mode="json"):
            self.run_state.record_artifact_quality(quality)
        return NodeExecutionResult(
            node_id=context.node.node_id, executor_id=self.descriptor.executor_id,
            status="completed" if quality.status == "passed" else "failed",
            summary=(
                "全部中间产物通过通用契约校验"
                if quality.status == "passed" else "中间产物未通过通用契约校验"
            ),
            output_artifacts=[],
            structured_output={"artifact_quality": quality.model_dump(mode="json")},
            evidence_refs=quality.checked_artifacts,
            error_type=None if quality.status == "passed" else quality.failure_type,
            error_message=None if quality.status == "passed" else "；".join(quality.issues),
        )


def build_default_registry(
    model_client,
    run_state: RunState,
    trace: list,
    normalized_inputs: dict,
    task_spec: dict | None = None,
    file_model_client=None,
    web_model_client=None,
) -> CapabilityRegistry:
    task_spec = task_spec or {}
    team_policy = task_spec.get("team_policy", {})
    team_mode = team_policy.get("mode", "dylan_lite")
    if team_mode not in {"dylan_lite", "static"}:
        raise ValueError(f"不支持的 team_policy.mode: {team_mode}")
    stats_path = team_policy.get("stats_path") or str(
        Path(task_spec.get("run_dir", run_state.run_dir)) / "executor_stats.json"
    )
    use_runtime_history = bool(
        team_mode == "dylan_lite"
        and team_policy.get("use_runtime_history", True)
    )
    try:
        registry = CapabilityRegistry(
            use_runtime_history=use_runtime_history,
            stats_path=stats_path,
        )
    except (OSError, ValueError) as exc:
        recovered_path = Path(task_spec.get("run_dir", run_state.run_dir)) / "executor_stats.json"
        run_state.record_event("executor_stats_fallback", {
            "configured_path": str(stats_path),
            "fallback_path": str(recovered_path),
            "reason": str(exc),
        })
        registry = CapabilityRegistry(
            use_runtime_history=use_runtime_history,
            stats_path=recovered_path,
        )
    registry.register(PreparedInputExecutor())
    registry.register(AgentAnalysisExecutor(
        "analysis_agent", ["analysis", "data_analysis"],
        create_analysis_agent, model_client, trace, run_state, normalized_inputs,
        quality_score=0.82, cost_score=0.45, latency_score=0.45,
    ))
    registry.register(AgentAnalysisExecutor(
        "research_agent", ["research"], create_research_agent, model_client, trace,
        run_state, normalized_inputs,
        quality_score=0.78, cost_score=0.65, latency_score=0.7,
    ))
    registry.register(AgentAnalysisExecutor(
        "reasoning_agent",
        ["analysis", "reasoning", "math_modeling", "data_analysis", "calculation"],
        create_reasoning_agent, model_client, trace, run_state, normalized_inputs,
        quality_score=0.9, cost_score=0.75, latency_score=0.75,
    ))
    registry.register(CodePipelineNodeExecutor(
        model_client, run_state, trace, normalized_inputs,
    ))
    registry.register(ArtifactValidationExecutor(run_state))
    capability_contract = task_spec.get("capability_contract", {})
    requested_external = set(capability_contract.get("required", [])) | set(
        capability_contract.get("preferred", [])
    )
    external_enabled = bool(
        task_spec.get("external_tools_policy", {}).get("enabled", True)
    )
    external_executors: list[str] = []
    if "evidence_verification" in requested_external:
        verifier_executor = EvidenceVerificationExecutor(model_client, run_state, trace)
        registry.register(verifier_executor)
        external_executors.append(verifier_executor.descriptor.executor_id)
    if "terminal_execution" in requested_external:
        terminal_executor = SandboxedTerminalNodeExecutor(run_state, trace)
        registry.register(terminal_executor)
        external_executors.append(terminal_executor.descriptor.executor_id)
    if external_enabled and requested_external & {"document_recovery", "document_conversion"}:
        if MarkItDownDocumentExecutor.available():
            markitdown_executor = MarkItDownDocumentExecutor(run_state, trace)
            registry.register(markitdown_executor)
            external_executors.append(markitdown_executor.descriptor.executor_id)
    if external_enabled and requested_external & {"document_recovery", "file_navigation"}:
        if file_model_client is not None:
            file_surfer_executor = FileSurferNodeExecutor(
                file_model_client, run_state, trace,
            )
            registry.register(file_surfer_executor)
            external_executors.append(file_surfer_executor.descriptor.executor_id)
    if external_enabled and "web_research" in requested_external and web_model_client is not None:
        web_surfer_executor = WebSurferNodeExecutor(
            web_model_client, run_state, trace,
        )
        registry.register(web_surfer_executor)
        external_executors.append(web_surfer_executor.descriptor.executor_id)
    run_state.record_event("external_capabilities_configured", {
        "enabled": external_enabled,
        "required": capability_contract.get("required", []),
        "preferred": capability_contract.get("preferred", []),
        "registered_executors": external_executors,
        "available_external_capabilities": sorted(
            set(registry.list_capabilities())
            & {
                "document_recovery", "document_conversion", "file_navigation",
                "web_research", "browser_navigation", "evidence_verification",
                "terminal_execution", "test_execution",
            }
        ),
    })
    run_state.record_event("dynamic_team_configured", {
        "mode": team_mode,
        "scoring_policy": registry.scorer.name,
        "stats_path": str(registry.stats_store.path) if registry.stats_store.path else None,
        "history_records_loaded": len(registry.stats_store.all()),
        "executors": [
            descriptor.model_dump(mode="json")
            for descriptor in registry.list_executors()
        ],
    })
    return registry
