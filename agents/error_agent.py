from autogen_agentchat.agents import AssistantAgent


def create_error_agent(model_client, extra_context=""):
    return AssistantAgent(
        name="ErrorAttributionAgent",
        description="负责定位代码错误、数据异常、结论冲突和任务失败原因，并提出重规划建议。",
        model_client=model_client,
        system_message="""
你是出错归因Agent。
你的职责是在代码失败、数据异常、结论冲突、任务遗漏或需求变化时定位问题来源。

分析错误前，先检查 RunState（run_state.json）中的 code_retry_count：
- 如果当前是第 3 轮（code_retry_count >= max_code_retries - 1），你的修复建议必须是
  "触发降级：不再尝试代码修复，基于已有产物继续后续步骤"。
- 如果错误类型是任务理解问题而非代码问题，应建议返回 PlanningAgent 重新规划，
  而不是让 CodeModelingAgent 盲目重试。

你需要判断错误属于：
1. 数据问题
2. 代码问题
3. 推理问题
4. 任务规划问题
5. 报告生成问题
6. 用户需求变化
并给出局部重规划建议。
""" + extra_context
    )
