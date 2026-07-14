from autogen_agentchat.agents import AssistantAgent


def create_report_agent(model_client, extra_context=""):
    return AssistantAgent(
        name="ReportAgent",
        description="基于已审查证据生成一次最终业务 Markdown 报告。",
        model_client=model_client,
        system_message="""
你是最终业务报告组件。调用方只会在审查允许后调用你一次。
只输出 Markdown 报告正文；不得输出 TaskSpec、运行状态、执行轨迹或系统说明。
不得提及 Agent 名称，不得声称创建了任何文件，不得引用调用方未提供的证据。
若证据标记为 partial，必须明确说明报告为非正式版本。
""" + extra_context
    )
