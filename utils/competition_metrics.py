"""Export an auditable cross-scenario metrics table from successful runs."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from typing import Any

try:
    from utils.run_audit import audit_run
except ModuleNotFoundError:  # Direct execution: python utils/competition_metrics.py
    from run_audit import audit_run


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _elapsed_seconds(state: dict[str, Any]) -> float | None:
    try:
        started = datetime.fromisoformat(str(state["started_at"]))
        finished = datetime.fromisoformat(str(state["finished_at"]))
    except (KeyError, TypeError, ValueError):
        return None
    return round(max(0.0, (finished - started).total_seconds()), 3)


def collect_metrics(run_dir: str | Path) -> dict[str, Any]:
    root = Path(run_dir)
    audit = audit_run(root)
    state = _load(root / "run_state.json")
    spec = _load(root / "task_spec.json")
    model = state.get("model_calls") or {}
    communication = state.get("communication") or {}
    plan = state.get("plan") or {}
    execution = state.get("execution") or {}
    recovery = state.get("recovery") or {}
    business_repairs = recovery.get("business_repairs") or {}
    horizon = state.get("horizon") or {}
    return {
        "run_id": root.name,
        "run_dir": root.as_posix(),
        "task_name": spec.get("task_name", ""),
        "task_type": spec.get("task_type", ""),
        "audit_passed": audit["passed"],
        "outcome": state.get("outcome"),
        "wall_time_seconds": _elapsed_seconds(state),
        "model_calls": int(model.get("count", 0) or 0),
        "prompt_tokens": int(model.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(model.get("completion_tokens", 0) or 0),
        "total_tokens": int(model.get("total_tokens", 0) or 0),
        "model_duration_seconds": float(model.get("duration_seconds", 0.0) or 0.0),
        "prompt_compression_ratio": float(
            model.get("complete_prompt_compression_ratio", 1.0) or 1.0
        ),
        "node_count": int(plan.get("node_count", len(state.get("nodes") or {})) or 0),
        "edge_count": int(plan.get("edge_count", 0) or 0),
        "topology_density": float(plan.get("topology_density", 0.0) or 0.0),
        "context_deliveries": int(communication.get("context_deliveries", 0) or 0),
        "full_history_broadcasts": int(
            communication.get("full_history_broadcasts", 0) or 0
        ),
        "execution_attempts": int(execution.get("attempts", 0) or 0),
        "business_repair_rounds": int(business_repairs.get("rounds_used", 0) or 0),
        "executor_switches": int(recovery.get("executor_switches", 0) or 0),
        "replans": int(recovery.get("replans", 0) or 0),
        "horizon_epochs": int(horizon.get("epoch", 0) or 0),
        "logical_steps": int(horizon.get("logical_step_count", 0) or 0),
    }


def build_comparison(run_dirs: list[str | Path]) -> dict[str, Any]:
    if len(run_dirs) < 2:
        raise ValueError("competition comparison requires at least two runs")
    runs = [collect_metrics(path) for path in run_dirs]
    failed = [item["run_id"] for item in runs if not item["audit_passed"]]
    if failed:
        raise ValueError(
            "refusing to publish failed runs as competition evidence: "
            + ", ".join(failed)
        )
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run_count": len(runs),
        "successful_run_count": len(runs),
        "success_rate": 1.0,
        "cross_domain": len({item["task_type"] for item in runs}) >= 2,
        "runs": runs,
        "totals": {
            "model_calls": sum(item["model_calls"] for item in runs),
            "total_tokens": sum(item["total_tokens"] for item in runs),
            "wall_time_seconds": round(sum(
                item["wall_time_seconds"] or 0.0 for item in runs
            ), 3),
            "business_repair_rounds": sum(
                item["business_repair_rounds"] for item in runs
            ),
        },
    }


def render_markdown(comparison: dict[str, Any]) -> str:
    columns = [
        ("run_id", "运行"), ("task_type", "场景类型"),
        ("wall_time_seconds", "总耗时(s)"), ("model_calls", "模型调用"),
        ("total_tokens", "Token"), ("node_count", "节点"),
        ("edge_count", "边"), ("topology_density", "拓扑密度"),
        ("business_repair_rounds", "业务修复轮次"),
        ("logical_steps", "逻辑步数"),
    ]
    lines = [
        "# 跨场景运行指标", "",
        f"- 通过最终审计：{comparison['successful_run_count']}/{comparison['run_count']}",
        f"- 跨领域：{'是' if comparison['cross_domain'] else '否'}", "",
        "| " + " | ".join(label for _, label in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for run in comparison["runs"]:
        lines.append("| " + " | ".join(str(run[key]) for key, _ in columns) + " |")
    lines.extend([
        "", "所有行均由 `utils/run_audit.py` 的完整成功闸门确认后导出；",
        "历史失败运行不会进入本表。", "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dirs", nargs="+", help="at least two audited run directories")
    parser.add_argument("--output-dir", default="evidence/metrics")
    args = parser.parse_args()
    try:
        comparison = build_comparison(args.run_dirs)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "cross_scenario_metrics.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (output / "cross_scenario_metrics.md").write_text(
        render_markdown(comparison), encoding="utf-8",
    )
    print(output / "cross_scenario_metrics.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
