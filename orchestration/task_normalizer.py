from datetime import datetime
from pathlib import Path
import re


# 暗示复杂代码需求的关键词
_COMPLEX_CODE_KW = [
    "建模", "优化", "求解", "算法", "训练", "预测",
    "仿真", "模拟", "回归", "聚类",
    "装箱", "调度", "路径规划",
    "模型训练", "模型评估", "分类模型", "分类器",
    "损失函数", "梯度下降", "神经网络",
]

# 暗示不需要代码的关键词（纯分析类）
_NON_CODE_KW = [
    "理解", "阅读", "总结", "摘要", "分析文档", "审查",
]

# 暗示可能需要轻量工具的关键词（数据处理类）
_LIGHT_CODE_HINT_KW = [
    "报销", "审批", "核对", "整理", "提取", "汇总", "校验",
    "写入", "导出", "生成.*表",
]

# 暗示需要轻量工具的关键词（JSON 输出、数据校验等）
_LIGHT_CODE_KW = [
    "输出.*json", "生成.*json", "保存.*json",
    "artifacts/", "输出结构化", "结构化的输出",
    "字段校验", "金额汇总", "格式校验",
]


def _detect_code_policy(files: list, raw_text: str) -> dict:
    """
    根据任务描述生成 code_policy。
    code_policy 是调度边界，不是流水线控制器。
    """

    text_lower = raw_text.lower()

    # --- 1. 显式声明优先 ---
    if "code_policy: none" in text_lower:
        return {
            "allows_complex": False,
            "allows_lightweight": False,
            "max_lines": 0,
            "max_retries": 0,
            "description": "任务显式声明不需要任何代码",
        }
    if "code_policy: lightweight" in text_lower:
        return {
            "allows_complex": False,
            "allows_lightweight": True,
            "max_lines": 80,
            "max_retries": 1,
            "description": "任务显式声明只需轻量工具脚本",
        }
    if "code_policy: complex" in text_lower:
        return {
            "allows_complex": True,
            "allows_lightweight": True,
            "max_lines": 200,
            "max_retries": 3,
            "description": "任务显式声明需要复杂代码",
        }

    # --- 2. 过滤被否定的代码关键词 ---
    effective_text = raw_text
    code_kw_pattern = "|".join(_COMPLEX_CODE_KW)
    negation_patterns = [
        r"不要求[^。\n]{{0,15}}({})".format(code_kw_pattern),
        r"不需要[^。\n]{{0,15}}({})".format(code_kw_pattern),
        r"无需[^。\n]{{0,15}}({})".format(code_kw_pattern),
    ]
    for pat in negation_patterns:
        effective_text = re.sub(pat, "", effective_text)

    # --- 3. 计数 ---
    complex_hits = sum(1 for kw in _COMPLEX_CODE_KW if kw in effective_text)
    non_code_hits = sum(1 for kw in _NON_CODE_KW if kw in raw_text)
    light_hint_hits = sum(1 for kw in _LIGHT_CODE_HINT_KW if kw in raw_text)
    light_json_hits = sum(1 for kw in _LIGHT_CODE_KW if re.search(kw, raw_text))

    # 检查文件类型
    all_docs = True
    for f in files:
        suffix = Path(f).suffix.lower()
        if suffix not in (".pdf", ".docx", ".xlsx", ".xls", ".md", ".txt"):
            all_docs = False
            break

    # --- 4. 判断 ---
    if complex_hits > 0 and non_code_hits >= complex_hits:
        # 文档类任务即使有关键词也是被否定的
        allows_complex = False
    elif complex_hits > 0:
        allows_complex = True
    else:
        allows_complex = False

    # 轻量工具：有 JSON/结构化输出需求，或有数据处理关键词且没有复杂代码需求
    allows_lightweight = (
        light_json_hits > 0
        or (light_hint_hits > 0 and not allows_complex)
    )

    if allows_lightweight:
        return {
            "allows_complex": False,
            "allows_lightweight": True,
            "max_lines": 80,
            "max_retries": 1,
            "description": (
                "仅允许轻量工具用途（JSON写入、字段校验、金额汇总、文件存在性检查）。"
                "禁止复杂算法、大型pipeline、模型训练、优化求解。"
                "脚本应保持很短，失败后转为阶段性报告，不进入多轮代码重写。"
            ),
        }

    if allows_complex:
        return {
            "allows_complex": True,
            "allows_lightweight": True,
            "max_lines": 200,
            "max_retries": 3,
            "description": (
                "允许复杂代码。分步生成，每步≤200行，小步验证。"
                "失败后最多重试3轮，超限降级为阶段性报告。"
            ),
        }

    return {
        "allows_complex": False,
        "allows_lightweight": False,
        "max_lines": 0,
        "max_retries": 0,
        "description": "本任务不需要代码。所有工作通过文件理解和分析完成。",
    }


def normalize_to_task_spec(raw_input: dict):
    files = raw_input.get("files", [])
    raw_text = raw_input.get("text", "")

    # 如果有 task.md，读取其内容用于判断
    for f in files:
        if Path(f).name == "task.md":
            try:
                task_text = Path(f).read_text(encoding="utf-8")
                raw_text = raw_text + "\n" + task_text
            except Exception:
                pass

    code_policy = _detect_code_policy(files, raw_text)

    task_spec = {
        "task_id": datetime.now().strftime("%Y%m%d_%H%M%S"),
        "task_name": "未命名复杂任务",
        "task_type": "general_complex_task",
        "input": {
            "type": raw_input.get("input_type", "unknown"),
            "files": files,
            "text": raw_input.get("text", ""),
        },
        "code_policy": code_policy,
        "requirements": {
            "must_understand_task": True,
            "must_decompose_task": True,
            "must_generate_code_if_needed": code_policy["allows_complex"] or code_policy["allows_lightweight"],
            "must_execute_code_if_generated": code_policy["allows_complex"] or code_policy["allows_lightweight"],
            "must_review_artifacts": True,
            "must_generate_report": True
        },
        "artifacts": {
            "code": "artifacts/code_pipeline.py",
            "result": "artifacts/result.json",
            "review": "review/validation_report.md",
            "report": "final_report.md",
            "trace": "agent_trace.md"
        },
        "success_criteria": {
            "required_artifacts": [
                "final_report.md",
                "agent_trace.md",
                "task_spec.json"
            ],
            "required_report_sections": [
                "任务理解",
                "任务拆解",
                "方法或模型设计",
                "代码实现或执行过程",
                "结果分析",
                "风险与改进建议"
            ]
        }
    }

    if files:
        first_file = Path(files[0]).name
        task_spec["task_name"] = first_file

    return task_spec
