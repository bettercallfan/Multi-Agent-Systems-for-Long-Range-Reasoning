"""Auditable 1000-candidate long-horizon search for the MathorCup D task."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Callable


GOAL = "在完整覆盖货物且满足装载约束的前提下，持续搜索低成本三维装箱方案并保持证据链可复算"
GOAL_DIGEST = hashlib.sha256(GOAL.encode("utf-8")).hexdigest()
TOTAL_CANDIDATES = 1000
CHECKPOINT_INTERVAL = 25
RESUME_AFTER = 500


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_problem(normalized_input: dict) -> tuple[dict[str, dict], dict[str, dict]]:
    source = next(
        (item for item in normalized_input.get("structured_sources", [])
         if item.get("name") == "attachment1.docx"),
        None,
    )
    if not source:
        raise ValueError("normalized_input 缺少 attachment1.docx")
    table = next(
        (item.get("rows", []) for item in source.get("tables", [])
         if item.get("rows") and str(item["rows"][0][0]).strip() == "货物编号"),
        None,
    )
    if not table:
        raise ValueError("attachment1.docx 缺少货物表")
    cargo: dict[str, dict] = {}
    for row in table[1:]:
        dimensions = re.findall(r"\d+(?:\.\d+)?", str(row[2]))
        if len(dimensions) != 3:
            raise ValueError(f"货物 {row[0]} 尺寸不可解析")
        cargo_id = str(row[0]).strip()
        cargo[cargo_id] = {
            "type": str(row[1]).strip(),
            "dims": tuple(map(float, dimensions)),
            "weight": float(row[3]),
            "quantity": int(row[4]),
        }
    content = str(source.get("content", ""))
    vehicles: dict[str, dict] = {}
    for vehicle_type, number in (("V1", "1"), ("V2", "2")):
        match = re.search(
            rf"车型\s*{number}.*?长\s*(\d+)cm.*?宽\s*(\d+)cm.*?高\s*(\d+)cm"
            rf".*?额定载重：(\d+)kg.*?成本：(\d+)\s*元/趟",
            content,
            re.S,
        )
        if not match:
            raise ValueError(f"车型 {vehicle_type} 参数不可解析")
        length, width, height, capacity, cost = map(float, match.groups())
        vehicles[vehicle_type] = {
            "length": length,
            "width": width,
            "height": height,
            "capacity": capacity,
            "cost": cost,
        }
    if not cargo or len(vehicles) != 2:
        raise ValueError("MathorCup 输入契约不完整")
    return cargo, vehicles


def _instances(cargo: dict[str, dict]) -> list[dict]:
    items = []
    for cargo_id, spec in cargo.items():
        for serial in range(1, spec["quantity"] + 1):
            items.append({"cargo_id": cargo_id, "serial": serial, **spec})
    return items


def _configuration(candidate_index: int) -> dict:
    modes = ("v1_only", "v2_only", "mixed")
    sort_keys = ("area", "volume", "weight", "length", "width")
    return {
        "candidate_index": candidate_index,
        "mode": modes[(candidate_index - 1) % len(modes)],
        "sort_key": sort_keys[((candidate_index - 1) // len(modes)) % len(sort_keys)],
        "descending": ((candidate_index - 1) // 15) % 2 == 0,
        "seed": candidate_index,
        "v1_share_percent": 10 + (candidate_index * 37) % 81,
    }


def _ordered_items(items: list[dict], config: dict) -> list[dict]:
    def primary(item: dict) -> float:
        length, width, height = item["dims"]
        values = {
            "area": length * width,
            "volume": length * width * height,
            "weight": item["weight"],
            "length": length,
            "width": width,
        }
        return values[config["sort_key"]]

    def tie_breaker(item: dict) -> str:
        value = f"{config['seed']}:{item['cargo_id']}:{item['serial']}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    return sorted(
        items,
        key=lambda item: ((-primary(item) if config["descending"] else primary(item)), tie_breaker(item)),
    )


def _pack(items: list[dict], vehicle_type: str, scenario: str,
          vehicles: dict[str, dict]) -> list[dict]:
    vehicle = vehicles[vehicle_type]
    placements: list[dict] = []
    vehicle_number = 1
    x = y = row_width = load = 0.0
    for item in items:
        length, width, height = item["dims"]
        if length > vehicle["length"] or width > vehicle["width"] or height > vehicle["height"] - 3:
            raise ValueError(f"货物 {item['cargo_id']} 无法装入 {vehicle_type}")
        if x + length > vehicle["length"]:
            x = 0.0
            y += row_width
            row_width = 0.0
        if y + width > vehicle["width"] or load + item["weight"] > vehicle["capacity"]:
            vehicle_number += 1
            x = y = row_width = load = 0.0
        placements.append({
            "cargo_id": item["cargo_id"],
            "instance_id": f"{item['cargo_id']}-{item['serial']:03d}",
            "quantity": 1,
            "vehicle_id": f"{vehicle_type}-{vehicle_number}",
            "vehicle_type": vehicle_type,
            "scenario": scenario,
            "orientation": "original",
            "x": x,
            "y": y,
            "z": 0.0,
            "length": length,
            "width": width,
            "height": height,
        })
        x += length
        row_width = max(row_width, width)
        load += item["weight"]
    return placements


def evaluate_candidate(candidate_index: int, cargo: dict[str, dict],
                       vehicles: dict[str, dict]) -> tuple[dict, list[dict]]:
    config = _configuration(candidate_index)
    ordered = _ordered_items(_instances(cargo), config)
    scenario = f"candidate_{candidate_index:04d}"
    if config["mode"] == "v1_only":
        placements = _pack(ordered, "V1", scenario, vehicles)
    elif config["mode"] == "v2_only":
        placements = _pack(ordered, "V2", scenario, vehicles)
    else:
        v1_items, v2_items = [], []
        for item in ordered:
            selector = int(hashlib.sha256(
                f"{config['seed']}:{item['cargo_id']}:{item['serial']}".encode("utf-8")
            ).hexdigest()[:8], 16) % 100
            (v1_items if selector < config["v1_share_percent"] else v2_items).append(item)
        placements = _pack(v1_items, "V1", scenario, vehicles) + _pack(
            v2_items, "V2", scenario, vehicles
        )

    used = {item["vehicle_id"]: item["vehicle_type"] for item in placements}
    total_volume = sum(item["length"] * item["width"] * item["height"] for item in placements)
    total_weight = sum(cargo[item["cargo_id"]]["weight"] for item in placements)
    volume_capacity = sum(
        vehicles[vehicle_type]["length"] * vehicles[vehicle_type]["width"]
        * vehicles[vehicle_type]["height"] for vehicle_type in used.values()
    )
    weight_capacity = sum(vehicles[vehicle_type]["capacity"] for vehicle_type in used.values())
    quantities = Counter(item["cargo_id"] for item in placements)
    expected = {cargo_id: spec["quantity"] for cargo_id, spec in cargo.items()}
    summary = {
        "scenario": scenario,
        "candidate_index": candidate_index,
        "mode": config["mode"],
        "configuration": config,
        "feasible": dict(quantities) == expected,
        "placement_count": len(placements),
        "vehicle_count": len(used),
        "total_cost": sum(vehicles[vehicle_type]["cost"] for vehicle_type in used.values()),
        "space_utilization_rate": total_volume / volume_capacity,
        "load_utilization_rate": total_weight / weight_capacity,
        "placement_digest": _digest(placements),
    }
    if not summary["feasible"] or not all(
        math.isfinite(summary[field]) and 0 <= summary[field] <= 1
        for field in ("space_utilization_rate", "load_utilization_rate")
    ):
        raise ValueError(f"候选方案 {candidate_index} 生成失败")
    return summary, placements


class CandidateLedger:
    def __init__(self, run_dir: Path, *, resume: bool = False) -> None:
        self.run_dir = run_dir
        self.path = run_dir / "artifacts" / "candidate_evaluation_ledger.jsonl"
        self.checkpoint_dir = run_dir / "checkpoints"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.records: list[dict] = []
        self.previous_digest = "GENESIS"
        if resume:
            self.records = [
                json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line
            ]
            if self.records:
                self.previous_digest = self.records[-1]["record_digest"]

    def append(self, summary: dict) -> dict:
        step = len(self.records) + 1
        record = {
            "step": step,
            "work_unit_id": f"packing_candidate_{step:04d}",
            "agent_role": "PackingSearchAgent" if step % 100 else "OptimizationReviewAgent",
            "goal_digest": GOAL_DIGEST,
            "previous_record_digest": self.previous_digest,
            "candidate": summary,
            "candidate_digest": _digest(summary),
            "status": "completed",
            "checkpoint_epoch": (step + CHECKPOINT_INTERVAL - 1) // CHECKPOINT_INTERVAL,
        }
        record["record_digest"] = _digest(record)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.records.append(record)
        self.previous_digest = record["record_digest"]
        if step % CHECKPOINT_INTERVAL == 0:
            self.write_checkpoint()
        return record

    def write_checkpoint(self) -> Path:
        epoch = len(self.records) // CHECKPOINT_INTERVAL
        payload = {
            "schema_version": "1.0",
            "epoch": epoch,
            "completed_steps": len(self.records),
            "next_step": len(self.records) + 1,
            "goal": GOAL,
            "goal_digest": GOAL_DIGEST,
            "last_record_digest": self.previous_digest,
            "best_candidate_index": min(
                self.records,
                key=lambda item: (
                    item["candidate"]["total_cost"], item["candidate"]["vehicle_count"], item["step"]
                ),
            )["step"],
            "resume_supported": True,
        }
        payload["integrity"] = {"algorithm": "sha256", "sha256": _digest(payload)}
        path = self.checkpoint_dir / f"epoch-{epoch:04d}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @classmethod
    def resume_at(cls, run_dir: Path, expected_steps: int) -> "CandidateLedger":
        checkpoint_path = run_dir / "checkpoints" / f"epoch-{expected_steps // CHECKPOINT_INTERVAL:04d}.json"
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        integrity = checkpoint.pop("integrity", {})
        if integrity.get("sha256") != _digest(checkpoint):
            raise ValueError("checkpoint integrity mismatch")
        ledger = cls(run_dir, resume=True)
        if len(ledger.records) != expected_steps or checkpoint.get("completed_steps") != expected_steps:
            raise ValueError("resume step mismatch")
        if checkpoint.get("goal_digest") != GOAL_DIGEST or checkpoint.get("last_record_digest") != ledger.previous_digest:
            raise ValueError("resume protected context mismatch")
        return ledger


def validate_search(run_dir: Path, result: dict) -> dict:
    records = [
        json.loads(line)
        for line in (run_dir / "artifacts" / "candidate_evaluation_ledger.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        if line
    ]
    failures: list[str] = []
    previous = "GENESIS"
    for expected_step, record in enumerate(records, 1):
        stored = record.pop("record_digest", None)
        if record.get("step") != expected_step:
            failures.append(f"step_sequence:{expected_step}")
        if record.get("previous_record_digest") != previous:
            failures.append(f"digest_chain:{expected_step}")
        if record.get("goal_digest") != GOAL_DIGEST:
            failures.append(f"goal_drift:{expected_step}")
        if record.get("candidate_digest") != _digest(record.get("candidate")):
            failures.append(f"candidate_digest:{expected_step}")
        if stored != _digest(record):
            failures.append(f"record_digest:{expected_step}")
        record["record_digest"] = stored
        previous = stored
    if len(records) != TOTAL_CANDIDATES:
        failures.append("candidate_coverage")
    if len({item.get("work_unit_id") for item in records}) != TOTAL_CANDIDATES:
        failures.append("duplicate_work_units")
    if not all(item.get("candidate", {}).get("feasible") for item in records):
        failures.append("infeasible_candidate")
    checkpoints = sorted((run_dir / "checkpoints").glob("epoch-*.json"))
    if len(checkpoints) != TOTAL_CANDIDATES // CHECKPOINT_INTERVAL:
        failures.append("checkpoint_count")
    resume_valid = False
    resume_path = run_dir / "checkpoints" / "epoch-0020.json"
    if resume_path.is_file() and len(records) >= RESUME_AFTER:
        checkpoint = json.loads(resume_path.read_text(encoding="utf-8"))
        integrity = checkpoint.pop("integrity", {})
        resume_valid = (
            integrity.get("sha256") == _digest(checkpoint)
            and checkpoint.get("completed_steps") == RESUME_AFTER
            and checkpoint.get("next_step") == RESUME_AFTER + 1
            and checkpoint.get("goal_digest") == GOAL_DIGEST
            and checkpoint.get("last_record_digest") == records[RESUME_AFTER - 1]["record_digest"]
        )
    if not resume_valid:
        failures.append("resume_checkpoint_500")
    selected = result.get("selected_candidate", {})
    selected_record = next(
        (item for item in records if item["step"] == selected.get("candidate_index")), None
    )
    if not selected_record or selected_record["candidate"].get("placement_digest") != _digest(
        result.get("placements", [])
    ):
        failures.append("selected_candidate_recompute")
    return {
        "status": "passed" if not failures else "failed",
        "passed": not failures,
        "failures": failures,
        "completed_steps": len(records),
        "unique_steps": len({item.get("work_unit_id") for item in records}),
        "duplicate_executions": len(records) - len({item.get("work_unit_id") for item in records}),
        "goal_preserved": not any(item.startswith("goal_drift") for item in failures),
        "checkpoint_count": len(checkpoints),
        "resume_after_step": RESUME_AFTER,
        "resume_checkpoint_valid": resume_valid,
        "feasible_candidates": sum(bool(item.get("candidate", {}).get("feasible")) for item in records),
        "final_chain_digest": previous,
    }


def run(normalized_input: dict, run_dir: Path,
        stage_reasoner: Callable[[int, dict], dict] | None = None) -> dict:
    started = datetime.now()
    cargo, vehicles = load_problem(normalized_input)
    ledger = CandidateLedger(run_dir)
    stage_reviews: list[dict] = []
    for candidate_index in range(1, RESUME_AFTER + 1):
        summary, _ = evaluate_candidate(candidate_index, cargo, vehicles)
        ledger.append(summary)
        if stage_reasoner and candidate_index % 100 == 0:
            stage_reviews.append(stage_reasoner(candidate_index, summary))
    ledger = CandidateLedger.resume_at(run_dir, RESUME_AFTER)
    for candidate_index in range(RESUME_AFTER + 1, TOTAL_CANDIDATES + 1):
        summary, _ = evaluate_candidate(candidate_index, cargo, vehicles)
        ledger.append(summary)
        if stage_reasoner and candidate_index % 100 == 0:
            stage_reviews.append(stage_reasoner(candidate_index, summary))

    feasible_records = [item for item in ledger.records if item["candidate"]["feasible"]]
    selected_record = min(
        feasible_records,
        key=lambda item: (
            item["candidate"]["total_cost"],
            item["candidate"]["vehicle_count"],
            -item["candidate"]["space_utilization_rate"],
            item["step"],
        ),
    )
    selected_summary, placements = evaluate_candidate(selected_record["step"], cargo, vehicles)
    best_by_mode = {}
    for mode in ("v1_only", "v2_only", "mixed"):
        best_by_mode[mode] = min(
            (item["candidate"] for item in feasible_records if item["candidate"]["mode"] == mode),
            key=lambda item: (item["total_cost"], item["vehicle_count"], -item["space_utilization_rate"]),
        )
    comparison = [best_by_mode[mode] for mode in ("v1_only", "v2_only", "mixed")]
    result = {
        "status": "success",
        "vehicle_scenarios": comparison[:2],
        "multi_vehicle_scenarios": [comparison[2]],
        "placements": placements,
        "vehicle_count": selected_summary["vehicle_count"],
        "total_cost": selected_summary["total_cost"],
        "space_utilization_rate": selected_summary["space_utilization_rate"],
        "load_utilization_rate": selected_summary["load_utilization_rate"],
        "validation": {
            "status": "pending_independent_validation",
            "method": "1000_candidate_floor_shelf_search",
            "top_clearance_cm": 3,
        },
        "selected_candidate": selected_summary,
    }
    artifacts = run_dir / "artifacts"
    for name in ("result.json", "result_complete.json"):
        (artifacts / name).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (artifacts / "cost_comparison.json").write_text(
        json.dumps({"scenarios": comparison}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    search_validation = validate_search(run_dir, result)
    (artifacts / "long_horizon_search_validation.json").write_text(
        json.dumps(search_validation, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (artifacts / "strategy_reviews.json").write_text(
        json.dumps(stage_reviews, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not search_validation["passed"]:
        raise AssertionError(json.dumps(search_validation, ensure_ascii=False))
    return {
        "schema_version": "1.0",
        "status": "passed",
        "task_type": "math_modeling",
        "goal": GOAL,
        "goal_digest": GOAL_DIGEST,
        "business_steps": TOTAL_CANDIDATES,
        "resume_checkpoint": "checkpoints/epoch-0020.json",
        "selected_candidate": selected_summary,
        "search_validation": search_validation,
        "stage_reviews": stage_reviews,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }
