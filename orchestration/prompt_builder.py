import json
from pathlib import Path


def _categorize_task(task_spec: dict) -> str:
    """
    根据 TaskSpec 和 file_previews 推断任务类别。
    返回: 'document_analysis' | 'math_modeling' | 'data_modeling' | 'general_complex_task'
    """
    code_policy = task_spec.get("code_policy", {})
    allows_complex = code_policy.get("allows_complex", True)
    file_previews = task_spec.get("file_previews", [])
    input_files = task_spec.get("input", {}).get("files", [])

    # 收集所有文本
    all_text = ""
    for fp in file_previews:
        all_text += fp.get("text_preview", "") + " "

    # 检查文件类型
    suffixes = set()
    for f in input_files:
        suffixes.add(Path(f).suffix.lower())

    # 数学建模关键词
    math_keywords = ["建模", "优化", "求解", "装箱", "路径规划", "调度", "算法"]
    # 数据建模关键词
    data_keywords = ["训练", "预测", "分类", "回归", "聚类", "特征", "模型评估"]
    # 文档分析关键词
    doc_keywords = ["整理", "提取", "汇总", "报销", "审批", "核对", "阅读", "理解", "摘要"]

    math_hits = sum(1 for kw in math_keywords if kw in all_text)
    data_hits = sum(1 for kw in data_keywords if kw in all_text)
    doc_hits = sum(1 for kw in doc_keywords if kw in all_text)

    # 纯文档后缀（无 .py .csv .json）
    is_all_docs = suffixes and suffixes.issubset({".pdf", ".docx", ".xlsx", ".xls", ".md", ".txt"})

    if not allows_complex and is_all_docs and doc_hits > 0:
        return "document_analysis"
    if math_hits > 0 and allows_complex:
        return "math_modeling"
    if data_hits > 0 and allows_complex:
        return "data_modeling"
    if doc_hits >= 2 and is_all_docs:
        return "document_analysis"

    return "general_complex_task"


def _build_filesurfer_policy(task_spec: dict) -> str:
    """生成 FileSurfer 使用策略。"""
    file_previews = task_spec.get("file_previews", [])

    # 统计解析状态
    success_count = 0
    failed_files = []
    success_files = []

    for fp in file_previews:
        fname = Path(fp.get("path", "")).name
        status = fp.get("status", "unknown")
        if status == "success":
            success_count += 1
            success_files.append(fname)
        else:
            failed_files.append((fname, fp.get("error", "未知错误")))

    total = len(file_previews)
    has_failures = len(failed_files) > 0

    # 生成摘要信息
    summary_lines = [
        f"- 成功解析：{success_count}/{total} 个文件",
    ]
    if success_files:
        summary_lines.append(f"- 可用的 file_previews：{', '.join(success_files)}")
    if failed_files:
        for name, err in failed_files:
            summary_lines.append(f"- 解析失败：{name}（{err}）")
        summary_lines.append("- **以上解析失败的文件需要在运行时通过 FileSurfer 补充查看**")

    summary_block = "\n".join(summary_lines)

    return f"""
## FileSurfer 使用策略（所有任务类型通用）

### 信息源优先级
1. **file_previews（优先）**：系统已在执行前通过 file_reader.py 对所有输入文件做了预读取。
   每个文件的 file_previews 包含：文本摘要、表格结构、段落数、sheet 信息等。
   {summary_block}

2. **FileSurfer（补充）**：仅在以下情况使用 FileSurfer 查看原始文件：
   - file_previews 中某个文件的 status 为 "error" 或 "unknown"
   - file_previews 的摘要明显不足以支撑任务理解（如关键字段缺失、截断）
   - ReviewAgent 在审查阶段需要核对产物文件与原始输入的一致性
   - task_spec.json 或 run_state.json 需要被读取时

### FileSurfer 路径规则（必须严格遵守）
- FileSurfer 的 base_path 已经是当前 run_dir，所有路径必须相对于 run_dir
- 查看输入文件使用：`inputs/attachment1.docx`、`inputs/problem.pdf`
- 查看产物使用：`artifacts/result.json`
- 查看配置使用：`task_spec.json`
- **绝对禁止**使用 `outputs/runs/<run_id>/inputs/...` 或 `run_dir/inputs/...` 这类绝对路径
- 如果不确定，先用 `ls inputs/` 查看有哪些文件

### FileSurfer 调用限制
- **禁止重复读取**：同一个文件最多通过 FileSurfer 查看 1 次，不要反复读取
- **禁止全量浏览**：不要用 FileSurfer 逐一重新读取 file_previews 中已成功解析的文件
- **每次调用应有明确目标**：说明"我要从 X 文件中确认 Y 信息"，而不是"让我看看 X 文件"
- **总计调用不超过 3 次**：FileSurfer 是补充手段，不应消耗过多轮次
- 如果 file_previews 已覆盖所有文件且摘要充分，TaskPlanningAgent 可以直接基于 file_previews 完成任务拆解，
  不需要在计划阶段调用 FileSurfer 重新验证每个文件
"""


def _build_scheduling_guidance(task_category: str, task_spec: dict) -> str:
    """根据任务类别生成调度边界提示。"""

    code_policy = task_spec.get("code_policy", {})
    allows_complex = code_policy.get("allows_complex", False)
    allows_lightweight = code_policy.get("allows_lightweight", False)
    max_lines = code_policy.get("max_lines", 0)
    max_retries = code_policy.get("max_retries", 0)
    has_code = allows_complex or allows_lightweight

    file_previews = task_spec.get("file_previews", [])
    has_failed = any(p.get("status") != "success" for p in file_previews)

    # 根据 code_policy 生成代码约束说明
    if allows_complex:
        code_guidance = f"允许复杂代码，每步≤{max_lines}行，最多{max_retries}轮重试"
    elif allows_lightweight:
        code_guidance = f"仅允许轻量工具脚本（JSON写入、字段校验、金额汇总），单次≤{max_lines}行，失败后不重试"
    else:
        code_guidance = "本任务不需要代码执行"

    guidance = f"""
## 任务类别感知的调度边界

以下是根据 TaskSpec 和 file_previews 推断的任务特征和调度建议。
这些是**边界提示**而非固定流程，Ochestrator 仍然根据实际情况动态选择 Agent。

**代码策略**：{code_guidance}
**FileSurfer 使用请参考上方的 FileSurfer 使用策略，此处不再重复。**
"""

    if task_category == "document_analysis":
        guidance += f"""
**任务类别**：文件理解与结构化摘要

**核心目标**：基于 file_previews 理解文档 → 提取关键信息 → 结构化整理 → 发现明显问题 → 生成报告

**Agent 能力边界**：
- ResearchAgent：本任务不涉及外部调研或学术依据，通常不需要调用
- ReasoningAgent：本任务不涉及复杂推理或模型选型，通常不需要调用
- CodeModelingAgent：{"不调用" if not has_code else ("仅用于轻量数据脚本（JSON写入、字段校验、金额汇总），禁止复杂算法" if not allows_complex else "按需调用")}
- CodeExecutorAgent：{"不调用" if not has_code else "仅执行轻量脚本"}
- ErrorAttributionAgent：{"不涉及" if not has_code else "仅在轻量脚本失败时调用，最多1轮，不进入多轮重写"}
- ReviewAgent：检查产物是否完整、报告内容是否可追溯到真实文件
- ReportAgent：基于 file_previews、上游输出和 artifacts 目录下的真实产物生成报告

**执行路径参考**（不固定）：
TaskPlanningAgent → {"FileSurfer（仅查看 file_previews 解析失败的文件）→ " if has_failed else ""}{"CodeModelingAgent → CodeExecutorAgent → " if has_code else ""}ReviewAgent → ReportAgent
"""
    elif task_category == "math_modeling":
        guidance += f"""
**任务类别**：数学建模与求解

**核心目标**：理解赛题 → 建立模型 → 编写求解代码 → 验证结果 → 生成报告

**Agent 能力边界**：
- TaskPlanningAgent：先完整理解赛题再拆解，不要跳过约束条件。优先基于 file_previews 理解赛题
- ResearchAgent：如需查阅公式、定理或标准方法，可调用
- ReasoningAgent：模型选择、假设合理性判断时可调用
- CodeModelingAgent：**分步生成代码**，第一步先建最小可运行基线（读数据+打印摘要），第二步再加模型逻辑
- CodeExecutorAgent：每步执行后检查 stdout/stderr
- ErrorAttributionAgent：失败时调用，最多 2 轮修复
- ReviewAgent：检查数值结果是否合理、代码是否可复现
- ReportAgent：基于真实执行结果和数值产物生成报告

**代码生成边界**：
- 第一版代码只做数据加载和基本统计，确保环境能跑通
- 第二版加核心模型或算法
- 第三版加优化、可视化
- 每版代码不超过 200 行
- 3 轮后仍未完全成功 → 降级为阶段性报告，记录当前结果和未完成部分
"""
    elif task_category == "data_modeling":
        guidance += f"""
**任务类别**：数据建模与分析

**核心目标**：理解数据 → 特征工程 → 模型训练 → 评估 → 生成报告

**Agent 能力边界**：
- TaskPlanningAgent：先分析数据结构再规划建模路径
- ResearchAgent：如需方法依据或模型选型参考，可调用
- ReasoningAgent：特征重要性、模型可解释性判断时可调用
- CodeModelingAgent：**分步生成代码**，先生成数据探索脚本，再生成建模脚本
- CodeExecutorAgent：每步执行
- ErrorAttributionAgent：失败时调用，最多 2 轮修复
- ReviewAgent：检查模型指标是否在合理范围，数据集划分是否正确
- ReportAgent：基于真实指标和产物生成报告

**代码生成边界**：
- 第一版只做数据探索和可视化
- 第二版做特征工程和基线模型
- 第三版做模型优化
- 3 轮后仍未成功 → 降级为阶段性报告
"""
    else:
        guidance += """
**任务类别**：通用复杂任务

所有 Agent 均可根据实际需要动态调用。调度策略由 Orchestrator 根据 TaskSpec 和运行时上下文决定。
"""

    # 通用代码生成边界（所有任务类型）—— 由 code_policy 动态确定
    if allows_complex:
        guidance += f"""
## 代码生成通用边界

当前 code_policy 允许复杂代码，约束如下：
1. **小步生成**：每次只生成一个可独立运行的功能模块，不超过 {max_lines} 行
2. **小步验证**：每次生成后立即执行验证，不要累积多个模块再一次执行
3. **降级优先**：代码重试 {max_retries} 轮仍未成功，立即触发降级，不要尝试第 {max_retries + 1} 轮
4. **降级方案**：降级后，基于 file_previews 和已有产物生成阶段性报告，明确标注"代码未完成"
5. **不写绝对路径**：所有路径相对于当前工作目录
"""
    elif allows_lightweight:
        guidance += f"""
## 代码生成边界（轻量工具模式）

当前 code_policy 仅允许轻量工具脚本：
1. 允许：JSON 写入、字段校验、金额汇总、文件存在性检查、简单数据转换
2. 禁止：复杂算法、大型 pipeline、模型训练、优化求解、多文件工程
3. 单次脚本 ≤ {max_lines} 行
4. 失败后只重试 {max_retries} 轮，不进入多轮代码重写
5. 失败后的兜底方案：由框架层 artifact_writer 用 CSV/手动方式完成数据落盘，
   或由 ReportAgent 在报告中声明"轻量脚本未能完成，数据整理转为人工方式"
6. Orchestrator 可根据实际情况决定是否调用 CodeModelingAgent，
   如果 file_previews 中的数据已足够支撑报告，可以不调用
"""
    else:
        guidance += """
## 代码生成边界（no-code 模式）

当前 code_policy 不要求代码执行。
如果 Orchestrator 判断不需要代码，TaskPlanningAgent 可以直接规划分析路径，
跳过 CodeModelingAgent 和 CodeExecutorAgent。
注意：这不是"禁止代码"，而是"不要求代码"。
如果运行过程中发现确实需要脚本辅助，Orchestrator 可以灵活决定。
"""

    return guidance


def _build_report_agent_constraint() -> str:
    """生成 ReportAgent 的硬性约束，嵌入到主 prompt 中。"""
    return """
## ReportAgent 硬性约束（由系统框架强制执行）

ReportAgent 是最后一个被调用的 Agent，它的唯一职责是撰写 final_report.md 的业务内容。
以下规则对 ReportAgent 是**硬性**的：

**输入来源（只能引用这些）**：
1. 你在本次对话中收到的上游 Agent 的真实输出消息
2. TaskSpec 中的 file_previews
3. artifacts 目录下真实存在的文件
4. run_state.json 中的 outcome、fallback_triggered、review_status

**绝对禁止生成的内容**：
- 任何形式的 "Agent Execution Trace""agent_trace.md""执行追踪日志"
- 任何 Mermaid 图（sequenceDiagram、gantt、flowchart）
- 任何表格列出 Agent 的"调用时间""调用次数""执行耗时""状态""exit_code"
- 任何 "XXXAgent 已完成""XXXAgent 未触发""XXXAgent 已调用""XXXAgent（后续迭代）" 等系统内部状态
- 任何 Agent 名字（TaskPlanningAgent、CodeModelingAgent、FileSurfer 等均不得出现在业务报告中）
  如需说明没有代码，只写"本轮任务配置为 no-code 模式，因此未生成可执行代码"
- 任何你未亲眼看到的上游 Agent 输出摘要
- 任何关于"系统如何工作"的元描述

**报告结构要求**：
- 如果 run_state.review_status 是 "partial"，在报告开头加 "⚠️ 本报告为非正式版本"
- 如果 run_state.fallback_triggered 是 true，在报告开头加降级说明
- 如果本任务不需要代码，报告中去掉"代码实现"章节，换成"分析过程"
- 报告末尾的"交付物"章节只能列出 artifacts 目录下真实存在的文件
"""


def build_task_prompt(task_spec: dict):
    task_category = _categorize_task(task_spec)
    filesurfer_policy = _build_filesurfer_policy(task_spec)
    scheduling = _build_scheduling_guidance(task_category, task_spec)
    report_constraint = _build_report_agent_constraint()

    return f"""
你是一个通用复杂任务执行系统，基于 AutoGen + MagenticOneGroupChat 运行。

请根据下面的 TaskSpec 完成任务：

```json
{json.dumps(task_spec, ensure_ascii=False, indent=2)}
```

## 基础执行要求

1. TaskPlanningAgent 首先理解输入文件和任务目标，识别任务类型、子问题、约束条件和交付物；
2. 所有代码和产物必须使用相对路径。当前工作目录是 run_dir，代码中写 artifacts/code_pipeline.py 即可，不要写成 outputs/runs/<run_id>/artifacts/... 这样的绝对路径；
3. 如果需要 CodeExecutorAgent 执行命令，必须使用 markdown 代码块，例如：

```bash
python artifacts/code_pipeline.py
```

4. 不得声称生成不存在的文件；
5. 不得把某个示例任务写死为系统能力。

{filesurfer_policy}

{scheduling}

{report_constraint}

## 共享运行状态（RunState）

系统维护了一个 run_dir/run_state.json，所有 Agent 可以通过 FileSurfer 读取。
关键字段：code_retry_count、fallback_triggered、review_status、can_generate_final_report、outcome。

**ErrorAttributionAgent**：每轮分析前读取 run_state.json。code_retry_count >= 2 时必须建议降级。
**ReviewAgent**：审查前读取 run_state.json。产物为空 → failed；部分缺失 → partial；全部满足 → passed。
**ReportAgent**：生成报告前读取 run_state.json。can_generate_final_report 为 false → 拒绝生成。

注意：

* 当前输入可能是数学建模题、数据建模题、企业需求文档、城市多模态数据任务或其他复杂任务；
* 具体任务只是一次运行实例，不是系统固定任务；
* 不要把 MathorCup D 题、电信客户流失预测或任何 demo 写死到框架中。
"""
