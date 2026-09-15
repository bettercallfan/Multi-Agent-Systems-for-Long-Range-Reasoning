"""Audit and render runtime injection recovery evidence for one real run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.run_audit import audit_run


EXPECTED_PHASES = {
    "node_failure": ["triggered", "detected", "recovery_started", "recovered"],
    "requirement_change": ["triggered", "recovery_started", "recovered"],
    "data_anomaly": ["triggered", "detected", "recovery_started", "recovered"],
}


def _ordered_subsequence(expected: list[str], actual: list[str]) -> bool:
    cursor = 0
    for phase in actual:
        if cursor < len(expected) and phase == expected[cursor]:
            cursor += 1
    return cursor == len(expected)


def audit_runtime_injections(run_dir: str | Path) -> dict:
    root = Path(run_dir)
    state_path = root / "run_state.json"
    if not state_path.is_file():
        raise FileNotFoundError(f"run_state 不存在: {state_path}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    runtime = state.get("runtime_injections") or {}
    requested = list(runtime.get("requested_types") or [])
    items = runtime.get("items") or {}
    item_types = {str(item.get("type")) for item in items.values()}
    profile = runtime.get("profile")
    checks = {
        "injections_enabled": bool(runtime.get("enabled")),
        "all_requested_types_present": bool(requested) and set(requested).issubset(item_types),
        "recovery_demo_has_all_three_types": (
            profile != "recovery-demo" or set(EXPECTED_PHASES).issubset(requested)
        ),
        "all_items_recovered": bool(items) and all(
            item.get("status") == "recovered" for item in items.values()
        ),
        "phase_order_valid": True,
        "evidence_files_exist": True,
        "data_integrity_restored": True,
        "requirement_graph_version_advanced": True,
        "complete_run_audit_passed": bool(audit_run(root).get("passed")),
    }
    queue_path = root / "control/injection_requests.json"
    if queue_path.is_file():
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        checks["interactive_requests_applied"] = bool(queue.get("requests")) and all(
            request.get("status") == "accepted" and request.get("injection_id") in items
            for request in queue.get("requests", [])
        )
    evidence: list[dict] = []
    for injection_id, item in items.items():
        injection_type = str(item.get("type") or "")
        phases = [entry.get("phase") for entry in item.get("history", [])]
        expected = EXPECTED_PHASES.get(injection_type, [])
        phase_ok = bool(expected and _ordered_subsequence(expected, phases))
        checks["phase_order_valid"] &= phase_ok
        evidence_path = root / "injections" / injection_id / "evidence.json"
        exists = evidence_path.is_file()
        checks["evidence_files_exist"] &= exists
        payload = json.loads(evidence_path.read_text(encoding="utf-8")) if exists else {}
        if injection_type == "data_anomaly":
            checks["data_integrity_restored"] &= bool(
                payload.get("original_sha256")
                and payload.get("restored_sha256") == payload.get("original_sha256")
                and payload.get("corrupted_sha256") != payload.get("original_sha256")
            )
        if injection_type == "requirement_change":
            checks["requirement_graph_version_advanced"] &= bool(
                int(payload.get("new_graph_version", 0))
                > int(payload.get("old_graph_version", 0))
            )
        evidence.append({
            "injection_id": injection_id,
            "type": injection_type,
            "status": item.get("status"),
            "target_node_id": item.get("target_node_id"),
            "phases": phases,
            "phase_order_valid": phase_ok,
            "evidence_ref": evidence_path.relative_to(root).as_posix(),
        })
    return {
        "schema_version": "1.0",
        "run_dir": root.as_posix(),
        "passed": all(checks.values()),
        "requested_types": requested,
        "checks": checks,
        "injections": evidence,
    }


def render_markdown(result: dict) -> str:
    lines = [
        "# 主工作流运行期注入与恢复证据", "",
        f"- 总体状态：**{'PASS' if result['passed'] else 'FAIL'}**",
        f"- 运行目录：`{result['run_dir']}`", "",
        "## 注入轨迹", "",
        "| 类型 | 目标节点 | 生命周期 | 最终状态 |",
        "|---|---|---|---|",
    ]
    labels = {
        "node_failure": "节点失效",
        "requirement_change": "需求变更",
        "data_anomaly": "数据异常",
    }
    for item in result["injections"]:
        phases = " → ".join(item["phases"])
        lines.append(
            f"| {labels.get(item['type'], item['type'])} | "
            f"`{item.get('target_node_id') or '-'}` | {phases} | "
            f"{item.get('status')} |"
        )
    lines.extend(["", "## 独立检查", ""])
    for name, passed in result["checks"].items():
        lines.append(f"- {'PASS' if passed else 'FAIL'}：`{name}`")
    lines.append("")
    return "\n".join(lines)


def write_injection_audit(run_dir: str | Path) -> dict:
    root = Path(run_dir)
    result = audit_runtime_injections(root)
    output = root / "injections"
    output.mkdir(parents=True, exist_ok=True)
    (output / "recovery_trace.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (output / "recovery_trace.md").write_text(
        render_markdown(result), encoding="utf-8",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = write_injection_audit(args.run_dir)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
