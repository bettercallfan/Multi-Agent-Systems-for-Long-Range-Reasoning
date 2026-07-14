"""Deterministic pre-report review and final delivery validation."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re

from orchestration.schemas import ArtifactQualityResult, ReviewDecision
from task_plugins.base import load_strict_json


_FORBIDDEN_REPORT_PATTERNS = {
    "执行轨迹": r"agent_trace|execution trace|执行轨迹",
    "Agent 内部名称": r"(?:TaskPlanning|CodeModeling|CodeExecutor|ErrorAttribution|Review|Report|FileSurfer)Agent",
    "Mermaid 系统图": r"sequenceDiagram|\bgantt\b",
}


def _validate_json_files(run_dir: Path, paths: list[str]) -> list[str]:
    issues = []
    for relative in paths:
        if not relative.lower().endswith(".json"):
            continue
        path = run_dir / relative
        if not path.is_file():
            continue
        try:
            load_strict_json(path)
        except (OSError, ValueError) as exc:
            issues.append(f"JSON 产物无法解析：{relative}（{exc}）")
    return issues


def pre_report_review(
    task_spec: dict,
    run_state: dict,
    artifact_quality: ArtifactQualityResult | dict | None = None,
) -> ReviewDecision:
    run_dir = Path(task_spec["run_dir"])
    contract = task_spec.get("artifact_contract", {})
    if "intermediate_artifacts" in contract:
        required = contract["intermediate_artifacts"]
    else:
        required = [
            path for path in task_spec.get("required_artifacts", [])
            if path != "final_report.md"
            and path not in {"task_spec.json", "agent_trace.md", "run_state.json", "file_previews.json"}
        ]
    missing = [path for path in required if not (run_dir / path).is_file()]
    issues = [f"缺少报告前必需产物：{path}" for path in missing]
    issues.extend(_validate_json_files(run_dir, required))

    execution = run_state.get("execution", {})
    code_mode = task_spec.get("code_policy", {}).get("mode", "none")
    execution_failed = code_mode != "none" and execution.get("exit_code") != 0
    if execution_failed:
        issues.append(f"代码执行退出码非零：{execution.get('exit_code')}")

    graph_failed = run_state.get("graph_outcome") == "failed"
    failed_nodes = sorted(
        node_id for node_id, node in run_state.get("nodes", {}).items()
        if node.get("status") in {"failed", "blocked"}
    )
    if graph_failed:
        issues.append("动态任务图执行失败")
    if failed_nodes:
        issues.append("未成功完成的任务图节点：" + ", ".join(failed_nodes))

    if artifact_quality is not None:
        quality = (
            artifact_quality
            if isinstance(artifact_quality, ArtifactQualityResult)
            else ArtifactQualityResult.model_validate(artifact_quality)
        )
        if quality.status == "failed":
            issues.extend(quality.issues)

    critical = bool(
        missing
        or any(issue.startswith("JSON 产物无法解析") for issue in issues)
        or (artifact_quality is not None and quality.status == "failed")
        or graph_failed
        or bool(failed_nodes)
    )
    if critical:
        status = "failed"
        can_report = False
    elif execution_failed:
        status = "partial"
        can_report = True
    else:
        status = "passed"
        can_report = True

    return ReviewDecision(
        status=status,
        can_generate_final_report=can_report,
        issues=issues,
        required_artifacts_checked=required,
    )


def merge_review_decisions(framework: ReviewDecision, semantic: ReviewDecision) -> ReviewDecision:
    """Merge decisions conservatively; an LLM can never upgrade framework evidence."""
    rank = {"passed": 0, "partial": 1, "failed": 2}
    status = framework.status if rank[framework.status] >= rank[semantic.status] else semantic.status
    can_report = framework.can_generate_final_report and semantic.can_generate_final_report and status != "failed"
    return ReviewDecision(
        status=status,
        can_generate_final_report=can_report,
        issues=list(dict.fromkeys(framework.issues + semantic.issues)),
        required_artifacts_checked=list(dict.fromkeys(
            framework.required_artifacts_checked + semantic.required_artifacts_checked
        )),
    )


def final_validation(task_spec: dict, run_state: dict) -> ReviewDecision:
    run_dir = Path(task_spec["run_dir"])
    contract = task_spec.get("artifact_contract", {})
    required = list(dict.fromkeys(
        contract.get("intermediate_artifacts", [])
        + contract.get("final_artifacts", [])
        + contract.get("framework_artifacts", [])
    )) or task_spec.get("required_artifacts", [])
    missing = [path for path in required if not (run_dir / path).is_file()]
    issues = [f"缺少最终必需产物：{path}" for path in missing]
    issues.extend(_validate_json_files(run_dir, required))

    report_path = run_dir / "final_report.md"
    content = ""
    if report_path.is_file():
        content = report_path.read_text(encoding="utf-8", errors="replace").strip()
        if len(content) < 200:
            issues.append(f"final_report.md 内容过短：{len(content)} 字符")
        if content.startswith("# task_spec.json") or content.startswith("```json"):
            issues.append("final_report.md 实际内容是 TaskSpec/JSON，不是业务报告")
        for section in task_spec.get("required_report_sections", []):
            if section not in content:
                issues.append(f"final_report.md 缺少必需章节：{section}")
        for label, pattern in _FORBIDDEN_REPORT_PATTERNS.items():
            if re.search(pattern, content, re.IGNORECASE):
                issues.append(f"final_report.md 包含禁止内容：{label}")
        for reference in set(re.findall(r"artifacts/[\w\-./]+", content)):
            clean = reference.rstrip("`。，、；;:：)）]")
            if not (run_dir / clean).exists():
                issues.append(f"报告引用了不存在的产物：{clean}")

    previous = run_state.get("review", {})
    execution_failed = (
        task_spec.get("code_policy", {}).get("mode", "none") != "none"
        and run_state.get("execution", {}).get("exit_code") != 0
    )
    graph_failed = run_state.get("graph_outcome") == "failed"
    failed_nodes = sorted(
        node_id for node_id, node in run_state.get("nodes", {}).items()
        if node.get("status") in {"failed", "blocked"}
    )
    if graph_failed:
        issues.append("动态任务图执行失败")
    if failed_nodes:
        issues.append("未成功完成的任务图节点：" + ", ".join(failed_nodes))
    if missing or issues:
        status = "failed"
    elif execution_failed or previous.get("status") == "partial":
        status = "partial"
    else:
        status = "passed"

    return ReviewDecision(
        status=status,
        can_generate_final_report=report_path.is_file() and status != "failed",
        issues=issues,
        required_artifacts_checked=required,
    )


def write_validation_report(run_dir: Path, decision: ReviewDecision, run_state: dict) -> str:
    path = run_dir / "review" / "validation_report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 框架最终验收报告",
        "",
        f"- 验收时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 验收状态：**{decision.status.upper()}**",
        f"- 代码退出码：{run_state.get('execution', {}).get('exit_code')}",
        f"- 是否允许交付报告：{'是' if decision.can_generate_final_report else '否'}",
        f"- 产物质量：{(run_state.get('artifact_quality', {}).get('current') or {}).get('status', 'unknown')}",
        f"- 当前失败路由：{(run_state.get('failure') or {}).get('resume_stage', '无')}",
        "",
        "## 必需产物",
        "",
    ]
    for relative in decision.required_artifacts_checked:
        exists = (run_dir / relative).is_file()
        lines.append(f"- {'✅' if exists else '❌'} {relative}")
    lines.extend(["", "## 问题", ""])
    lines.extend(f"- {issue}" for issue in decision.issues)
    if not decision.issues:
        lines.append("- 未发现阻断性问题")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path.relative_to(run_dir).as_posix()


def review_artifacts(task_spec: dict):
    """Compatibility wrapper that performs final validation from persisted state."""
    run_dir = Path(task_spec["run_dir"])
    state_path = run_dir / "run_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    decision = final_validation(task_spec, state)
    report_path = write_validation_report(run_dir, decision, state)
    return {
        "status": decision.status.upper(),
        "passed": len(decision.required_artifacts_checked) - len([
            p for p in decision.required_artifacts_checked if not (run_dir / p).is_file()
        ]),
        "total": len(decision.required_artifacts_checked),
        "report_path": str(run_dir / report_path),
    }
