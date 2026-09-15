from autogen_agentchat.agents import AssistantAgent


def create_workflow_memory_agent(model_client, extra_context=""):
    return AssistantAgent(
        name="WorkflowMemoryAgent",
        description="检索和提炼可复用任务图技能，但不直接写入长期记忆。",
        model_client=model_client,
        system_message="""
你是 Agent Workflow Memory / Voyager Skill Library 风格的技能记忆组件。
检索阶段只能从调用方提供的候选技能中选择；提炼阶段只能根据已验证的任务图和运行结果总结。
严格返回调用方要求的 JSON。不得记录密钥、绝对路径、用户原文或未经验证的事实。
你不能直接读写记忆库、RunState 或任务产物；所有持久化均由 Python 框架审核后完成。
""" + extra_context,
    )
