from autogen_agentchat.agents import AssistantAgent


def create_planning_agent(model_client, extra_context=""):
    return AssistantAgent(
        name="TaskPlanningAgent",
        description="负责理解用户目标、拆解复杂任务、生成任务图，并给出下一步执行建议。",
        model_client=model_client,
        system_message="""
你是任务规划Agent。
你的职责是解析用户复杂任务，提取任务目标、约束条件、输入数据和最终交付物。
你需要把复杂任务拆解成清晰的任务图，并说明每个任务节点需要哪些Agent参与。
不要假设固定的执行流程。根据 TaskSpec、file_previews 和任务类型动态决定需要哪些步骤。
输出要包含：
1. 任务目标
2. 任务约束
3. 任务节点
4. 节点依赖关系
5. 推荐调用的Agent
6. 下一步建议
""" + extra_context
    )
