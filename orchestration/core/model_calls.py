"""Shared, framework-owned language-model invocation policy."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Callable

from utils.output import TraceEvent, content_to_text, get_last_content


class PromptBudgetExceededError(ValueError):
    """Legacy exception kept for integrations that still enforce their own limit."""


class PromptContextBlockedError(ValueError):
    """The model input limit remained exceeded after one context reduction."""


def estimate_tokens(value: Any) -> int:
    """Conservative dependency-free estimate for mixed Chinese/English text."""
    text = content_to_text(value) if not isinstance(value, str) else value
    if not text:
        return 0
    # Chinese characters are commonly close to one token, while latin text is
    # closer to one token per four characters.  Counting non-ASCII separately
    # is intentionally conservative and deterministic across model providers.
    non_ascii = sum(1 for char in text if ord(char) > 127 and not char.isspace())
    ascii_chars = len(text) - non_ascii
    return non_ascii + (ascii_chars + 3) // 4


def compact_prompt_for_overflow(prompt: str, *, max_chars: int | None = None) -> str:
    """Minimal one-shot context reduction used only at a real model limit.

    Keep the instruction/contract envelope and the tail (usually the latest
    result or repair instruction), while dropping repeated middle history.
    This is deliberately deterministic and does not replace the normal
    communication compressors.
    """
    if not prompt:
        return prompt
    if max_chars is None:
        max_chars = max(1200, int(len(prompt) * 0.55))
    else:
        max_chars = max(1, int(max_chars))
    if len(prompt) <= max_chars:
        return prompt
    head = max_chars * 2 // 3
    tail = max_chars - head
    return (
        prompt[:head]
        + "\n\n[framework context reduction: repeated history/file previews omitted]\n\n"
        + prompt[-tail:]
    )


def _provider_usage(messages: list) -> tuple[int, int] | None:
    prompt_tokens = 0
    completion_tokens = 0
    found = False
    for message in messages:
        usage = getattr(message, "models_usage", None)
        if usage is None:
            continue
        found = True
        prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
    return (prompt_tokens, completion_tokens) if found else None


async def run_agent(
    agent,
    prompt: str,
    trace: list,
    *,
    run_state=None,
    stage: str | None = None,
    node_id: str | None = None,
    prompt_budget_tokens: int | None = None,
    raw_prompt_tokens_estimated: int | None = None,
    model_input_limit_tokens: int | None = None,
    prompt_reducer: Callable[[str], str] | None = None,
) -> object:
    """Call an agent with soft framework budgets and recoverable overflow.

    ``prompt_budget_tokens`` is an optimization target.  A provider/model
    limit is a separate, optional fact supplied by the executor.  We never
    turn the former into a fatal preflight check.
    """
    prompt_tokens_estimated = estimate_tokens(prompt)
    prompt_bytes = len(prompt.encode("utf-8"))
    soft_budget_exceeded = (
        prompt_budget_tokens is not None
        and prompt_tokens_estimated > prompt_budget_tokens
    )
    if prompt_budget_tokens is not None and prompt_tokens_estimated > prompt_budget_tokens:
        if run_state is not None:
            warning = {
                "stage": stage or run_state.get("stage"),
                "node_id": node_id,
                "agent": getattr(agent, "name", agent.__class__.__name__),
                "prompt_tokens_estimated": prompt_tokens_estimated,
                "prompt_bytes": prompt_bytes,
                "prompt_budget_tokens": prompt_budget_tokens,
                "warning": "soft_prompt_budget_exceeded",
            }
            recorder = getattr(run_state, "record_prompt_budget_warning", None)
            if recorder is not None:
                recorder(warning)
            else:
                # Backward-compatible fallback for older RunState objects.
                run_state.record_prompt_budget_violation(warning)

    if model_input_limit_tokens is not None and prompt_tokens_estimated > model_input_limit_tokens:
        if prompt_reducer is not None:
            reduced_prompt = prompt_reducer(prompt)
            reduced_tokens = estimate_tokens(reduced_prompt)
            if reduced_tokens <= model_input_limit_tokens:
                prompt = reduced_prompt
                prompt_tokens_estimated = reduced_tokens
                prompt_bytes = len(prompt.encode("utf-8"))
            else:
                raise PromptContextBlockedError(
                    f"模型输入仍为 {reduced_tokens} tokens，超过真实上限 "
                    f"{model_input_limit_tokens}；建议 reduce_context_or_split_node"
                )
        else:
            raise PromptContextBlockedError(
                f"模型输入估算 {prompt_tokens_estimated} tokens，超过真实上限 "
                f"{model_input_limit_tokens}；建议 reduce_context_or_split_node"
            )
    transient_markers = (
        "connection error", "timeout", "timed out", "temporarily unavailable",
        "rate limit", "429", "502", "503", "504",
        # Some OpenAI-compatible gateways intermittently return this provider
        # routing error although adjacent calls to the same deployment work.
        # Handle it as transport instability so it cannot consume a business
        # or code-repair attempt.
        "accessdenied.unpurchased", "access to model denied",
    )
    max_transport_attempts = 3
    try:
        call_timeout = max(10.0, float(os.getenv("MODEL_CALL_TIMEOUT_SECONDS", "240")))
    except ValueError:
        call_timeout = 240.0
    for attempt in range(1, max_transport_attempts + 1):
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                agent.run(task=prompt, output_task_messages=False),
                timeout=call_timeout,
            )
            trace.extend(result.messages)
            content = get_last_content(result.messages)
            usage = _provider_usage(result.messages)
            if usage is None:
                prompt_tokens = estimate_tokens(prompt)
                completion_tokens = estimate_tokens(content)
                usage_source = "estimated"
            else:
                prompt_tokens, completion_tokens = usage
                usage_source = "provider"
            if run_state is not None:
                run_state.record_model_call({
                    "stage": stage or run_state.get("stage"),
                    "node_id": node_id,
                    "agent": getattr(agent, "name", agent.__class__.__name__),
                    "prompt_tokens": prompt_tokens,
                    "prompt_tokens_estimated": prompt_tokens_estimated,
                    "raw_prompt_tokens_estimated": raw_prompt_tokens_estimated,
                    "prompt_bytes": prompt_bytes,
                    "prompt_budget_tokens": prompt_budget_tokens,
                    "within_prompt_budget": (
                        prompt_budget_tokens is None
                        or prompt_tokens_estimated <= prompt_budget_tokens
                    ),
                    "soft_prompt_budget_exceeded": soft_budget_exceeded,
                    "model_input_limit_tokens": model_input_limit_tokens,
                    "completion_tokens": completion_tokens,
                    "duration_seconds": time.monotonic() - started,
                    "usage_source": usage_source,
                    "attempt": attempt,
                })
            return content
        except Exception as exc:
            transient = isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or any(
                marker in str(exc).lower() for marker in transient_markers
            )
            if not transient or attempt >= max_transport_attempts:
                raise
            trace.append(TraceEvent("FrameworkTransport", {
                "event": "model_call_retry",
                "attempt": attempt,
                "max_attempts": max_transport_attempts,
                "reason": type(exc).__name__,
            }))
            if run_state is not None:
                run_state.record_event("model_transport_retry", {
                    "stage": stage or run_state.get("stage"),
                    "node_id": node_id,
                    "agent": getattr(agent, "name", agent.__class__.__name__),
                    "attempt": attempt,
                    "max_attempts": max_transport_attempts,
                    "reason": type(exc).__name__,
                })
            await asyncio.sleep(attempt)
    raise RuntimeError("模型调用重试循环异常结束")
