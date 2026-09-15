"""Deterministic execution of the finite AcceptanceContract vocabulary."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

from orchestration.validation.models import (
    AcceptanceContract,
    AcceptanceCriterionContract,
    RequirementLedger,
    RequirementLedgerEntry,
)


_MISSING = object()


def _split_reference(reference: str) -> tuple[str, str]:
    value = str(reference or "").strip().replace("\\", "/")
    if "#" in value:
        path, pointer = value.split("#", 1)
        return path, pointer if pointer.startswith("/") else "/" + pointer
    if "::" in value:
        path, field = value.split("::", 1)
        pointer = "/" + "/".join(
            part for part in field.replace("[", ".").replace("]", "").split(".")
            if part
        )
        return path, pointer
    return value, ""


def _safe_path(run_dir: Path, relative: str) -> Path | None:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts or not relative:
        return None
    resolved = (run_dir / candidate).resolve()
    try:
        resolved.relative_to(run_dir.resolve())
    except ValueError:
        return None
    return resolved


def _json_pointer(value: Any, pointer: str) -> Any:
    if not pointer:
        return value
    current = value
    for raw in pointer.lstrip("/").split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        try:
            current = current[int(part)] if isinstance(current, list) else current[part]
        except (KeyError, IndexError, TypeError, ValueError):
            return _MISSING
    return current


def _load_reference(run_dir: Path, reference: str) -> tuple[Any, str | None, str | None]:
    path_text, pointer = _split_reference(reference)
    path = _safe_path(run_dir, path_text)
    if path is None or not path.is_file():
        return _MISSING, None, f"产物不存在或路径不安全：{path_text or '<empty>'}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return _MISSING, path_text, f"JSON 无法解析：{path_text}（{exc}）"
    value = _json_pointer(payload, pointer)
    if value is _MISSING:
        return _MISSING, path_text, f"JSON Pointer 不存在：{path_text}#{pointer}"
    return value, path_text + (f"#{pointer}" if pointer else ""), None


def _hashes(run_dir: Path, references: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for reference in references:
        path_text, _ = _split_reference(reference)
        path = _safe_path(run_dir, path_text)
        if path and path.is_file():
            try:
                result[path_text] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
    return result


def _entry(
    criterion: AcceptanceCriterionContract,
    status: str,
    summary: str,
    *,
    evidence_refs: list[str] | None = None,
    run_dir: Path,
    observed: Any = None,
    expected: Any = None,
) -> RequirementLedgerEntry:
    refs = list(dict.fromkeys(evidence_refs or []))
    return RequirementLedgerEntry(
        criterion_id=criterion.criterion_id,
        requirement_id=criterion.requirement_id,
        statement=criterion.statement,
        mandatory=criterion.mandatory,
        severity=criterion.severity,
        method=criterion.method,
        target=criterion.target,
        producer_node_id=criterion.producer_node_id,
        status=status,
        summary=summary,
        evidence_refs=refs,
        evidence_hashes=_hashes(run_dir, refs),
        observed=observed,
        expected=expected,
    )


def _compare(left: Any, operator: str, right: Any) -> bool:
    operations = {
        "eq": lambda a, b: a == b,
        "ne": lambda a, b: a != b,
        "lt": lambda a, b: a < b,
        "le": lambda a, b: a <= b,
        "gt": lambda a, b: a > b,
        "ge": lambda a, b: a >= b,
    }
    if operator not in operations:
        raise ValueError(f"不支持的比较运算符：{operator}")
    return bool(operations[operator](left, right))


def _evaluate_evidence(
    criterion: AcceptanceCriterionContract,
    run_dir: Path,
) -> RequirementLedgerEntry:
    candidates = sorted(run_dir.glob(
        "artifacts/tool_results/*/evidence_verification.json"
    ))
    if not candidates:
        return _entry(
            criterion, "unverified", "缺少独立 EvidenceVerifier 记录",
            run_dir=run_dir,
        )
    record_path = candidates[-1]
    reference = record_path.relative_to(run_dir).as_posix()
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _entry(
            criterion, "unverified", f"EvidenceVerifier 记录不可解析：{exc}",
            evidence_refs=[reference], run_dir=run_dir,
        )
    claims = list((record.get("decision") or {}).get("claims") or [])
    producer = criterion.producer_node_id
    matching = [
        claim for claim in claims
        if producer and producer in (claim.get("source_node_ids") or [])
    ]
    claim_id = str(criterion.params.get("claim_id") or "").strip()
    if claim_id:
        matching = [
            claim for claim in matching
            if str(claim.get("claim_id") or "") == claim_id
        ]
    keywords = [str(item).lower() for item in criterion.params.get("keywords", [])]
    if keywords:
        matching = [
            claim for claim in matching
            if all(word in str(claim.get("claim", "")).lower() for word in keywords)
        ]
    supported = [claim for claim in matching if claim.get("verdict") == "supported"]
    verified_catalog_refs = {
        str(item.get("evidence_ref"))
        for item in record.get("catalog", [])
        if item.get("verified") and item.get("evidence_ref")
    }
    supported = [
        claim for claim in supported
        if claim.get("evidence_refs")
        and set(map(str, claim.get("evidence_refs", []))).issubset(
            verified_catalog_refs
        )
    ]
    refs = [reference, *[
        str(ref) for claim in supported for ref in claim.get("evidence_refs", [])
    ]]
    # One contradicted claim makes the global verifier gate fail, but it must
    # not erase independently supported claims from unrelated producers.  The
    # failed verifier node remains a separate blocking issue and is routed to
    # the responsible producer by the repair loop.
    if supported:
        return _entry(
            criterion, "passed", "独立证据记录包含责任节点的受支持事实",
            evidence_refs=refs, run_dir=run_dir,
            observed=[claim.get("claim") for claim in supported],
        )
    if record.get("passed") is not True:
        return _entry(
            criterion, "failed", "独立证据核验总体未通过",
            evidence_refs=refs, run_dir=run_dir,
        )
    return _entry(
        criterion, "unverified", "没有找到与责任节点绑定的受支持原子事实",
        evidence_refs=[reference], run_dir=run_dir,
    )


def _evaluate(
    criterion: AcceptanceCriterionContract,
    run_dir: Path,
) -> RequirementLedgerEntry:
    if criterion.compilation_status != "ready":
        return _entry(
            criterion, "unverified",
            criterion.compilation_issue or "验收条件不可执行",
            run_dir=run_dir,
        )
    if criterion.method == "evidence_supported":
        return _evaluate_evidence(criterion, run_dir)
    if criterion.method == "artifact_exists":
        path_text, _ = _split_reference(criterion.target)
        path = _safe_path(run_dir, path_text)
        passed = bool(path and path.is_file())
        return _entry(
            criterion, "passed" if passed else "failed",
            "目标产物存在" if passed else "目标产物不存在",
            evidence_refs=[path_text] if passed else [], run_dir=run_dir,
            observed=passed, expected=True,
        )
    if criterion.method == "json_parseable":
        value, reference, error = _load_reference(run_dir, criterion.target)
        return _entry(
            criterion, "failed" if error else "passed",
            error or "JSON 产物可严格解析",
            evidence_refs=[reference] if reference else [], run_dir=run_dir,
            observed=None if error else type(value).__name__, expected="valid_json",
        )
    if criterion.method == "required_fields":
        base, target_pointer = _split_reference(criterion.target)
        fields = list(criterion.params.get("fields") or [])
        if target_pointer and not fields:
            fields = [target_pointer]
        if not fields:
            return _entry(
                criterion, "unverified", "required_fields 缺少 fields 参数",
                run_dir=run_dir,
            )
        root, reference, error = _load_reference(run_dir, base)
        if error:
            return _entry(
                criterion, "failed", error, run_dir=run_dir,
            )
        missing = [field for field in fields if _json_pointer(root, field) is _MISSING]
        return _entry(
            criterion, "failed" if missing else "passed",
            "缺少必需字段：" + ", ".join(missing) if missing else "必需字段完整",
            evidence_refs=[reference] if reference else [], run_dir=run_dir,
            observed={"missing": missing}, expected={"fields": fields},
        )
    if criterion.method in {"record_count_compare", "numeric_compare"}:
        params = criterion.params
        left_ref = str(params.get("left") or criterion.target)
        left, left_evidence, left_error = _load_reference(run_dir, left_ref)
        right_ref = params.get("right")
        if right_ref is not None:
            right, right_evidence, right_error = _load_reference(
                run_dir, str(right_ref),
            )
        else:
            right = params.get("value")
            right_evidence, right_error = None, None
        if left_error or right_error:
            return _entry(
                criterion, "failed", left_error or right_error or "比较值不可用",
                evidence_refs=[item for item in [left_evidence, right_evidence] if item],
                run_dir=run_dir,
            )
        if criterion.method == "record_count_compare":
            left = len(left) if isinstance(left, (list, dict)) else left
            right = len(right) if isinstance(right, (list, dict)) else right
        operator = str(params.get("operator", "eq"))
        try:
            passed = _compare(left, operator, right)
        except (TypeError, ValueError) as exc:
            return _entry(
                criterion, "unverified", str(exc), run_dir=run_dir,
                observed=left, expected=right,
            )
        return _entry(
            criterion, "passed" if passed else "failed",
            f"比较结果：{left!r} {operator} {right!r}",
            evidence_refs=[item for item in [left_evidence, right_evidence] if item],
            run_dir=run_dir, observed=left, expected=right,
        )
    return _entry(
        criterion, "unverified", f"不支持的验收方法：{criterion.method}",
        run_dir=run_dir,
    )


def run_acceptance_contract(
    run_dir: str | Path,
    contract: AcceptanceContract,
    *,
    phase: str = "business",
) -> RequirementLedger:
    started = datetime.now().isoformat(timespec="seconds")
    root = Path(run_dir)
    criteria = [item for item in contract.criteria if item.phase == phase]
    entries = [_evaluate(item, root) for item in criteria]
    blocking = [
        item for item in entries
        if item.mandatory and item.severity == "blocking"
    ]
    failed = [item.criterion_id for item in blocking if item.status == "failed"]
    unverified = [
        item.criterion_id for item in blocking if item.status == "unverified"
    ]
    if failed:
        status = "failed"
    elif unverified or not entries:
        status = "unverified"
    else:
        status = "passed"
    return RequirementLedger(
        status=status,
        phase=phase,
        entries=entries,
        mandatory_requirements_passed=not failed and not unverified,
        failed_blocking_criteria=failed,
        unverified_blocking_criteria=unverified,
        evidence_refs=list(dict.fromkeys(
            ref for item in entries for ref in item.evidence_refs
        )),
        started_at=started,
        finished_at=datetime.now().isoformat(timespec="seconds"),
    )


def merge_requirement_ledgers(
    *ledgers: RequirementLedger | None,
) -> RequirementLedger:
    """Combine phase ledgers into the single final delivery gate."""
    started = datetime.now().isoformat(timespec="seconds")
    entries = [
        entry for ledger in ledgers if ledger is not None
        for entry in ledger.entries
    ]
    blocking = [
        item for item in entries
        if item.mandatory and item.severity == "blocking"
    ]
    failed = [item.criterion_id for item in blocking if item.status == "failed"]
    unverified = [
        item.criterion_id for item in blocking if item.status == "unverified"
    ]
    if failed:
        status = "failed"
    elif unverified or not entries:
        status = "unverified"
    else:
        status = "passed"
    return RequirementLedger(
        status=status,
        phase="all",
        entries=entries,
        mandatory_requirements_passed=not failed and not unverified,
        failed_blocking_criteria=failed,
        unverified_blocking_criteria=unverified,
        evidence_refs=list(dict.fromkeys(
            ref for item in entries for ref in item.evidence_refs
        )),
        started_at=started,
        finished_at=datetime.now().isoformat(timespec="seconds"),
    )


def write_acceptance_outputs(
    run_dir: str | Path,
    contract: AcceptanceContract,
    ledger: RequirementLedger,
) -> tuple[str, str, str]:
    root = Path(run_dir)
    review = root / "review"
    review.mkdir(parents=True, exist_ok=True)
    contract_path = review / "acceptance_contract.json"
    ledger_path = review / "requirement_ledger.json"
    markdown_path = review / "requirement_ledger.md"
    contract_path.write_text(contract.model_dump_json(indent=2), encoding="utf-8")
    ledger_path.write_text(ledger.model_dump_json(indent=2), encoding="utf-8")
    lines = [
        "# 需求验收账本", "",
        f"- 阶段：{ledger.phase}",
        f"- 状态：**{ledger.status.upper()}**", "",
        "## 验收项", "",
    ]
    for item in ledger.entries:
        lines.append(
            f"- **{item.criterion_id}** / {item.requirement_id}: "
            f"{item.status} — {item.summary}"
        )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return (
        contract_path.relative_to(root).as_posix(),
        ledger_path.relative_to(root).as_posix(),
        markdown_path.relative_to(root).as_posix(),
    )
