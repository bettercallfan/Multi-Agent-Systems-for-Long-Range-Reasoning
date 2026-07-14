"""Build the single authoritative TaskSpec used by every workflow stage."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re

from task_plugins.registry import detect_plugin_id


_COMPLEX_CODE_KW = [
    "建模", "优化", "求解", "算法", "训练", "预测", "仿真", "模拟",
    "回归", "聚类", "装箱", "调度", "路径规划", "神经网络",
]
_NON_CODE_KW = ["不要求代码", "不需要代码", "无需代码", "只要求完成文件理解"]
_LIGHT_CODE_KW = [
    r"输出.*json", r"生成.*json", r"保存.*json", r"artifacts/",
    r"结构化.*输出", r"字段校验", r"金额汇总", r"格式校验",
]


def _task_text(files: list[str], raw_text: str) -> str:
    parts = [raw_text]
    for file_path in files:
        path = Path(file_path)
        if path.name.lower() in {"task.md", "task.txt", "readme.md"}:
            try:
                parts.append(path.read_text(encoding="utf-8"))
            except OSError:
                continue
    return "\n".join(part for part in parts if part)


def _preview_text(file_previews: list[dict]) -> str:
    parts = []
    for preview in file_previews:
        parts.append(preview.get("text_preview", ""))
        for sheet in preview.get("sheet_summaries", {}).values():
            parts.append(" ".join(sheet.get("columns", [])))
    return "\n".join(parts)


def _detect_task_type(text: str, files: list[str]) -> str:
    suffixes = {Path(path).suffix.lower() for path in files}
    math_hits = sum(word in text for word in ["数学建模", "优化", "求解", "装箱", "路径规划", "调度"])
    data_hits = sum(word in text for word in [
        "模型训练", "预测模型", "分类模型", "回归模型", "聚类分析", "特征工程", "模型评估", "训练数据",
    ])
    doc_hits = sum(word in text for word in ["文档", "整理", "提取", "汇总", "报销", "审批", "核对", "摘要"])

    if any(word in text for word in ["文件理解", "文档分析", "资料整理", "报销"]):
        return "document_analysis"
    if math_hits:
        return "math_modeling"
    if data_hits:
        return "data_modeling"
    if doc_hits or (suffixes and suffixes.issubset({".pdf", ".docx", ".xlsx", ".xls", ".md", ".txt"})):
        return "document_analysis"
    return "general_complex_task"


def _detect_code_policy(text: str) -> dict:
    lower = text.lower()
    if "code_policy: none" in lower:
        mode = "none"
    elif "code_policy: lightweight" in lower:
        mode = "lightweight"
    elif "code_policy: complex" in lower:
        mode = "complex"
    else:
        negated = any(keyword in text for keyword in _NON_CODE_KW)
        complex_hits = sum(keyword in text for keyword in _COMPLEX_CODE_KW)
        light_hits = sum(bool(re.search(pattern, text, re.IGNORECASE)) for pattern in _LIGHT_CODE_KW)
        if complex_hits and not negated:
            mode = "complex"
        elif light_hits:
            mode = "lightweight"
        else:
            mode = "none"

    settings = {
        "none": (False, False, 0, 0, "不需要代码执行"),
        "lightweight": (False, True, 160, 1, "仅允许轻量数据处理、校验和结构化输出；保持可读性，不得压缩为单行代码"),
        "complex": (True, True, 200, 3, "允许分步生成和执行复杂代码"),
    }
    allows_complex, allows_lightweight, max_lines, max_retries, description = settings[mode]
    return {
        "mode": mode,
        "allows_complex": allows_complex,
        "allows_lightweight": allows_lightweight,
        "max_lines": max_lines,
        "max_retries": max_retries,
        "description": description,
    }


def _extract_requested_artifacts(text: str) -> list[str]:
    matches = re.findall(r"(?<![\w.-])(artifacts/[\w\-./]+\.(?:json|csv|xlsx|md|txt|py))", text, re.IGNORECASE)
    return list(dict.fromkeys(path.rstrip("。；;,，") for path in matches))


def _extract_report_sections(text: str, task_type: str) -> list[str]:
    marker = re.search(r"最终报告(?:至少)?包含[：:]?([^\0]*)", text)
    if marker:
        cleaned = []
        for line in marker.group(1).splitlines():
            match = re.match(r"^[一二三四五六七八九十\d]+[、.．]\s*([^；;]+)", line.strip())
            if match:
                cleaned.append(match.group(1).strip().rstrip("。"))
            elif cleaned and line.strip():
                break
        if cleaned:
            return cleaned

    if task_type == "document_analysis":
        return ["任务理解", "输入文件清单", "分析过程", "结果分析", "风险与改进建议", "最终交付物"]
    return ["任务理解", "任务拆解", "方法或模型设计", "代码实现或执行过程", "结果分析", "风险与改进建议"]


def _task_name(text: str, files: list[str]) -> str:
    match = re.search(r"任务名称[：:]\s*([^\n]+)", text)
    if match:
        return match.group(1).strip()
    return Path(files[0]).name if files else "未命名复杂任务"


def normalize_to_task_spec(raw_input: dict) -> dict:
    """Create an initial TaskSpec; call ``finalize_task_spec`` after previews exist."""
    files = raw_input.get("files", [])
    text = _task_text(files, raw_input.get("text", ""))
    task_type = _detect_task_type(text, files)
    code_policy = _detect_code_policy(text)

    task_spec = {
        "schema_version": "2.0",
        "task_id": datetime.now().strftime("%Y%m%d_%H%M%S"),
        "task_name": _task_name(text, files),
        "task_type": task_type,
        "input": {
            "type": raw_input.get("input_type", "unknown"),
            "files": files,
            "text": raw_input.get("text", ""),
        },
        "code_policy": code_policy,
        "requirements": {
            "must_understand_task": True,
            "must_decompose_task": True,
            "must_generate_code_if_needed": code_policy["mode"] != "none",
            "must_execute_code_if_generated": code_policy["mode"] != "none",
            "must_review_artifacts": True,
            "must_generate_report": True,
        },
        "success_criteria": {},
    }
    return _apply_contract(task_spec, text)


def finalize_task_spec(task_spec: dict, file_previews: list[dict]) -> dict:
    """Finalize classification and contracts once file previews are available."""
    files = task_spec.get("input", {}).get("files", [])
    combined_text = "\n".join([
        task_spec.get("input", {}).get("text", ""),
        _task_text(files, ""),
        _preview_text(file_previews),
    ])
    task_spec["task_type"] = _detect_task_type(combined_text, files)
    task_spec["code_policy"] = _detect_code_policy(combined_text)
    task_spec["requirements"]["must_generate_code_if_needed"] = task_spec["code_policy"]["mode"] != "none"
    task_spec["requirements"]["must_execute_code_if_generated"] = task_spec["code_policy"]["mode"] != "none"
    task_spec["file_previews"] = file_previews
    return _apply_contract(task_spec, combined_text)


def _apply_contract(task_spec: dict, text: str) -> dict:
    code_policy = task_spec["code_policy"]
    requested = _extract_requested_artifacts(text)
    if code_policy["mode"] != "none" and not requested:
        requested = ["artifacts/result.json"]

    code_path = "artifacts/code_pipeline.py"
    intermediate = list(requested)
    if code_policy["mode"] != "none" and code_path not in intermediate:
        intermediate.append(code_path)
    final = ["final_report.md"]
    framework = [
        "task_spec.json", "agent_trace.md", "run_state.json",
        "file_previews.json", "normalized_input.json", "task_graph.json",
    ]
    required = list(dict.fromkeys(intermediate + final + framework))

    report_sections = _extract_report_sections(text, task_spec["task_type"])
    task_spec["required_artifacts"] = required
    task_spec["artifact_contract"] = {
        "intermediate_artifacts": intermediate,
        "final_artifacts": final,
        "framework_artifacts": framework,
    }
    task_spec["required_report_sections"] = report_sections
    task_spec["success_criteria"] = {
        "required_artifacts": required,
        "required_report_sections": report_sections,
        "json_artifacts_must_parse": True,
        "execution_must_succeed_for_pass": True,
    }
    task_spec["artifacts"] = {
        "code": code_path if code_policy["mode"] != "none" else None,
        "results": requested,
        "review": "review/validation_report.md",
        "report": "final_report.md",
        "trace": "agent_trace.md",
    }
    task_spec["plugin_id"] = detect_plugin_id(task_spec, text)
    return task_spec
