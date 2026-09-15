"""Framework-owned runtime injections for auditable recovery demonstrations.

The controller is deliberately deterministic and opt-in.  It never touches the
user's source files: data anomalies are injected only into the copy under the
current run directory, quarantined, and restored before normal execution can
resume.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
from pathlib import Path
import shutil
from typing import Any

from orchestration.execution.node_executor import NodeExecutionResult
from orchestration.graph.task_graph import NodeStatus, TaskGraph, TaskNode


INJECTION_TYPES = {"node_failure", "requirement_change", "data_anomaly"}
_GOVERNANCE_CAPABILITIES = {
    "artifact_validation", "evidence_verification", "reporting",
    "terminal_execution",
}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize_injection_policy(raw: Any) -> dict[str, Any]:
    """Validate the small TaskSpec injection contract without silent coercion."""

    if not raw:
        return {"enabled": False, "injections": []}
    if not isinstance(raw, dict):
        raise ValueError("runtime_injection_policy 必须是对象")
    items = raw.get("injections", [])
    if not isinstance(items, list):
        raise ValueError("runtime_injection_policy.injections 必须是列表")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError("每个运行期注入配置必须是对象")
        injection_type = str(item.get("type") or "").strip().replace("-", "_")
        if injection_type not in INJECTION_TYPES:
            raise ValueError(f"不支持的运行期注入类型: {injection_type}")
        injection_id = str(
            item.get("injection_id") or f"demo-{injection_type}-{index}"
        ).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", injection_id) or injection_id in seen:
            raise ValueError(f"运行期注入 ID 为空或重复: {injection_id}")
        seen.add(injection_id)
        after = int(item.get(
            "after_completed_nodes", 0 if injection_type == "node_failure" else 1,
        ))
        if after < 0:
            raise ValueError("after_completed_nodes 不能小于 0")
        normalized.append({
            "injection_id": injection_id,
            "type": injection_type,
            "after_completed_nodes": after,
            "target_node_id": str(item.get("target_node_id") or "").strip() or None,
            "target_capability": str(item.get("target_capability") or "").strip() or None,
            "change_text": str(item.get("change_text") or (
                "新增运行期要求：保持原始全局目标，并在本节点输出中保留可审计证据。"
            )).strip(),
        })
    return {"enabled": bool(raw.get("enabled", True) and normalized),
            "injections": normalized}


class RuntimeInjectionController:
    """Apply each requested injection once and persist its complete lifecycle."""

    def __init__(self, task_spec: dict, run_state, run_dir: str | Path) -> None:
        self.task_spec = task_spec
        self.run_state = run_state
        self.run_dir = Path(run_dir).resolve()
        self.policy = normalize_injection_policy(
            task_spec.get("runtime_injection_policy")
        )
        # Disabled injections must add zero persistence overhead to normal and
        # thousand-step scheduler runs.
        if self.policy.get("enabled"):
            self.run_state.configure_runtime_injections(self.policy)

    @property
    def enabled(self) -> bool:
        return bool(self.policy.get("enabled"))

    def _status(self, injection_id: str) -> str:
        items = self.run_state.get("runtime_injections", {}).get("items", {})
        return str(items.get(injection_id, {}).get("status", "armed"))

    def poll_requests(self, graph: TaskGraph) -> None:
        """Only the scheduler mutates live state; HTTP writes a separate mailbox."""
        if not self.task_spec.get("accept_runtime_injections"):
            return
        from utils.runtime_injection_queue import read_injection_queue, acknowledge_injection
        for request in read_injection_queue(self.run_dir)["requests"]:
            if request["status"] != "queued":
                continue
            injection_id = request["injection_id"]
            # A restart after state commit but before acknowledgement is idempotent.
            if injection_id in self.run_state.get("runtime_injections", {}).get("items", {}):
                acknowledge_injection(self.run_dir, injection_id, "accepted")
                continue
            target = request.get("target_node_id")
            candidates = [n for n in graph.nodes if n.status in {NodeStatus.READY, NodeStatus.PENDING}
                          and n.capability not in _GOVERNANCE_CAPABILITIES
                          and (not target or n.node_id == target)]
            if not candidates:
                reason = "当前任务图没有匹配的待执行业务节点"
                acknowledge_injection(self.run_dir, injection_id, "rejected", reason)
                self.run_state.record_event("runtime_injection_request_rejected", {
                    "injection_id": injection_id, "type": request["type"], "reason": reason,
                })
                continue
            combined = list(self.task_spec.get("runtime_injection_policy", {}).get("injections", []))
            combined.append(request)
            self.policy = normalize_injection_policy({"enabled": True, "injections": combined})
            self.task_spec["runtime_injection_policy"] = {**self.policy, "profile": "interactive"}
            self.run_state.configure_runtime_injections(self.task_spec["runtime_injection_policy"])
            (self.run_dir / "task_spec.json").write_text(
                json.dumps(self.task_spec, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            self.run_state.record_event("runtime_injection_request_accepted", {
                "injection_id": injection_id, "type": request["type"],
                "submitted_at": request["submitted_at"], "target_node_id": target,
            })
            acknowledge_injection(self.run_dir, injection_id, "accepted")

    @staticmethod
    def _completed_count(graph: TaskGraph) -> int:
        return sum(node.status == NodeStatus.COMPLETED for node in graph.nodes)

    def _eligible(self, spec: dict, graph: TaskGraph, node: TaskNode) -> bool:
        if self._status(spec["injection_id"]) != "armed":
            return False
        if self._completed_count(graph) < int(spec["after_completed_nodes"]):
            return False
        if spec.get("target_node_id") and node.node_id != spec["target_node_id"]:
            return False
        if spec.get("target_capability") and node.capability != spec["target_capability"]:
            return False
        if not spec.get("target_node_id") and node.capability in _GOVERNANCE_CAPABILITIES:
            return False
        return node.status in {NodeStatus.PENDING, NodeStatus.READY, NodeStatus.RUNNING}

    def _write_evidence(self, spec: dict, payload: dict) -> str:
        relative = Path("injections") / spec["injection_id"] / "evidence.json"
        target = self.run_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({
                "schema_version": "1.0",
                "injection_id": spec["injection_id"],
                "type": spec["type"],
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                **payload,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.run_state.record_artifact(relative.as_posix())
        return relative.as_posix()

    def apply_requirement_change(self, graph: TaskGraph) -> bool:
        """Amend one pending node and the live TaskSpec, preserving completed work."""

        if not self.enabled:
            return False
        for spec in self.policy["injections"]:
            if spec["type"] != "requirement_change":
                continue
            candidates = [node for node in graph.ready_nodes()
                          if self._eligible(spec, graph, node)]
            if not candidates:
                candidates = [node for node in graph.nodes
                              if self._eligible(spec, graph, node)]
            if not candidates:
                continue
            node = candidates[0]
            old_version = graph.version
            old_description = node.description
            change_text = spec["change_text"]
            self.run_state.record_runtime_injection(
                spec["injection_id"], "triggered",
                {"node_id": node.node_id, "graph_version": old_version,
                 "change_text": change_text},
            )
            node.description = f"{old_description}\n{change_text}"
            if not any(
                criterion.get("type") == "evidence_refs_nonempty"
                for criterion in node.success_criteria
            ):
                node.success_criteria.append({"type": "evidence_refs_nonempty"})
            changes = list(node.recovery_context.get("runtime_requirement_changes", []))
            changes.append({"injection_id": spec["injection_id"], "text": change_text})
            node.recovery_context = {
                **node.recovery_context,
                "runtime_requirement_changes": changes,
            }
            graph.version += 1
            graph.validate_graph()
            runtime_changes = self.task_spec.setdefault("runtime_requirement_changes", [])
            runtime_changes.append({
                "injection_id": spec["injection_id"],
                "target_node_id": node.node_id,
                "change_text": change_text,
                "old_graph_version": old_version,
                "new_graph_version": graph.version,
            })
            task_spec_path = self.run_dir / "task_spec.json"
            task_spec_path.write_text(
                json.dumps(self.task_spec, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            evidence_ref = self._write_evidence(spec, {
                "status": "recovered",
                "recovery_action": "amend_pending_node_contract",
                "target_node_id": node.node_id,
                "old_description": old_description,
                "new_description": node.description,
                "enforced_success_criterion": "evidence_refs_nonempty",
                "old_graph_version": old_version,
                "new_graph_version": graph.version,
                "completed_nodes_preserved": sorted(
                    item.node_id for item in graph.nodes
                    if item.status == NodeStatus.COMPLETED
                ),
            })
            self.run_state.record_runtime_injection(
                spec["injection_id"], "recovery_started",
                {"action": "amend_pending_node_contract", "node_id": node.node_id},
            )
            self.run_state.record_runtime_injection(
                spec["injection_id"], "recovered",
                {"node_id": node.node_id, "evidence_ref": evidence_ref,
                 "new_graph_version": graph.version},
            )
            return True
        return False

    def failure_for(
        self, graph: TaskGraph, node: TaskNode, executor_id: str,
    ) -> NodeExecutionResult | None:
        """Return an injected first-attempt failure, or ``None`` for normal work."""

        if not self.enabled:
            return None
        # Let an in-flight recovery reach real execution before another fault.
        if any(item.get("status") == "recovering" and item.get("target_node_id") == node.node_id
               for item in self.run_state.get("runtime_injections", {}).get("items", {}).values()):
            return None
        for injection_type in ("node_failure", "data_anomaly"):
            for spec in self.policy["injections"]:
                if spec["type"] != injection_type or not self._eligible(spec, graph, node):
                    continue
                if injection_type == "node_failure":
                    return self._inject_node_failure(spec, node, executor_id)
                return self._inject_data_anomaly(spec, node, executor_id)
        return None

    def _inject_node_failure(
        self, spec: dict, node: TaskNode, executor_id: str,
    ) -> NodeExecutionResult:
        payload = {
            "node_id": node.node_id,
            "executor_id": executor_id,
            "failure_mode": "one_shot_executor_crash",
            "attempt": node.attempts,
        }
        self.run_state.record_runtime_injection(
            spec["injection_id"], "triggered", payload,
        )
        evidence_ref = self._write_evidence(spec, {
            **payload,
            "status": "detected",
            "detector": "scheduler execution boundary",
            "recovery_action": "retry_or_switch_executor",
        })
        self.run_state.record_runtime_injection(
            spec["injection_id"], "detected",
            {**payload, "evidence_ref": evidence_ref},
        )
        self.run_state.record_runtime_injection(
            spec["injection_id"], "recovery_started",
            {"node_id": node.node_id, "action": "retry_or_switch_executor",
             "evidence_ref": evidence_ref},
        )
        return NodeExecutionResult(
            node_id=node.node_id,
            executor_id=executor_id,
            status="failed",
            summary="演示注入：执行节点首次调用发生可控失效",
            evidence_refs=[evidence_ref],
            error_type="injected_node_failure",
            error_message=(
                f"运行期注入 {spec['injection_id']} 模拟节点 {node.node_id} "
                "的一次性执行器崩溃；调度器必须通过重试、切换或重规划恢复。"
            ),
        )

    def _select_input_file(self) -> Path:
        candidates: list[Path] = []
        input_root = (self.run_dir / "inputs").resolve()
        for raw in self.task_spec.get("input", {}).get("files", []):
            path = Path(raw).resolve()
            try:
                path.relative_to(input_root)
            except ValueError:
                continue
            if path.is_file() and 0 < path.stat().st_size <= 4_000_000:
                candidates.append(path)
        if not candidates:
            raise FileNotFoundError("没有可安全注入的数据副本（要求 run_dir/inputs 下且不超过 4MB）")
        preferred = {".md": 0, ".txt": 1, ".json": 2, ".csv": 3}
        return min(candidates, key=lambda path: (
            preferred.get(path.suffix.lower(), 9), path.stat().st_size, path.name,
        ))

    def _inject_data_anomaly(
        self, spec: dict, node: TaskNode, executor_id: str,
    ) -> NodeExecutionResult:
        target = self._select_input_file()
        root = self.run_dir / "injections" / spec["injection_id"]
        backup = root / "backup" / target.name
        quarantine = root / "quarantine" / f"{target.name}.corrupted"
        backup.parent.mkdir(parents=True, exist_ok=True)
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup)
        original_digest = _digest(target)
        evidence_relative = (
            Path("injections") / spec["injection_id"] / "evidence.json"
        ).as_posix()
        self.run_state.record_runtime_injection(
            spec["injection_id"], "triggered",
            {"node_id": node.node_id,
             "target": target.relative_to(self.run_dir).as_posix(),
             "original_sha256": original_digest},
        )
        with target.open("ab") as stream:
            stream.write(b"\n__RUNTIME_INJECTED_DATA_ANOMALY__\n")
        corrupted_digest = _digest(target)
        if corrupted_digest == original_digest:
            raise RuntimeError("数据异常注入没有改变输入哈希")
        shutil.copy2(target, quarantine)
        self.run_state.record_runtime_injection(
            spec["injection_id"], "detected",
            {"node_id": node.node_id, "detector": "sha256_input_integrity",
             "expected_sha256": original_digest,
             "actual_sha256": corrupted_digest,
             "quarantine_ref": quarantine.relative_to(self.run_dir).as_posix()},
        )
        self.run_state.record_runtime_injection(
            spec["injection_id"], "recovery_started",
            {"node_id": node.node_id, "action": "quarantine_and_restore",
             "evidence_ref": evidence_relative},
        )
        shutil.copy2(backup, target)
        restored_digest = _digest(target)
        if restored_digest != original_digest:
            raise RuntimeError("隔离后输入恢复哈希不一致")
        evidence_ref = self._write_evidence(spec, {
            "status": "input_restored_pending_node_retry",
            "target_node_id": node.node_id,
            "target": target.relative_to(self.run_dir).as_posix(),
            "detector": "sha256_input_integrity",
            "original_sha256": original_digest,
            "corrupted_sha256": corrupted_digest,
            "restored_sha256": restored_digest,
            "backup_ref": backup.relative_to(self.run_dir).as_posix(),
            "quarantine_ref": quarantine.relative_to(self.run_dir).as_posix(),
            "recovery_action": "quarantine_and_restore_then_retry",
        })
        return NodeExecutionResult(
            node_id=node.node_id,
            executor_id=executor_id,
            status="failed",
            summary="演示注入：输入完整性异常已检测、隔离并恢复",
            evidence_refs=[evidence_ref],
            error_type="injected_data_anomaly",
            error_message=(
                f"运行期注入 {spec['injection_id']} 在 {target.name} 检测到哈希异常；"
                "框架已隔离异常副本并恢复原始输入，当前节点需重新执行。"
            ),
        )

    def observe_completed_node(self, node: TaskNode) -> None:
        """Close a pending failure/anomaly recovery after a later real success."""

        items = self.run_state.get("runtime_injections", {}).get("items", {})
        for injection_id, item in list(items.items()):
            if (item.get("type") not in {"node_failure", "data_anomaly"}
                    or item.get("status") != "recovering"):
                continue
            target = item.get("target_node_id") or item.get("last_payload", {}).get("node_id")
            if target != node.node_id:
                continue
            history = item.get("history", [])
            evidence_ref = next((
                event.get("payload", {}).get("evidence_ref")
                for event in reversed(history)
                if event.get("payload", {}).get("evidence_ref")
            ), None)
            self.run_state.record_runtime_injection(
                injection_id, "recovered",
                {"node_id": node.node_id, "successful_attempt": node.attempts,
                 "selected_executor_id": node.assigned_executor,
                 "evidence_ref": evidence_ref},
            )
            evidence_path = self.run_dir / "injections" / injection_id / "evidence.json"
            if evidence_path.is_file():
                evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
                evidence.update({
                    "status": "recovered",
                    "successful_attempt": node.attempts,
                    "selected_executor_id": node.assigned_executor,
                })
                evidence_path.write_text(
                    json.dumps(evidence, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
