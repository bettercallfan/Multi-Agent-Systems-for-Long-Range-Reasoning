"""Shared, framework-owned language-model invocation policy."""

from __future__ import annotations

import asyncio

from utils.output import TraceEvent, get_last_content


async def run_agent(agent, prompt: str, trace: list) -> object:
    """Call one agent with transport retries that do not consume task retries."""
    transient_markers = (
        "connection error", "timeout", "timed out", "temporarily unavailable",
        "rate limit", "429", "502", "503", "504",
    )
    max_transport_attempts = 3
    for attempt in range(1, max_transport_attempts + 1):
        try:
            result = await agent.run(task=prompt, output_task_messages=False)
            trace.extend(result.messages)
            return get_last_content(result.messages)
        except Exception as exc:
            transient = any(marker in str(exc).lower() for marker in transient_markers)
            if not transient or attempt >= max_transport_attempts:
                raise
            trace.append(TraceEvent("FrameworkTransport", {
                "event": "model_call_retry",
                "attempt": attempt,
                "max_attempts": max_transport_attempts,
                "reason": type(exc).__name__,
            }))
            await asyncio.sleep(attempt)
    raise RuntimeError("模型调用重试循环异常结束")
