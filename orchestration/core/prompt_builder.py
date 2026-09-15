"""Stage-specific prompts for the explicit workflow.

Prompts describe a single decision. They never grant an agent authority to move
the workflow, write state, or claim that a file has been persisted.
"""

from __future__ import annotations

import json
from copy import deepcopy
from collections import Counter
from pathlib import Path

from orchestration.planning.requirement_compiler import (
    extract_requirement_source_candidates,
)


def _input_files_view(files: list[str]) -> list[str | dict]:
    names = [str(path).replace("\\", "/").rsplit("/", 1)[-1] for path in files]
    if len(names) <= 50:
        return names
    suffix_counts = Counter(Path(name).suffix.lower() or "<none>" for name in names)
    representative = []
    seen_suffixes: set[str] = set()
    for name in names:
        suffix = Path(name).suffix.lower() or "<none>"
        if suffix_counts[suffix] <= 10 or suffix not in seen_suffixes:
            representative.append(name)
            seen_suffixes.add(suffix)
    return [{
        "manifest": "large_input_collection",
        "total_file_count": len(names),
        "counts_by_suffix": dict(sorted(suffix_counts.items())),
        "representative_files": representative[:20],
        "note": "完整文件清单保留在 TaskSpec；Prompt 仅展示集合摘要",
    }]


def _spec(task_spec: dict) -> str:
    view = deepcopy(task_spec)
    view.get("input", {})["files"] = _input_files_view(
        task_spec.get("input", {}).get("files", [])
    )
    return json.dumps(view, ensure_ascii=False, indent=2)


def _execution_spec(task_spec: dict) -> dict:
    """Exclude previews, absolute run paths and policy duplicates from node prompts."""
    return {
        "task_id": task_spec.get("task_id"),
        "task_name": task_spec.get("task_name"),
        "task_type": task_spec.get("task_type"),
        "input": {
            "type": task_spec.get("input", {}).get("type"),
            "files": _input_files_view(
                task_spec.get("input", {}).get("files", [])
            ),
            "text": task_spec.get("input", {}).get("text", ""),
        },
        "code_policy": task_spec.get("code_policy", {}),
        "artifact_contract": task_spec.get("artifact_contract", {}),
        "success_criteria": task_spec.get("success_criteria", {}),
        "artifacts": task_spec.get("artifacts", {}),
        "domain_result_contract": task_spec.get("domain_result_contract", {}),
    }


def _code_acceptance_view(task_spec: dict) -> list[dict]:
    """Expose only mandatory machine checks relevant to code-owned artifacts."""

    intermediate = set(
        task_spec.get("artifact_contract", {}).get(
            "intermediate_artifacts", []
        ) or []
    )
    obligations: list[dict] = []
    contract = task_spec.get("requirement_contract") or {}
    for requirement in contract.get("requirements", []) or []:
        if not requirement.get("mandatory", True):
            continue
        for criterion in requirement.get("acceptance_criteria", []) or []:
            target = str(criterion.get("target") or "")
            artifact = target.split("#", 1)[0].split("::", 1)[0]
            if artifact not in intermediate:
                continue
            obligations.append({
                "criterion_id": criterion.get("criterion_id"),
                "requirement_id": requirement.get("requirement_id"),
                "requirement": requirement.get("statement"),
                "method": criterion.get("method"),
                "target": target,
                "params": criterion.get("params") or {},
                "condition": criterion.get("condition") or "",
                "severity": criterion.get("severity", "blocking"),
            })
    return obligations


def build_task_understanding_prompt(
    task_spec: dict,
    available_capabilities: list[str],
) -> str:
    """Build a bounded semantic-briefing request before graph planning."""
    policy = task_spec.get("task_understanding_policy", {})
    limit = int(policy.get("max_preview_chars_per_file", 1_500))
    previews = []
    for preview in task_spec.get("file_previews", []):
        previews.append({
            "file": str(preview.get("path") or "").replace("\\", "/").rsplit("/", 1)[-1],
            "type": preview.get("type"),
            "status": preview.get("status"),
            "text_preview": str(preview.get("text_preview") or "")[:limit],
            "sheets": preview.get("sheets", []),
            "sheet_summaries": preview.get("sheet_summaries", {}),
            "error": preview.get("error"),
        })
    source_candidates = [
        candidate.model_dump(mode="json")
        for candidate in extract_requirement_source_candidates(task_spec)
    ]
    domain_contract = task_spec.get("domain_result_contract") or {}
    domain_instruction = ""
    if domain_contract:
        domain_instruction = f"""
本任务存在冻结的领域结果合同：
{json.dumps(domain_contract, ensure_ascii=False, indent=2)}
所有 required_fields/numeric_compare/record_count_compare 的 JSON Pointer 必须从
allowed_acceptance_targets 对应文件的列表中选择，禁止发明别名字段，禁止要求车辆数或成本等
正数指标等于 0。无法由这些固定字段确定性表达的约束必须使用 evidence_supported。
"""
    return f"""
请在任务图规划前生成一份结构化任务理解简报和原子需求清单。只提炼输入中明确出现的
信息，不能修改 TaskSpec，也不能生成任务步骤或 TaskGraph。每条 requirement 必须绑定
source_refs；不要把多个可以分别失败、分别验收的要求合并成一句。requirements 是需求合同，
不是执行计划。业务分析/建模/代码产物使用 owner="planner"；最终报告、RunState 和框架治理
产物使用 owner="outer_workflow"，不要为它们要求 PlanningAgent 生成业务节点。
depends_on 只表示原子需求之间真实存在的先后依赖；不要把 parent_id 当作执行依赖，
也不要为了形成长链而虚构依赖。算法实现可依赖算法规格，结果分析可依赖计算结果。
必须逐项检查原文中的编号问题、场景、优化目标、强制输出字段、比较分析、指标、约束和验证
要求；可以分别失败或分别验收的内容必须拆成不同 requirement。完成提取前反向检查每个编号
问题是否至少有一条 mandatory requirement，不能只保留总体目标。
Python 已定位下列“可能包含义务的来源片段”。每个片段都必须被至少一条 requirement 的
source_refs.locator 以 candidate_id 精确引用。若片段只是背景而非真实要求，仍创建
mandatory=false、status=derived 的追踪项并说明排除理由。candidate 只用于防漏，不代表
Python 已经把它判定为需求。

acceptance_criteria 只能使用以下有限方法，禁止自造 method：
- artifact_exists：target 为运行目录相对文件路径；
- json_parseable：target 为 JSON 文件路径；
- required_fields：target 为 JSON 文件路径，params.fields 为 JSON Pointer 数组；
- record_count_compare：params.left/right 为“文件路径#/JSON/Pointer”，operator 为 eq/ne/lt/le/gt/ge；
- numeric_compare：同上，或使用 params.value 提供常量；
- evidence_supported：用于无法确定性计算的语义事实，必须由独立证据核验支持，
  params.keywords 必须给出 1 至 3 个能唯一识别该事实的关键词。
不得把 result.json 的 status=success、Agent 自报完成或代码退出码为零当成业务验收条件。
所有 target 和 expected_outputs 必须来自 TaskSpec.artifact_contract；不得自行创造
validation_report.md、额外 CSV 或其他未声明业务产物。需要机器复核的业务字段统一写入
TaskSpec 已声明的 JSON 中间产物，并用 JSON Pointer 定位。

{domain_instruction}

TaskSpec 摘要：
{json.dumps(_execution_spec(task_spec), ensure_ascii=False, indent=2)}

受预算限制的文件预览：
{json.dumps(previews, ensure_ascii=False, indent=2)}

来源覆盖候选：
{json.dumps(source_candidates, ensure_ascii=False, indent=2)}

可建议的 capability（recommended_capabilities 只能从中选择）：
{json.dumps(available_capabilities, ensure_ascii=False)}

严格返回 JSON，不使用 Markdown：
{{
  "summary": "任务的一句话准确概括",
  "goals": ["明确交付目标"],
  "constraints": ["用户或 TaskSpec 明确约束"],
  "ambiguities": ["缺失、冲突或无法确认的信息"],
  "risk_flags": ["可能影响完成质量的风险"],
  "recommended_capabilities": ["从可用列表选择的必要能力"],
  "requirements": [
    {{
      "requirement_id": "REQ-001",
      "parent_id": null,
      "depends_on": [],
      "relation": "and",
      "requirement_type": "goal",
      "statement": "一条原子需求",
      "mandatory": true,
      "owner": "planner",
      "source_refs": [{{"artifact": "输入文件名", "locator": "SRC-01-001"}}],
      "expected_outputs": ["可验证输出标识"],
      "acceptance_criteria": [{{
        "criterion_id": "AC-001",
        "method": "required_fields",
        "target": "artifacts/result.json",
        "condition": "明确可检查条件",
        "params": {{"fields": ["/必需字段"]}},
        "severity": "blocking"
      }}],
      "status": "explicit"
    }}
  ],
  "confidence": 0.0
}}
""".strip()


def build_requirement_refinement_prompt(
    requirement_contract: dict,
    validation: dict,
) -> str:
    """Request one local semantic refinement of rejected requirements."""
    return f"""
请只细化下面 RequirementSet 中被验证器拒绝的 requirement，或补齐验证器指出的未覆盖
source candidate，不要生成执行步骤、PlanIR 或 TaskGraph。未被拒绝的 requirement 必须
原样保留。过粗 requirement 应保留原 ID 作为父级
and requirement，并增加多个子 requirement；每个子需求必须只有一个可独立失败、可独立验收
的职责，拥有自己的 expected_outputs、acceptance_criteria 和 source_refs。不要按固定数量
拆分，也不要创建同义子需求。每个 uncovered_source_candidate_id 必须被新增或已有需求的
source_refs.locator 精确引用；若不是业务要求，创建 mandatory=false、status=derived 的追踪
项。最终报告和框架状态需求 owner=outer_workflow，其余业务需求 owner=planner。
acceptance_criteria 只能使用 artifact_exists、json_parseable、required_fields、
record_count_compare、numeric_compare、evidence_supported，并按初始任务理解 Prompt 的
格式提供 target 和 params。禁止用自造方法或仅凭 status=success 验收。

当前 RequirementSet：
{json.dumps(requirement_contract, ensure_ascii=False, indent=2)}

确定性验证结果：
{json.dumps(validation, ensure_ascii=False, indent=2)}

严格返回完整 RequirementSet JSON：
{{
  "contract_id": "{requirement_contract.get('contract_id', 'requirements')}",
  "version": {int(requirement_contract.get('version', 1)) + 1},
  "global_goal": {json.dumps(requirement_contract.get('global_goal', ''), ensure_ascii=False)},
  "source_candidates": {json.dumps(requirement_contract.get('source_candidates', []), ensure_ascii=False)},
  "requirements": []
}}
""".strip()


def build_planning_prompt(
    task_spec: dict,
    available_capabilities: list[str] | None = None,
    validation_errors: list[str] | None = None,
    workflow_memory: dict | None = None,
    task_understanding: dict | None = None,
) -> str:
    available_capabilities = available_capabilities or [
        "analysis", "research", "reasoning", "code", "artifact_validation"
    ]
    contract = task_spec.get("artifact_contract", {})
    allowed_outputs = contract.get("intermediate_artifacts", [])
    capability_contract = task_spec.get("capability_contract", {})
    memory_view = workflow_memory or {
        "selected_skill_ids": [],
        "planning_guidance": [],
    }
    understanding_view = task_understanding or {
        "status": "not_available",
        "summary": "",
        "goals": [],
        "constraints": [],
        "ambiguities": [],
        "risk_flags": [],
        "recommended_capabilities": [],
    }
    return f"""
请把下面的 TaskSpec 规划为业务语义有向无环图（SemanticGraph），并返回严格 JSON。
你只负责业务步骤、能力选择和真实语义依赖。Python 框架会自动补充代码产物、退出码、
终端复核、证据核验和产物校验节点。不得指定具体 Agent、调用其他 Agent 或填写运行时字段。

TaskSpec:
{_spec(task_spec)}

本次可使用的 capability（只能从中选择）：
{json.dumps(available_capabilities, ensure_ascii=False)}

任务级能力契约（required 必须各有至少一个节点；preferred 仅在有信息增益时使用）：
{json.dumps(capability_contract, ensure_ascii=False, indent=2)}

框架将自动绑定以下中间产物，规划节点不要填写 output_artifacts：
{json.dumps(allowed_outputs, ensure_ascii=False)}

上一版图的校验错误（若非空，必须逐项修复）：
{json.dumps(validation_errors or [], ensure_ascii=False, indent=2)}

框架筛选后的历史工作流记忆（仅为建议，不能覆盖 TaskSpec、能力契约或产物契约）：
{json.dumps(memory_view, ensure_ascii=False, indent=2)}

TaskUnderstandingAgent 的结构化任务简报（仅用于减少重复理解，不能覆盖 TaskSpec）：
{json.dumps(understanding_view, ensure_ascii=False, indent=2)}

严格返回以下 JSON，不使用 Markdown 代码块：
{{
  "graph_id": "{task_spec.get('task_id', 'task')}_graph",
  "version": 1,
  "goal": "任务总目标",
  "nodes": [
    {{
      "node_id": "以字母开头的唯一ID",
      "objective": "单一、可执行的业务目标",
      "capability": "从可用 capability 中选择",
      "dependencies": [],
      "required_inputs": {{"直接依赖节点ID": ["真正需要的结构化字段"]}},
      "expected_output": "该节点产生的结构化业务结论"
    }}
  ]
}}

规则：
1. dependencies 必须表达真实数据依赖，不能有环、自依赖或不存在的节点。
2. 不要填写 output_artifacts、success_criteria、max_retries 或任何运行时字段。
3. TaskSpec 要求代码时，必须且只能包含一个 capability=code 的业务节点；禁止时不得包含 code。
4. 不要生成 terminal_execution、evidence_verification、artifact_validation、review、report、
   final_validate、finish；这些治理节点由 Python 框架自动插入。
5. 不要为了显得复杂而拆分无信息增益的节点；只建立必要的稀疏业务依赖边。
6. data_analysis/calculation/reasoning 是只返回结构化结论的语义能力，不代表代码执行，
   不能负责文件产物。不要把同一完整代码流水线拆成多个代码节点。
7. required_inputs 只声明下游真正需要的上游字段，不要复制完整上下文。
8. capability_contract.required 中的业务能力必须有真实节点；terminal_execution 和
   evidence_verification 由框架负责，不要在语义图中生成。
9. capability_contract.preferred 不是强制步骤；只有任务确有外部信息需求时才加入，避免
    为增加 Agent 数量制造无信息增益节点。
10. normalized_input.json、file_previews.json 和 TaskSpec 已由 prepare 阶段生成；不要规划
    复制文件、生成预览或生成 TaskSpec。仍可规划必要的业务字段提取、单位统一和数据清洗。
11. 历史工作流记忆只可帮助选择稀疏拓扑；与当前 TaskSpec 冲突时必须忽略，不能复制其中
    的具体业务事实、路径或运行结果。
""".strip()


def build_replanning_prompt(
    task_spec: dict,
    current_graph: dict,
    failure: dict,
    available_capabilities: list[str],
    task_understanding: dict | None = None,
) -> str:
    """Request a full replacement SemanticGraph while protecting verified work."""
    completed = [
        {
            key: node.get(key)
            for key in (
                "node_id", "description", "capability", "dependencies",
                "dependency_fields", "input_artifacts", "output_artifacts",
                "success_criteria", "max_retries",
            )
        }
        for node in current_graph.get("nodes", [])
        if node.get("status") == "completed"
    ]
    graph_view = {
        "graph_id": current_graph.get("graph_id"),
        "version": current_graph.get("version", 1),
        "goal": current_graph.get("goal", ""),
        "nodes": [
            {
                **{
                    key: node.get(key)
                    for key in (
                        "node_id", "description", "capability", "dependencies",
                        "dependency_fields", "input_artifacts", "output_artifacts", "success_criteria",
                        "max_retries", "status",
                    )
                },
                "error_summary": (
                    (node.get("error") or {}).get("error_message")
                    if isinstance(node.get("error"), dict)
                    else None
                ),
            }
            for node in current_graph.get("nodes", [])
        ],
    }
    base = build_planning_prompt(
        task_spec,
        available_capabilities,
        [
            "这是执行期重规划；必须返回完整替代图，而不是补丁。",
            "已完成节点定义不可修改或删除，框架会校验并保留其结果。",
            "只替换失败节点及受其影响的未完成子图。",
        ],
        task_understanding=task_understanding,
    )
    return f"""
{base}

当前任务图（运行时结果只用于理解故障，禁止伪造完成状态）：
{json.dumps(graph_view, ensure_ascii=False, indent=2)}

本次触发重规划的真实失败：
{json.dumps(failure, ensure_ascii=False, indent=2)}

必须原样保留的已验证节点定义：
{json.dumps(completed, ensure_ascii=False, indent=2)}

返回完整 SemanticGraph JSON，不要返回 TaskGraph、GraphPatch 或运行时状态。
只返回业务节点；不要返回 terminal_execution、evidence_verification 或
artifact_validation，GraphCompiler 会重新插入这些治理节点。已完成业务节点必须保留
相同 node_id、objective/description、capability 和业务依赖，不能重新执行或改写。
""".strip()


def build_code_prompt(
    task_spec: dict,
    plan: dict,
    repair_instruction: str = "",
    analysis_context: dict | None = None,
    normalized_inputs: dict | None = None,
) -> str:
    code_path = task_spec.get("artifacts", {}).get("code") or "artifacts/code_pipeline.py"
    outputs = [p for p in task_spec.get("required_artifacts", []) if p.startswith("artifacts/") and p != code_path]
    is_mathorcup = any(
        str(item).replace("\\", "/").endswith("attachment1.docx")
        for item in task_spec.get("input", {}).get("files", [])
    ) and task_spec.get("task_type") == "math_modeling"
    domain_contract = ""
    if is_mathorcup:
        domain_contract = """
这是 MathorCup 装箱任务，必须严格输出固定结果契约，禁止只输出 solution/items 或自定义字段：
1. artifacts/result.json 必须是对象，至少包含 status、vehicle_scenarios、
   multi_vehicle_scenarios、placements、vehicle_count、total_cost、
   space_utilization_rate、load_utilization_rate、validation。
2. placements 必须是平面数组；每项包含 cargo_id（G1-G5）、quantity 或 count、
   vehicle_id、scenario、x/y/z、length/width/height、orientation。数量必须覆盖输入表中
   G1=80、G2=100、G3=30、G4=40、G5=50（运行时以 normalized_input.json 为准）。
3. 所有坐标使用 cm，车辆边界按 V1 420x210x220、V2 680x245x250，顶部留 3cm；利用率是
   0 到 1 的小数，不得百分数。必须做三维 AABB 去重、重量和易碎件单层校验。
4. 同时写入 artifacts/result_complete.json（完整结果副本或逐项摘要）、
   artifacts/constraint_validation_log.json（每项约束及 pass/fail/reason）、
   artifacts/cost_comparison.json（各方案 vehicle_count、total_cost、space/load 利用率）。
   三个审计文件必须是标准 JSON，且不能用占位空对象。
"""
    return f"""
为下面的 TaskSpec 生成一个可直接执行的 Python 脚本。框架会负责保存和执行，
你不得声称已经保存或执行。所有路径相对于当前 run_dir，不得 cd，不得写 outputs/runs/...。

TaskSpec:
{json.dumps(_execution_spec(task_spec), ensure_ascii=False, indent=2)}

执行计划:
{json.dumps(plan, ensure_ascii=False, indent=2)}

已完成的分析上下文（必须消费，不能重新假设）：
{json.dumps(analysis_context or {}, ensure_ascii=False, indent=2)}

业务产物的机器验收合同（这些条件来自冻结 RequirementSet，不能弱化、删除或只用
status=success 代替；代码必须生成能够逐条独立复核的数据）：
{json.dumps(_code_acceptance_view(task_spec), ensure_ascii=False, indent=2)}

框架已将异构输入确定性标准化并写入 normalized_input.json。下面只提供结构和代表性
样本以控制 Prompt 预算；代码运行时必须读取完整文件，不要把预览误当作完整数据，
也不要自行重新解释原始文件布局：
{json.dumps(normalized_inputs or {}, ensure_ascii=False, indent=2)}

产物要求只能来自 TaskSpec.artifact_contract；业务 JSON 必须是标准 JSON，禁止
NaN、Infinity 和空键。只生成契约声明的 intermediate_artifacts。

{domain_contract}

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
通用输入边界位于 normalized_input.json；不得绕过任务图重新猜测原始文件布局。
对于 Excel、CSV、JSON，完整的无业务语义原始结构位于
normalized_input.json#/structured_sources。脚本必须优先消费这里的 sheets/rows/content，
不得依赖预览样本，不得在数据存在时返回 `No data`、占位结果或伪造默认样本。
必须遵守 normalized_input.json#/format_contract：workbook 的每个 sheet 以及通用 table/rows
均为保留原始单元格顺序的二维数组，不是按列名映射的字典；代码应从真实表头行识别列位置。
DOCX 的完整正文和表格位于 content/tables，PDF 内容位于 pages，不能因某一种来源没有
目标记录就忽略其他已加载来源。
如果必需输入缺失、结构无法解析或真实记录数无法确定，脚本必须写出
`status="failed"` 的诊断结果并以非零退出码结束；不得用固定数量、默认值、
warning/fallback 结果配合退出码 0 伪装业务成功。
只能创建 expected_outputs 中列出的业务产物，不能写入
agent_trace.md、final_report.md、task_spec.json、run_state.json 或 validation_report.md。
PDF 请使用项目已安装的 pdfplumber；Pandas 必须写为 `import pandas as pd`，不要导入 `pd` 或 `pypdf`。
可直接使用的科学计算依赖为 numpy、scipy、pandas、openpyxl、python-docx(docx) 和
pdfplumber；除此之外优先使用 Python 标准库。不得依赖运行时 pip install，不得导入
未安装模块。修复轮次中如果分析上下文包含 `__previous_code_candidate__`，必须基于该
真实失败源码做局部修复，而不是忽略它后重新随机生成。
脚本成功时必须以退出码 0 结束。
""".strip()


def build_error_prompt(task_spec: dict, execution: dict, retries_remaining: int) -> str:
    return f"""
根据真实代码执行结果给出一次结构化错误归因。你只提供决策建议，不能直接重试、
写状态或修改文件。剩余重试次数为 {retries_remaining}。

TaskSpec:
{json.dumps(_execution_spec(task_spec), ensure_ascii=False, indent=2)}

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
{json.dumps(_execution_spec(task_spec), ensure_ascii=False, indent=2)}

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
        "user_request": task_spec.get("input", {}).get("text", ""),
        "report_policy": task_spec.get("report_policy", {}),
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
必须服从 user_request 中“不要、不需要、不得、禁止”等排除性约束；事实整理不能改写成
证据未支持的最终业务裁决。无法从证据确定的字段必须明确写“缺失/待确认”。
当 report_policy.decision_scope=descriptive_only 时，后续建议只能限于 report_policy 中列出的
补充证据、核实事实和人工复核，不得自行增加处置结论或决策性建议。
""".strip()
