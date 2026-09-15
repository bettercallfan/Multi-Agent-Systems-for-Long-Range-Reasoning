"""Deterministic, task-local validation for the MathorCup D packing demo."""
from __future__ import annotations

from collections import Counter, defaultdict
import json
import math
import re
import time
from pathlib import Path
from typing import Any

from orchestration.validation.models import BusinessCheckResult, BusinessValidationContext


def _source(ctx: BusinessValidationContext) -> dict[str, Any]:
    try:
        data = json.loads((Path(ctx.run_dir) / ctx.normalized_input_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return next((x for x in data.get("structured_sources", []) if x.get("name") == "attachment1.docx"), {})


def _inputs(ctx: BusinessValidationContext) -> tuple[dict[str, dict], dict[str, dict]]:
    source = _source(ctx)
    cargo: dict[str, dict] = {}
    for table in source.get("tables", []):
        rows = table.get("rows", [])
        if not rows or str(rows[0][0]).strip() != "货物编号":
            continue
        for row in rows[1:]:
            dims = re.findall(r"\d+(?:\.\d+)?", str(row[2])) if len(row) >= 5 else []
            try:
                if len(dims) == 3:
                    cargo[str(row[0]).strip()] = {"type": str(row[1]).strip(),
                        "dims": tuple(map(float, dims)), "weight": float(row[3]), "quantity": int(row[4])}
            except (TypeError, ValueError):
                pass
    text = str(source.get("content", ""))
    vehicles: dict[str, dict] = {}
    for key, number in (("V1", "1"), ("V2", "2")):
        match = re.search(rf"车型\s*{number}.*?长\s*(\d+)cm.*?宽\s*(\d+)cm.*?高\s*(\d+)cm.*?额定载重：(\d+)kg.*?成本：(\d+)\s*元/趟", text, re.S)
        if match:
            length, width, height, weight, cost = map(float, match.groups())
            vehicles[key] = {"length": length, "width": width, "height": height,
                             "weight": weight, "cost": cost}
    return cargo, vehicles


def _vehicle_type(item: dict) -> str:
    raw = str(item.get("vehicle_type") or item.get("vehicle_model") or "").upper()
    match = re.search(r"V[12]", raw + " " + str(item.get("vehicle_id", "")).upper())
    return match.group(0) if match else ""


def _overlap(a: dict, b: dict) -> bool:
    return all(a[p] < b[p] + b[s] and a[p] + a[s] > b[p]
               for p, s in (("x", "length"), ("y", "width"), ("z", "height")))


class MathorCupPackingValidator:
    validator_id = "mathorcup_packing"
    validator_type = "domain"

    def supports(self, task_spec: dict, validator_config: dict) -> bool:
        names = {Path(str(x)).name for x in task_spec.get("input", {}).get("files", [])}
        return task_spec.get("task_type") == "math_modeling" and "attachment1.docx" in names

    def validate(self, context: BusinessValidationContext) -> BusinessCheckResult:
        started = time.monotonic()
        root = Path(context.run_dir)
        relative = str(context.validator_config.get("target_artifact", "artifacts/result.json"))
        failures: list[dict[str, Any]] = []
        checked: list[dict[str, Any]] = []
        try:
            payload = json.loads((root / relative).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            payload = {}
            failures.append({"check": "result_parseable", "artifact": relative, "reason": str(exc)})
        if not isinstance(payload, dict):
            payload = {}
            failures.append({"check": "result_object"})
        required = ["vehicle_scenarios", "multi_vehicle_scenarios", "placements", "vehicle_count",
                    "total_cost", "space_utilization_rate", "load_utilization_rate", "validation"]
        for field in required:
            if field not in payload:
                failures.append({"check": "required_field", "field": field})
        for audit_name in ("result_complete.json", "cost_comparison.json"):
            try:
                audit_value = json.loads((root / "artifacts" / audit_name).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                failures.append({"check": "audit_artifact", "artifact": f"artifacts/{audit_name}",
                                 "reason": str(exc)})
                audit_value = None
            if audit_name == "result_complete.json" and isinstance(audit_value, dict):
                if any(field not in audit_value for field in required):
                    failures.append({"check": "result_complete_schema", "missing":
                                     [field for field in required if field not in audit_value]})
                audit_placements = audit_value.get("placements", [])
                result_placements = payload.get("placements", [])
                if (not isinstance(audit_placements, list) or not isinstance(result_placements, list)
                        or len(audit_placements) != len(result_placements)):
                    failures.append({"check": "result_complete_placements", "reason": "与 result.json 数量不一致"})
            if audit_name == "cost_comparison.json" and audit_value is not None:
                rows = audit_value.get("scenarios", []) if isinstance(audit_value, dict) else audit_value
                cost_fields = ("vehicle_count", "total_cost", "space_utilization_rate", "load_utilization_rate")
                if not isinstance(rows, list) or len(rows) < 2:
                    failures.append({"check": "cost_comparison_scenarios", "reason": "至少比较两个方案"})
                else:
                    for row_index, row in enumerate(rows):
                        missing = [field for field in cost_fields if not isinstance(row, dict) or field not in row]
                        if missing:
                            failures.append({"check": "cost_comparison_fields", "index": row_index, "missing": missing})
        placements = payload.get("placements", [])
        if not isinstance(placements, list):
            failures.append({"check": "placements_schema", "reason": "placements 必须是数组"})
            placements = []
        cargo, vehicles = _inputs(context)
        if not cargo or len(vehicles) != 2:
            failures.append({"check": "input_contract", "reason": "货物或车辆输入解析失败"})

        quantities: Counter[str] = Counter()
        instances: dict[tuple[str, str], str] = {}
        boxes_by_vehicle: defaultdict[tuple[str, str], list[dict]] = defaultdict(list)
        weights: defaultdict[tuple[str, str], float] = defaultdict(float)
        volumes: defaultdict[tuple[str, str], float] = defaultdict(float)
        for index, item in enumerate(placements):
            if not isinstance(item, dict):
                failures.append({"check": "placement_schema", "index": index})
                continue
            cargo_id = str(item.get("cargo_id", ""))
            if cargo_id not in cargo:
                failures.append({"check": "cargo_id", "index": index, "value": cargo_id})
                continue
            try:
                quantity = int(item.get("quantity", item.get("count", 1)))
                if quantity != 1:
                    raise ValueError
            except (TypeError, ValueError):
                failures.append({"check": "placement_quantity", "index": index,
                                 "reason": "每个带坐标 placement 必须代表 1 件"})
                continue
            missing = [x for x in ("vehicle_id", "scenario", "orientation") if not item.get(x)]
            if missing:
                failures.append({"check": "placement_required_fields", "index": index, "missing": missing})
            try:
                values = {x: float(item[x]) for x in ("x", "y", "z", "length", "width", "height")}
                if any(not math.isfinite(v) for v in values.values()):
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                failures.append({"check": "placement_numeric", "index": index})
                continue
            box = {**values, "index": index, "cargo_id": cargo_id,
                   "vehicle_id": str(item.get("vehicle_id", "")), "scenario": str(item.get("scenario", "")),
                   "vehicle_type": _vehicle_type(item)}
            if min(values[x] for x in ("length", "width", "height")) <= 0:
                failures.append({"check": "placement_dimensions", "index": index})
                continue
            actual = (box["length"], box["width"], box["height"])
            expected = cargo[cargo_id]["dims"]
            valid_dims = actual == expected if cargo[cargo_id]["type"] == "定向件" else sorted(actual) == sorted(expected)
            if not valid_dims:
                failures.append({"check": "orientation_dimensions", "index": index,
                                 "expected": expected, "actual": actual})
            vehicle_type = box["vehicle_type"]
            if vehicle_type not in vehicles:
                failures.append({"check": "vehicle_type", "index": index})
            else:
                vehicle = vehicles[vehicle_type]
                if (box["x"] < 0 or box["y"] < 0 or box["z"] < 0 or
                    box["x"] + box["length"] > vehicle["length"] or
                    box["y"] + box["width"] > vehicle["width"] or
                    box["z"] + box["height"] > vehicle["height"] - 3):
                    failures.append({"check": "boundary", "index": index, "vehicle": vehicle_type,
                                     "position": [box["x"], box["y"], box["z"]], "size": list(actual)})
                key = (box["scenario"], box["vehicle_id"])
                if key in instances and instances[key] != vehicle_type:
                    failures.append({"check": "vehicle_instance_type", "index": index})
                instances[key] = vehicle_type
                boxes_by_vehicle[key].append(box)
                weights[key] += cargo[cargo_id]["weight"]
                volumes[key] += math.prod(actual)
            quantities[cargo_id] += 1
            checked.append({"index": index, "cargo_id": cargo_id, "vehicle_id": box["vehicle_id"]})

        for cargo_id, spec in cargo.items():
            if quantities[cargo_id] != spec["quantity"]:
                failures.append({"check": "cargo_quantity_coverage", "cargo_id": cargo_id,
                                 "expected": spec["quantity"], "actual": quantities[cargo_id]})
        for key, boxes in boxes_by_vehicle.items():
            for pos, first in enumerate(boxes):
                for second in boxes[pos + 1:]:
                    if _overlap(first, second):
                        failures.append({"check": "aabb_overlap", "first": first["index"],
                                         "second": second["index"], "vehicle_id": key[1]})
                    fragile, upper = (first, second) if cargo[first["cargo_id"]]["type"] == "易碎件" else (second, first)
                    if cargo[fragile["cargo_id"]]["type"] == "易碎件" and upper["z"] >= fragile["z"] + fragile["height"] and (
                        fragile["x"] < upper["x"] + upper["length"] and fragile["x"] + fragile["length"] > upper["x"] and
                        fragile["y"] < upper["y"] + upper["width"] and fragile["y"] + fragile["width"] > upper["y"]):
                        failures.append({"check": "fragile_stacking", "fragile": fragile["index"], "upper": upper["index"]})
            vtype = instances.get(key)
            if vtype in vehicles and weights[key] > vehicles[vtype]["weight"]:
                failures.append({"check": "vehicle_weight", "scenario": key[0], "vehicle_id": key[1],
                                 "actual": weights[key], "limit": vehicles[vtype]["weight"]})

        vehicle_count = len(instances)
        total_cost = sum(vehicles[t]["cost"] for t in instances.values() if t in vehicles)
        if payload.get("vehicle_count") != vehicle_count:
            failures.append({"check": "vehicle_count", "expected": vehicle_count, "actual": payload.get("vehicle_count")})
        if not isinstance(payload.get("total_cost"), (int, float)) or abs(float(payload["total_cost"]) - total_cost) > 1e-6:
            failures.append({"check": "total_cost", "expected": total_cost, "actual": payload.get("total_cost")})
        volume_capacity = sum(math.prod((vehicles[t]["length"], vehicles[t]["width"], vehicles[t]["height"])) for t in instances.values() if t in vehicles)
        weight_capacity = sum(vehicles[t]["weight"] for t in instances.values() if t in vehicles)
        rates = {"space_utilization_rate": sum(volumes.values()) / volume_capacity if volume_capacity else 0.0,
                 "load_utilization_rate": sum(weights.values()) / weight_capacity if weight_capacity else 0.0}
        for field, expected in rates.items():
            value = payload.get(field)
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not 0 <= float(value) <= 1:
                failures.append({"check": "utilization_range", "field": field, "actual": value})
            elif abs(float(value) - expected) > 1e-6:
                failures.append({"check": "utilization_recompute", "field": field, "expected": expected, "actual": value})
        if not isinstance(payload.get("vehicle_scenarios"), list) or not payload.get("vehicle_scenarios"):
            failures.append({"check": "vehicle_scenarios", "reason": "至少一个单车型方案"})
        if not isinstance(payload.get("multi_vehicle_scenarios"), list) or not payload.get("multi_vehicle_scenarios"):
            failures.append({"check": "multi_vehicle_scenarios", "reason": "至少一个多车型方案"})

        recomputed = {"cargo_quantities": dict(quantities), "vehicle_count": vehicle_count,
                      "total_cost": total_cost, **rates}
        audit = {"validator": self.validator_id, "passed": not failures,
                 "checked_placement_count": len(checked), "failures": failures, "recomputed": recomputed}
        audit_path = root / "artifacts" / "constraint_validation_log.json"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        return BusinessCheckResult(check_id=self.validator_id, validator_id=self.validator_id,
            status="failed" if failures else "passed",
            summary="装箱领域确定性校验通过" if not failures else f"装箱领域校验发现 {len(failures)} 项错误",
            checked_items=checked, failed_checks=failures, recomputed_metrics=recomputed,
            evidence_refs=[relative, "artifacts/result_complete.json", "artifacts/constraint_validation_log.json",
                           "artifacts/cost_comparison.json", context.normalized_input_path],
            duration_seconds=time.monotonic() - started)
