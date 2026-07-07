from pathlib import Path


def get_last_text_message(messages):
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if isinstance(content, str) and content.strip():
            return content
    return ""


def get_last_text_from_source(messages, source_name):
    for msg in reversed(messages):
        source = getattr(msg, "source", "")
        content = getattr(msg, "content", None)

        if source == source_name and isinstance(content, str) and content.strip():
            return content

    return ""


def get_final_report(messages):
    """
    优先保存 ReportAgent 的正式报告。
    如果 ReportAgent 没有输出，再退回最后一条文本消息。
    """
    report = get_last_text_from_source(messages, "ReportAgent")
    if report:
        return report

    return get_last_text_message(messages)


def save_agent_trace(messages, path="outputs/agent_trace.md"):
    from pathlib import Path

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = []

    allowed_types = {
        "TextMessage",
        "CodeExecutionEvent",
        "CodeGenerationEvent",
    }

    for i, msg in enumerate(messages, 1):
        msg_type = msg.__class__.__name__

        if msg_type not in allowed_types:
            continue

        source = getattr(msg, "source", "unknown")
        content = getattr(msg, "content", "")

        if not isinstance(content, str):
            content = str(content)

        if not content.strip():
            continue

        lines.append(f"## Step {i}: {source} / {msg_type}\n")
        lines.append(content)
        lines.append("\n---\n")

    path.write_text("\n".join(lines), encoding="utf-8")


def save_final_report(text, path="outputs/final_report.md"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")