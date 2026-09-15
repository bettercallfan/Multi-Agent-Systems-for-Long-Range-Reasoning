"""Independent deterministic validator for the expense reimbursement demo."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
import json
from pathlib import Path
import re
import time
from typing import Any

from orchestration.validation.models import BusinessCheckResult, BusinessValidationContext


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _source(data: dict, name: str) -> dict:
    return next((item for item in data.get("structured_sources", [])
                 if item.get("name") == name), {})


class ExpenseSummaryValidator:
    validator_id = "expense_summary"
    validator_type = "domain"

    def supports(self, task_spec: dict, validator_config: dict) -> bool:
        names = {Path(str(item)).name for item in task_spec.get("input", {}).get("files", [])}
        return {"travel_application.docx", "expense_policy.pdf",
                "expense_detail.xlsx"}.issubset(names)

    def validate(self, context: BusinessValidationContext) -> BusinessCheckResult:
        started = time.monotonic()
        root = Path(context.run_dir)
        target = str(context.validator_config.get(
            "target_artifact", "artifacts/expense_summary.json",
        ))
        failures: list[dict[str, Any]] = []
        checked: list[dict[str, Any]] = []
        try:
            data = _load_json(root / context.normalized_input_path)
            result = _load_json(root / target)
        except (OSError, ValueError, TypeError) as exc:
            data, result = {}, {}
            failures.append({"check": "parseable_inputs", "path": target,
                             "reason": f"{type(exc).__name__}: {exc}"})
        if not isinstance(result, dict):
            result = {}
            failures.append({"check": "result_object", "path": target})
        required = ["source_files", "trip_information", "policy_rules",
                    "expense_details", "summary", "issues", "validation"]
        missing = [field for field in required if field not in result]
        if missing:
            failures.append({"check": "required_fields", "path": target,
                             "missing": missing})

        sources = {item.get("name"): item for item in data.get("structured_sources", [])}
        expected_names = {"task.md", "travel_application.docx",
                          "expense_policy.pdf", "expense_detail.xlsx"}
        actual_names = {item.get("name") for item in result.get("source_files", [])
                        if isinstance(item, dict)}
        if actual_names != expected_names:
            failures.append({"check": "source_coverage", "path": target,
                             "expected": sorted(expected_names),
                             "actual": sorted(str(item) for item in actual_names)})
        for name in expected_names:
            if sources.get(name, {}).get("status") != "loaded":
                failures.append({"check": "source_loaded", "source": name,
                                 "reason": sources.get(name, {}).get("error", "未加载")})

        trip_source = sources.get("travel_application.docx", {})
        trip_rows = (trip_source.get("tables") or [{}])[0].get("rows", [])
        trip_map = {str(row[0]).strip(): str(row[1]).strip()
                    for row in trip_rows if len(row) >= 2}
        trip_dates = re.findall(r"\d{4}-\d{2}-\d{2}", trip_map.get("出差时间", ""))
        expected_trip = {
            "employee_name": trip_map.get("员工姓名", "缺失"),
            "department": trip_map.get("部门", "缺失"),
            "destination": trip_map.get("出差地点", "缺失"),
            "start_date": trip_dates[0] if len(trip_dates) == 2 else "缺失",
            "end_date": trip_dates[1] if len(trip_dates) == 2 else "缺失",
            "purpose": trip_map.get("出差事由", "缺失"),
            "approval_status": trip_map.get("审批状态", "缺失"),
        }
        actual_trip = result.get("trip_information")
        if actual_trip != expected_trip:
            failures.append({"check": "trip_information", "path": target,
                             "expected": expected_trip, "actual": actual_trip})

        policy_source = sources.get("expense_policy.pdf", {})
        policy_text = "\n".join(
            str(page.get("content", "")) for page in policy_source.get("pages", [])
        )
        numeric_patterns = {
            "first_tier_accommodation_limit": r"一线城市住宿费不超过\s*(\d+(?:\.\d+)?)",
            "other_city_accommodation_limit": r"其他城市住宿费不超过\s*(\d+(?:\.\d+)?)",
            "meal_daily_limit": r"餐补标准为每人每天\s*(\d+(?:\.\d+)?)",
            "submission_deadline_days": r"出差结束后\s*(\d+)\s*天",
        }
        expected_policy: dict[str, Any] = {}
        for field, pattern in numeric_patterns.items():
            match = re.search(pattern, policy_text, re.S)
            expected_policy[field] = float(match.group(1)) if match else None
        expected_policy["submission_deadline_days"] = (
            int(expected_policy["submission_deadline_days"])
            if expected_policy["submission_deadline_days"] is not None else None
        )
        expected_policy["invoice_required"] = "所有报销项目必须提供合规发票" in policy_text
        expected_policy["allowed_transport"] = ["高铁二等座", "动车二等座", "飞机经济舱"]
        if result.get("policy_rules") != expected_policy:
            failures.append({"check": "policy_rules", "path": target,
                             "expected": expected_policy,
                             "actual": result.get("policy_rules")})

        detail_source = sources.get("expense_detail.xlsx", {})
        sheet_rows = next(iter(detail_source.get("sheets", {}).values()), [])
        headers = ({str(value): index for index, value in enumerate(sheet_rows[0])
                    if value is not None} if sheet_rows else {})
        expected_details = []
        category_totals: defaultdict[str, float] = defaultdict(float)
        for source_row, row in enumerate(sheet_rows[1:], start=2):
            raw_date = row[headers["日期"]] if "日期" in headers else None
            if raw_date in (None, ""):
                continue
            item = {"source_row": source_row, "date": str(raw_date)[:10],
                    "expense_type": str(row[headers["费用类型"]] or ""),
                    "amount": row[headers["金额"]],
                    "invoice_status": str(row[headers["发票状态"]] or ""),
                    "notes": str(row[headers["备注"]] or "")}
            expected_details.append(item)
            category = ("交通" if item["expense_type"] in {"高铁", "动车", "飞机", "打车"}
                        else "住宿" if item["expense_type"] == "住宿"
                        else "餐饮" if item["expense_type"] == "餐饮" else item["expense_type"])
            if isinstance(item["amount"], (int, float)):
                category_totals[category] += float(item["amount"])
        if result.get("expense_details") != expected_details:
            failures.append({"check": "expense_details", "path": target,
                             "expected_count": len(expected_details),
                             "actual_count": len(result.get("expense_details", []))
                             if isinstance(result.get("expense_details"), list) else None})
        expected_summary = {"record_count": len(expected_details),
                            "total_amount": sum(category_totals.values()),
                            "category_totals": dict(category_totals)}
        if result.get("summary") != expected_summary:
            failures.append({"check": "summary_recompute", "path": target,
                             "expected": expected_summary,
                             "actual": result.get("summary")})

        expected_issue_keys = set()
        if len(trip_dates) == 2:
            start_date, end_date = map(date.fromisoformat, trip_dates)
            for item in expected_details:
                if item["invoice_status"] != "已提供":
                    expected_issue_keys.add(("missing_invoice", item["source_row"]))
                if not start_date <= date.fromisoformat(item["date"]) <= end_date:
                    expected_issue_keys.add(("outside_trip_dates", item["source_row"]))
                if (item["expense_type"] == "餐饮"
                        and isinstance(item["amount"], (int, float))
                        and expected_policy["meal_daily_limit"] is not None
                        and item["amount"] > expected_policy["meal_daily_limit"]):
                    expected_issue_keys.add(("meal_limit_exceeded", item["source_row"]))
        actual_issue_keys = {
            (str(item.get("type")), item.get("source_row"))
            for item in result.get("issues", []) if isinstance(item, dict)
        }
        if not expected_issue_keys.issubset(actual_issue_keys):
            failures.append({"check": "issue_detection", "path": target,
                             "missing": sorted(expected_issue_keys - actual_issue_keys)})
        validation = result.get("validation", {})
        if not isinstance(validation, dict) or validation.get("approval_decision_made") is not False:
            failures.append({"check": "decision_scope", "path": target,
                             "reason": "本任务不得代替人工作出最终报销审批决定"})

        checked.extend({"source": name, "status": sources.get(name, {}).get("status")}
                       for name in sorted(expected_names))
        recomputed = {"record_count": len(expected_details),
                      "total_amount": sum(category_totals.values()),
                      "category_totals": dict(category_totals),
                      "required_issue_keys": [list(item) for item in sorted(expected_issue_keys)]}
        audit = {"validator": self.validator_id, "passed": not failures,
                 "failures": failures, "recomputed": recomputed,
                 "checked_sources": checked}
        audit_path = root / "artifacts" / "expense_validation_log.json"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        return BusinessCheckResult(
            check_id=self.validator_id, validator_id=self.validator_id,
            status="failed" if failures else "passed",
            summary=("报销资料确定性校验通过" if not failures
                     else f"报销资料校验发现 {len(failures)} 项错误"),
            checked_items=checked, failed_checks=failures,
            recomputed_metrics=recomputed,
            evidence_refs=[target, "artifacts/expense_validation_log.json",
                           context.normalized_input_path],
            duration_seconds=time.monotonic() - started,
        )
