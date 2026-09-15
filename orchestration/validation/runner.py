"""Framework runner and persistence for independent business validation."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import time
import importlib

from orchestration.graph.task_graph import TaskGraph
from orchestration.validation.acceptance_contract import (
    compile_acceptance_contract,
)
from orchestration.validation.acceptance_runner import (
    run_acceptance_contract,
    write_acceptance_outputs,
)

from orchestration.validation.builtin_validators import (
    register_builtin_validators,
)
from orchestration.validation.models import (
    BusinessCheckResult,
    BusinessValidationContext,
    BusinessValidationResult,
)
from orchestration.validation.registry import BusinessValidatorRegistry


def default_business_validation_policy(task_spec: dict) -> dict:
    """Return a conservative policy for legacy TaskSpecs.

    Code-producing tasks require independent checks.  Non-code tasks remain
    optional until their TaskSpec explicitly declares a stronger contract.
    """
    code_mode = task_spec.get("code_policy", {}).get("mode", "none")
    results = list(
        task_spec.get("artifact_contract", {}).get("intermediate_artifacts", [])
    )
    json_result = next(
        (path for path in results if path.lower().endswith(".json")), None,
    )
    validators = [{
        "validator_id": "input_coverage",
        "validator_type": "input_coverage",
        "required": True,
        "config": {},
    }]
    if json_result:
        validators.append({
            "validator_id": "result_integrity",
            "validator_type": "result_integrity",
            "required": True,
            "config": {"target_artifact": json_result},
        })
    code_path = task_spec.get("artifacts", {}).get("code")
    if code_mode != "none" and code_path:
        validators.append({
            "validator_id": "code_shortcut_scan",
            "validator_type": "code_shortcut_scan",
            "required": True,
            "config": {"target_artifact": code_path},
        })
    return {
        "source": "framework_default",
        "mode": "required" if code_mode != "none" else "optional",
        "success_rule": "all",
        "validators": validators,
    }


def _write_outputs(
    run_dir: Path,
    result: BusinessValidationResult,
) -> tuple[str, str]:
    review_dir = run_dir / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    json_path = review_dir / "business_validation.json"
    md_path = review_dir / "business_validation.md"
    json_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    lines = [
        "# 独立业务验证",
        "",
        f"- 状态：**{result.status.upper()}**",
        f"- 模式：{result.mode}",
        f"- 验证器数量：{len(result.checks)}",
        "",
        "## 检查结果",
        "",
    ]
    for check in result.checks:
        lines.append(
            f"- **{check.check_id}**：{check.status} — {check.summary}"
        )
    if result.unresolved_validators:
        lines.extend([
            "", "## 未解析验证器", "",
            *[f"- {item}" for item in result.unresolved_validators],
        ])
    if result.failed_required_checks:
        lines.extend([
            "", "## 阻断项", "",
            *[f"- {item}" for item in result.failed_required_checks],
        ])
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return (
        json_path.relative_to(run_dir).as_posix(),
        md_path.relative_to(run_dir).as_posix(),
    )


def run_business_validation(
    run_dir: str | Path,
    task_spec: dict,
    execution_result: dict | None = None,
    *,
    registry: BusinessValidatorRegistry | None = None,
    persist: bool = True,
    task_graph: TaskGraph | dict | None = None,
) -> BusinessValidationResult:
    started = datetime.now().isoformat(timespec="seconds")
    path = Path(run_dir)
    policy = task_spec.get("business_validation")
    if not isinstance(policy, dict):
        policy = default_business_validation_policy(task_spec)
    mode = policy.get("mode", "required")
    if mode not in {"required", "optional", "disabled"}:
        raise ValueError(f"不支持的 business_validation.mode: {mode}")
    requirement_contract = task_spec.get("requirement_contract")
    if isinstance(requirement_contract, dict) and any(
        bool(item.get("mandatory", True)) and item.get("owner") == "planner"
        for item in requirement_contract.get("requirements", [])
    ):
        # A frozen mandatory requirement contract is never advisory.  This
        # upgrade is framework-owned and applies equally to code/non-code work.
        mode = "required"
    if mode == "disabled":
        result = BusinessValidationResult(
            status="not_applicable",
            mode="disabled",
            started_at=started,
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )
        if persist:
            _write_outputs(path, result)
        return result

    active_registry = registry or BusinessValidatorRegistry()
    if registry is None:
        register_builtin_validators(active_registry)
        # Domain validators are opt-in TaskSpec plugins. Core validators above
        # remain task-independent and never import an industry implementation.
        for declaration in task_spec.get("domain_validator_plugins", []):
            module_name = str(declaration.get("module", ""))
            class_name = str(declaration.get("class", ""))
            if not module_name or not class_name:
                continue
            try:
                plugin_class = getattr(importlib.import_module(module_name), class_name)
                active_registry.register(plugin_class())
            except (ImportError, AttributeError, TypeError, ValueError):
                continue
    checks: list[BusinessCheckResult] = []
    failed_required: list[str] = []
    failed_executed_required: list[str] = []
    unresolved: list[str] = []
    validators = list(policy.get("validators", []))
    for declaration in validators:
        validator_id = str(declaration.get("validator_id", "")).strip()
        required = bool(declaration.get("required", True))
        config = dict(declaration.get("config") or {})
        validator = active_registry.resolve(
            validator_id, task_spec, config,
        )
        if validator is None:
            unresolved.append(validator_id or "<missing-validator-id>")
            if required:
                failed_required.append(validator_id or "<missing-validator-id>")
            continue
        context = BusinessValidationContext(
            run_dir=str(path),
            task_spec=task_spec,
            execution_result=execution_result,
            artifact_paths=list(task_spec.get("required_artifacts", [])),
            validator_config=config,
        )
        call_started = time.monotonic()
        try:
            check = validator.validate(context)
        except Exception as exc:
            check = BusinessCheckResult(
                check_id=validator_id,
                validator_id=validator_id,
                status="error",
                summary="业务验证器执行异常",
                failed_checks=[{
                    "check": "validator_execution",
                    "reason": str(exc),
                }],
                duration_seconds=time.monotonic() - call_started,
            )
        checks.append(check)
        if required and check.status != "passed":
            failed_required.append(validator_id)
            failed_executed_required.append(validator_id)

    requirement_ledger = None
    if isinstance(requirement_contract, dict):
        graph = task_graph
        if graph is None:
            graph_path = path / "task_graph.json"
            if graph_path.is_file():
                try:
                    graph = json.loads(graph_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    graph = None
        if isinstance(graph, dict):
            try:
                graph = TaskGraph.model_validate(graph)
            except Exception:
                graph = None
        if isinstance(graph, TaskGraph):
            # Historical task_graph.json files predate requirement ownership.
            # Recover the immutable mapping from the persisted final PlanIR.
            plan_path = path / "planning" / "plan_ir_final.json"
            if plan_path.is_file():
                try:
                    plan_nodes = {
                        str(item.get("node_id")): item
                        for item in json.loads(
                            plan_path.read_text(encoding="utf-8")
                        ).get("nodes", [])
                    }
                    for node in graph.nodes:
                        source = plan_nodes.get(node.node_id, {})
                        if not node.requirement_ids:
                            node.requirement_ids = list(
                                source.get("requirement_ids", [])
                            )
                        if not node.acceptance_refs:
                            node.acceptance_refs = list(
                                source.get("acceptance_refs", [])
                            )
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    pass
            acceptance_contract = compile_acceptance_contract(
                requirement_contract, graph, task_spec,
            )
            requirement_ledger = run_acceptance_contract(
                path, acceptance_contract, phase="business",
            )
            compiled_by_id = {
                item.criterion_id: item
                for item in acceptance_contract.criteria
            }
            failed_items = [
                {
                    "check": "requirement_acceptance",
                    "criterion_id": item.criterion_id,
                    "requirement_id": item.requirement_id,
                    "producer_node_id": (
                        item.producer_node_id
                        if compiled_by_id[item.criterion_id].compilation_status
                        == "ready"
                        else None
                    ),
                    "path": item.target,
                    "reason": item.summary,
                    "status": item.status,
                }
                for item in requirement_ledger.entries
                if item.mandatory and item.severity == "blocking"
                and item.status != "passed"
            ]
            acceptance_check = BusinessCheckResult(
                check_id="acceptance_contract",
                validator_id="acceptance_contract",
                status=requirement_ledger.status,
                summary=(
                    "全部强制需求已获得独立验收证据"
                    if requirement_ledger.status == "passed"
                    else "仍有强制需求失败或未经验证"
                ),
                checked_items=[
                    item.model_dump(mode="json")
                    for item in requirement_ledger.entries
                ],
                failed_checks=failed_items,
                evidence_refs=requirement_ledger.evidence_refs,
            )
            checks.append(acceptance_check)
            if requirement_ledger.status != "passed":
                failed_required.append("acceptance_contract")
                failed_executed_required.append("acceptance_contract")
            if persist:
                write_acceptance_outputs(
                    path, acceptance_contract, requirement_ledger,
                )
        else:
            unresolved.append("acceptance_contract")
            failed_required.append("acceptance_contract")

    if failed_executed_required:
        status = "failed"
    elif unresolved or not checks:
        status = "unverified"
    elif all(check.status in {"passed", "not_applicable"} for check in checks):
        status = "passed"
    elif any(check.status == "error" for check in checks):
        status = "error"
    else:
        status = "unverified"
    evidence = list(dict.fromkeys(
        reference for check in checks for reference in check.evidence_refs
    ))
    result = BusinessValidationResult(
        status=status,
        mode=mode,
        checks=checks,
        failed_required_checks=list(dict.fromkeys(failed_required)),
        unresolved_validators=unresolved,
        evidence_refs=evidence,
        requirement_ledger=requirement_ledger,
        started_at=started,
        finished_at=datetime.now().isoformat(timespec="seconds"),
    )
    if persist:
        _write_outputs(path, result)
    return result
