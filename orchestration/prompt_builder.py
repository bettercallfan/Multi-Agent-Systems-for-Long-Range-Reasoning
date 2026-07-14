"""Stage-specific prompts for the explicit workflow.

Prompts describe a single decision. They never grant an agent authority to move
the workflow, write state, or claim that a file has been persisted.
"""

from __future__ import annotations

import json


def _spec(task_spec: dict) -> str:
    return json.dumps(task_spec, ensure_ascii=False, indent=2)


def build_planning_prompt(
    task_spec: dict,
    available_capabilities: list[str] | None = None,
    validation_errors: list[str] | None = None,
) -> str:
    available_capabilities = available_capabilities or [
        "analysis", "research", "reasoning", "code", "artifact_validation"
    ]
    contract = task_spec.get("artifact_contract", {})
    allowed_outputs = contract.get("intermediate_artifacts", [])
    return f"""
请把下面的 TaskSpec 规划为一个真正可执行的有向无环任务图（DAG），并返回严格 JSON。
你只描述节点需要的 capability，不得指定具体 Agent，不得调用其他 Agent，不得声称节点
已经运行或完成，也不得填写 assigned_executor、attempts、result、error 等运行时字段。

TaskSpec:
{_spec(task_spec)}

本次可使用的 capability（只能从中选择）：
{json.dumps(available_capabilities, ensure_ascii=False)}

允许节点写入的中间产物（必须使用精确路径）：
{json.dumps(allowed_outputs, ensure_ascii=False)}

上一版图的校验错误（若非空，必须逐项修复）：
{json.dumps(validation_errors or [], ensure_ascii=False, indent=2)}

严格返回以下 JSON，不使用 Markdown 代码块：
{{
  "graph_id": "{task_spec.get('task_id', 'task')}_graph",
  "version": 1,
  "goal": "任务总目标",
  "nodes": [
    {{
      "node_id": "以字母开头的唯一ID",
      "description": "单一、可执行的节点目标",
      "capability": "从可用 capability 中选择",
      "dependencies": [],
      "input_artifacts": [],
      "output_artifacts": [],
      "success_criteria": [{{"type": "node_result"}}],
      "max_retries": 1
    }}
  ]
}}

规则：
1. dependencies 必须表达真实数据依赖，不能有环、自依赖或不存在的节点。
2. 节点没有 output_artifacts 时，必须有可机器检查的 success_criteria。
3. TaskSpec 要求代码时，必须且只能包含一个 capability=code 的节点；禁止时不得包含 code。
4. 必须且只能包含一个 artifact_validation 节点，且所有其他节点最终都汇入它。
5. review、report、final_validate、finish 属于框架外层，绝不能出现在 TaskGraph。
6. 不要为了显得复杂而拆分无信息增益的节点；只建立必要的稀疏依赖边。
7. 代码任务中，唯一 code 节点必须负责“允许节点写入的中间产物”的全部精确路径，
   并声明 {{"type":"execution_exit_code","equals":0}}；其他节点的 output_artifacts 必须为空。
8. data_analysis/calculation/reasoning 是只返回结构化结论的语义能力，不代表代码执行，
   不能负责文件产物。不要把同一完整代码流水线拆成多个代码节点。
""".strip()


def build_analysis_prompt(task_spec: dict, plan: dict, capability: str) -> str:
    return f"""
你处于显式工作流的 {capability} 阶段。只提供本阶段分析结论，不调度其他 Agent，
不写状态，不声称文件已经生成。

TaskSpec:
{_spec(task_spec)}

已验证计划:
{json.dumps(plan, ensure_ascii=False, indent=2)}
""".strip()


def build_code_prompt(
    task_spec: dict,
    plan: dict,
    repair_instruction: str = "",
    analysis_context: dict | None = None,
    plugin_instructions: str = "",
    normalized_inputs: dict | None = None,
) -> str:
    code_path = task_spec.get("artifacts", {}).get("code") or "artifacts/code_pipeline.py"
    outputs = [p for p in task_spec.get("required_artifacts", []) if p.startswith("artifacts/") and p != code_path]
    return f"""
为下面的 TaskSpec 生成一个可直接执行的 Python 脚本。框架会负责保存和执行，
你不得声称已经保存或执行。所有路径相对于当前 run_dir，不得 cd，不得写 outputs/runs/...。

TaskSpec:
{_spec(task_spec)}

执行计划:
{json.dumps(plan, ensure_ascii=False, indent=2)}

已完成的分析上下文（必须消费，不能重新假设）：
{json.dumps(analysis_context or {}, ensure_ascii=False, indent=2)}

框架已将异构输入确定性标准化并写入 normalized_input.json。下面是其完整内容；
代码应读取该文件，不要自行重新解释原始文件布局：
{json.dumps(normalized_inputs or {}, ensure_ascii=False, indent=2)}

任务插件的业务产物契约：
{plugin_instructions or "仅遵循通用 ArtifactContract"}

修复要求:
{repair_instruction or "无，这是第一次生成"}

严格返回一个 JSON 对象，不要使用 Markdown 代码块：
{{
  "language": "python",
  "code": "完整 Python 源码",
  "entrypoint": "{code_path}",
  "expected_outputs": {json.dumps(outputs, ensure_ascii=False)},
  "notes": "简短说明"
}}

脚本行数不得超过 {task_spec.get('code_policy', {}).get('max_lines', 0)} 行。保持正常、可读的 Python，
禁止为了压缩行数把多条语句塞进一行。
标准化输入位于 normalized_input.json；除非插件明确要求，否则不要重新读取 inputs/。
只能创建 expected_outputs 中列出的业务产物，不能写入
agent_trace.md、final_report.md、task_spec.json、run_state.json 或 validation_report.md。
PDF 请使用项目已安装的 pdfplumber；Pandas 必须写为 `import pandas as pd`，不要导入 `pd` 或 `pypdf`。
脚本成功时必须以退出码 0 结束。
""".strip()


def build_error_prompt(task_spec: dict, execution: dict, retries_remaining: int) -> str:
    return f"""
根据真实代码执行结果给出一次结构化错误归因。你只提供决策建议，不能直接重试、
写状态或修改文件。剩余重试次数为 {retries_remaining}。

TaskSpec:
{_spec(task_spec)}

ExecutionResult:
{json.dumps(execution, ensure_ascii=False, indent=2)}

严格返回 JSON，不使用 Markdown：
{{
  "error_type": "data|code|reasoning|planning|report|unknown",
  "retry_recommended": true,
  "retry_count_remaining": {retries_remaining},
  "fallback_recommended": false,
  "repair_instruction": "具体、局部、可执行的修复说明"
}}
当剩余重试次数为 0 时，retry_recommended 必须为 false。
""".strip()


def build_review_prompt(task_spec: dict, evidence: dict) -> str:
    return f"""
请对已经通过技术预审的中间业务产物进行语义质量审查。你只能返回建议，不能决定
工作流状态、不能写 run_state.json、不能生成报告、不能检查尚未生成的 final_report.md。

TaskSpec:
{_spec(task_spec)}

框架证据:
{json.dumps(evidence, ensure_ascii=False, indent=2)}

严格返回 JSON，不使用 Markdown：
{{
  "blocking_issues": ["有明确证据的业务问题"],
  "advisory_issues": ["非阻断建议"],
  "evidence_references": ["中间产物中的字段或路径"],
  "repair_recommended": false,
  "repair_target": "code|analysis|input|human|none"
}}
不要把 final_report.md、agent_trace.md 或其他框架文件是否存在作为语义审查问题。
""".strip()


def build_report_prompt(task_spec: dict, evidence: dict) -> str:
    task_view = {
        "task_name": task_spec.get("task_name"),
        "task_type": task_spec.get("task_type"),
        "plugin_id": task_spec.get("plugin_id", "generic"),
        "required_report_sections": task_spec.get("required_report_sections", []),
        "artifact_contract": task_spec.get("artifact_contract", {}),
    }
    return f"""
基于下面经过框架审查的真实证据，生成一次最终业务 Markdown 报告。
这是你在本轮唯一一次输出。只输出报告正文，不输出 JSON，不生成执行轨迹、TaskSpec、
状态文件或其他系统文件，不提及任何 Agent 名称。

报告必须包含这些章节：
{json.dumps(task_spec.get('required_report_sections', []), ensure_ascii=False)}

TaskSpec:
{_spec(task_view)}

已验证证据:
{json.dumps(evidence, ensure_ascii=False, indent=2)}

只能引用证据中真实存在的数据和文件。若 review_status=partial，必须在开头明确标注非正式版本。
""".strip()


def build_task_prompt(task_spec: dict) -> str:
    """Backward-compatible alias for callers that only need the plan prompt."""
    return build_planning_prompt(task_spec)
