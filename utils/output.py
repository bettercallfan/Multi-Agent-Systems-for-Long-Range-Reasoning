"""Framework-owned persistence for reports and execution traces."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel


@dataclass
class TraceEvent:
    source: str
    content: Any
    type: str = "FrameworkEvent"


def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, BaseModel):
        return json.dumps(content.model_dump(), ensure_ascii=False, indent=2, default=str)
    if isinstance(content, (dict, list)):
        # Tool agents may return multimodal lists containing image wrapper
        # objects.  Traces retain their textual metadata without attempting to
        # serialise binary pixels or failing the already-completed tool call.
        return json.dumps(content, ensure_ascii=False, indent=2, default=str)
    return str(content)


def get_last_content(messages) -> Any:
    for message in reversed(messages):
        content = getattr(message, "content", None)
        if content is not None and content_to_text(content).strip():
            return content
    return ""


def save_agent_trace(messages, path="outputs/agent_trace.md"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# 框架执行轨迹", ""]
    for index, message in enumerate(messages, 1):
        source = getattr(message, "source", "unknown")
        msg_type = getattr(message, "type", message.__class__.__name__)
        if msg_type in {"ThoughtEvent", "ModelClientStreamingChunkEvent"}:
            continue
        content = content_to_text(getattr(message, "content", ""))
        if not content.strip():
            continue
        lines.extend([
            f"## Step {index}: {source} / {msg_type}",
            "",
            content,
            "",
            "---",
            "",
        ])
    path.write_text("\n".join(lines), encoding="utf-8")


def save_final_report(text, path="outputs/final_report.md"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")
