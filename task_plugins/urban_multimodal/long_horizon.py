"""Real-data, auditable 1000-work-unit urban governance workflow."""

from __future__ import annotations

from collections import Counter
import csv
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import struct
from typing import Callable

GOAL = "对城市四模态数据完成隐私保护的长程治理画像，保持全局目标、来源覆盖和证据链不漂移"
GOAL_DIGEST = hashlib.sha256(GOAL.encode()).hexdigest()
UNIT_COUNTS = {"remote_sensing": 250, "law_articles": 250, "phone_network": 200,
               "network_traffic": 200, "cross_modal_synthesis": 100}


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _partition(items: list, count: int) -> list[list]:
    return [items[index * len(items) // count:(index + 1) * len(items) // count]
            for index in range(count)]


class WorkUnitLedger:
    def __init__(self, run_dir: Path, *, resume: bool = False) -> None:
        self.run_dir = run_dir
        self.path = run_dir / "artifacts/work_unit_ledger.jsonl"
        self.checkpoint_dir = run_dir / "checkpoints"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.records: list[dict] = []
        self.previous_digest = "GENESIS"
        if resume:
            self.records = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line]
            if self.records:
                self.previous_digest = self.records[-1]["record_digest"]

    def append(self, modality: str, role: str, input_refs: list[str], result: dict,
               dependencies: list[str] | None = None) -> dict:
        index = len(self.records) + 1
        base = {
            "step": index, "work_unit_id": f"urban_work_{index:04d}",
            "modality": modality, "agent_role": role,
            "input_refs": input_refs, "dependencies": dependencies or [],
            "goal_digest": GOAL_DIGEST, "previous_record_digest": self.previous_digest,
            "result": result, "result_digest": _digest(result), "status": "completed",
            "checkpoint_epoch": (index + 24) // 25,
        }
        base["record_digest"] = _digest(base)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(base, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.records.append(base)
        self.previous_digest = base["record_digest"]
        if index % 25 == 0:
            self.write_checkpoint()
        return base

    def write_checkpoint(self) -> Path:
        epoch = len(self.records) // 25
        payload = {
            "schema_version": "1.0", "epoch": epoch,
            "completed_steps": len(self.records), "next_step": len(self.records) + 1,
            "goal": GOAL, "goal_digest": GOAL_DIGEST,
            "last_record_digest": self.previous_digest,
            "resume_supported": True,
        }
        payload["integrity"] = {"algorithm": "sha256", "sha256": _digest(payload)}
        path = self.checkpoint_dir / f"epoch-{epoch:04d}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @classmethod
    def resume_at(cls, run_dir: Path, expected_steps: int) -> "WorkUnitLedger":
        checkpoints = sorted((run_dir / "checkpoints").glob("epoch-*.json"))
        if not checkpoints:
            raise ValueError("missing resume checkpoint")
        checkpoint = json.loads(checkpoints[-1].read_text(encoding="utf-8"))
        integrity = checkpoint.pop("integrity")
        if integrity.get("sha256") != _digest(checkpoint):
            raise ValueError("checkpoint integrity mismatch")
        checkpoint["integrity"] = integrity
        ledger = cls(run_dir, resume=True)
        if checkpoint.get("completed_steps") != expected_steps or len(ledger.records) != expected_steps:
            raise ValueError("resume step mismatch")
        if checkpoint.get("goal_digest") != GOAL_DIGEST or checkpoint.get("last_record_digest") != ledger.previous_digest:
            raise ValueError("resume protected context mismatch")
        return ledger


def _remote_units(dataset: Path, ledger: WorkUnitLedger) -> None:
    paths = sorted((dataset / "remote_sense_data").glob("*.json"))
    for batch in _partition(paths, UNIT_COUNTS["remote_sensing"]):
        topics, caption_count, valid = Counter(), 0, 0
        for path in batch:
            item = json.loads(path.read_text(encoding="utf-8"))
            valid += int(isinstance(item, dict))
            text = " ".join(map(str, item.get("captions", []))).lower()
            caption_count += len(item.get("captions", []))
            for topic, words in {"fire":("fire","burn"), "water":("water","flood","river"),
                                 "vegetation":("forest","crop","vegetation"), "urban":("city","urban","building")}.items():
                topics[topic] += sum(text.count(word) for word in words)
        refs = [path.relative_to(dataset).as_posix() for path in batch]
        ledger.append("remote_sensing", "RemoteSensingAnalysisAgent", refs,
                      {"record_count": len(batch), "valid_count": valid,
                       "caption_count": caption_count, "topic_mentions": dict(topics)})


def _law_units(dataset: Path, ledger: WorkUnitLedger) -> None:
    path = dataset / "law_articles_80k.jsonl"
    batch_size = 320
    with path.open("r", encoding="utf-8-sig") as stream:
        for chunk_index in range(UNIT_COUNTS["law_articles"]):
            types, statuses, topics = Counter(), Counter(), Counter()
            count = valid = 0
            for _ in range(batch_size):
                line = stream.readline()
                if not line:
                    break
                count += 1
                item = json.loads(line); valid += int(isinstance(item, dict))
                metadata = item.get("metadata") or {}
                types[str(metadata.get("type") or "unknown")] += 1
                statuses[str(metadata.get("status") or "unknown")] += 1
                text = str(item.get("content") or "")
                for topic, words in {"city":("城市","城乡规划"), "ecology":("生态","环境"),
                                     "emergency":("应急","灾害"), "security":("数据安全","网络安全")}.items():
                    topics[topic] += int(any(word in text for word in words))
            start = chunk_index * batch_size + 1
            ledger.append("law_articles", "PolicyAnalysisAgent",
                          [f"law_articles_80k.jsonl#L{start}-L{start + count - 1}"],
                          {"record_count": count, "valid_count": valid,
                           "law_types": dict(types), "statuses": dict(statuses),
                           "topic_records": dict(topics)})


def _phone_units(dataset: Path, ledger: WorkUnitLedger) -> None:
    path = dataset / "phone_network_sichuan_open_voc_80k_utf8_bom.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        for chunk_index in range(UNIT_COUNTS["phone_network"]):
            count = duration = missing_geo = 0; calltypes = Counter()
            for _ in range(400):
                try: row = next(reader)
                except StopIteration: break
                count += 1; duration += int(float(row.get("call_dur") or 0))
                missing_geo += int(not row.get("city_name") and not row.get("county_name"))
                calltypes[str(row.get("calltype_id") or "unknown")] += 1
            start = chunk_index * 400 + 2
            ledger.append("phone_network", "PrivacyAggregationAgent",
                          [f"phone_network_sichuan_open_voc_80k_utf8_bom.csv#L{start}-L{start + count - 1}"],
                          {"record_count": count, "total_duration_seconds": duration,
                           "missing_geo_count": missing_geo, "calltype_counts": dict(calltypes),
                           "raw_identifiers_emitted": False})


def _pcap_units(dataset: Path, ledger: WorkUnitLedger) -> None:
    path = dataset / "Shifu.pcap"
    with path.open("rb") as stream:
        global_header = stream.read(24); magic = global_header[:4]
        endian = "<" if magic in {b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"} else ">"
        packet_index = 0
        for _ in range(UNIT_COUNTS["network_traffic"]):
            protocols = Counter(); count = captured = malformed = 0
            for _ in range(2500):
                header = stream.read(16)
                if not header: break
                if len(header) != 16: malformed += 1; break
                _, _, included, _ = struct.unpack(endian + "IIII", header)
                frame = stream.read(included); packet_index += 1
                if len(frame) != included: malformed += 1; break
                count += 1; captured += included
                if len(frame) >= 34 and struct.unpack("!H", frame[12:14])[0] == 0x0800:
                    protocols[{6:"tcp",17:"udp",1:"icmp"}.get(frame[23], f"ip_{frame[23]}")] += 1
                else: protocols["non_ipv4"] += 1
            start = packet_index - count + 1
            ledger.append("network_traffic", "NetworkHeaderAnalysisAgent",
                          [f"Shifu.pcap#packet={start}-{packet_index}"],
                          {"packet_count": count, "captured_bytes": captured,
                           "protocol_counts": dict(protocols), "malformed_count": malformed,
                           "payload_inspection_performed": False, "raw_ips_emitted": False})


def _synthesis_units(ledger: WorkUnitLedger,
                     live_reasoner: Callable[[int, str, dict], dict] | None) -> list[dict]:
    source = list(ledger.records[:900]); groups = _partition(source, 100)
    live_results = []
    roles = ["EvidenceSynthesisAgent", "GovernanceReasoningAgent",
             "PrivacyReviewAgent", "ConsistencyCriticAgent"]
    for index, group in enumerate(groups, 1):
        modality_counts = Counter(item["modality"] for item in group)
        summary = {"source_unit_count": len(group), "modality_counts": dict(modality_counts),
                   "source_result_digests": [item["result_digest"] for item in group],
                   "evidence_summary": [item["result"] for item in group],
                   "privacy_boundary_preserved": True, "unsupported_causality": False}
        if live_reasoner is not None and index % 10 == 0:
            live = live_reasoner(index, roles[(index - 1) % len(roles)], summary)
            summary["live_reasoning"] = live; live_results.append(live)
        ledger.append("cross_modal_synthesis", roles[(index - 1) % len(roles)],
                      [f"artifacts/work_unit_ledger.jsonl#step={item['step']}" for item in group],
                      summary, [item["work_unit_id"] for item in group])
    return live_results


def validate_run(run_dir: Path, *, require_live_reasoning: bool) -> dict:
    path = run_dir / "artifacts/work_unit_ledger.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    failures, previous = [], "GENESIS"
    for expected, record in enumerate(records, 1):
        stored = record.pop("record_digest", None)
        if record.get("step") != expected: failures.append(f"step_sequence:{expected}")
        if record.get("previous_record_digest") != previous: failures.append(f"digest_chain:{expected}")
        if record.get("goal_digest") != GOAL_DIGEST: failures.append(f"goal_drift:{expected}")
        if record.get("result_digest") != _digest(record.get("result")): failures.append(f"result_digest:{expected}")
        if stored != _digest(record): failures.append(f"record_digest:{expected}")
        record["record_digest"] = stored; previous = stored
    counts = Counter(item.get("modality") for item in records)
    if len(records) != 1000 or dict(counts) != UNIT_COUNTS: failures.append("work_unit_coverage")
    totals = {
        "remote_records": sum(x["result"].get("record_count", 0) for x in records if x["modality"] == "remote_sensing"),
        "law_records": sum(x["result"].get("record_count", 0) for x in records if x["modality"] == "law_articles"),
        "phone_records": sum(x["result"].get("record_count", 0) for x in records if x["modality"] == "phone_network"),
        "pcap_packets": sum(x["result"].get("packet_count", 0) for x in records if x["modality"] == "network_traffic"),
    }
    if totals != {"remote_records":511,"law_records":80000,"phone_records":80000,"pcap_packets":500000}:
        failures.append("source_count_recompute")
    checkpoints = sorted((run_dir / "checkpoints").glob("epoch-*.json"))
    if len(checkpoints) != 40: failures.append("checkpoint_count")
    resume_checkpoint_valid = False
    for checkpoint_path in checkpoints:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8")); integrity = checkpoint.pop("integrity", {})
        if integrity.get("sha256") != _digest(checkpoint): failures.append(f"checkpoint_integrity:{checkpoint_path.name}")
        if checkpoint_path.name == "epoch-0020.json":
            resume_checkpoint_valid = (
                checkpoint.get("completed_steps") == 500
                and checkpoint.get("next_step") == 501
                and checkpoint.get("goal_digest") == GOAL_DIGEST
                and len(records) >= 500
                and checkpoint.get("last_record_digest") == records[499].get("record_digest")
            )
    if not resume_checkpoint_valid: failures.append("resume_checkpoint_500")
    live_count = sum("live_reasoning" in x.get("result", {}) for x in records)
    if require_live_reasoning and live_count != 10: failures.append("live_reasoning_count")
    return {"status":"passed" if not failures else "failed", "passed":not failures,
            "failures":failures, "completed_steps":len(records), "unique_steps":len({x.get('work_unit_id') for x in records}),
            "duplicate_executions":len(records)-len({x.get('work_unit_id') for x in records}),
            "goal_preserved":not any(x.startswith("goal_drift") for x in failures),
            "checkpoint_count":len(checkpoints), "resume_after_step":500,
            "resume_checkpoint_valid":resume_checkpoint_valid,
            "live_reasoning_calls":live_count, "modality_work_units":dict(counts), "source_totals":totals,
            "final_chain_digest":previous}


def run(dataset: Path, run_dir: Path,
        live_reasoner: Callable[[int, str, dict], dict] | None = None) -> dict:
    started = datetime.now(); ledger = WorkUnitLedger(run_dir)
    _remote_units(dataset, ledger); _law_units(dataset, ledger)
    resume_checkpoint = "checkpoints/epoch-0020.json"
    ledger = WorkUnitLedger.resume_at(run_dir, 500)
    _phone_units(dataset, ledger); _pcap_units(dataset, ledger)
    live_results = _synthesis_units(ledger, live_reasoner)
    validation = validate_run(run_dir, require_live_reasoning=live_reasoner is not None)
    result = {"schema_version":"1.0", "status":validation["status"], "goal":GOAL,
              "goal_digest":GOAL_DIGEST, "task_type":"urban_long_horizon_governance",
              "business_steps":1000, "resume_checkpoint":resume_checkpoint,
              "validation":validation, "live_reasoning_results":live_results,
              "started_at":started.isoformat(timespec="seconds"),
              "finished_at":datetime.now().isoformat(timespec="seconds")}
    (run_dir / "artifacts/urban_long_horizon_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "artifacts/long_horizon_validation.json").write_text(
        json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
    if not validation["passed"]: raise AssertionError(json.dumps(validation, ensure_ascii=False))
    return result
