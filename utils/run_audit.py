"""Audit one workflow run against the final delivery gates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def latest_run(root: Path) -> Path:
    candidates = sorted(
        path for path in (root / "outputs" / "runs").glob("*")
        if (path / "run_state.json").is_file()
    )
    if not candidates:
        raise FileNotFoundError("no run_state.json found under outputs/runs")
    return candidates[-1]


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def audit_run(run_dir: str | Path) -> dict[str, Any]:
    root = Path(run_dir)
    state_path = root / "run_state.json"
    if not state_path.is_file():
        raise FileNotFoundError(f"missing {state_path}")
    state = _load_json(state_path)
    business = state.get("business_validation") or {}
    requirement = state.get("requirement_acceptance") or {}
    review = state.get("review") or {}
    nodes = state.get("nodes") or {}
    failed_nodes = sorted(
        node_id for node_id, node in nodes.items()
        if isinstance(node, dict) and node.get("status") in {"failed", "blocked"}
    )
    unresolved = [
        item for item in state.get("blocking_issues", [])
        if isinstance(item, dict) and not item.get("resolved", False)
    ]
    report_exists = (root / "final_report.md").is_file()
    evidence_path = (
        root / "artifacts" / "tool_results" / "evidence_verification"
        / "evidence_verification.json"
    )
    evidence_passed = False
    evidence_error = None
    if evidence_path.is_file():
        try:
            evidence_passed = bool(_load_json(evidence_path).get("passed"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            evidence_error = str(exc)

    checks = {
        "outcome_success": state.get("outcome") == "success",
        "finished": state.get("finished") is True,
        "business_validation_passed": business.get("status") == "passed",
        "requirement_acceptance_passed": requirement.get("status") == "passed",
        "review_allows_report": review.get("can_generate_final_report") is True,
        "final_report_exists": report_exists,
        "evidence_verification_passed": evidence_passed,
        "no_failed_or_blocked_nodes": not failed_nodes,
        "no_unresolved_blocking_issues": not unresolved,
    }
    return {
        "run_dir": root.as_posix(),
        "passed": all(checks.values()),
        "checks": checks,
        "failed_nodes": failed_nodes,
        "unresolved_blocking_issues": [
            {
                "issue_id": item.get("issue_id"),
                "source": item.get("source"),
                "description": item.get("description"),
            }
            for item in unresolved
        ],
        "evidence_error": evidence_error,
        "review_issues": list(review.get("issues") or []),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", nargs="?", help="run directory; defaults to latest")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    run_dir = Path(args.run_dir) if args.run_dir else latest_run(project_root)
    result = audit_run(run_dir)
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Run: {result['run_dir']}")
        for name, passed in result["checks"].items():
            print(f"[{'PASS' if passed else 'FAIL'}] {name}")
        if result["failed_nodes"]:
            print("Failed nodes: " + ", ".join(result["failed_nodes"]))
        for issue in result["unresolved_blocking_issues"][:10]:
            print(f"Blocker: {issue.get('description')}")
        print("FINAL: " + ("PASS" if result["passed"] else "FAIL"))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
