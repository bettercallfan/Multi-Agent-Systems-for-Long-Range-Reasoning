"""Deterministic repair baseline and evidence builder for the expense demo."""

from __future__ import annotations

import json
from pathlib import Path


def build_fallback_code(task_spec: dict) -> str:
    """Return an auditable program that derives its result from normalized input."""

    return r'''import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(".")
OUT = ROOT / "artifacts"


def source(data, name):
    return next(item for item in data["structured_sources"] if item["name"] == name)


def number(pattern, text):
    match = re.search(pattern, text, re.S)
    if not match:
        raise ValueError("无法从制度解析数值: " + pattern)
    return float(match.group(1))


def main():
    data = json.loads((ROOT / "normalized_input.json").read_text(encoding="utf-8"))
    trip_source = source(data, "travel_application.docx")
    policy_source = source(data, "expense_policy.pdf")
    detail_source = source(data, "expense_detail.xlsx")
    trip = {str(row[0]).strip(): str(row[1]).strip()
            for row in trip_source["tables"][0]["rows"] if len(row) >= 2}
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", trip["出差时间"])
    if len(dates) != 2:
        raise ValueError("出差时间必须包含开始和结束日期")
    trip_info = {"employee_name": trip.get("员工姓名", "缺失"),
                 "department": trip.get("部门", "缺失"),
                 "destination": trip.get("出差地点", "缺失"),
                 "start_date": dates[0], "end_date": dates[1],
                 "purpose": trip.get("出差事由", "缺失"),
                 "approval_status": trip.get("审批状态", "缺失")}
    policy_text = "\n".join(page.get("content", "") for page in policy_source["pages"])
    policy = {
        "first_tier_accommodation_limit": number(r"一线城市住宿费不超过\s*(\d+(?:\.\d+)?)", policy_text),
        "other_city_accommodation_limit": number(r"其他城市住宿费不超过\s*(\d+(?:\.\d+)?)", policy_text),
        "meal_daily_limit": number(r"餐补标准为每人每天\s*(\d+(?:\.\d+)?)", policy_text),
        "invoice_required": "所有报销项目必须提供合规发票" in policy_text,
        "submission_deadline_days": int(number(r"出差结束后\s*(\d+)\s*天", policy_text)),
        "allowed_transport": ["高铁二等座", "动车二等座", "飞机经济舱"],
    }
    rows = next(iter(detail_source["sheets"].values()))
    headers = {str(value): index for index, value in enumerate(rows[0]) if value is not None}
    details = []
    totals = defaultdict(float)
    for source_row, row in enumerate(rows[1:], start=2):
        raw_date = row[headers["日期"]]
        if raw_date in (None, ""):
            continue
        item = {"source_row": source_row, "date": str(raw_date)[:10],
                "expense_type": str(row[headers["费用类型"]] or ""),
                "amount": row[headers["金额"]],
                "invoice_status": str(row[headers["发票状态"]] or ""),
                "notes": str(row[headers["备注"]] or "")}
        details.append(item)
        category = ("交通" if item["expense_type"] in {"高铁", "动车", "飞机", "打车"}
                    else "住宿" if item["expense_type"] == "住宿"
                    else "餐饮" if item["expense_type"] == "餐饮" else item["expense_type"])
        if isinstance(item["amount"], (int, float)):
            totals[category] += float(item["amount"])
    issues = []
    start, end = date.fromisoformat(dates[0]), date.fromisoformat(dates[1])
    for item in details:
        if item["invoice_status"] != "已提供":
            issues.append({"type": "missing_invoice", "source_row": item["source_row"],
                           "description": "该费用未提供发票，需补票"})
        if not isinstance(item["amount"], (int, float)):
            issues.append({"type": "missing_amount", "source_row": item["source_row"],
                           "description": "金额为空或不是数值"})
        if not item["expense_type"]:
            issues.append({"type": "unclear_expense_type", "source_row": item["source_row"],
                           "description": "费用类型不清楚"})
        if not start <= date.fromisoformat(item["date"]) <= end:
            issues.append({"type": "outside_trip_dates", "source_row": item["source_row"],
                           "description": "报销日期超出出差日期范围"})
        if item["expense_type"] == "餐饮" and isinstance(item["amount"], (int, float)) \
                and item["amount"] > policy["meal_daily_limit"]:
            issues.append({"type": "meal_limit_exceeded", "source_row": item["source_row"],
                           "description": "餐饮金额超过每日餐补标准，需说明原因"})
    result = {
        "source_files": [{"name": item["name"], "status": item["status"],
                          "role": {"travel_application.docx": "trip_application",
                                   "expense_policy.pdf": "expense_policy",
                                   "expense_detail.xlsx": "expense_details"}.get(item["name"], "task_instruction")}
                         for item in data["structured_sources"]],
        "trip_information": trip_info, "policy_rules": policy,
        "expense_details": details,
        "summary": {"record_count": len(details), "total_amount": sum(totals.values()),
                    "category_totals": dict(totals)},
        "issues": issues,
        "validation": {"status": "pending_independent_validation",
                       "approval_decision_made": False},
    }
    OUT.mkdir(exist_ok=True)
    (OUT / "expense_summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "expense_validation_log.json").write_text(json.dumps({
        "validator": "expense_summary", "passed": None,
        "status": "pending_independent_validation", "failures": [],
    }, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
'''


def build_evidence_fallback(
    task_spec: dict,
    dependency_view: dict,
    verified_refs: set[str],
) -> dict:
    """Use the independently recomputed audit as delivery evidence."""

    run_dir = Path(task_spec.get("run_dir", "."))
    result_ref = "artifacts/expense_summary.json"
    audit_ref = "artifacts/expense_validation_log.json"
    if not {result_ref, audit_ref}.issubset(verified_refs):
        raise ValueError("报销证据 fallback 缺少结果或独立审计文件")
    from orchestration.validation.models import BusinessValidationContext
    from task_plugins.expense_reimbursement.validator import ExpenseSummaryValidator

    check = ExpenseSummaryValidator().validate(BusinessValidationContext(
        run_dir=str(run_dir), task_spec=task_spec,
        validator_config={"target_artifact": result_ref},
    ))
    if check.status != "passed":
        raise ValueError("报销证据 fallback 的独立资料校验未通过")
    audit = json.loads((run_dir / audit_ref).read_text(encoding="utf-8"))
    if audit.get("passed") is not True:
        raise ValueError("报销证据 fallback 拒绝未通过的独立审计")
    delivery_nodes = [
        node_id for node_id, view in dependency_view.items()
        if view.get("role") == "executed_delivery_evidence"
    ]
    claims = [{
        "claim_id": "expense_result_validated",
        "source_node_ids": delivery_nodes,
        "claim": "报销资料已从三类真实输入完整提取，金额与问题清单经确定性复算",
        "verdict": "supported",
        "evidence_refs": [result_ref, audit_ref],
        "rationale": "独立审计 passed=true，且结果来自当前运行的标准化输入。",
        "confidence": 1.0,
    }]
    for node_id, view in dependency_view.items():
        if node_id in delivery_nodes:
            continue
        refs = [ref for ref in view.get("evidence_refs", []) if ref in verified_refs]
        refs = refs[:4] or (["normalized_input.json"] if "normalized_input.json" in verified_refs else [])
        if refs:
            claims.append({
                "claim_id": f"dependency_coverage_{len(claims) + 1:03d}",
                "source_node_ids": [node_id],
                "claim": f"直接依赖 {node_id} 的持久化证据已读取",
                "verdict": "supported", "evidence_refs": refs,
                "rationale": "该声明仅证明依赖覆盖，不替代报销业务事实。",
                "confidence": 1.0,
            })
    return {"status": "passed", "claims": claims, "issues": [],
            "additional_evidence_requests": [], "can_continue": True}
