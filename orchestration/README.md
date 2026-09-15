# Orchestration 目录说明

这个目录只保存框架控制面代码。业务语义由 Agent 提议，流程推进、状态更新、权限检查和验收仍由 Python 确定性执行。

| 子目录 | 职责 | 是否适合 Agent 化 |
|---|---|---|
| `core/` | 工作流阶段、状态机、结构化模型、模型调用和提示 | 否，必须由框架掌权 |
| `task/` | 输入准备、TaskSpec 和通用产物契约 | 分类语义可由 Agent 建议，契约冻结不可 Agent 化 |
| `graph/` | TaskGraph 校验、调度和失败恢复 | 规划建议由 PlanningAgent 产生，校验与调度不可 Agent 化 |
| `execution/` | Capability Registry、执行者选择和统一执行接口 | 执行者可以是 Agent，注册和结果验收不可 Agent 化 |
| `communication/` | 稀疏上下文、消息预算、压缩和指标 | 摘要器可插拔，预算与指标不可 Agent 化 |
| `integrations/` | 文件、网页、终端和事实核验适配器 | 适合包装真实工具型 Agent |
| `memory/` | 工作流经验的检索、过滤、提炼和保存 | 提炼可由 Agent 完成，过滤和持久化不可 Agent 化 |

当前新增的 `TaskUnderstandingAgent` 位于 `agents/task_understanding_agent.py`。它在 PlanningAgent 之前真实运行，只输出目标、约束、歧义、风险和能力建议；TaskSpec、TaskGraph、RunState 和阶段推进仍由框架控制。

主调用链如下：

```text
main.py
  -> task/input_loader.py + task/task_normalizer.py
  -> core/workflow.py
       -> TaskUnderstandingAgent
       -> PlanningAgent
       -> graph/graph_planner.py
       -> graph/graph_scheduler.py
            -> execution/capability_registry.py
            -> Agent、工具或确定性 Executor
       -> ReviewAgent
       -> ReportAgent
       -> Python final validation
```
