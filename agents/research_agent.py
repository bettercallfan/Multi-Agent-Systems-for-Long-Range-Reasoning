from autogen_agentchat.agents import AssistantAgent


def create_research_agent(model_client, extra_context=""):
    return AssistantAgent(
        name="ResearchAgent",
        description="负责资料调研、背景补充、方法依据整理和参考方案总结。",
        model_client=model_client,
        system_message="""
你是资料调研Agent。
你的职责是补充任务背景、相关方法、技术路线、案例和参考依据。
你需要根据 TaskSpec 和输入文件自动识别任务类型，并针对该类型整理对应的背景知识、常用方法和评价标准。
输出要简洁，包含：
1. 调研结论
2. 可用方法
3. 适用条件
4. 对后续任务的建议
""" + extra_context
    )
