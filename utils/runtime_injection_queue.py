"""Process-safe, run-local mailbox for interactive injections (Linux/macOS)."""

from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import re
import uuid

TYPES = {"node_failure", "requirement_change", "data_anomaly"}


@contextmanager
def _mailbox(run_dir):
    import fcntl
    root = Path(run_dir) / "control"
    root.mkdir(parents=True, exist_ok=True)
    with (root / "injections.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / "injection_requests.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
            "closed": False, "requests": [],
        }
        yield data
        temporary = root / f".{uuid.uuid4().hex}.tmp"
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)


def enqueue_injection(run_dir, body):
    if not isinstance(body, dict) or body.get("type") not in TYPES:
        raise ValueError("请选择节点失效、需求变更或数据异常")
    target = body.get("target_node_id") or None
    if target is not None and (not isinstance(target, str) or not re.fullmatch(r"[\w.-]{1,160}", target)):
        raise ValueError("无效目标节点")
    change = body.get("change_text", "新增运行期要求：本节点输出必须包含非空证据引用。")
    if not isinstance(change, str) or not 1 <= len(change.strip()) <= 1000:
        raise ValueError("追加要求须为 1–1000 字的文本")
    with _mailbox(run_dir) as data:
        if data["closed"]:
            raise ValueError("执行窗口已关闭，不能再接收注入")
        if sum(item["status"] == "queued" for item in data["requests"]) >= 3:
            raise ValueError("已有 3 个注入等待调度，请等待处理")
        item = {
            "injection_id": f"live-{uuid.uuid4().hex}", "type": body["type"],
            "target_node_id": target, "change_text": change.strip(),
            "after_completed_nodes": 0, "status": "queued",
            "submitted_at": datetime.now().isoformat(timespec="seconds"),
        }
        data["requests"].append(item)
    return item


def read_injection_queue(run_dir):
    path = Path(run_dir) / "control/injection_requests.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
        "closed": False, "requests": [],
    }


def acknowledge_injection(run_dir, injection_id, status, reason=""):
    with _mailbox(run_dir) as data:
        for item in data["requests"]:
            if item["injection_id"] == injection_id:
                item.update(status=status, reason=reason)
                break


def close_injection_queue(run_dir):
    with _mailbox(run_dir) as data:
        data["closed"] = True
        pending = [dict(item) for item in data["requests"] if item["status"] == "queued"]
        for item in data["requests"]:
            if item["status"] == "queued":
                item.update(status="rejected", reason="执行窗口已结束，未应用到业务节点")
    return pending
