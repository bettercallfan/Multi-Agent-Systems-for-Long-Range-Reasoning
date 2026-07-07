from pathlib import Path
from datetime import datetime


def review_artifacts(task_spec: dict):
    """
    通用产物审查。

    在文件存在性检查基础上，参考 run_state.json 的 outcome，
    避免在明显失败、降级或结果为空时仍然机械 PASS。
    """
    run_dir = Path(task_spec["run_dir"])

    checks = []

    # --- 基础文件存在性检查 ---

    expected_files = [
        run_dir / "task_spec.json",
        run_dir / "agent_trace.md",
        run_dir / "final_report.md",
    ]

    for path in expected_files:
        checks.append({
            "item": f"{path.name} 是否存在",
            "passed": path.exists(),
            "detail": str(path)
        })

    artifacts_dir = run_dir / "artifacts"
    review_dir = run_dir / "review"

    checks.append({
        "item": "artifacts 目录是否存在",
        "passed": artifacts_dir.exists(),
        "detail": str(artifacts_dir)
    })

    checks.append({
        "item": "review 目录是否存在",
        "passed": review_dir.exists(),
        "detail": str(review_dir)
    })

    base_passed = sum(1 for x in checks if x["passed"])
    base_total = len(checks)

    # --- 读取 RunState 做语义判断 ---

    run_state_path = run_dir / "run_state.json"
    run_state = {}
    if run_state_path.exists():
        try:
            import json
            run_state = json.loads(run_state_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    outcome = run_state.get("outcome", "unknown")
    fallback = run_state.get("fallback_triggered", False)
    fallback_reason = run_state.get("fallback_reason", "")
    report_source = run_state.get("final_report_source", "")
    artifacts_found = run_state.get("artifacts_found", [])
    artifacts_missing = run_state.get("artifacts_missing", [])
    code_retries = run_state.get("code_retry_count", 0)
    agents_called = run_state.get("agents_called", [])

    # --- 语义检查项 ---

    # 1. 最终报告是否由 ReportAgent 生成
    if report_source and report_source != "ReportAgent":
        checks.append({
            "item": f"最终报告由 ReportAgent 生成（实际来源: {report_source}）",
            "passed": False,
            "detail": f"报告由 {report_source} 生成，非 ReportAgent"
        })
    elif report_source == "ReportAgent":
        checks.append({
            "item": "最终报告由 ReportAgent 生成",
            "passed": True,
            "detail": "来源正确"
        })

    # 2. 降级检查
    if fallback:
        checks.append({
            "item": f"降级机制触发（{fallback_reason}）",
            "passed": True,  # 降级本身不算失败
            "detail": "系统在代码多轮失败后正确降级，基于已有产物继续"
        })

    # 3. 产物为空 — 尊重 code_policy，不强制所有任务都要代码产物
    code_policy = task_spec.get("code_policy", {})
    allows_code = code_policy.get("allows_complex", True) or code_policy.get("allows_lightweight", True)
    if not artifacts_found and artifacts_dir.exists():
        has_files = any(artifacts_dir.iterdir())
        if not has_files:
            if allows_code:
                checks.append({
                    "item": "产物目录为空（code_policy 允许代码但未生成任何产物）",
                    "passed": False,
                    "detail": str(artifacts_dir)
                })
            else:
                checks.append({
                    "item": "产物目录为空（code_policy 不要求代码，此为正常状态）",
                    "passed": True,
                    "detail": "code_policy 不要求代码产物"
                })

    # 4. ReportAgent 从未被调用但任务需要报告
    if "ReportAgent" not in agents_called and agents_called:
        checks.append({
            "item": "ReportAgent 是否被调用",
            "passed": False,
            "detail": f"已调用: {', '.join(agents_called)}"
        })

    # --- 内容质量检查 ---

    # 5. 检查 final_report.md 内容是否过短（可能是空壳或拒绝生成）
    final_report_path = run_dir / "final_report.md"
    if final_report_path.exists():
        try:
            content = final_report_path.read_text(encoding="utf-8")
            content_len = len(content.strip())
            if content_len < 200:
                checks.append({
                    "item": "final_report.md 内容过短（可能为空壳或未完成）",
                    "passed": False,
                    "detail": f"内容仅 {content_len} 字符"
                })
            else:
                # 5b. 检查是否包含禁止的虚构内容
                forbidden_patterns = [
                    ("Mermaid 序列图", "sequenceDiagram"),
                    ("Mermaid 甘特图", "gantt"),
                    ("虚构的 agent_trace 章节", "## Step 1:"),
                    ("Agent 调用统计表", "调用次数"),
                ]
                for label, pattern in forbidden_patterns:
                    if pattern in content:
                        checks.append({
                            "item": f"报告含禁止内容：{label}",
                            "passed": False,
                            "detail": f"final_report.md 中检测到 \"{pattern}\""
                        })
        except Exception:
            pass

    # --- 综合判断 ---

    passed = sum(1 for x in checks if x["passed"])
    total = len(checks)

    # 使用 code_policy 判断代码要求
    code_policy = task_spec.get("code_policy", {})
    requires_code = code_policy.get("allows_complex", True) or code_policy.get("allows_lightweight", True)

    if outcome == "failed" or not run_state.get("final_report_generated", True):
        status = "FAIL"
    elif outcome == "fallback":
        status = "PARTIAL"
    elif outcome == "partial":
        status = "PARTIAL"
    elif fallback:
        status = "PARTIAL"
    elif report_source and report_source != "ReportAgent":
        status = "PARTIAL"
    elif passed == total and base_passed == base_total:
        status = "PASS"
    elif base_passed == base_total and not requires_code:
        # code_policy 不要求代码：基础文件齐全就是分析完成
        status = "PASS_ANALYSIS_ONLY"
    elif base_passed == base_total:
        status = "PARTIAL"
    else:
        status = "FAIL"

    # --- 生成审查报告 ---

    report_path = review_dir / "validation_report.md"

    lines = [
        "# 通用产物审查报告\n",
        f"- 审查时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 审查状态：**{status}**",
        f"- 通过项：{passed}/{total}",
        f"- 基础检查：{base_passed}/{base_total}",
    ]

    if run_state:
        lines.extend([
            "",
            f"- 运行结果(outcome)：{outcome}",
            f"- 是否降级：{'是' if fallback else '否'}",
            f"- 报告来源：{report_source or '无'}",
            f"- 已调用 Agent：{', '.join(agents_called) if agents_called else '无'}",
            f"- 产物数：{len(artifacts_found)}",
            f"- 代码重试：{code_retries} 轮",
        ])

    lines.extend([
        "",
        "## 检查明细\n"
    ])

    for item in checks:
        mark = "✅" if item["passed"] else "❌"
        lines.append(f"- {mark} {item['item']}：{item['detail']}")

    report_path.write_text("\n".join(lines), encoding="utf-8")

    return {
        "status": status,
        "passed": passed,
        "total": total,
        "report_path": str(report_path)
    }
