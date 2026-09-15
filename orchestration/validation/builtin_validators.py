"""Task-independent validators supplied by the framework."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import time
from typing import Any

from orchestration.validation.models import (
    BusinessCheckResult,
    BusinessValidationContext,
)


def _safe_path(run_dir: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"不安全的验证路径: {relative}")
    return run_dir / candidate


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class InputCoverageValidator:
    validator_id = "input_coverage"
    validator_type = "input_coverage"

    def supports(self, task_spec: dict, validator_config: dict) -> bool:
        return True

    def validate(self, context: BusinessValidationContext) -> BusinessCheckResult:
        started = time.monotonic()
        run_dir = Path(context.run_dir)
        expected = list(context.task_spec.get("input", {}).get("files", []))
        normalized_path = _safe_path(run_dir, context.normalized_input_path)
        failed: list[dict[str, Any]] = []
        checked: list[dict[str, Any]] = []
        evidence: list[str] = []
        if not normalized_path.is_file():
            failed.append({
                "check": "normalized_input_exists",
                "reason": f"缺少 {context.normalized_input_path}",
            })
            normalized = {}
        else:
            evidence.append(context.normalized_input_path)
            try:
                normalized = _json(normalized_path)
            except (OSError, json.JSONDecodeError) as exc:
                normalized = {}
                failed.append({
                    "check": "normalized_input_parseable",
                    "reason": str(exc),
                })
        for source in expected:
            name = Path(source).name
            copied = run_dir / "inputs" / name
            exists = copied.is_file() or Path(source).is_file()
            checked.append({"input": name, "exists": exists})
            if not exists:
                failed.append({
                    "check": "required_input_exists",
                    "input": name,
                    "reason": "TaskSpec 声明的输入不存在",
                })
        serialized = json.dumps(normalized, ensure_ascii=False, default=str)
        referenced = [
            Path(source).name for source in expected
            if Path(source).name in serialized
        ]
        if expected and not referenced:
            failed.append({
                "check": "input_provenance_present",
                "reason": "normalized_input 未保留任何 TaskSpec 输入来源",
            })
        return BusinessCheckResult(
            check_id=self.validator_id,
            validator_id=self.validator_id,
            status="failed" if failed else "passed",
            summary=(
                "输入来源与标准化边界通过检查"
                if not failed else "输入来源或标准化边界不完整"
            ),
            checked_items=checked,
            failed_checks=failed,
            recomputed_metrics={
                "input_source_count": len(expected),
                "referenced_source_count": len(referenced),
                "input_record_count": None,
                "input_record_count_status": "unknown",
            },
            evidence_refs=evidence,
            duration_seconds=time.monotonic() - started,
        )


class ResultIntegrityValidator:
    validator_id = "result_integrity"
    validator_type = "result_integrity"

    def supports(self, task_spec: dict, validator_config: dict) -> bool:
        return bool(validator_config.get("target_artifact"))

    def validate(self, context: BusinessValidationContext) -> BusinessCheckResult:
        started = time.monotonic()
        run_dir = Path(context.run_dir)
        relative = str(context.validator_config["target_artifact"])
        path = _safe_path(run_dir, relative)
        failed: list[dict[str, Any]] = []
        checked: list[dict[str, Any]] = []
        payload: Any = None
        if not path.is_file():
            failed.append({
                "check": "result_exists", "reason": f"缺少 {relative}",
            })
        else:
            try:
                payload = _json(path)
            except (OSError, json.JSONDecodeError) as exc:
                failed.append({
                    "check": "result_parseable", "reason": str(exc),
                })
        if payload is not None:
            non_empty = bool(payload)
            checked.append({"check": "result_non_empty", "passed": non_empty})
            if not non_empty:
                failed.append({
                    "check": "result_non_empty", "reason": "结果 JSON 为空",
                })
            if isinstance(payload, dict):
                self_reported = str(payload.get("status", "")).lower()
                checked.append({
                    "check": "self_reported_status_not_authoritative",
                    "value": self_reported or None,
                    "authoritative": False,
                })
                if self_reported in {
                    "error", "failed", "failure", "warning", "partial",
                    "incomplete", "unverified", "fallback", "placeholder",
                }:
                    failed.append({
                        "check": "result_not_delivery_ready",
                        "reason": (
                            f"结果自报状态为 {self_reported}；该状态不能作为"
                            "完整业务交付"
                        ),
                    })
                shortcut_flags = (
                    "default_values_applied",
                    "fallback_used",
                    "placeholder_used",
                    "mock_data_used",
                    "dummy_data_used",
                    "estimated_values_applied",
                    "synthetic_data_used",
                )
                for flag in shortcut_flags:
                    active = payload.get(flag) is True
                    checked.append({
                        "check": "no_shortcut_result_flag",
                        "field": flag,
                        "active": active,
                    })
                    if active:
                        failed.append({
                            "check": "no_shortcut_result_flag",
                            "field": flag,
                            "reason": (
                                f"结果声明 {flag}=true，说明交付依赖默认值、"
                                "占位数据、估算或降级路径"
                            ),
                        })
                required_fields = list(
                    context.validator_config.get("required_fields", [])
                )
                for field in required_fields:
                    present = field in payload
                    checked.append({"field": field, "present": present})
                    if not present:
                        failed.append({
                            "check": "required_result_field",
                            "field": field,
                            "reason": "缺少框架声明的结果字段",
                        })
        return BusinessCheckResult(
            check_id=self.validator_id,
            validator_id=self.validator_id,
            status="failed" if failed else "passed",
            summary=(
                "结果产物通过独立完整性检查"
                if not failed else "结果产物未通过独立完整性检查"
            ),
            checked_items=checked,
            failed_checks=failed,
            evidence_refs=[relative] if path.is_file() else [],
            duration_seconds=time.monotonic() - started,
        )


class CodeShortcutScanner:
    validator_id = "code_shortcut_scan"
    validator_type = "code_shortcut_scan"

    _KEYWORD = re.compile(
        r"\b(dummy|mock|placeholder|fake|estimated|hardcoded|fallback)\b",
        re.IGNORECASE,
    )

    def supports(self, task_spec: dict, validator_config: dict) -> bool:
        return bool(validator_config.get("target_artifact"))

    @staticmethod
    def _line_item(
        shortcut_type: str,
        relative: str,
        line: int,
        evidence: str,
        severity: str,
    ) -> dict[str, Any]:
        return {
            "type": shortcut_type,
            "path": relative,
            "line": line,
            "evidence": evidence.strip()[:500],
            "severity": severity,
        }

    def validate(self, context: BusinessValidationContext) -> BusinessCheckResult:
        started = time.monotonic()
        run_dir = Path(context.run_dir)
        relative = str(context.validator_config["target_artifact"])
        path = _safe_path(run_dir, relative)
        failed: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        shortcuts: list[dict[str, Any]] = []
        if not path.is_file():
            failed.append({
                "check": "generated_code_exists",
                "reason": f"缺少 {relative}",
            })
            text = ""
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        try:
            tree = ast.parse(text) if text else None
        except SyntaxError as exc:
            tree = None
            failed.append({"check": "code_parseable", "reason": str(exc)})

        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler) and (
                    not node.body
                    or all(isinstance(item, ast.Pass) for item in node.body)
                ):
                    item = self._line_item(
                        "silent_exception", relative, node.lineno,
                        lines[node.lineno - 1] if lines else "", "warning",
                    )
                    shortcuts.append(item)
                    warnings.append(item)
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                    names = [
                        target.id for target in node.targets
                        if isinstance(target, ast.Name)
                    ]
                    source = lines[node.lineno - 1] if lines else ""
                    nearby = "\n".join(
                        lines[max(0, node.lineno - 2):min(len(lines), node.lineno + 1)]
                    )
                    if (
                        names
                        and isinstance(node.value.value, (int, float))
                        and any(name.lower().startswith(
                            ("total_", "aggregate_", "estimated_")
                        ) for name in names)
                        and self._KEYWORD.search(nearby)
                    ):
                        item = self._line_item(
                            "estimated_fixed_aggregate", relative,
                            node.lineno, source, "fatal",
                        )
                        shortcuts.append(item)
                        failed.append({
                            "check": "no_estimated_fixed_aggregate", **item,
                        })
                if isinstance(node, ast.If):
                    try:
                        condition = ast.unparse(node.test).lower()
                    except Exception:
                        condition = ""
                    missing_input_signal = (
                        ".empty" in condition
                        or " is none" in condition
                        or bool(re.search(
                            r"\bnot\s+\w*(?:data|input|records?|items?|rows?|"
                            r"products?|vehicles?|cargo|cargos|trucks?|sources?)\w*",
                            condition,
                        ))
                    )
                    if not missing_input_signal:
                        continue
                    body_tree = ast.Module(body=node.body, type_ignores=[])
                    returns_success = False
                    synthetic_assignments: list[ast.Assign] = []
                    for body_node in ast.walk(body_tree):
                        if isinstance(body_node, ast.Assign):
                            target_names = [
                                target.id
                                for target in body_node.targets
                                if isinstance(target, ast.Name)
                            ]
                            data_target = any(re.search(
                                r"(?:data|input|records?|items?|rows?|products?|"
                                r"vehicles?|cargo|cargos|trucks?|sources?)",
                                name.lower(),
                            ) for name in target_names)
                            nonempty_literal = (
                                isinstance(body_node.value, (ast.List, ast.Tuple))
                                and bool(body_node.value.elts)
                            ) or (
                                isinstance(body_node.value, ast.Dict)
                                and bool(body_node.value.keys)
                            )
                            if data_target and nonempty_literal:
                                synthetic_assignments.append(body_node)
                        if (
                            isinstance(body_node, ast.Return)
                            and isinstance(body_node.value, ast.Constant)
                            and body_node.value.value == 0
                        ):
                            returns_success = True
                        if (
                            isinstance(body_node, ast.Call)
                            and isinstance(body_node.func, ast.Attribute)
                            and body_node.func.attr == "exit"
                            and body_node.args
                            and isinstance(body_node.args[0], ast.Constant)
                            and body_node.args[0].value == 0
                        ):
                            returns_success = True
                        if isinstance(body_node, ast.Dict):
                            for key, value in zip(
                                body_node.keys, body_node.values,
                            ):
                                if (
                                    isinstance(key, ast.Constant)
                                    and str(key.value).lower() == "status"
                                    and isinstance(value, ast.Constant)
                                    and str(value.value).lower()
                                    in {"warning", "success", "ok", "passed"}
                                ):
                                    returns_success = True
                    if returns_success:
                        source = "\n".join(
                            lines[
                                max(0, node.lineno - 1):
                                min(len(lines), getattr(node, "end_lineno", node.lineno))
                            ]
                        )
                        item = self._line_item(
                            "missing_input_returns_success", relative,
                            node.lineno, source, "fatal",
                        )
                        shortcuts.append(item)
                        failed.append({
                            "check": "no_missing_input_returns_success",
                            **item,
                        })
                    for assignment in synthetic_assignments:
                        source = "\n".join(
                            lines[
                                max(0, assignment.lineno - 1):
                                min(
                                    len(lines),
                                    getattr(
                                        assignment,
                                        "end_lineno",
                                        assignment.lineno,
                                    ),
                                )
                            ]
                        )
                        item = self._line_item(
                            "synthetic_data_on_missing_input", relative,
                            assignment.lineno, source, "fatal",
                        )
                        shortcuts.append(item)
                        failed.append({
                            "check": "no_synthetic_data_on_missing_input",
                            **item,
                        })

        patterns = [
            (
                "declared_default_or_synthetic_result",
                re.compile(
                    r"['\"](?:default_values_applied|fallback_used|"
                    r"placeholder_used|mock_data_used|dummy_data_used|"
                    r"estimated_values_applied|synthetic_data_used)['\"]"
                    r"\s*:\s*True\b",
                    re.IGNORECASE,
                ),
                "fatal",
            ),
            (
                "default_data_on_missing_input",
                re.compile(
                    r"if\s+([A-Za-z_]\w*)\s+is\s+None"
                    r"(?:\s+or\s+\1\.empty)?.{0,200}"
                    r"\1\s*=\s*(?:[A-Za-z_]\w*\.)?DataFrame",
                    re.IGNORECASE | re.DOTALL,
                ),
                "fatal",
            ),
            (
                "partial_requirement_threshold",
                re.compile(
                    r"(?:>=|<=|>|<|==)\s*"
                    r"(?:total_\w+|aggregate_\w+|requirement\w*|demand\w*)"
                    r"\s*\*\s*0\.[0-9]+",
                    re.IGNORECASE | re.DOTALL,
                ),
                "fatal",
            ),
        ]
        for shortcut_type, pattern, severity in patterns:
            match = pattern.search(text)
            if not match:
                continue
            line = text[:match.start()].count("\n") + 1
            item = self._line_item(
                shortcut_type, relative, line, match.group(0), severity,
            )
            shortcuts.append(item)
            failed.append({"check": f"no_{shortcut_type}", **item})

        for index, line_text in enumerate(lines, start=1):
            if self._KEYWORD.search(line_text):
                item = self._line_item(
                    "shortcut_keyword", relative, index, line_text, "warning",
                )
                if not any(
                    existing["line"] == index and existing["path"] == relative
                    for existing in shortcuts
                ):
                    shortcuts.append(item)
                    warnings.append(item)

        return BusinessCheckResult(
            check_id=self.validator_id,
            validator_id=self.validator_id,
            status="failed" if failed else "passed",
            summary=(
                "生成代码未发现致命占位捷径"
                if not failed else "生成代码包含可能伪造业务成功的捷径"
            ),
            failed_checks=failed,
            warnings=warnings,
            detected_shortcuts=shortcuts,
            evidence_refs=[relative] if path.is_file() else [],
            duration_seconds=time.monotonic() - started,
        )


def register_builtin_validators(registry) -> None:
    registry.register(InputCoverageValidator())
    registry.register(ResultIntegrityValidator())
    registry.register(CodeShortcutScanner())
