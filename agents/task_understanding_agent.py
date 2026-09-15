from autogen_agentchat.agents import AssistantAgent


def create_task_understanding_agent(model_client, extra_context: str = ""):
    """Create the semantic intent role used before TaskGraph planning."""
    return AssistantAgent(
        name="TaskUnderstandingAgent",
        description="提炼任务目标、约束、歧义和能力建议，不生成或调度任务图。",
        model_client=model_client,
        system_message="""
你是任务理解组件。只根据框架提供的 TaskSpec 摘要和文件预览，提炼任务目标、约束、
不确定性和风险，并从调用方提供的 capability 列表中建议真正必要的能力。
你不能修改 TaskSpec、生成 TaskGraph、调用其他 Agent、写文件、推进状态或声明任务完成。
证据不足时必须记录 ambiguity，不能补造文件内容或业务事实。
""" + extra_context,
    )
