from autogen_agentchat.agents import AssistantAgent


def create_reasoning_agent(model_client, extra_context=""):
    return AssistantAgent(
        name="ReasoningAgent",
        description="负责模型选择、结果解释、方案判断、跨步骤推理和结论分析。",
        model_client=model_client,
        system_message="""
你是推理分析Agent。
你的职责是进行方案判断、模型选择、结果解释、成因分析和跨模态推理。
你需要说明为什么选择某种方法，结论是否有依据，结果是否合理。
输出要包含：
1. 分析判断
2. 推理依据
3. 结论解释
4. 风险点
5. 后续建议
""" + extra_context
    )
