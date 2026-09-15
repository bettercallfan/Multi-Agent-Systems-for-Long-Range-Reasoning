"""Build the single authoritative TaskSpec used by every workflow stage."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
import re

from orchestration.core.workflow_policy import (
    DEFAULT_COMMUNICATION_POLICY,
    DEFAULT_RECOVERY_POLICY,
    DEFAULT_ROUTING_POLICY,
    DEFAULT_TEAM_POLICY,
    DEFAULT_EXTERNAL_TOOLS_POLICY,
    DEFAULT_MEMORY_POLICY,
    DEFAULT_TERMINAL_POLICY,
    DEFAULT_TASK_UNDERSTANDING_POLICY,
    DEFAULT_VERIFICATION_POLICY,
    DEFAULT_PLANNING_POLICY,
    DEFAULT_HORIZON_POLICY,
)
from orchestration.validation.runner import default_business_validation_policy

_COMPLEX_CODE_KW = [
    "建模", "优化", "求解", "算法", "训练", "预测", "仿真", "模拟",
    "回归", "聚类", "装箱", "调度", "路径规划", "神经网络",
]
_NON_CODE_KW = ["不要求代码", "不需要代码", "无需代码", "只要求完成文件理解"]
_LIGHT_CODE_KW = [
    r"输出.*json", r"生成.*json", r"保存.*json", r"artifacts/",
    r"结构化.*输出", r"字段校验", r"数据汇总", r"格式校验",
]

_WEB_REQUIRED_PATTERNS = [
    r"https?://", r"联网", r"网页搜索", r"网络搜索", r"搜索互联网",
    r"查阅官网", r"官方网站", r"最新(?:消息|资料|政策|版本|数据|进展)",
    r"在线资料", r"从网上", r"浏览网页",
]
_WEB_PREFERRED_PATTERNS = [r"外部调研", r"资料调研", r"背景调研", r"文献调研"]
_FILE_NAVIGATION_PATTERNS = [
    r"逐页", r"全文检索", r"跨文档检索", r"长文档", r"超长文档",
    r"第\s*\d+\s*页", r"(?:定位|查找|搜索).{0,12}(?:文件|文档|原文)",
]
_CONVERSION_SUFFIXES = {
    ".ppt", ".pptx", ".html", ".htm", ".epub", ".zip", ".msg",
    ".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".gif", ".wav", ".mp3",
}
_TEXT_PREVIEWABLE_CONVERSION_SUFFIXES = {".html", ".htm"}


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
    doc_hits = sum(word in text for word in ["文档", "整理", "提取", "汇总", "核对", "摘要"])

    if any(word in text for word in ["文件理解", "文档分析", "资料整理"]):
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
        # Complex tasks need enough room for real input parsing, validation and
        # reproducible algorithms.  This remains a strict source-size guard;
        # it does not weaken compilation, sandbox, artifact or business checks.
        "complex": (True, True, 320, 3, "允许分步生成和执行复杂代码"),
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


def _detect_report_policy(text: str) -> dict:
    excludes_final_decision = bool(re.search(
        r"(?:不需要|无需|不要|不得|不要求|禁止).{0,30}"
        r"(?:最终(?:决定|决策|裁决|结论|批准|审批)|决策性建议|处置结论)",
        text,
    ))
    return {
        "decision_scope": "descriptive_only" if excludes_final_decision else "advisory",
        "forbid_unsupported_decisions": excludes_final_decision,
        "allowed_recommendations": (
            ["补充证据", "核实事实", "人工复核"]
            if excludes_final_decision else []
        ),
    }


def _task_name(text: str, files: list[str]) -> str:
    match = re.search(r"任务名称[：:]\s*([^\n]+)", text)
    if match:
        return match.group(1).strip()
    return Path(files[0]).name if files else "未命名复杂任务"


def _capability_contract(task_spec: dict, text: str) -> dict:
    """Derive external capability requirements once and freeze them in TaskSpec."""

    required: list[str] = []
    preferred: list[str] = []
    previews = task_spec.get("file_previews") or []
    files = task_spec.get("input", {}).get("files", [])
    if task_spec.get("verification_policy", {}).get("enabled", True):
        required.append("evidence_verification")
    if (
        task_spec.get("terminal_policy", {}).get("enabled", True)
        and task_spec.get("code_policy", {}).get("mode", "none") != "none"
    ):
        required.append("terminal_execution")
    preview_requires_conversion = any(
        preview.get("status") != "success"
        or (
            preview.get("type") in {"pdf", "docx"}
            and not str(preview.get("text_preview") or "").strip()
        )
        for preview in previews
    )
    suffix_requires_conversion = any(
        Path(path).suffix.lower() in _CONVERSION_SUFFIXES
        and (
            Path(path).suffix.lower() not in _TEXT_PREVIEWABLE_CONVERSION_SUFFIXES
            or not any(
                Path(str(preview.get("path") or "")).name == Path(path).name
                and preview.get("status") == "success"
                and str(preview.get("text_preview") or "").strip()
                for preview in previews
            )
        )
        for path in files
    )
    if preview_requires_conversion or suffix_requires_conversion:
        required.append("document_conversion")

    long_document = any(
        int(preview.get("page_count", 0) or 0) > 10
        or int(preview.get("paragraph_count", 0) or 0) > 80
        for preview in previews
    )
    targeted_navigation = any(
        re.search(pattern, text, re.IGNORECASE)
        for pattern in _FILE_NAVIGATION_PATTERNS
    )
    if files and (long_document or targeted_navigation):
        required.append("file_navigation")

    local_only = bool(re.search(
        r"禁止联网|无需联网|不需要联网|仅处理本地|不使用外部(?:网页|网络|数据)",
        text, re.IGNORECASE,
    ))
    if not local_only and any(
        re.search(pattern, text, re.IGNORECASE)
        for pattern in _WEB_REQUIRED_PATTERNS
    ):
        required.append("web_research")
    elif not local_only and any(
        re.search(pattern, text, re.IGNORECASE)
        for pattern in _WEB_PREFERRED_PATTERNS
    ):
        preferred.append("web_research")

    return {
        "required": list(dict.fromkeys(required)),
        "preferred": [item for item in dict.fromkeys(preferred) if item not in required],
        "reasons": {
            "evidence_verification": (
                "所有事实型交付在进入审查和报告前必须绑定真实证据"
                if "evidence_verification" in required else ""
            ),
            "terminal_execution": (
                "代码任务需要独立的受控终端节点复核已落盘程序"
                if "terminal_execution" in required else ""
            ),
            "document_conversion": (
                "存在预览失败、空扫描文档或需要多格式转换的输入"
                if "document_conversion" in required else ""
            ),
            "file_navigation": (
                "任务要求长文档、逐页或定位式文件浏览"
                if "file_navigation" in required else ""
            ),
            "web_research": (
                "任务明确要求联网、网页、官网、URL 或最新外部信息"
                if "web_research" in required else
                "任务包含调研语义，可在能力可用时按需调用"
                if "web_research" in preferred else ""
            ),
        },
    }


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
    # These policies are part of the frozen task contract.  Runtime components
    # may select an implementation, but they must not invent their own budgets
    # or recovery limits outside TaskSpec.
    def complete_policy(name: str, defaults: dict) -> None:
        override = task_spec.get(name) or {}
        if not isinstance(override, dict):
            raise ValueError(f"TaskSpec.{name} 必须是对象")
        task_spec[name] = {**deepcopy(defaults), **deepcopy(override)}

    complete_policy("communication_policy", DEFAULT_COMMUNICATION_POLICY)
    complete_policy("routing_policy", DEFAULT_ROUTING_POLICY)
    complete_policy("team_policy", DEFAULT_TEAM_POLICY)
    complete_policy("recovery_policy", DEFAULT_RECOVERY_POLICY)
    complete_policy("external_tools_policy", DEFAULT_EXTERNAL_TOOLS_POLICY)
    complete_policy("verification_policy", DEFAULT_VERIFICATION_POLICY)
    complete_policy("memory_policy", DEFAULT_MEMORY_POLICY)
    complete_policy("terminal_policy", DEFAULT_TERMINAL_POLICY)
    complete_policy(
        "task_understanding_policy", DEFAULT_TASK_UNDERSTANDING_POLICY,
    )
    complete_policy("planning_policy", DEFAULT_PLANNING_POLICY)
    complete_policy("horizon_policy", DEFAULT_HORIZON_POLICY)
    task_spec["capability_contract"] = _capability_contract(task_spec, text)
    requested = _extract_requested_artifacts(text)
    if code_policy["mode"] != "none" and not requested:
        requested = ["artifacts/result.json"]

    code_path = "artifacts/code_pipeline.py"
    intermediate = list(requested)
    if code_policy["mode"] != "none" and code_path not in intermediate:
        intermediate.append(code_path)
    # MathorCup has a deliberately small, task-local audit contract.  It is
    # declared here (rather than in the generic artifact validator) so every
    # generated solution is required to leave independently inspectable
    # result, constraint, and cost evidence.
    input_names = {Path(str(item)).name for item in task_spec.get("input", {}).get("files", [])}
    is_mathorcup = task_spec.get("task_type") == "math_modeling" and "attachment1.docx" in input_names
    is_expense_reimbursement = {
        "travel_application.docx", "expense_policy.pdf", "expense_detail.xlsx",
    }.issubset(input_names)
    is_urban_multimodal = {
        "law_articles_80k.jsonl",
        "phone_network_sichuan_open_voc_80k_utf8_bom.csv",
        "Shifu.pcap",
    }.issubset(input_names)
    if is_mathorcup and code_policy["mode"] != "none":
        # Keep the competition demo bounded and reproducible.  The generic
        # framework still supports progressive horizons for other task types;
        # this fixed benchmark should prioritize a complete auditable run.
        task_spec["horizon_policy"]["mode"] = "one_shot"
        task_spec["horizon_policy"]["progressive_execution"] = False
        task_spec["horizon_policy"]["requirement_driven_expansion"] = False
        # Two real model-generated executions (initial + one repair) are
        # enough to demonstrate and exercise the repair path.  Beyond that,
        # switch to the auditable baseline instead of spending an unbounded
        # competition-time budget sampling unrelated rewrites.
        task_spec["code_policy"]["max_retries"] = min(
            int(task_spec["code_policy"].get("max_retries", 1)), 1,
        )
        task_spec["code_fallback_plugin"] = {
            "module": "task_plugins.mathorcup_d.solver",
            "function": "build_fallback_code",
        }
        task_spec["evidence_fallback_plugin"] = {
            "module": "task_plugins.mathorcup_d.solver",
            "function": "build_evidence_fallback",
        }
        task_spec["domain_result_contract"] = {
            "semantic_validation_target": "artifacts/constraint_validation_log.json",
            "semantic_validation_fields": ["/passed", "/failures", "/recomputed"],
            "result_schema_fields": [
                "vehicle_scenarios", "multi_vehicle_scenarios", "placements",
                "vehicle_count", "total_cost", "space_utilization_rate",
                "load_utilization_rate", "validation",
            ],
            "constraint_log_fields": ["passed", "failures", "recomputed"],
            "cost_comparison_fields": ["scenarios"],
            "allowed_acceptance_targets": {
                "artifacts/result.json": [
                    "/vehicle_scenarios", "/multi_vehicle_scenarios", "/placements",
                    "/vehicle_count", "/total_cost", "/space_utilization_rate",
                    "/load_utilization_rate", "/validation",
                ],
                "artifacts/result_complete.json": [
                    "/vehicle_scenarios", "/multi_vehicle_scenarios", "/placements",
                    "/vehicle_count", "/total_cost", "/space_utilization_rate",
                    "/load_utilization_rate", "/validation",
                ],
                "artifacts/constraint_validation_log.json": ["/passed", "/failures", "/recomputed"],
                "artifacts/cost_comparison.json": ["/scenarios"],
            },
        }
        for audit_path in (
            "artifacts/result_complete.json",
            "artifacts/constraint_validation_log.json",
            "artifacts/cost_comparison.json",
        ):
            if audit_path not in intermediate:
                intermediate.append(audit_path)
    if is_expense_reimbursement and code_policy["mode"] != "none":
        task_spec["code_policy"]["max_retries"] = min(
            int(task_spec["code_policy"].get("max_retries", 1)), 1,
        )
        task_spec["code_fallback_plugin"] = {
            "module": "task_plugins.expense_reimbursement.solver",
            "function": "build_fallback_code",
        }
        task_spec["evidence_fallback_plugin"] = {
            "module": "task_plugins.expense_reimbursement.solver",
            "function": "build_evidence_fallback",
        }
        task_spec["domain_result_contract"] = {
            "semantic_validation_target": "artifacts/expense_validation_log.json",
            "semantic_validation_fields": ["/passed", "/failures", "/recomputed"],
            "result_schema_fields": [
                "source_files", "trip_information", "policy_rules",
                "expense_details", "summary", "issues", "validation",
            ],
            "validation_log_fields": ["passed", "failures", "recomputed"],
            "allowed_acceptance_targets": {
                "artifacts/expense_summary.json": [
                    "/source_files", "/trip_information", "/policy_rules",
                    "/expense_details", "/summary", "/issues", "/validation",
                ],
                "artifacts/expense_validation_log.json": [
                    "/passed", "/failures", "/recomputed",
                ],
            },
        }
        audit_path = "artifacts/expense_validation_log.json"
        if audit_path not in intermediate:
            intermediate.append(audit_path)
    if is_urban_multimodal and code_policy["mode"] != "none":
        task_spec["horizon_policy"]["mode"] = "one_shot"
        task_spec["horizon_policy"]["progressive_execution"] = False
        task_spec["horizon_policy"]["requirement_driven_expansion"] = False
        task_spec["code_policy"]["max_retries"] = min(
            int(task_spec["code_policy"].get("max_retries", 1)), 1,
        )
        task_spec["code_fallback_plugin"] = {
            "module": "task_plugins.urban_multimodal.solver",
            "function": "build_fallback_code",
        }
        task_spec["evidence_fallback_plugin"] = {
            "module": "task_plugins.urban_multimodal.solver",
            "function": "build_evidence_fallback",
        }
        task_spec["domain_result_contract"] = {
            "semantic_validation_target": "artifacts/urban_validation_log.json",
            "semantic_validation_fields": ["/passed", "/failures", "/recomputed"],
            "result_schema_fields": [
                "source_coverage", "modality_profiles", "cross_modal_assessment",
                "governance_recommendations", "limitations", "validation",
            ],
            "allowed_acceptance_targets": {
                "artifacts/urban_multimodal_result.json": [
                    "/source_coverage", "/modality_profiles",
                    "/cross_modal_assessment", "/governance_recommendations",
                    "/limitations", "/validation",
                ],
                "artifacts/urban_modality_summary.json": [
                    "/source_coverage", "/modality_profiles",
                ],
                "artifacts/urban_validation_log.json": [
                    "/passed", "/failures", "/recomputed",
                ],
            },
        }
        for audit_path in (
            "artifacts/urban_modality_summary.json",
            "artifacts/urban_validation_log.json",
        ):
            if audit_path not in intermediate:
                intermediate.append(audit_path)
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
    # The planner owns only business-graph deliverables.  Report and framework
    # artifacts are deliberately outside the generated DAG and are produced by
    # explicit workflow stages after graph execution.
    task_spec["planning_contract"] = {
        "graph_deliverables": list(intermediate),
        "postprocess_deliverables": list(final),
        "framework_artifacts": list(framework),
        "validation_requirements": [],
    }
    task_spec["required_report_sections"] = report_sections
    task_spec["report_policy"] = _detect_report_policy(text)
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
    business_validation = task_spec.get("business_validation")
    if (
        business_validation is None
        or (
            isinstance(business_validation, dict)
            and business_validation.get("source") == "framework_default"
        )
    ):
        # TaskSpec is built twice: once from names/task text and once after
        # file previews exist.  A task can legitimately change from no-code
        # to code-producing during finalization, so framework-derived
        # validators must be regenerated from the final artifact/code
        # contract.  Explicit caller policies remain untouched.
        task_spec["business_validation"] = default_business_validation_policy(
            task_spec
        )
        if is_mathorcup:
            task_spec["domain_validator_plugins"] = [{
                "module": "task_plugins.mathorcup_d.validator",
                "class": "MathorCupPackingValidator",
            }]
            task_spec["business_validation"]["validators"].append({
                "validator_id": "mathorcup_packing",
                "validator_type": "domain",
                "required": True,
                "config": {
                    "target_artifact": "artifacts/result.json",
                    "required_fields": [
                        "status", "vehicle_scenarios", "multi_vehicle_scenarios",
                        "space_utilization_rate", "load_utilization_rate",
                    ],
                },
            })
        elif is_expense_reimbursement:
            task_spec["domain_validator_plugins"] = [{
                "module": "task_plugins.expense_reimbursement.validator",
                "class": "ExpenseSummaryValidator",
            }]
            task_spec["business_validation"]["validators"].append({
                "validator_id": "expense_summary",
                "validator_type": "domain",
                "required": True,
                "config": {
                    "target_artifact": "artifacts/expense_summary.json",
                    "required_fields": [
                        "source_files", "trip_information", "policy_rules",
                        "expense_details", "summary", "issues", "validation",
                    ],
                },
            })
        elif is_urban_multimodal:
            task_spec["domain_validator_plugins"] = [{
                "module": "task_plugins.urban_multimodal.validator",
                "class": "UrbanMultimodalValidator",
            }]
            task_spec["business_validation"]["validators"].append({
                "validator_id": "urban_multimodal",
                "validator_type": "domain",
                "required": True,
                "config": {
                    "target_artifact": "artifacts/urban_multimodal_result.json",
                    "required_fields": [
                        "source_coverage", "modality_profiles",
                        "cross_modal_assessment", "governance_recommendations",
                        "limitations", "validation",
                    ],
                },
            })
    elif not isinstance(business_validation, dict):
        raise ValueError("TaskSpec.business_validation 必须是对象")
    return task_spec
