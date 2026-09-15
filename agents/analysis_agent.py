from autogen_agentchat.agents import AssistantAgent


def create_analysis_agent(model_client, extra_context=""):
    """Create the evidence-first analysis role used by DyLAN-lite routing.

    This role intentionally differs from ``ReasoningAgent``: it prioritises
    extraction, comparison and auditable facts, while the latter handles
    cross-step reasoning and model/strategy judgement.
    """

    return AssistantAgent(
        name="AnalysisAgent",
        description="负责基于输入数据做事实提取、字段核对、统计汇总和证据绑定。",
        model_client=model_client,
        system_message="""
你是证据优先的数据分析 Agent。
你的职责是从框架提供的标准化输入预览和直接依赖结果中提取事实、核对字段、进行必要的
统计汇总，并把每项结论绑定到真实证据引用。不得补造预览中不存在的数字、日期或文件内容。
如果证据不足，应明确标记风险，而不是猜测。
""" + extra_context,
    )
