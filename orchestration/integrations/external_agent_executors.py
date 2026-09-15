"""Adapters for existing capability-bearing agents and deterministic tools.

The external components never receive RunState or scheduler authority.  Each
adapter creates a fresh component for one TaskNode, constrains its filesystem
or browser scope, converts its response to ``NodeExecutionResult``, and closes
the component before returning control to ``GraphScheduler``.
"""

from __future__ import annotations

import asyncio
from hashlib import sha256
import inspect
import json
from pathlib import Path
import re
import time
from typing import Any, Callable

from autogen_agentchat.base import Response
from autogen_agentchat.messages import TextMessage
from autogen_core import CancellationToken

from orchestration.core.model_calls import (
    PromptBudgetExceededError,
    estimate_tokens,
    run_agent,
)
from orchestration.execution.node_executor import (
    ExecutorDescriptor,
    NodeExecutionContext,
    NodeExecutionResult,
)
from orchestration.core.run_state import RunState
from utils.output import TraceEvent, content_to_text


ExternalAgentFactory = Callable[..., Any]


def _external_policy(context: NodeExecutionContext) -> dict[str, Any]:
    return dict(context.task_spec.get("external_tools_policy") or {})


def _safe_input_files(context: NodeExecutionContext) -> list[Path]:
    run_dir = Path(context.run_dir).resolve()
    input_dir = (run_dir / "inputs").resolve()
    requested = list(context.input_artifacts)
    fallback = list(context.task_spec.get("input", {}).get("files", []))

    def collect(items: list[str]) -> list[Path]:
        found: list[Path] = []
        for item in items:
            candidate = Path(item)
            if not candidate.is_absolute():
                candidate = run_dir / candidate
            candidate = candidate.resolve()
            if candidate.is_file() and (candidate == input_dir or input_dir in candidate.parents):
                found.append(candidate)
        return found

    files = collect(requested)
    if not files:
        files = collect(fallback)
    return list(dict.fromkeys(files))


def _recovery_files(context: NodeExecutionContext) -> list[Path]:
    """Prefer files that deterministic previewing could not understand."""

    files = _safe_input_files(context)
    previews = context.task_spec.get("file_previews") or []
    recovery_names = {
        Path(str(item.get("path") or "")).name
        for item in previews
        if item.get("status") != "success"
        or (
            item.get("type") in {"pdf", "docx"}
            and not str(item.get("text_preview") or "").strip()
        )
    }
    conversion_suffixes = {
        ".ppt", ".pptx", ".html", ".htm", ".epub", ".zip", ".msg",
        ".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".gif", ".wav", ".mp3",
    }
    selected = [
        path for path in files
        if path.name in recovery_names or path.suffix.lower() in conversion_suffixes
    ]
    return selected or files


def _relative(run_dir: str | Path, path: Path) -> str:
    return path.resolve().relative_to(Path(run_dir).resolve()).as_posix()


def _tool_result_path(context: NodeExecutionContext, provider: str, suffix: str = ".json") -> Path:
    safe_node = re.sub(r"[^A-Za-z0-9_-]", "_", context.node.node_id)
    destination = (
        Path(context.run_dir) / "artifacts" / "tool_results" / safe_node
        / f"{provider}{suffix}"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


async def _close_component(component: Any) -> None:
    close = getattr(component, "close", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


async def _run_web_agent_step(
    agent: Any,
    prompt: str,
    trace: list,
    run_state: RunState,
    node_id: str,
    prompt_budget_tokens: int,
) -> Any:
    """Exhaust WebSurfer's stream to avoid its GeneratorExit error path."""

    prompt_tokens_estimated = estimate_tokens(prompt)
    if prompt_tokens_estimated > prompt_budget_tokens:
        warning = {
            "stage": "execute",
            "node_id": node_id,
            "agent": getattr(agent, "name", agent.__class__.__name__),
            "prompt_tokens_estimated": prompt_tokens_estimated,
            "prompt_bytes": len(prompt.encode("utf-8")),
            "prompt_budget_tokens": prompt_budget_tokens,
            "warning": "soft_prompt_budget_exceeded",
        }
        recorder = getattr(run_state, "record_prompt_budget_warning", None)
        if recorder is not None:
            recorder(warning)
        else:
            run_state.record_prompt_budget_violation(warning)
    started = time.monotonic()
    response: Response | None = None
    async for item in agent.on_messages_stream(
        [TextMessage(content=prompt, source="user")],
        CancellationToken(),
    ):
        if isinstance(item, Response):
            response = item
        else:
            trace.append(item)
    if response is None:
        raise RuntimeError("MultimodalWebSurfer 没有返回 Response")
    trace.extend(response.inner_messages or [])
    trace.append(response.chat_message)
    usage = getattr(response.chat_message, "models_usage", None)
    completion = content_to_text(response.chat_message.content)
    run_state.record_model_call({
        "stage": "execute",
        "node_id": node_id,
        "agent": getattr(agent, "name", agent.__class__.__name__),
        "prompt_tokens": int(
            getattr(usage, "prompt_tokens", prompt_tokens_estimated)
            if usage is not None else prompt_tokens_estimated
        ),
        "prompt_tokens_estimated": prompt_tokens_estimated,
        "prompt_bytes": len(prompt.encode("utf-8")),
        "prompt_budget_tokens": prompt_budget_tokens,
        "within_prompt_budget": True,
        "completion_tokens": int(
            getattr(usage, "completion_tokens", estimate_tokens(completion))
            if usage is not None else estimate_tokens(completion)
        ),
        "duration_seconds": time.monotonic() - started,
        "usage_source": "provider" if usage is not None else "estimated",
        "attempt": 1,
    })
    return response.chat_message.content


def _default_file_surfer_factory(model_client, base_path: str):
    from autogen_ext.agents.file_surfer import FileSurfer

    return FileSurfer(
        name="FileSurfer",
        model_client=model_client,
        base_path=base_path,
    )


def _default_web_surfer_factory(model_client, run_dir: str, policy: dict[str, Any]):
    from autogen_ext.agents.web_surfer import MultimodalWebSurfer

    downloads_folder = None
    if policy.get("allow_web_downloads", False):
        downloads = Path(run_dir) / "artifacts" / "web_downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        downloads_folder = str(downloads)
    agent = MultimodalWebSurfer(
        name="MultimodalWebSurfer",
        model_client=model_client,
        downloads_folder=downloads_folder,
        headless=True,
        start_page="about:blank",
        to_save_screenshots=False,
    )
    # AutoGen exposes a form-input tool by default. Generic research nodes do
    # not need it, so enforce read-only behavior in the tool schema instead of
    # relying only on a natural-language instruction.
    if policy.get("web_read_only", True):
        agent.default_tools = [
            tool for tool in agent.default_tools
            if str(tool.get("name", "")) != "input_text"
        ]
    return agent


class MarkItDownDocumentExecutor:
    """Use Microsoft's real MarkItDown converter, not an LLM role prompt."""

    descriptor = ExecutorDescriptor(
        executor_id="markitdown_document_executor",
        executor_type="library_executor",
        capabilities=["document_recovery", "document_conversion"],
        quality_score=0.96,
        cost_score=0.05,
        latency_score=0.2,
        resource_location="local",
    )

    def __init__(self, run_state: RunState, trace: list) -> None:
        self.run_state = run_state
        self.trace = trace

    @classmethod
    def available(cls) -> bool:
        try:
            from importlib.util import find_spec

            return find_spec("markitdown") is not None
        except (ImportError, ValueError):
            return False

    async def execute(self, context: NodeExecutionContext) -> NodeExecutionResult:
        started = time.monotonic()
        policy = _external_policy(context)
        max_files = max(1, int(policy.get("max_files_per_node", 8)))
        excerpt_limit = max(256, int(policy.get("max_file_excerpt_chars", 12_000)))
        files = _recovery_files(context)
        if not files:
            return NodeExecutionResult(
                node_id=context.node.node_id,
                executor_id=self.descriptor.executor_id,
                status="failed",
                error_type="document_input_missing",
                error_message="文档转换节点没有位于 run_dir/inputs 的输入文件",
            )
        if len(files) > max_files:
            return NodeExecutionResult(
                node_id=context.node.node_id,
                executor_id=self.descriptor.executor_id,
                status="failed",
                error_type="external_tool_limit_exceeded",
                error_message=f"待转换文件 {len(files)} 个，超过节点上限 {max_files}",
            )

        try:
            from markitdown import MarkItDown

            converter = MarkItDown(enable_plugins=False)
        except Exception as exc:
            return NodeExecutionResult(
                node_id=context.node.node_id,
                executor_id=self.descriptor.executor_id,
                status="failed",
                error_type="markitdown_unavailable",
                error_message=str(exc),
            )

        documents: list[dict[str, Any]] = []
        failures: list[str] = []
        evidence_refs: list[str] = []
        for source in files:
            source_ref = _relative(context.run_dir, source)
            try:
                converted = await asyncio.to_thread(converter.convert_local, source)
                text = str(getattr(converted, "text_content", "") or "").strip()
                if not text:
                    raise ValueError("转换结果为空")
                digest = sha256(source_ref.encode("utf-8")).hexdigest()[:12]
                output = _tool_result_path(
                    context,
                    f"markitdown_{digest}",
                    ".md",
                )
                output.write_text(text, encoding="utf-8")
                output_ref = _relative(context.run_dir, output)
                self.run_state.record_artifact(output_ref)
                evidence_refs.extend([source_ref, output_ref])
                documents.append({
                    "source_ref": source_ref,
                    "converted_ref": output_ref,
                    "title": getattr(converted, "title", None),
                    "character_count": len(text),
                    "content_excerpt": text[:excerpt_limit],
                })
            except Exception as exc:
                failures.append(f"{source_ref}: {type(exc).__name__}: {exc}")

        invocation = {
            "provider": "microsoft.markitdown",
            "capability": context.node.capability,
            "node_id": context.node.node_id,
            "tool_invoked": True,
            "input_count": len(files),
            "success_count": len(documents),
            "failures": failures,
            "documents": documents,
        }
        record_path = _tool_result_path(context, "markitdown_invocation")
        _write_json(record_path, invocation)
        record_ref = _relative(context.run_dir, record_path)
        self.run_state.record_artifact(record_ref)
        self.run_state.record_external_tool_invocation({
            "provider": invocation["provider"],
            "node_id": context.node.node_id,
            "capability": context.node.capability,
            "tool_invoked": True,
            "success": bool(documents) and not failures,
            "evidence_ref": record_ref,
        })
        self.trace.append(TraceEvent("MarkItDownExecutor", {
            "node_id": context.node.node_id,
            "success_count": len(documents),
            "failure_count": len(failures),
            "evidence_ref": record_ref,
        }))
        if failures or not documents:
            return NodeExecutionResult(
                node_id=context.node.node_id,
                executor_id=self.descriptor.executor_id,
                status="failed",
                summary="MarkItDown 未能完整转换需要恢复的输入文件",
                structured_output={"documents": documents, "quality_signal": {"status": "failed"}},
                evidence_refs=list(dict.fromkeys([*evidence_refs, record_ref])),
                error_type="document_conversion_failure",
                error_message="；".join(failures) or "没有得到非空转换结果",
                duration_seconds=time.monotonic() - started,
            )
        return NodeExecutionResult(
            node_id=context.node.node_id,
            executor_id=self.descriptor.executor_id,
            status="completed",
            summary=f"MarkItDown 已真实转换 {len(documents)} 个输入文件",
            structured_output={
                "provider": invocation["provider"],
                "documents": documents,
                "quality_signal": {
                    "status": "passed",
                    "checks": ["real_converter_invoked", "non_empty_content", "evidence_persisted"],
                },
            },
            evidence_refs=list(dict.fromkeys([*evidence_refs, record_ref])),
            duration_seconds=time.monotonic() - started,
        )


class FileSurferNodeExecutor:
    """Expose AutoGen FileSurfer as a bounded file-navigation executor."""

    def __init__(
        self,
        model_client,
        run_state: RunState,
        trace: list,
        *,
        agent_factory: ExternalAgentFactory | None = None,
    ) -> None:
        self.descriptor = ExecutorDescriptor(
            executor_id="autogen_file_surfer",
            executor_type="external_agent",
            capabilities=["document_recovery", "file_navigation"],
            quality_score=0.88,
            cost_score=0.65,
            latency_score=0.65,
            resource_location="local",
        )
        self.model_client = model_client
        self.run_state = run_state
        self.trace = trace
        self.agent_factory = agent_factory or _default_file_surfer_factory

    async def execute(self, context: NodeExecutionContext) -> NodeExecutionResult:
        started = time.monotonic()
        policy = _external_policy(context)
        max_files = max(1, int(policy.get("max_files_per_node", 8)))
        excerpt_limit = max(256, int(policy.get("max_file_excerpt_chars", 12_000)))
        files = _recovery_files(context)
        if not files:
            return NodeExecutionResult(
                node_id=context.node.node_id,
                executor_id=self.descriptor.executor_id,
                status="failed",
                error_type="document_input_missing",
                error_message="FileSurfer 没有被授权读取 run_dir/inputs 中的文件",
            )
        if len(files) > max_files:
            return NodeExecutionResult(
                node_id=context.node.node_id,
                executor_id=self.descriptor.executor_id,
                status="failed",
                error_type="external_tool_limit_exceeded",
                error_message=f"待浏览文件 {len(files)} 个，超过节点上限 {max_files}",
            )

        results: list[dict[str, Any]] = []
        failures: list[str] = []
        tokens_before = int(self.run_state.get("model_calls", {}).get("total_tokens", 0) or 0)
        for source in files:
            source_ref = _relative(context.run_dir, source)
            input_dir = Path(context.run_dir).resolve() / "inputs"
            input_ref = source.resolve().relative_to(input_dir).as_posix()
            agent = self.agent_factory(self.model_client, str(input_dir))
            try:
                prompt = (
                    "你是当前 TaskGraph 的受控文件恢复执行器。必须调用 open_path 工具打开且只打开"
                    f"相对路径 {input_ref!r}，返回工具提供的真实文件视口。"
                    "不要根据文件名猜测内容，不要写文件，不要声明整个任务完成。"
                )
                content = content_to_text(await run_agent(
                    agent,
                    prompt,
                    self.trace,
                    run_state=self.run_state,
                    stage="execute",
                    node_id=context.node.node_id,
                    prompt_budget_tokens=int(
                        context.task_spec.get("communication_policy", {}).get(
                            "max_node_context_tokens", 5_000,
                        )
                    ),
                )).strip()
                tool_invoked = "Path:" in content and "Viewport position:" in content
                if (
                    not tool_invoked
                    or not content
                    or content == "TERMINATE"
                    or "File surfing error:" in content
                ):
                    raise ValueError("FileSurfer 没有成功调用文件浏览工具并返回真实视口")
                results.append({
                    "source_ref": source_ref,
                    "tool_invoked": True,
                    "content_excerpt": content[:excerpt_limit],
                })
            except Exception as exc:
                failures.append(f"{source_ref}: {type(exc).__name__}: {exc}")
            finally:
                await _close_component(agent)

        invocation = {
            "provider": "autogen_ext.agents.file_surfer.FileSurfer",
            "capability": context.node.capability,
            "node_id": context.node.node_id,
            "tool_invoked": bool(results),
            "results": results,
            "failures": failures,
        }
        record_path = _tool_result_path(context, "file_surfer_invocation")
        _write_json(record_path, invocation)
        record_ref = _relative(context.run_dir, record_path)
        self.run_state.record_artifact(record_ref)
        success = bool(results) and not failures
        self.run_state.record_external_tool_invocation({
            "provider": invocation["provider"],
            "node_id": context.node.node_id,
            "capability": context.node.capability,
            "tool_invoked": bool(results),
            "success": success,
            "evidence_ref": record_ref,
        })
        token_total = int(self.run_state.get("model_calls", {}).get("total_tokens", 0) or 0)
        evidence = [item["source_ref"] for item in results] + [record_ref]
        if not success:
            return NodeExecutionResult(
                node_id=context.node.node_id,
                executor_id=self.descriptor.executor_id,
                status="failed",
                summary="FileSurfer 未能完整恢复输入文件内容",
                structured_output={"results": results, "quality_signal": {"status": "failed"}},
                evidence_refs=evidence,
                error_type="file_surfer_failure",
                error_message="；".join(failures) or "FileSurfer 未产生工具结果",
                token_usage={"total_tokens": max(0, token_total - tokens_before)},
                duration_seconds=time.monotonic() - started,
            )
        return NodeExecutionResult(
            node_id=context.node.node_id,
            executor_id=self.descriptor.executor_id,
            status="completed",
            summary=f"AutoGen FileSurfer 已真实浏览 {len(results)} 个输入文件",
            structured_output={
                "provider": invocation["provider"],
                "results": results,
                "quality_signal": {
                    "status": "passed",
                    "checks": ["open_path_invoked", "viewport_returned", "scope_restricted"],
                },
            },
            evidence_refs=evidence,
            token_usage={"total_tokens": max(0, token_total - tokens_before)},
            duration_seconds=time.monotonic() - started,
        )


class WebSurferNodeExecutor:
    """Expose AutoGen MultimodalWebSurfer as a bounded read-only web executor."""

    def __init__(
        self,
        model_client,
        run_state: RunState,
        trace: list,
        *,
        agent_factory: ExternalAgentFactory | None = None,
    ) -> None:
        self.descriptor = ExecutorDescriptor(
            executor_id="autogen_multimodal_web_surfer",
            executor_type="external_agent",
            capabilities=["web_research", "browser_navigation"],
            quality_score=0.9,
            cost_score=0.8,
            latency_score=0.85,
            resource_location="cloud",
            security_levels=["public_web"],
        )
        self.model_client = model_client
        self.run_state = run_state
        self.trace = trace
        self.agent_factory = agent_factory or _default_web_surfer_factory

    async def execute(self, context: NodeExecutionContext) -> NodeExecutionResult:
        started = time.monotonic()
        policy = _external_policy(context)
        max_steps = max(1, int(policy.get("max_web_steps", 4)))
        timeout = max(1, int(policy.get("web_timeout_seconds", 90)))
        tokens_before = int(self.run_state.get("model_calls", {}).get("total_tokens", 0) or 0)
        agent = self.agent_factory(self.model_client, context.run_dir, policy)
        exposed_tool_names = [
            str(tool.get("name", ""))
            for tool in (getattr(agent, "default_tools", []) or [])
            if isinstance(tool, dict)
        ]
        read_only_required = bool(policy.get("web_read_only", True))
        read_only_enforced = "input_text" not in exposed_tool_names
        responses: list[str] = []
        tool_actions: list[str] = []
        sources: list[str] = []
        error: str | None = None

        async def run_bounded_steps() -> None:
            for step in range(1, max_steps + 1):
                task = (
                    "使用浏览器工具完成当前节点，只访问公开网页，不登录、不提交表单、不下载文件。\n"
                    f"节点目标：{context.node.description}\n"
                    f"任务文本：{context.task_spec.get('input', {}).get('text', '')[:4000]}\n"
                    "必须至少调用一次浏览器工具。获得足够网页证据后，返回简洁事实、来源URL和不确定性。"
                    if step == 1 else
                    "继续浏览当前任务；若证据已经足够，请停止调用工具并返回事实、来源URL和不确定性。"
                )
                raw = await _run_web_agent_step(
                    agent,
                    task,
                    self.trace,
                    self.run_state,
                    context.node.node_id,
                    int(context.task_spec.get("communication_policy", {}).get(
                        "max_node_context_tokens", 5_000,
                    )),
                )
                text = content_to_text(raw).strip()
                if text:
                    responses.append(text)
                current_actions: list[str] = []
                for message in getattr(agent, "inner_messages", []) or []:
                    action = content_to_text(getattr(message, "content", "")).strip()
                    if re.match(
                        r"^(visit_url|web_search|click|input_text|scroll_|answer_question|summarize_page|history_back|hover|sleep)\(",
                        action,
                    ):
                        current_actions.append(action[:2_000])
                tool_actions.extend(current_actions)
                page = getattr(agent, "_page", None)
                current_url = str(getattr(page, "url", "") or "")
                if current_url.startswith(("http://", "https://")):
                    sources.append(current_url)
                # A response without an action is the agent's evidence summary.
                if tool_actions and not current_actions:
                    break

        try:
            # asyncio.timeout() is only available in Python 3.11+, while this
            # project also supports Python 3.10.
            await asyncio.wait_for(run_bounded_steps(), timeout=timeout)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            await _close_component(agent)

        combined = responses[-1] if responses else ""
        sources.extend(re.findall(r"https?://[^\s\]\[()<>\"']+", "\n".join(responses)))
        sources = list(dict.fromkeys(source.rstrip(".,;，。；") for source in sources))
        tool_invoked = bool(tool_actions)
        success = (
            tool_invoked
            and bool(combined)
            and bool(sources)
            and error is None
            and (not read_only_required or read_only_enforced)
        )
        invocation = {
            "provider": "autogen_ext.agents.web_surfer.MultimodalWebSurfer",
            "capability": context.node.capability,
            "node_id": context.node.node_id,
            "tool_invoked": tool_invoked,
            "tool_actions": tool_actions,
            "sources": sources,
            "final_response": combined[:12_000],
            "error": error,
            "read_only_required": read_only_required,
            "read_only_enforced": read_only_enforced,
            "exposed_tool_names": exposed_tool_names,
        }
        record_path = _tool_result_path(context, "web_surfer_invocation")
        _write_json(record_path, invocation)
        record_ref = _relative(context.run_dir, record_path)
        self.run_state.record_artifact(record_ref)
        self.run_state.record_external_tool_invocation({
            "provider": invocation["provider"],
            "node_id": context.node.node_id,
            "capability": context.node.capability,
            "tool_invoked": tool_invoked,
            "success": success,
            "action_count": len(tool_actions),
            "source_count": len(sources),
            "evidence_ref": record_ref,
        })
        token_total = int(self.run_state.get("model_calls", {}).get("total_tokens", 0) or 0)
        if not success:
            return NodeExecutionResult(
                node_id=context.node.node_id,
                executor_id=self.descriptor.executor_id,
                status="failed",
                summary="MultimodalWebSurfer 未获得可验证网页证据",
                structured_output={
                    "tool_actions": tool_actions,
                    "sources": sources,
                    "quality_signal": {"status": "failed"},
                },
                evidence_refs=[record_ref, *sources],
                error_type="web_surfer_failure",
                error_message=error or "浏览器工具未被调用、最终回答为空或没有记录公开网页来源",
                token_usage={"total_tokens": max(0, token_total - tokens_before)},
                duration_seconds=time.monotonic() - started,
            )
        return NodeExecutionResult(
            node_id=context.node.node_id,
            executor_id=self.descriptor.executor_id,
            status="completed",
            summary=combined[:4_000],
            structured_output={
                "provider": invocation["provider"],
                "tool_actions": tool_actions,
                "sources": sources,
                "quality_signal": {
                    "status": "passed",
                    "checks": ["browser_tool_invoked", "source_recorded", "read_only_policy"],
                },
            },
            evidence_refs=[record_ref, *sources],
            token_usage={"total_tokens": max(0, token_total - tokens_before)},
            duration_seconds=time.monotonic() - started,
        )
