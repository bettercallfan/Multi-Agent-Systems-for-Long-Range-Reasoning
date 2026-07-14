from autogen_agentchat.agents import AssistantAgent


def create_planning_agent(model_client, extra_context=""):
    return AssistantAgent(
        name="TaskPlanningAgent",
        description="负责理解用户目标并返回结构化 TaskGraph，不负责调度。",
        model_client=model_client,
        system_message="""
你是任务图规划组件。解析 TaskSpec 和可用能力，输出调用方指定结构的 JSON DAG。
你无权调用其他 Agent、推进工作流、执行代码、写入文件或修改运行状态。
你只能提出 capability 需求，不能指定 Agent；不要输出 Mermaid、调度命令或“已完成”声明。
代码需求不得突破 code_policy，review/report/final validation 不属于任务图。
""" + extra_context
    )
