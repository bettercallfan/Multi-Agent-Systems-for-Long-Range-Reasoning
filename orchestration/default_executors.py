"""Adapters that expose existing agents/tools through the NodeExecutor contract."""

from __future__ import annotations

import json
from pathlib import Path
import re
import time

from agents.code_agents import (
    create_code_modeling_agent,
    execute_code_file,
    list_run_files,
    save_generated_code,
)
from agents.error_agent import create_error_agent
from agents.reasoning_agent import create_reasoning_agent
from agents.research_agent import create_research_agent
from orchestration.capability_registry import CapabilityRegistry
from orchestration.model_calls import run_agent
from orchestration.node_executor import ExecutorDescriptor, NodeExecutionContext, NodeExecutionResult
from orchestration.prompt_builder import build_code_prompt, build_error_prompt
from orchestration.run_state import RunState
from orchestration.schemas import CodeGenerationResult, ErrorDecision, ExecutionResult, parse_model
from task_plugins.base import TaskPlugin
from utils.output import TraceEvent, content_to_text


def _intermediate_artifacts(task_spec: dict) -> list[str]:
    contract = task_spec.get("artifact_contract", {})
    if "intermediate_artifacts" in contract:
        return list(contract["intermediate_artifacts"])
    excluded = {
        "final_report.md", "task_spec.json", "task_graph.json", "agent_trace.md",
        "run_state.json", "file_previews.json", "normalized_input.json",
    }
    return [path for path in task_spec.get("required_artifacts", []) if path not in excluded]


def _parse_code_output(raw: object, task_spec: dict) -> CodeGenerationResult:
    try:
        return parse_model(CodeGenerationResult, raw)
    except ValueError:
        text = content_to_text(raw)
        match = re.search(r"```python\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
        if not match:
            raise
        code_path = task_spec.get("artifacts", {}).get("code") or "artifacts/code_pipeline.py"
        outputs = [path for path in _intermediate_artifacts(task_spec) if path != code_path]
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
            status="completed", summary="框架已完成异构输入的确定性标准化",
            output_artifacts=["normalized_input.json"], evidence_refs=["normalized_input.json"],
            structured_output={"normalized_input_ref": "normalized_input.json"},
        )


class AgentAnalysisExecutor:
    """Create a fresh agent per node so unrelated node history cannot leak."""

    def __init__(self, executor_id, capabilities, agent_factory, model_client, trace: list):
        self.descriptor = ExecutorDescriptor(
            executor_id=executor_id, executor_type="agent", capabilities=capabilities,
            quality_score=0.8, cost_score=0.6, latency_score=0.6,
        )
        self.agent_factory = agent_factory
        self.model_client = model_client
        self.trace = trace

    async def execute(self, context: NodeExecutionContext) -> NodeExecutionResult:
        start = time.monotonic()
        agent = self.agent_factory(
            self.model_client,
            extra_context="\n你是动态图中的单节点执行者，只能消费调用方提供的直接依赖结果。",
        )
        prompt = f"""
只完成下面一个任务图节点，不调度其他节点，不写 RunState，不声称外层任务完成。

任务概要：
{json.dumps(_task_view(context.task_spec), ensure_ascii=False, indent=2)}

当前节点：
{json.dumps(context.node.model_dump(mode='json', exclude={'result', 'error'}), ensure_ascii=False, indent=2)}

直接依赖节点结果（仅这些节点被任务图授权通信）：
{json.dumps(context.dependency_results, ensure_ascii=False, indent=2)}

输入产物引用：
{json.dumps(context.input_artifacts, ensure_ascii=False)}

输出一段简洁的节点结论，并明确使用了哪些证据引用。不要输出整个历史或无关节点内容。
""".strip()
        try:
            raw = await run_agent(agent, prompt, self.trace)
            text = content_to_text(raw).strip()
            if not text:
                raise ValueError("Agent 返回空节点结果")
            return NodeExecutionResult(
                node_id=context.node.node_id, executor_id=self.descriptor.executor_id,
                status="completed", summary=text,
                structured_output={"analysis": text},
                evidence_refs=list(context.input_artifacts),
                duration_seconds=time.monotonic() - start,
            )
        except Exception as exc:
            return NodeExecutionResult(
                node_id=context.node.node_id, executor_id=self.descriptor.executor_id,
                status="failed", error_type="agent_execution_failure", error_message=str(exc),
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
        plugin: TaskPlugin,
        run_state: RunState,
        trace: list,
        normalized_inputs: dict,
    ) -> None:
        self.model_client = model_client
        self.plugin = plugin
        self.run_state = run_state
        self.trace = trace
        self.normalized_inputs = normalized_inputs

    async def execute(self, context: NodeExecutionContext) -> NodeExecutionResult:
        start = time.monotonic()
        task_spec = context.task_spec
        run_dir = Path(context.run_dir)
        coder = create_code_modeling_agent(
            self.model_client,
            extra_context="\n你只负责当前 TaskGraph 代码节点；框架拥有保存、执行和状态控制权。",
        )
        error_agent = create_error_agent(self.model_client)
        max_retries = int(task_spec.get("code_policy", {}).get("max_retries", 0))
        repair_instruction = ""
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
        plan_view = {
            "execution_mode": "task_graph",
            "graph_id": context.graph_id,
            "current_node": context.node.model_dump(mode="json", exclude={"result", "error"}),
            "direct_dependency_ids": list(context.dependency_results),
        }

        for local_attempt in range(1, max_retries + 2):
            attempt = len(self.run_state.get("execution", {}).get("history", [])) + 1
            try:
                raw_code = await run_agent(
                    coder,
                    build_code_prompt(
                        task_spec, plan_view, repair_instruction,
                        analysis_context=dependency_context,
                        plugin_instructions=self.plugin.code_instructions(task_spec),
                        normalized_inputs=self.normalized_inputs,
                    ),
                    self.trace,
                )
                generated = _parse_code_output(raw_code, task_spec)
                expected_entrypoint = task_spec.get("artifacts", {}).get("code")
                if expected_entrypoint:
                    generated.entrypoint = expected_entrypoint
                allowed_outputs = [
                    path for path in _intermediate_artifacts(task_spec)
                    if path.startswith("artifacts/") and path != generated.entrypoint
                ]
                saved_path = save_generated_code(
                    generated, run_dir,
                    int(task_spec.get("code_policy", {}).get("max_lines", 0)),
                    allowed_outputs=allowed_outputs,
                )
                self.run_state.record_artifact(saved_path)
                last_execution = await execute_code_file(run_dir, saved_path, attempt)
            except Exception as exc:
                last_execution = ExecutionResult(
                    attempt=attempt, phase="generation_validation",
                    command=["python", task_spec.get("artifacts", {}).get("code", "artifacts/code_pipeline.py")],
                    exit_code=65, stderr=f"代码生成或预检失败：{exc}",
                    produced_files=self.run_state.get("artifacts", {}).get("produced", []),
                )

            self.run_state.record_execution(last_execution)
            self.trace.append(TraceEvent("GraphCodeExecutor", {
                "node_id": context.node.node_id, **last_execution.model_dump(),
            }))
            if last_execution.succeeded:
                last_quality = self.plugin.validate_intermediate(task_spec, run_dir)
                self.run_state.record_artifact_quality(last_quality)
                self.trace.append(TraceEvent("GraphArtifactValidator", {
                    "node_id": context.node.node_id, **last_quality.model_dump(),
                }))
                if last_quality.status == "passed":
                    return NodeExecutionResult(
                        node_id=context.node.node_id, executor_id=self.descriptor.executor_id,
                        status="completed", summary="代码已真实执行且中间产物通过确定性校验",
                        output_artifacts=[
                            path for path in _intermediate_artifacts(task_spec)
                            if (run_dir / path).is_file()
                        ],
                        structured_output={
                            "execution_result": last_execution.model_dump(mode="json"),
                            "artifact_quality": last_quality.model_dump(mode="json"),
                        },
                        evidence_refs=[
                            path for path in _intermediate_artifacts(task_spec)
                            if (run_dir / path).is_file()
                        ],
                        duration_seconds=time.monotonic() - start,
                    )

            retries_remaining = max_retries + 1 - local_attempt
            if retries_remaining <= 0:
                break
            if last_execution.succeeded and last_quality is not None:
                repair_instruction = (
                    "上一次代码执行成功，但业务产物未通过确定性校验。只修复下列问题：\n- "
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
            try:
                raw_error = await run_agent(
                    error_agent,
                    build_error_prompt(task_spec, last_execution.model_dump(), retries_remaining),
                    self.trace,
                )
                error_decision = parse_model(ErrorDecision, raw_error)
            except Exception as exc:
                error_decision = ErrorDecision(
                    error_type="code", retry_recommended=True,
                    retry_count_remaining=retries_remaining,
                    repair_instruction=f"根据 stderr 修复上一次失败。结构化归因解析失败：{exc}",
                )
            self.run_state.record_event("error_attribution_recorded", error_decision.model_dump())
            if not error_decision.retry_recommended:
                break
            repair_instruction = error_decision.repair_instruction

        error_message = (
            "; ".join(last_quality.issues)
            if last_execution and last_execution.succeeded and last_quality is not None
            else (last_execution.stderr if last_execution else "代码节点没有执行结果")
        )
        return NodeExecutionResult(
            node_id=context.node.node_id, executor_id=self.descriptor.executor_id,
            status="failed",
            summary="代码节点达到 TaskSpec 重试上限",
            output_artifacts=list_run_files(run_dir),
            structured_output={
                "execution_result": last_execution.model_dump(mode="json") if last_execution else {},
                "artifact_quality": last_quality.model_dump(mode="json") if last_quality else {},
            },
            error_type="artifact_quality_failure" if last_execution and last_execution.succeeded else "execution_failure",
            error_message=error_message,
            duration_seconds=time.monotonic() - start,
        )


class ArtifactValidationExecutor:
    descriptor = ExecutorDescriptor(
        executor_id="artifact_validation_executor",
        executor_type="framework_executor",
        capabilities=["artifact_validation"],
        quality_score=1.0, cost_score=0.0, latency_score=0.0, resource_location="local",
    )

    def __init__(self, plugin: TaskPlugin, run_state: RunState):
        self.plugin = plugin
        self.run_state = run_state

    async def execute(self, context: NodeExecutionContext) -> NodeExecutionResult:
        quality = self.plugin.validate_intermediate(context.task_spec, Path(context.run_dir))
        current = self.run_state.get("artifact_quality", {}).get("current")
        if current != quality.model_dump(mode="json"):
            self.run_state.record_artifact_quality(quality)
        return NodeExecutionResult(
            node_id=context.node.node_id, executor_id=self.descriptor.executor_id,
            status="completed" if quality.status == "passed" else "failed",
            summary=(
                "全部中间产物通过任务插件确定性校验"
                if quality.status == "passed" else "中间产物未通过任务插件确定性校验"
            ),
            output_artifacts=[],
            structured_output={"artifact_quality": quality.model_dump(mode="json")},
            evidence_refs=quality.checked_artifacts,
            error_type=None if quality.status == "passed" else quality.failure_type,
            error_message=None if quality.status == "passed" else "；".join(quality.issues),
        )


def build_default_registry(
    model_client,
    plugin: TaskPlugin,
    run_state: RunState,
    trace: list,
    normalized_inputs: dict,
) -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register(PreparedInputExecutor())
    registry.register(AgentAnalysisExecutor(
        "analysis_agent", ["analysis"], create_reasoning_agent, model_client, trace,
    ))
    registry.register(AgentAnalysisExecutor(
        "research_agent", ["research"], create_research_agent, model_client, trace,
    ))
    registry.register(AgentAnalysisExecutor(
        "reasoning_agent",
        ["reasoning", "math_modeling", "data_analysis", "calculation"],
        create_reasoning_agent,
        model_client,
        trace,
    ))
    registry.register(CodePipelineNodeExecutor(
        model_client, plugin, run_state, trace, normalized_inputs,
    ))
    registry.register(ArtifactValidationExecutor(plugin, run_state))
    return registry
