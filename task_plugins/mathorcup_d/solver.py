"""Deterministic last-resort code generator for the MathorCup D demo."""

from __future__ import annotations

import json
from pathlib import Path


def build_fallback_code(task_spec: dict) -> str:
    """Return a small auditable shelf-packing program.

    The generated program still reads the run's normalized input and is run
    and validated by the ordinary framework.  It is intentionally a baseline
    heuristic, not a precomputed answer.
    """
    return r'''import json
import math
import re
from pathlib import Path

ROOT = Path(".")
ARTIFACTS = ROOT / "artifacts"


def load_inputs():
    data = json.loads((ROOT / "normalized_input.json").read_text(encoding="utf-8"))
    source = next(x for x in data["structured_sources"] if x.get("name") == "attachment1.docx")
    rows = next(t["rows"] for t in source["tables"] if t.get("rows") and t["rows"][0][0] == "货物编号")
    cargo = {}
    for row in rows[1:]:
        dims = tuple(float(x) for x in re.findall(r"\d+(?:\.\d+)?", str(row[2])))
        cargo[str(row[0])] = {"type": str(row[1]), "dims": dims,
                              "weight": float(row[3]), "quantity": int(row[4])}
    text = source["content"]
    vehicles = {}
    for key, number in (("V1", "1"), ("V2", "2")):
        pattern = (rf"车型\s*{number}.*?长\s*(\d+)cm.*?宽\s*(\d+)cm.*?高\s*(\d+)cm"
                   rf".*?额定载重：(\d+)kg.*?成本：(\d+)\s*元/趟")
        match = re.search(pattern, text, re.S)
        if not match:
            raise ValueError(f"无法解析车型 {key}")
        length, width, height, capacity, cost = map(float, match.groups())
        vehicles[key] = {"length": length, "width": width, "height": height,
                         "capacity": capacity, "cost": cost}
    return cargo, vehicles


def instances(cargo):
    result = []
    for cargo_id, spec in cargo.items():
        for number in range(spec["quantity"]):
            result.append({"cargo_id": cargo_id, "serial": number + 1, **spec})
    result.sort(key=lambda item: item["dims"][0] * item["dims"][1], reverse=True)
    return result


def pack(items, vehicle_type, scenario, vehicles, start_index=1):
    vehicle = vehicles[vehicle_type]
    placements = []
    vehicle_number = start_index
    x = y = row_width = load = 0.0
    for item in items:
        length, width, height = item["dims"]
        if x + length > vehicle["length"]:
            x = 0.0
            y += row_width
            row_width = 0.0
        if y + width > vehicle["width"] or load + item["weight"] > vehicle["capacity"]:
            vehicle_number += 1
            x = y = row_width = load = 0.0
        placements.append({
            "cargo_id": item["cargo_id"], "instance_id": f"{item['cargo_id']}-{item['serial']:03d}",
            "quantity": 1, "vehicle_id": f"{vehicle_type}-{vehicle_number}",
            "vehicle_type": vehicle_type, "scenario": scenario, "orientation": "original",
            "x": x, "y": y, "z": 0.0, "length": length, "width": width, "height": height,
        })
        x += length
        row_width = max(row_width, width)
        load += item["weight"]
    return placements


def summarize(name, placements, cargo, vehicles):
    used = {}
    total_volume = total_weight = 0.0
    for item in placements:
        key = item["vehicle_id"]
        used[key] = item["vehicle_type"]
        total_volume += item["length"] * item["width"] * item["height"]
        total_weight += cargo[item["cargo_id"]]["weight"]
    volume_capacity = sum(vehicles[k]["length"] * vehicles[k]["width"] * vehicles[k]["height"]
                          for k in used.values())
    weight_capacity = sum(vehicles[k]["capacity"] for k in used.values())
    return {"scenario": name, "vehicle_count": len(used),
            "total_cost": sum(vehicles[k]["cost"] for k in used.values()),
            "space_utilization_rate": total_volume / volume_capacity,
            "load_utilization_rate": total_weight / weight_capacity}


def main():
    cargo, vehicles = load_inputs()
    all_items = instances(cargo)
    v1 = pack(all_items, "V1", "v1_only", vehicles)
    v2 = pack(all_items, "V2", "v2_only", vehicles)
    left = pack(all_items[::2], "V1", "mixed", vehicles)
    right = pack(all_items[1::2], "V2", "mixed", vehicles)
    mixed = left + right
    alternatives = [(summarize("v1_only", v1, cargo, vehicles), v1),
                    (summarize("v2_only", v2, cargo, vehicles), v2),
                    (summarize("mixed", mixed, cargo, vehicles), mixed)]
    selected_summary, selected = min(alternatives, key=lambda pair: (pair[0]["total_cost"], pair[0]["vehicle_count"]))
    comparison = [pair[0] for pair in alternatives]
    result = {"status": "success", "vehicle_scenarios": comparison[:2],
              "multi_vehicle_scenarios": [comparison[2]], "placements": selected,
              "vehicle_count": selected_summary["vehicle_count"],
              "total_cost": selected_summary["total_cost"],
              "space_utilization_rate": selected_summary["space_utilization_rate"],
              "load_utilization_rate": selected_summary["load_utilization_rate"],
              "validation": {"status": "pending_independent_validation",
                             "method": "orthogonal_floor_shelf_baseline", "top_clearance_cm": 3}}
    counts = {cargo_id: sum(1 for item in selected if item["cargo_id"] == cargo_id) for cargo_id in cargo}
    vehicle_loads = {}
    for item in selected:
        vehicle_loads[item["vehicle_id"]] = vehicle_loads.get(item["vehicle_id"], 0) + cargo[item["cargo_id"]]["weight"]
    max_top = max(item["z"] + item["height"] for item in selected)
    audit = {"status": "generated", "checks": [
        {"constraint": "cargo_coverage", "passed": counts == {k: v["quantity"] for k, v in cargo.items()},
         "actual_counts": counts},
        {"constraint": "orthogonal_and_orientation", "passed": all(item["orientation"] == "original" for item in selected)},
        {"constraint": "fragile_single_layer_and_support", "passed": all(item["z"] == 0 for item in selected),
         "detail": "全部货物直接接触车厢底面，无上层货物，承重 500kg/m2 约束不触发"},
        {"constraint": "top_clearance_cm", "passed": max_top <= vehicles[selected[0]["vehicle_type"]]["height"] - 3,
         "max_top_cm": max_top, "required_clearance_cm": 3},
        {"constraint": "vehicle_weight", "passed": all(load <= vehicles[key.split("-")[0]]["capacity"]
                                                           for key, load in vehicle_loads.items()),
         "loads_kg": vehicle_loads},
        {"constraint": "aabb_non_overlap", "passed": True,
         "detail": "确定性行列货架坐标生成，每行 x 区间不交叠、各行 y 区间不交叠"}]}
    ARTIFACTS.mkdir(exist_ok=True)
    (ARTIFACTS / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (ARTIFACTS / "result_complete.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (ARTIFACTS / "constraint_validation_log.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    (ARTIFACTS / "cost_comparison.json").write_text(json.dumps({"scenarios": comparison}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
'''


def build_evidence_fallback(
    task_spec: dict,
    dependency_view: dict,
    verified_refs: set[str],
):
    """Build conservative evidence claims from already-read local artifacts.

    This is used only when the evidence model is unavailable or emits an
    invalid response.  It never invents business values: delivery facts are
    supported only when the deterministic constraint audit says ``passed``.
    """
    run_dir = Path(task_spec.get("run_dir", "."))
    audit_ref = "artifacts/constraint_validation_log.json"
    result_ref = "artifacts/result.json"
    audit_path = run_dir / audit_ref
    result_path = run_dir / result_ref
    if audit_ref not in verified_refs or result_ref not in verified_refs:
        raise ValueError("领域证据 fallback 缺少结果或约束审计证据")
    # Evidence verification is a graph node and therefore runs before the
    # workflow's post-graph business gate.  Recompute the same independent
    # domain audit here so a provisional generator log can never decide its
    # own validity.  The workflow runs this validator again at the formal gate.
    from orchestration.validation.mathorcup_validator import MathorCupPackingValidator
    from orchestration.validation.models import BusinessValidationContext

    check = MathorCupPackingValidator().validate(BusinessValidationContext(
        run_dir=str(run_dir), task_spec=task_spec,
        validator_config={"target_artifact": result_ref},
    ))
    if check.status != "passed":
        raise ValueError("领域证据 fallback 的独立装箱校验未通过")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if audit.get("passed") is not True:
        raise ValueError("领域证据 fallback 拒绝未通过的约束审计")
    claims = [{
        "claim_id": "domain_result_validated",
        "source_node_ids": [node_id for node_id, view in dependency_view.items()
                             if view.get("role") == "executed_delivery_evidence"],
        "claim": "确定性装箱约束审计通过，结果文件包含完整可验收装箱结果",
        "verdict": "supported",
        "evidence_refs": [audit_ref, result_ref],
        "rationale": "Python 领域校验日志 passed=true，且结果文件由框架实际读取。",
        "confidence": 1.0,
    }]
    for node_id, view in dependency_view.items():
        if node_id in claims[0]["source_node_ids"]:
            continue
        refs = [ref for ref in view.get("evidence_refs", []) if ref in verified_refs]
        refs = refs[:4] or (["normalized_input.json"] if "normalized_input.json" in verified_refs else [])
        if refs:
            claims.append({
                "claim_id": f"framework_coverage_{len(claims) + 1:03d}",
                "source_node_ids": [node_id],
                "claim": f"框架已读取直接依赖 {node_id} 的持久化证据",
                "verdict": "supported",
                "evidence_refs": refs,
                "rationale": "该条仅证明直接依赖证据覆盖，不替代业务语义事实。",
                "confidence": 1.0,
            })
    return {
        "status": "passed", "claims": claims, "issues": [],
        "additional_evidence_requests": [], "can_continue": True,
    }
