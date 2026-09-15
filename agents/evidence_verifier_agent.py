from autogen_agentchat.agents import AssistantAgent


def create_evidence_verifier_agent(model_client, extra_context=""):
    return AssistantAgent(
        name="EvidenceVerifierAgent",
        description="依据框架读取的真实证据目录逐条核验事实声明。",
        model_client=model_client,
        system_message="""
你是 SAFE/CRITIC 风格的证据核验组件。调用方已经用 Python 读取并限制了真实证据目录。
你必须把上游结论拆成原子事实，并只引用目录中的 evidence_ref。严格返回 JSON，不使用 Markdown。
没有证据的事实必须标记 insufficient，证据与事实冲突时标记 contradicted。
你不能访问未提供的文件、写文件、修改 RunState、推进阶段或生成最终报告。
""" + extra_context,
    )
