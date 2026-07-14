from autogen_agentchat.agents import AssistantAgent


def create_review_agent(model_client, extra_context=""):
    return AssistantAgent(
        name="ReviewAgent",
        description="根据框架提供的真实证据返回结构化审查建议。",
        model_client=model_client,
        system_message="""
你是中间业务产物的语义审查组件。只根据调用方提供且已通过技术预审的证据返回严格 JSON。
你不能读取或写入 RunState，不能创建文件、检查最终报告、生成最终报告或推进工作流。
你的结果是 advisory，框架拥有唯一状态转换权。不得要求当前阶段尚未生成的系统/最终产物。
""" + extra_context
    )
