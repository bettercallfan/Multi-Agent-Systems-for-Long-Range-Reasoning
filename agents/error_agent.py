from autogen_agentchat.agents import AssistantAgent


def create_error_agent(model_client, extra_context=""):
    return AssistantAgent(
        name="ErrorAttributionAgent",
        description="根据真实执行结果返回结构化错误归因和修复建议。",
        model_client=model_client,
        system_message="""
你是错误归因组件。只分析调用方提供的真实 ExecutionResult，并返回严格 JSON。
你不能直接重试、修改代码、写入状态或推进工作流。是否重试由框架根据剩余次数决定。
""" + extra_context
    )
