from autogen_agentchat.agents import AssistantAgent


def create_planning_agent(model_client, extra_context=""):
    return AssistantAgent(
        name="TaskPlanningAgent",
        description="负责理解用户目标并返回调用方指定的结构化规划表示，不负责调度。",
        model_client=model_client,
        system_message="""
你是纯规划组件。解析 TaskSpec 和可用能力，严格输出调用方指定的 JSON PlanIR、PlanPatch
或 SemanticGraph；具体输出类型以当前用户提示为准。
你无权调用其他 Agent、推进工作流、执行代码、写入文件或修改运行状态。
你只能提出 capability 需求，不能指定 Agent；不要输出 Mermaid、调度命令或“已完成”声明。
代码需求不得突破 code_policy；终端复核、证据核验、产物校验、review、report 和 final
validation 均由 Python 框架补全，不属于你规划的业务语义图。
""" + extra_context
    )
