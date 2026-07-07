import asyncio
from pathlib import Path

from autogen_ext.models.openai import OpenAIChatCompletionClient
from autogen_ext.teams.magentic_one import MagenticOne


def get_last_text_message(messages):
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if isinstance(content, str) and content.strip():
            return content
    return ""


def save_trace(messages, path: Path):
    lines = []

    for i, msg in enumerate(messages, 1):
        source = getattr(msg, "source", "unknown")
        msg_type = getattr(msg, "type", msg.__class__.__name__)
        content = getattr(msg, "content", "")

        lines.append(f"## Step {i}: {source} / {msg_type}\n")

        if isinstance(content, str):
            lines.append(content)
        else:
            lines.append(f"```text\n{content}\n```")

        lines.append("\n---\n")

    path.write_text("\n".join(lines), encoding="utf-8")


async def main():
    model_client = OpenAIChatCompletionClient(
        model="qwen3.5-35b-a3b",
        api_key="sk-8b89c3d813844f9390c440905aa26b20",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model_info={
            "vision": False,
            "function_calling": True,
            "json_output": True,
            "structured_output": False,
            "family": "unknown",
        },
    )

    team = MagenticOne(client=model_client)

    task = """
请围绕一个数据建模任务，完成任务拆解、建模流程设计、代码实现思路、结果分析方式和最终报告结构。

最终请输出一份 Markdown 格式报告，包含：
1. 任务理解
2. 任务拆解
3. 建模流程
4. 代码实现思路
5. 结果分析方式
6. 风险与改进建议
"""

    result = await team.run(task=task)

    output_dir = Path("outputs")
    output_dir.mkdir(exist_ok=True)

    final_report = get_last_text_message(result.messages)

    (output_dir / "final_report.md").write_text(final_report, encoding="utf-8")
    save_trace(result.messages, output_dir / "agent_trace.md")

    print("最终报告已保存到：outputs/final_report.md")
    print("Agent 执行轨迹已保存到：outputs/agent_trace.md")

    await model_client.close()


asyncio.run(main())