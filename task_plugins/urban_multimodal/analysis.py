"""Deterministic, privacy-preserving profiles for the urban dataset demo."""

from __future__ import annotations

from collections import Counter
import csv
from datetime import datetime
import json
from pathlib import Path
import socket
import struct


REMOTE_TOPICS = {
    "urban": ("urban", "city", "building", "infrastructure"),
    "water": ("water", "river", "lake", "flood", "coast"),
    "vegetation": ("forest", "vegetation", "crop", "agriculture", "park"),
    "disaster": ("disaster", "fire", "flood", "earthquake", "storm"),
}
LAW_TOPICS = {
    "city_governance": ("城市", "城乡规划", "市政", "公共安全"),
    "ecology": ("生态环境", "环境保护", "污染防治", "自然资源"),
    "emergency": ("突发事件", "应急", "灾害", "防洪"),
    "data_security": ("数据安全", "网络安全", "个人信息", "通信"),
}


def _inputs(root: Path) -> Path:
    return root / "inputs" if (root / "inputs").is_dir() else root


def profile_remote(root: Path) -> dict:
    records = []
    for path in sorted(_inputs(root).rglob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(item, dict) and {"caption", "sha256", "width", "height"} <= set(item):
            records.append(item)
    topics = Counter()
    success = 0
    for item in records:
        success += int(item.get("status") == "success")
        text = " ".join([str(item.get("caption", "")), *map(str, item.get("captions", []))]).lower()
        for topic, words in REMOTE_TOPICS.items():
            topics[topic] += int(any(word in text for word in words))
    return {
        "record_count": len(records),
        "success_count": success,
        "unique_sha256_count": len({item.get("sha256") for item in records}),
        "average_width": round(sum(float(x.get("width", 0)) for x in records) / len(records), 3) if records else 0,
        "average_height": round(sum(float(x.get("height", 0)) for x in records) / len(records), 3) if records else 0,
        "topic_record_counts": dict(sorted(topics.items())),
        "data_level": "metadata_and_captions_only",
    }


def profile_law(root: Path) -> dict:
    path = _inputs(root) / "law_articles_80k.jsonl"
    types, statuses, topics = Counter(), Counter(), Counter()
    count = valid = 0
    with path.open("r", encoding="utf-8-sig") as stream:
        for line in stream:
            if not line.strip():
                continue
            count += 1
            item = json.loads(line)
            if not isinstance(item, dict):
                continue
            valid += 1
            metadata = item.get("metadata") or {}
            types[str(metadata.get("type") or "unknown")] += 1
            statuses[str(metadata.get("status") or "unknown")] += 1
            text = str(item.get("content") or "")
            for topic, words in LAW_TOPICS.items():
                topics[topic] += int(any(word in text for word in words))
    return {
        "record_count": count,
        "valid_json_count": valid,
        "law_type_top10": dict(types.most_common(10)),
        "status_counts": dict(statuses.most_common()),
        "topic_record_counts": dict(sorted(topics.items())),
    }


def profile_phone(root: Path) -> dict:
    path = _inputs(root) / "phone_network_sichuan_open_voc_80k_utf8_bom.csv"
    count = total_duration = 0
    callers, peers, devices = set(), set(), set()
    calltypes, hours, months = Counter(), Counter(), Counter()
    missing_geo = 0
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            count += 1
            callers.add(row.get("phone_no_m", ""))
            peers.add(row.get("opposite_no_m", ""))
            devices.add(row.get("imei_m", ""))
            calltypes[str(row.get("calltype_id") or "unknown")] += 1
            total_duration += int(float(row.get("call_dur") or 0))
            try:
                timestamp = datetime.strptime(row["start_datetime"], "%Y-%m-%d %H:%M:%S")
                hours[f"{timestamp.hour:02d}"] += 1
                months[timestamp.strftime("%Y-%m")] += 1
            except (KeyError, ValueError):
                pass
            missing_geo += int(not row.get("city_name") and not row.get("county_name"))
    return {
        "record_count": count,
        "unique_caller_count": len(callers - {""}),
        "unique_peer_count": len(peers - {""}),
        "unique_device_count": len(devices - {""}),
        "total_call_duration_seconds": total_duration,
        "average_call_duration_seconds": round(total_duration / count, 3) if count else 0,
        "calltype_counts": dict(sorted(calltypes.items())),
        "peak_hour": max(hours, key=lambda key: (hours[key], key)) if hours else None,
        "peak_month": max(months, key=lambda key: (months[key], key)) if months else None,
        "missing_geo_count": missing_geo,
        "identifiers_are_hashed": True,
    }


def profile_pcap(root: Path) -> dict:
    path = _inputs(root) / "Shifu.pcap"
    packets = captured_bytes = malformed = 0
    protocols, source_ips, destination_ips, destination_ports = Counter(), set(), set(), Counter()
    with path.open("rb") as stream:
        global_header = stream.read(24)
        magic = global_header[:4]
        endian = "<" if magic in {b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"} else ">"
        while True:
            header = stream.read(16)
            if not header:
                break
            if len(header) != 16:
                malformed += 1
                break
            _, _, included, _ = struct.unpack(endian + "IIII", header)
            frame = stream.read(included)
            if len(frame) != included:
                malformed += 1
                break
            packets += 1
            captured_bytes += included
            if len(frame) < 34 or struct.unpack("!H", frame[12:14])[0] != 0x0800:
                protocols["non_ipv4"] += 1
                continue
            ihl = (frame[14] & 0x0F) * 4
            proto = frame[23]
            source_ips.add(socket.inet_ntoa(frame[26:30]))
            destination_ips.add(socket.inet_ntoa(frame[30:34]))
            name = {6: "tcp", 17: "udp", 1: "icmp"}.get(proto, f"ip_{proto}")
            protocols[name] += 1
            offset = 14 + ihl
            if proto in {6, 17} and len(frame) >= offset + 4:
                destination_ports[str(struct.unpack("!H", frame[offset + 2:offset + 4])[0])] += 1
    return {
        "packet_count": packets,
        "captured_bytes": captured_bytes,
        "average_packet_bytes": round(captured_bytes / packets, 3) if packets else 0,
        "protocol_counts": dict(sorted(protocols.items())),
        "unique_source_ip_count": len(source_ips),
        "unique_destination_ip_count": len(destination_ips),
        "top_destination_ports": dict(destination_ports.most_common(10)),
        "malformed_packet_count": malformed,
        "payload_inspection_performed": False,
    }


def analyze(root: str | Path) -> dict:
    root = Path(root)
    profiles = {
        "remote_sensing": profile_remote(root),
        "law_articles": profile_law(root),
        "phone_network": profile_phone(root),
        "network_traffic": profile_pcap(root),
    }
    return {
        "status": "success",
        "source_coverage": {
            "remote_sensing": "remote_sense_data/*.json",
            "law_articles": "law_articles_80k.jsonl",
            "phone_network": "phone_network_sichuan_open_voc_80k_utf8_bom.csv",
            "network_traffic": "Shifu.pcap",
        },
        "modality_profiles": profiles,
        "cross_modal_assessment": {
            "scenario_count": 4,
            "fusion_level": "aggregate_governance_signals_only",
            "entity_level_linkage_performed": False,
            "findings": [
                "遥感 caption 可用于生态、城乡空间与灾害主题的数据发现，不等同于像素级识别结果",
                "法规主题计数可建立治理议题索引，不构成具体案件的法律意见",
                "匿名通联仅输出聚合时序和网络规模，不尝试反识别个人",
                "PCAP 仅分析包头和协议统计，不检查载荷内容",
            ],
        },
        "governance_recommendations": [
            "建立按生态、应急、数据安全和城市治理主题组织的法规检索索引",
            "将遥感元数据发现结果用于人工筛选候选区域，再接入真实影像做空间分析",
            "对通信与流量异常只做聚合预警，任何处置前必须进行授权复核",
            "按数据敏感等级将匿名通信和流量数据留在端边侧，仅上传脱敏聚合指标",
        ],
        "limitations": [
            "四类数据缺少统一时间、空间和实体键，不能证明跨模态因果关系",
            "遥感目录不含实际影像像素，仅能验证元数据与 caption 处理",
            "电话标识已散列且地理字段存在缺失，不进行个人身份推断",
            "PCAP 分析限定为包头统计，不输出原始 IP 或载荷",
        ],
        "validation": {
            "status": "pending_independent_validation",
            "privacy_preserving": True,
            "unsupported_causal_claims_made": False,
        },
    }
