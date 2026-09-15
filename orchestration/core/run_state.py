"""Persisted, framework-owned runtime state machine."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
from orchestration.core.schemas import ArtifactQualityResult, ExecutionResult, ReviewDecision


class RunState:
    STAGES = ["prepare", "classify", "plan", "execute", "review", "report", "final_validate", "finish"]
    OUTCOMES = {"running", "success", "partial", "blocked", "failed"}

    def __init__(
        self,
        run_dir: str,
        task_type: str = "general_complex_task",
        code_policy: dict | None = None,
        required_artifacts: list[str] | None = None,
    ):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "run_state.json"
        self._save_defer_depth = 0
        self._save_dirty = False
        self.data = {
            "schema_version": "2.0",
            "stage": "prepare",
            "task_type": task_type,
            "code_policy": code_policy or {"mode": "none", "max_retries": 0},
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "execution": {
                "attempts": 0,
                "exit_code": None,
                "failed": False,
                "history": [],
            },
            "review": {
                "status": "pending",
                "can_generate_final_report": False,
                "issues": [],
                "required_artifacts_checked": [],
            },
            "blocking_issues": [],
            "artifacts": {
                "required": required_artifacts or [],
                "produced": [],
                "missing": list(required_artifacts or []),
                "metadata": {},
            },
            "artifact_contract": {},
            "artifact_quality": {"current": None, "history": []},
            "business_validation": {
                "policy": {},
                "status": "pending",
                "result_path": None,
                "result": None,
            },
            "requirement_acceptance": {
                "required": False,
                "status": "pending",
                "ledger_path": None,
                "ledger": None,
            },
            "horizon": {
                "mode": "one_shot",
                "epoch": 0,
                "active_node_ids": [],
                "completed_epoch_node_ids": [],
                "total_generated_nodes": 0,
                "total_completed_nodes": 0,
                "logical_step_count": 0,
                "checkpoints": [],
                "next_action": "execute",
            },
            "success_dimensions": {
                "execution": {"status": "pending", "evidence_refs": []},
                "artifacts": {"status": "pending", "evidence_refs": []},
                "business": {"status": "pending", "evidence_refs": []},
                "delivery": {"status": "pending", "evidence_refs": []},
            },
            "plan": None,
            "task_graph": None,
            "nodes": {},
            "artifact_provenance": [],
            "routing": [],
            "graph_outcome": "pending",
            "communication": {
                "metric_scope": "dependency_message_plane",
                "context_deliveries": 0,
                "dependency_edges_used": [],
                "planned_edge_count": 0,
                "possible_full_connection_edges": 0,
                "topology_density": 0.0,
                "full_history_broadcasts": 0,
                "messages": [],
                "raw_bytes": 0,
                "projected_bytes": 0,
                "delivered_bytes": 0,
                "raw_tokens_estimated": 0,
                "projected_tokens_estimated": 0,
                "delivered_tokens_estimated": 0,
                "compression_ratio": 1.0,
                "bytes_saved_ratio": 0.0,
                "projection_saved_ratio": 0.0,
                "duplicates_suppressed": 0,
                "duplicate_rate": 0.0,
                "global_duplicate_observations": 0,
                "global_duplicate_rate": 0.0,
                "artifact_references": 0,
                "budget_violations": 0,
                "compressor_usage": {},
                "compressor_attempts": {},
                "semantic_compressions": 0,
                "node_summaries": [],
            },
            "model_calls": {
                "count": 0,
                "prompt_tokens": 0,
                "prompt_tokens_estimated": 0,
                "raw_prompt_tokens_estimated": 0,
                "complete_prompt_compression_ratio": 1.0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "duration_seconds": 0.0,
                "calls": [],
                "prompt_budget_violations": 0,
            },
            "recovery": {
                "executor_switches": 0,
                "replans": 0,
                "history": [],
                "business_repairs": {
                    "rounds_used": 0,
                    "active": None,
                    "history": [],
                },
            },
            "runtime_injections": {
                "enabled": False,
                "profile": None,
                "requested_types": [],
                "items": {},
                "history": [],
                "recovered_count": 0,
            },
            "external_tools": {
                "policy": {},
                "capability_contract": {"required": [], "preferred": [], "reasons": {}},
                "total_invocations": 0,
                "successful_invocations": 0,
                "failed_invocations": 0,
                "providers": {},
                "invocations": [],
            },
            "workflow_memory": {
                "policy": {},
                "retrieval_calls": 0,
                "curation_calls": 0,
                "last_retrieval": None,
                "last_curation": None,
            },
            "runtime_memory": {
                "architecture": ["CoALA", "MemGPT-inspired", "Generative-Agents-inspired", "LLMLingua-2"],
                "enabled": False,
                "store_path": None,
                "capsule_dir": None,
                "capsules_built": 0,
                "last_capsule": None,
            },
            "failure": None,
            "events": [],
            "fallback": {"triggered": False, "reason": ""},
            "outcome": "running",
            "finished": False,
            "finished_at": None,
        }
        self._append_event("run_created", {"task_type": task_type})
        self._save()

    @classmethod
    def load_existing(cls, run_dir: str | Path) -> "RunState":
        """Reopen a persisted run without creating a second run identity.

        This is used by process-resume tooling.  The on-disk state remains the
        source of truth; callers still validate the horizon checkpoint before
        scheduling any pending nodes.
        """
        root = Path(run_dir)
        path = root / "run_state.json"
        if not path.is_file():
            raise FileNotFoundError(f"run_state 不存在: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data.get("schema_version"):
            raise ValueError("run_state 格式无效，拒绝恢复")
        instance = cls.__new__(cls)
        instance.run_dir = root
        instance.path = path
        instance.data = data
        instance._save_defer_depth = 0
        instance._save_dirty = False
        return instance

    def begin_save_batch(self) -> None:
        """Defer expensive full snapshots while keeping in-memory state live."""
        self._save_defer_depth += 1

    def end_save_batch(self, *, flush: bool = True) -> None:
        if self._save_defer_depth <= 0:
            raise RuntimeError("RunState save batch 未开始")
        self._save_defer_depth -= 1
        if flush and self._save_defer_depth == 0 and self._save_dirty:
            self.flush()

    def flush(self) -> None:
        """Atomically persist the latest coherent framework state."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(self.path)
        self._save_dirty = False

    def _save(self):
        if self._save_defer_depth:
            self._save_dirty = True
            return
        self.flush()

    def to_dict(self) -> dict:
        return json.loads(json.dumps(self.data, ensure_ascii=False))

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def configure(self, task_spec: dict):
        """Apply the finalized TaskSpec after the classification stage."""
        self.data["task_type"] = task_spec["task_type"]
        self.data["code_policy"] = task_spec["code_policy"]
        self.data["artifacts"]["required"] = list(task_spec.get("required_artifacts", []))
        self.data["artifact_contract"] = task_spec.get("artifact_contract", {})
        self.data["business_validation"]["policy"] = task_spec.get(
            "business_validation", {}
        )
        self.data["communication_policy"] = task_spec.get("communication_policy", {})
        self.data["routing_policy"] = task_spec.get("routing_policy", {})
        self.data["team_policy"] = task_spec.get("team_policy", {})
        self.data["recovery_policy"] = task_spec.get("recovery_policy", {})
        self.configure_runtime_injections(
            task_spec.get("runtime_injection_policy", {}), save=False,
        )
        self.data["external_tools"]["policy"] = task_spec.get(
            "external_tools_policy", {}
        )
        self.data["external_tools"]["capability_contract"] = task_spec.get(
            "capability_contract", {"required": [], "preferred": [], "reasons": {}}
        )
        self.data["workflow_memory"]["policy"] = task_spec.get("memory_policy", {})
        memory_policy = task_spec.get("memory_policy", {})
        runtime_memory = self.data["runtime_memory"]
        runtime_memory["enabled"] = bool(
            memory_policy.get("enabled", True)
            and memory_policy.get("runtime_enabled", True)
        )
        runtime_memory["store_path"] = memory_policy.get(
            "runtime_store", "memory/runtime_memory.sqlite3"
        )
        runtime_memory["capsule_dir"] = memory_policy.get(
            "capsule_dir", "memory/capsules"
        )
        self._append_event("task_configured", {
            "task_type": task_spec["task_type"],
            "artifact_contract": self.data["artifact_contract"],
        })
        self.refresh_artifacts()

    def configure_runtime_injections(self, policy: dict, *, save: bool = True):
        """Arm opt-in demo injections while preserving already recorded phases."""

        runtime = self.data.setdefault("runtime_injections", {
            "enabled": False, "profile": None, "requested_types": [], "items": {},
            "history": [], "recovered_count": 0,
        })
        injections = list(policy.get("injections", [])) if isinstance(policy, dict) else []
        runtime["enabled"] = bool(policy.get("enabled", False) and injections) \
            if isinstance(policy, dict) else False
        if isinstance(policy, dict) and policy.get("profile"):
            runtime["profile"] = policy.get("profile")
        runtime["requested_types"] = list(dict.fromkeys(
            str(item.get("type")) for item in injections if item.get("type")
        ))
        for spec in injections:
            injection_id = str(spec.get("injection_id") or "").strip()
            if not injection_id:
                continue
            if injection_id not in runtime["items"]:
                runtime["items"][injection_id] = {
                    "injection_id": injection_id,
                    "type": spec.get("type"),
                    "status": "armed",
                    "target_node_id": spec.get("target_node_id"),
                    "history": [],
                }
                self._append_event("runtime_injection_armed", {
                    "injection_id": injection_id,
                    "type": spec.get("type"),
                    "after_completed_nodes": spec.get("after_completed_nodes", 0),
                    "target_node_id": spec.get("target_node_id"),
                })
        if save:
            self._save()

    def record_runtime_injection(
        self, injection_id: str, phase: str, payload: dict | None = None,
    ) -> None:
        """Persist one ordered injection/recovery phase for audit and UI use."""

        allowed = {
            "triggered", "detected", "recovery_started", "recovered", "failed",
        }
        if phase not in allowed:
            raise ValueError(f"无效运行期注入阶段: {phase}")
        runtime = self.data.setdefault("runtime_injections", {
            "enabled": True, "profile": None, "requested_types": [], "items": {},
            "history": [], "recovered_count": 0,
        })
        item = runtime.setdefault("items", {}).get(injection_id)
        if item is None:
            raise KeyError(f"运行期注入未配置: {injection_id}")
        body = json.loads(json.dumps(payload or {}, ensure_ascii=False, default=str))
        status_by_phase = {
            "triggered": "triggered",
            "detected": "detected",
            "recovery_started": "recovering",
            "recovered": "recovered",
            "failed": "failed",
        }
        event = {
            "sequence": len(runtime.setdefault("history", [])) + 1,
            "time": datetime.now().isoformat(timespec="seconds"),
            "injection_id": injection_id,
            "type": item.get("type"),
            "phase": phase,
            "payload": body,
        }
        if body.get("node_id"):
            item["target_node_id"] = body["node_id"]
        was_recovered = item.get("status") == "recovered"
        item["status"] = status_by_phase[phase]
        item["last_payload"] = body
        item.setdefault("history", []).append(event)
        runtime["history"].append(event)
        if phase == "recovered" and not was_recovered:
            runtime["recovered_count"] = int(runtime.get("recovered_count", 0)) + 1
        self._append_event(f"runtime_injection_{phase}", {
            "injection_id": injection_id,
            "type": item.get("type"),
            **body,
        })
        self._save()

    def record_memory_retrieval(self, payload: dict):
        item = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
        self.data["workflow_memory"]["retrieval_calls"] += 1
        self.data["workflow_memory"]["last_retrieval"] = item
        self._append_event("workflow_memory_retrieved", item)
        self._save()

    def record_memory_curation(self, payload: dict):
        item = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
        self.data["workflow_memory"]["curation_calls"] += 1
        self.data["workflow_memory"]["last_curation"] = item
        self._append_event("workflow_memory_curated", item)
        self._save()

    def record_runtime_memory_capsule(self, payload: dict):
        """Record only capsule metadata; memory contents live in run memory files."""

        item = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
        runtime = self.data["runtime_memory"]
        runtime["capsules_built"] += 1
        runtime["last_capsule"] = item
        self._append_event("runtime_memory_capsule_built", item)
        self._save()

    def record_external_tool_invocation(self, payload: dict):
        """Persist one framework-observed external component invocation."""

        item = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
        provider = str(item.get("provider") or "unknown")
        success = bool(item.get("success", False))
        metrics = self.data["external_tools"]
        metrics["total_invocations"] += 1
        metrics["successful_invocations"] += int(success)
        metrics["failed_invocations"] += int(not success)
        provider_metrics = metrics["providers"].setdefault(provider, {
            "invocations": 0,
            "successes": 0,
            "failures": 0,
        })
        provider_metrics["invocations"] += 1
        provider_metrics["successes"] += int(success)
        provider_metrics["failures"] += int(not success)
        item["sequence"] = metrics["total_invocations"]
        item["time"] = datetime.now().isoformat(timespec="seconds")
        metrics["invocations"].append(item)
        self._append_event("external_component_invoked", item)
        self._save()

    def _append_event(self, event_type: str, payload: dict | None = None):
        self.data["events"].append({
            "sequence": len(self.data["events"]) + 1,
            "time": datetime.now().isoformat(timespec="seconds"),
            "stage": self.data.get("stage"),
            "type": event_type,
            "payload": payload or {},
        })

    def record_event(self, event_type: str, payload: dict | None = None):
        self._append_event(event_type, payload)
        self._save()

    def start_stage(self, stage: str):
        if stage not in self.STAGES:
            raise ValueError(f"未知工作流阶段: {stage}")
        current = self.data["stage"]
        if self.STAGES.index(stage) < self.STAGES.index(current):
            raise ValueError(f"不允许从 {current} 回退到 {stage}")
        if self.data["finished"]:
            raise RuntimeError("运行已结束，不能推进阶段")
        self.data["stage"] = stage
        self._append_event("stage_started", {"stage": stage})
        self._save()

    def record_horizon_epoch(
        self,
        *,
        epoch: int,
        mode: str,
        active_node_ids: list[str],
        completed_node_ids: list[str],
        total_generated_nodes: int,
        total_completed_nodes: int,
        logical_step_count: int,
        next_action: str,
        checkpoint_ref: str | None = None,
    ) -> None:
        """Persist one bounded long-horizon execution window and its resume ref."""

        horizon = self.data.setdefault("horizon", {})
        horizon.update({
            "mode": mode,
            "epoch": int(epoch),
            "active_node_ids": list(active_node_ids),
            "completed_epoch_node_ids": list(completed_node_ids),
            "total_generated_nodes": int(total_generated_nodes),
            "total_completed_nodes": int(total_completed_nodes),
            "logical_step_count": int(logical_step_count),
            "next_action": next_action,
        })
        if checkpoint_ref:
            checkpoints = horizon.setdefault("checkpoints", [])
            checkpoints.append({
                "epoch": int(epoch),
                "ref": checkpoint_ref,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            })
        self._append_event("horizon_epoch_recorded", {
            "epoch": int(epoch),
            "mode": mode,
            "active_node_ids": list(active_node_ids),
            "completed_node_ids": list(completed_node_ids),
            "total_generated_nodes": int(total_generated_nodes),
            "total_completed_nodes": int(total_completed_nodes),
            "logical_step_count": int(logical_step_count),
            "next_action": next_action,
            "checkpoint_ref": checkpoint_ref,
        })
        self._save()

    def record_plan(self, plan: dict):
        self.data["plan"] = plan
        self._append_event("plan_recorded", {
            "execution_mode": plan.get("execution_mode", "fixed_pipeline"),
            "requires_code": plan.get("requires_code", False),
            "use_research": plan.get("use_research", False),
            "use_reasoning": plan.get("use_reasoning", False),
        })
        self._save()

    def record_task_graph(self, graph):
        """Persist the validated graph summary; runtime state remains framework-owned."""
        payload = graph.model_dump(mode="json") if hasattr(graph, "model_dump") else graph
        self.data["task_graph"] = {
            "graph_id": payload["graph_id"],
            "version": payload.get("version", 1),
            "goal": payload.get("goal", ""),
            "path": "task_graph.json",
            "node_count": len(payload.get("nodes", [])),
        }
        previous_nodes = self.data.get("nodes", {})
        self.data["nodes"] = {
            node["node_id"]: {
                "status": node.get("status", "pending"),
                "capability": node.get("capability", ""),
                "dependencies": node.get("dependencies", []),
                "executor_id": node.get("assigned_executor"),
                "attempts": node.get("attempts", 0),
                "result": node.get("result"),
                "error": node.get("error"),
            }
            for node in payload.get("nodes", [])
        }
        for node_id, node in self.data["nodes"].items():
            previous = previous_nodes.get(node_id, {})
            if node.get("status") == "completed" and previous.get("artifact_manifest"):
                node["artifact_manifest"] = previous["artifact_manifest"]
        self.data["graph_outcome"] = "running"
        self._append_event("task_graph_recorded", self.data["task_graph"])
        self._save()

    def record_routing_decision(self, decision: dict):
        self.data["routing"].append(decision)
        node_id = decision.get("node_id")
        if node_id in self.data["nodes"]:
            self.data["nodes"][node_id]["executor_id"] = decision.get("selected_executor_id")
        self._append_event("routing_decision_recorded", decision)
        self._save()

    def start_node(self, node_id: str, executor_id: str, attempt: int):
        if self.data["stage"] != "execute":
            raise RuntimeError("只能在 execute 阶段启动任务图节点")
        node = self.data["nodes"].setdefault(node_id, {})
        node.update({
            "status": "running",
            "executor_id": executor_id,
            "attempts": attempt,
            "started_at": datetime.now().isoformat(timespec="seconds"),
        })
        self._append_event("graph_node_started", {
            "node_id": node_id, "executor_id": executor_id, "attempt": attempt,
        })
        self._save()

    def record_node_context(
        self,
        node_id: str,
        dependency_ids: list[str],
        metrics: list[dict] | None = None,
        aggregate: dict | None = None,
    ):
        """Record sparse delivery metadata, never the full prompt or unrelated results."""
        communication = self.data["communication"]
        communication["context_deliveries"] += 1
        for dependency_id in dependency_ids:
            edge = [dependency_id, node_id]
            if edge not in communication["dependency_edges_used"]:
                communication["dependency_edges_used"].append(edge)
        self._append_event("node_context_built", {
            "node_id": node_id,
            "direct_dependency_ids": dependency_ids,
            "message_count": len(metrics or []),
            "aggregate": aggregate or {},
        })
        if aggregate:
            communication["node_summaries"].append(aggregate)
        for item in metrics or []:
            self._record_message_metrics(item)
        self._save()

    def _record_message_metrics(self, metrics: dict):
        """Aggregate one framework-routed message without storing its payload."""
        communication = self.data["communication"]
        payload = {
            "message_id": metrics.get("message_id"),
            "source_node": metrics.get("source_node") or metrics.get("source"),
            "target_node": metrics.get("target_node") or metrics.get("target"),
            "message_type": metrics.get("message_type", "dependency_result"),
            "visibility": metrics.get("visibility", "direct_dependencies"),
            "confidence": float(metrics.get("confidence", 1.0)),
            "payload_hash": metrics.get("payload_hash"),
            "payload_ref": metrics.get("payload_ref"),
            "evidence_refs": list(metrics.get("evidence_refs", [])),
            "raw_bytes": int(metrics.get("raw_bytes", 0)),
            "projected_bytes": int(metrics.get("projected_bytes", 0)),
            "delivered_bytes": int(metrics.get("delivered_bytes", 0)),
            "raw_tokens_estimated": int(metrics.get("raw_tokens", 0)),
            "delivered_tokens_estimated": int(metrics.get("delivered_tokens", 0)),
            "projected_tokens_estimated": int(metrics.get("projected_tokens", 0)),
            "compressor": metrics.get("compressor", "none"),
            "attempted_compressor": metrics.get("attempted_compressor"),
            "compressed": bool(metrics.get("was_compressed", False)),
            "compression_fallback_used": bool(
                metrics.get("compression_fallback_used", False)
            ),
            "compression_warnings": list(metrics.get("compression_warnings", [])),
            "duplicate": bool(metrics.get("exact_duplicate", False)),
            "duplicate_scope": metrics.get("duplicate_scope", "none"),
            "global_duplicate_observation": bool(
                metrics.get("global_duplicate_observation", False)
            ),
            "reference_only": bool(metrics.get("reference_only", False)),
            "within_budget": bool(metrics.get("within_message_budget", True)),
        }
        communication["messages"].append(payload)
        communication["raw_bytes"] += payload["raw_bytes"]
        communication["projected_bytes"] += payload["projected_bytes"]
        communication["delivered_bytes"] += payload["delivered_bytes"]
        communication["raw_tokens_estimated"] += payload["raw_tokens_estimated"]
        communication["projected_tokens_estimated"] += payload["projected_tokens_estimated"]
        communication["delivered_tokens_estimated"] += payload["delivered_tokens_estimated"]
        if payload["duplicate"]:
            communication["duplicates_suppressed"] += 1
        if payload["global_duplicate_observation"]:
            communication["global_duplicate_observations"] += 1
        if payload["reference_only"]:
            communication["artifact_references"] += 1
        if not payload["within_budget"]:
            communication["budget_violations"] += 1
        compressor_usage = communication["compressor_usage"]
        compressor_usage[payload["compressor"]] = compressor_usage.get(payload["compressor"], 0) + 1
        attempted = payload.get("attempted_compressor")
        if attempted:
            attempts = communication["compressor_attempts"]
            attempts[attempted] = attempts.get(attempted, 0) + 1
        if payload["compressor"] == "llmlingua" and payload["compressed"]:
            communication["semantic_compressions"] += 1
        delivered = communication["delivered_bytes"]
        raw = communication["raw_bytes"]
        communication["compression_ratio"] = round(raw / delivered, 6) if delivered else 1.0
        communication["bytes_saved_ratio"] = round((raw - delivered) / raw, 6) if raw else 0.0
        projected = communication["projected_bytes"]
        communication["projection_saved_ratio"] = round(
            (raw - projected) / raw, 6
        ) if raw else 0.0
        count = len(communication["messages"])
        communication["duplicate_rate"] = round(
            communication["duplicates_suppressed"] / count, 6
        ) if count else 0.0
        communication["global_duplicate_rate"] = round(
            communication["global_duplicate_observations"] / count, 6
        ) if count else 0.0

    def record_model_call(self, metrics: dict):
        """Record provider token usage, or deterministic estimates when unavailable."""
        model_calls = self.data["model_calls"]
        delivered_estimate = int(
            metrics.get("prompt_tokens_estimated", metrics.get("prompt_tokens", 0))
        )
        raw_estimate = metrics.get("raw_prompt_tokens_estimated")
        if raw_estimate is None and metrics.get("node_id"):
            summary = next((
                item for item in reversed(self.data["communication"]["node_summaries"])
                if item.get("target_node") == metrics.get("node_id")
            ), None)
            if summary:
                raw_estimate = max(
                    delivered_estimate,
                    delivered_estimate
                    - int(summary.get("delivered_tokens", 0))
                    + int(summary.get("raw_tokens", 0)),
                )
        raw_estimate = int(raw_estimate if raw_estimate is not None else delivered_estimate)
        payload = {
            "stage": metrics.get("stage", self.data.get("stage")),
            "node_id": metrics.get("node_id"),
            "agent": metrics.get("agent", "unknown"),
            "prompt_tokens": int(metrics.get("prompt_tokens", 0)),
            "prompt_tokens_estimated": delivered_estimate,
            "raw_prompt_tokens_estimated": raw_estimate,
            "prompt_bytes": int(metrics.get("prompt_bytes", 0)),
            "prompt_budget_tokens": metrics.get("prompt_budget_tokens"),
            "within_prompt_budget": bool(metrics.get("within_prompt_budget", True)),
            "completion_tokens": int(metrics.get("completion_tokens", 0)),
            "duration_seconds": round(float(metrics.get("duration_seconds", 0.0)), 6),
            "usage_source": metrics.get("usage_source", "estimated"),
            "attempt": int(metrics.get("attempt", 1)),
        }
        payload["total_tokens"] = payload["prompt_tokens"] + payload["completion_tokens"]
        payload["complete_prompt_compression_ratio"] = round(
            raw_estimate / delivered_estimate, 6
        ) if delivered_estimate else 1.0
        model_calls["count"] += 1
        model_calls["prompt_tokens"] += payload["prompt_tokens"]
        model_calls["prompt_tokens_estimated"] += payload["prompt_tokens_estimated"]
        model_calls["raw_prompt_tokens_estimated"] += payload["raw_prompt_tokens_estimated"]
        model_calls["complete_prompt_compression_ratio"] = round(
            model_calls["raw_prompt_tokens_estimated"]
            / model_calls["prompt_tokens_estimated"],
            6,
        ) if model_calls["prompt_tokens_estimated"] else 1.0
        model_calls["completion_tokens"] += payload["completion_tokens"]
        model_calls["total_tokens"] += payload["total_tokens"]
        model_calls["duration_seconds"] = round(
            model_calls["duration_seconds"] + payload["duration_seconds"], 6
        )
        model_calls["calls"].append(payload)
        self._append_event("model_call_recorded", payload)
        self._save()

    def record_communication_budget_violation(self, node_id: str, reason: str):
        communication = self.data["communication"]
        communication["budget_violations"] += 1
        self._append_event("communication_budget_exceeded", {
            "node_id": node_id,
            "reason": reason,
        })
        self._save()

    def record_prompt_budget_violation(self, metrics: dict):
        payload = {
            "stage": metrics.get("stage", self.data.get("stage")),
            "node_id": metrics.get("node_id"),
            "agent": metrics.get("agent", "unknown"),
            "prompt_tokens_estimated": int(metrics.get("prompt_tokens_estimated", 0)),
            "prompt_bytes": int(metrics.get("prompt_bytes", 0)),
            "prompt_budget_tokens": int(metrics.get("prompt_budget_tokens", 0)),
        }
        self.data["model_calls"]["prompt_budget_violations"] += 1
        self._append_event("model_prompt_budget_exceeded", payload)
        self._save()

    def record_prompt_budget_warning(self, metrics: dict):
        """Record a soft optimization warning without marking the run failed."""
        payload = {
            "stage": metrics.get("stage", self.data.get("stage")),
            "node_id": metrics.get("node_id"),
            "agent": metrics.get("agent", "unknown"),
            "prompt_tokens_estimated": int(metrics.get("prompt_tokens_estimated", 0)),
            "prompt_bytes": int(metrics.get("prompt_bytes", 0)),
            "prompt_budget_tokens": metrics.get("prompt_budget_tokens"),
            "warning": "soft_prompt_budget_exceeded",
        }
        self.data.setdefault("model_calls", {}).setdefault(
            "soft_prompt_budget_warnings", 0
        )
        self.data["model_calls"]["soft_prompt_budget_warnings"] += 1
        self._append_event("soft_prompt_budget_exceeded", payload)
        self._save()

    def record_executor_switch(self, node_id: str, previous: str, selected: str):
        recovery = self.data["recovery"]
        recovery["executor_switches"] += 1
        payload = {
            "action": "switch_executor",
            "node_id": node_id,
            "previous_executor_id": previous,
            "selected_executor_id": selected,
        }
        recovery["history"].append(payload)
        self._append_event("executor_switched", payload)
        self._save()

    def record_replan(self, old_graph_id: str, new_graph_id: str, reason: str):
        recovery = self.data["recovery"]
        recovery["replans"] += 1
        payload = {
            "action": "replan",
            "old_graph_id": old_graph_id,
            "new_graph_id": new_graph_id,
            "reason": reason,
        }
        recovery["history"].append(payload)
        self._append_event("task_graph_replanned", payload)
        self._save()

    def record_node_result(self, node_id: str, result: dict):
        node = self.data["nodes"].setdefault(node_id, {})
        status = result.get("status", "failed")
        node.update({
            "status": status,
            "result": result if status == "completed" else None,
            "error": None if status == "completed" else {
                "error_type": result.get("error_type"),
                "error_message": result.get("error_message"),
            },
            "finished_at": datetime.now().isoformat(timespec="seconds"),
        })
        self._append_event("graph_node_result_recorded", {
            "node_id": node_id,
            "status": status,
            "executor_id": result.get("executor_id"),
            "output_artifacts": result.get("output_artifacts", []),
        })
        self.refresh_artifacts(save=False)
        self._save()

    def record_node_artifacts(
        self,
        node_id: str,
        graph_version: int,
        relative_paths: list[str],
    ):
        """Bind verified files to the node and graph version that produced them."""
        manifest: list[dict] = []
        for relative in relative_paths:
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"不安全的节点产物路径: {relative}")
            actual = self.run_dir / path
            if not actual.is_file():
                raise ValueError(f"无法登记不存在的节点产物: {relative}")
            item = {
                "path": path.as_posix(),
                "source_node": node_id,
                "graph_version": graph_version,
                "size": actual.stat().st_size,
                "sha256": hashlib.sha256(actual.read_bytes()).hexdigest(),
                "status": "verified",
            }
            manifest.append(item)
            self.data["artifact_provenance"].append(item)
        self.data["nodes"].setdefault(node_id, {})["artifact_manifest"] = manifest
        if manifest:
            self._append_event("node_artifacts_bound", {
                "node_id": node_id,
                "graph_version": graph_version,
                "artifacts": manifest,
            })
        self._save()

    def record_untrusted_node_artifacts(
        self,
        node_id: str,
        graph_version: int,
        relative_paths: list[str],
    ):
        """Mark files left by a failed node so they cannot imply completion."""
        recorded: list[dict] = []
        for relative in relative_paths:
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts:
                continue
            actual = self.run_dir / path
            if not actual.is_file():
                continue
            item = {
                "path": path.as_posix(),
                "source_node": node_id,
                "graph_version": graph_version,
                "size": actual.stat().st_size,
                "sha256": hashlib.sha256(actual.read_bytes()).hexdigest(),
                "status": "untrusted_partial",
            }
            self.data["artifact_provenance"].append(item)
            recorded.append(item)
        if recorded:
            self._append_event("failed_node_artifacts_marked_untrusted", {
                "node_id": node_id,
                "graph_version": graph_version,
                "artifacts": recorded,
            })
            self._save()

    def schedule_node_recovery(self, decision: dict):
        """Persist a framework recovery action before the next node attempt."""
        node_id = decision["node_id"]
        node = self.data["nodes"].setdefault(node_id, {})
        node["status"] = "pending"
        node["result"] = None
        node["error"] = None
        self.data["recovery"]["history"].append(decision)
        self._append_event("node_recovery_scheduled", decision)
        self._save()

    def block_node(self, node_id: str, failed_dependencies: list[str]):
        node = self.data["nodes"].setdefault(node_id, {})
        node["status"] = "blocked"
        node["error"] = {"failed_dependencies": failed_dependencies}
        self._append_event("graph_node_blocked", {
            "node_id": node_id,
            "failed_dependencies": failed_dependencies,
        })
        self._save()

    def finish_graph(self, outcome: str):
        if outcome not in {"success", "blocked", "failed"}:
            raise ValueError(f"无效任务图结果: {outcome}")
        self.data["graph_outcome"] = outcome
        self._append_event("task_graph_finished", {"outcome": outcome})
        self._save()

    def record_communication_metrics(self, node_count: int, planned_edge_count: int):
        possible = node_count * (node_count - 1)
        communication = self.data["communication"]
        communication["planned_edge_count"] = planned_edge_count
        communication["possible_full_connection_edges"] = possible
        communication["topology_density"] = (
            round(planned_edge_count / possible, 6) if possible else 0.0
        )
        # The NodeExecutionContext validator and scheduler construction prohibit
        # whole-graph result injection; keep this explicit as an auditable metric.
        communication["full_history_broadcasts"] = 0
        self._append_event("communication_metrics_recorded", communication.copy())
        self._save()

    def record_execution(self, result: ExecutionResult | dict):
        if self.data["stage"] != "execute":
            raise RuntimeError("只能在 execute 阶段记录执行结果")
        parsed = result if isinstance(result, ExecutionResult) else ExecutionResult.model_validate(result)
        execution = self.data["execution"]
        execution["attempts"] = parsed.attempt
        execution["exit_code"] = parsed.exit_code
        execution["failed"] = parsed.exit_code != 0
        execution["history"].append(parsed.model_dump())
        self.data["success_dimensions"]["execution"] = {
            "status": "passed" if parsed.exit_code == 0 else "failed",
            "evidence_refs": list(parsed.produced_files),
        }
        self._append_event("execution_recorded", {
            "attempt": parsed.attempt,
            "phase": parsed.phase,
            "exit_code": parsed.exit_code,
            "produced_files": parsed.produced_files,
        })
        for path in parsed.produced_files:
            self.record_artifact(path, save=False)
        self.refresh_artifacts(save=False)
        self._save()

    def record_artifact_quality(self, result: ArtifactQualityResult | dict):
        parsed = result if isinstance(result, ArtifactQualityResult) else ArtifactQualityResult.model_validate(result)
        payload = parsed.model_dump()
        self.data["artifact_quality"]["current"] = payload
        self.data["artifact_quality"]["history"].append(payload)
        self.data["success_dimensions"]["artifacts"] = {
            "status": parsed.status,
            "evidence_refs": list(parsed.checked_artifacts),
        }
        self._append_event("artifact_quality_recorded", payload)
        if parsed.status == "failed":
            existing = self.data.get("failure")
            upstream_failures = {
                "input_normalization_failure",
                "code_generation_failure",
                "execution_failure",
                "report_generation_failure",
            }
            if existing and existing.get("failure_type") in upstream_failures:
                self._append_event("derived_failure_recorded", {
                    "failure": payload,
                    "root_failure_preserved": existing,
                })
            else:
                self.set_failure(
                    parsed.failure_type,
                    parsed.repair_target,
                    parsed.resume_stage,
                    parsed.issues,
                    save=False,
                )
        else:
            existing = self.data.get("failure")
            clearable_failures = {
                "artifact_missing", "artifact_json_invalid", "artifact_schema_failure",
                "code_generation_failure", "execution_failure",
            }
            if not existing or existing.get("failure_type") in clearable_failures:
                self.clear_failure(save=False)
        self._save()

    def record_business_validation(self, result: dict, result_path: str):
        """Persist the framework-owned business gate without agent authority."""
        status = str(result.get("status", "unverified"))
        if status not in {
            "passed", "failed", "unverified", "error", "not_applicable",
        }:
            raise ValueError(f"无效业务验证状态: {status}")
        payload = json.loads(json.dumps(result, ensure_ascii=False, default=str))
        self.data["business_validation"].update({
            "status": status,
            "result_path": result_path,
            "result": payload,
        })
        self.data["success_dimensions"]["business"] = {
            "status": status,
            "evidence_refs": list(payload.get("evidence_refs", [])),
        }
        self._append_event("business_validation_recorded", {
            "status": status,
            "result_path": result_path,
            "failed_required_checks": payload.get(
                "failed_required_checks", []
            ),
        })
        if (
            self.data["business_validation"].get("policy", {}).get(
                "mode", payload.get("mode")
            ) == "required"
            and status != "passed"
        ):
            self.set_failure(
                "business_validation_failure",
                "business_result",
                "review",
                [
                    f"业务验证状态为 {status}",
                    *list(payload.get("failed_required_checks", [])),
                    *list(payload.get("unresolved_validators", [])),
                ],
                save=False,
            )
        elif status == "passed":
            existing = self.data.get("failure")
            if existing and existing.get("failure_type") == "business_validation_failure":
                self.clear_failure(save=False)
        self._save()

    def record_requirement_ledger(self, ledger: dict, ledger_path: str):
        """Persist framework-evaluated requirement closure."""
        status = str(ledger.get("status", "unverified"))
        if status not in {"passed", "failed", "unverified"}:
            raise ValueError(f"无效需求验收状态: {status}")
        payload = json.loads(json.dumps(
            ledger, ensure_ascii=False, default=str,
        ))
        self.data["requirement_acceptance"] = {
            "required": True,
            "status": status,
            "ledger_path": ledger_path,
            "ledger": payload,
        }
        self._append_event("requirement_ledger_recorded", {
            "status": status,
            "ledger_path": ledger_path,
            "failed_blocking_criteria": payload.get(
                "failed_blocking_criteria", []
            ),
            "unverified_blocking_criteria": payload.get(
                "unverified_blocking_criteria", []
            ),
        })
        self._save()

    def begin_business_repair(
        self,
        *,
        round_number: int,
        target_node_ids: list[str],
        affected_node_ids: list[str],
        issue_ids: list[str],
        repair_contexts: dict[str, dict],
    ):
        """Persist a post-validation repair checkpoint before replay starts."""

        repairs = self.data["recovery"].setdefault("business_repairs", {
            "rounds_used": 0, "active": None, "history": [],
        })
        if round_number != int(repairs.get("rounds_used", 0)) + 1:
            raise ValueError("业务修复轮次必须严格递增")
        payload = {
            "round": round_number,
            "status": "running",
            "target_node_ids": list(target_node_ids),
            "affected_node_ids": list(affected_node_ids),
            "issue_ids": list(issue_ids),
            "repair_contexts": json.loads(json.dumps(
                repair_contexts, ensure_ascii=False, default=str,
            )),
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }
        repairs["rounds_used"] = round_number
        repairs["active"] = payload
        repairs["history"].append(payload)
        for node_id in affected_node_ids:
            node = self.data["nodes"].setdefault(node_id, {})
            node["status"] = "pending"
            node["result"] = None
            node["error"] = None
        self._append_event("business_repair_started", payload)
        self._save()

    def finish_business_repair(
        self,
        *,
        round_number: int,
        status: str,
        validation_status: str,
        resolved_issue_ids: list[str],
        remaining_issue_ids: list[str],
        progress: bool,
    ):
        """Close one repair checkpoint without allowing an Agent to claim success."""

        if status not in {"repaired", "retryable", "blocked", "failed"}:
            raise ValueError(f"无效业务修复状态: {status}")
        repairs = self.data["recovery"].setdefault("business_repairs", {
            "rounds_used": 0, "active": None, "history": [],
        })
        active = repairs.get("active")
        if not active or int(active.get("round", -1)) != round_number:
            raise RuntimeError("没有匹配的活动业务修复轮次")
        active.update({
            "status": status,
            "validation_status": validation_status,
            "resolved_issue_ids": list(resolved_issue_ids),
            "remaining_issue_ids": list(remaining_issue_ids),
            "progress": bool(progress),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
        })
        repairs["active"] = None
        self._append_event("business_repair_finished", active)
        self._save()

    def record_delivery_outcome(
        self, status: str, evidence_refs: list[str] | None = None,
    ):
        if status not in {"passed", "failed", "partial", "pending"}:
            raise ValueError(f"无效交付状态: {status}")
        self.data["success_dimensions"]["delivery"] = {
            "status": status,
            "evidence_refs": list(evidence_refs or []),
        }
        self._append_event("delivery_outcome_recorded", {"status": status})
        self._save()

    def set_failure(
        self,
        failure_type: str,
        repair_target: str,
        resume_stage: str,
        issues: list[str],
        save: bool = True,
    ):
        self.data["failure"] = {
            "failure_type": failure_type,
            "repair_target": repair_target,
            "resume_stage": resume_stage,
            "issues": issues,
        }
        self._append_event("failure_routed", self.data["failure"])
        if save:
            self._save()

    def clear_failure(self, save: bool = True):
        if self.data.get("failure") is not None:
            self._append_event("failure_cleared", self.data["failure"])
        self.data["failure"] = None
        if save:
            self._save()

    def record_artifact(self, relative_path: str, save: bool = True):
        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"产物路径必须位于 run_dir 内: {relative_path}")
        normalized = path.as_posix()
        actual = self.run_dir / path
        produced = self.data["artifacts"]["produced"]
        if actual.is_file() and normalized not in produced:
            produced.append(normalized)
            produced.sort()
        if save:
            self.refresh_artifacts()

    def refresh_artifacts(self, save: bool = True):
        if self._save_defer_depth and save is False:
            # Defer directory-wide scans and SHA256 hashing to the epoch
            # boundary; otherwise long chains repeatedly hash old artifacts.
            self._save_dirty = True
            return
        required = self.data["artifacts"]["required"]
        self.data["artifacts"]["produced"] = sorted(
            path.relative_to(self.run_dir).as_posix()
            for path in self.run_dir.rglob("*")
            if path.is_file() and "inputs" not in path.relative_to(self.run_dir).parts
        )
        self.data["artifacts"]["missing"] = [
            path for path in required if not (self.run_dir / path).is_file()
        ]
        metadata = {}
        # These framework files are intentionally rewritten after intermediate
        # refreshes.  Embedding their hashes inside RunState would either be
        # self-referential (run_state.json) or immediately stale (trace).
        mutable_framework_files = {"run_state.json", "agent_trace.md"}
        for relative in self.data["artifacts"]["produced"]:
            if relative in mutable_framework_files:
                continue
            path = self.run_dir / relative
            try:
                metadata[relative] = {
                    "size": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            except OSError:
                continue
        self.data["artifacts"]["metadata"] = metadata
        if save:
            self._save()

    def record_review(self, decision: ReviewDecision | dict):
        if self.data["stage"] not in {"review", "final_validate"}:
            raise RuntimeError("只能在 review 或 final_validate 阶段记录审查结果")
        parsed = decision if isinstance(decision, ReviewDecision) else ReviewDecision.model_validate(decision)
        payload = parsed.model_dump()
        unresolved = self.get_unresolved_blocking_issues()
        if unresolved:
            payload["status"] = "failed"
            payload["can_generate_final_report"] = False
            payload["issues"] = list(dict.fromkeys(
                list(payload.get("issues", []))
                + [item["description"] for item in unresolved]
            ))
        self.data["review"] = payload
        self._append_event("review_recorded", payload)
        self._save()

    def allow_report(self) -> bool:
        review = self.data["review"]
        business = self.data.get("business_validation", {})
        acceptance = self.data.get("requirement_acceptance", {})
        business_blocked = (
            business.get("policy", {}).get("mode") == "required"
            and business.get("status") != "passed"
        )
        return bool(
            review["can_generate_final_report"]
            and review["status"] in {"passed", "partial"}
            and not self.get_unresolved_blocking_issues()
            and not business_blocked
            and (
                not acceptance.get("required", False)
                or acceptance.get("status") == "passed"
            )
        )

    def record_blocking_issues(
        self,
        issues: list[str] | list[dict],
        *,
        source: str,
    ):
        """Merge blockers monotonically; retries cannot silently erase them."""
        existing = {
            item.get("issue_id"): item
            for item in self.data.get("blocking_issues", [])
            if item.get("issue_id")
        }
        for raw in issues or []:
            if isinstance(raw, dict):
                structured = json.loads(json.dumps(
                    raw, ensure_ascii=False, default=str,
                ))
                description = str(
                    raw.get("description")
                    or raw.get("issue")
                    or raw.get("reason")
                    or raw
                )
                issue_id = str(raw.get("issue_id") or "")
                severity = str(raw.get("severity") or "blocking")
            else:
                structured = {}
                description = str(raw)
                issue_id = ""
                severity = "blocking"
            if not description.strip():
                continue
            if not issue_id:
                issue_id = hashlib.sha256(
                    f"{source}:{description}".encode("utf-8")
                ).hexdigest()[:16]
            previous = existing.get(issue_id, {})
            now = datetime.now().isoformat(timespec="seconds")
            existing[issue_id] = {
                **previous,
                **structured,
                "issue_id": issue_id,
                "source": source,
                "severity": severity,
                "description": description,
                "status": "open",
                "first_seen_at": previous.get("first_seen_at", now),
                "last_seen_at": now,
                "occurrence_count": int(previous.get("occurrence_count", 0)) + 1,
                # A new observation reopens the issue even if an older run
                # had resolved the same fingerprint.
                "resolved": False,
            }
        self.data["blocking_issues"] = list(existing.values())
        self._append_event("blocking_issues_recorded", {
            "source": source,
            "count": len(issues or []),
            "unresolved_count": len(self.get_unresolved_blocking_issues()),
        })
        self._save()

    def get_unresolved_blocking_issues(self) -> list[dict]:
        return [
            dict(item)
            for item in self.data.get("blocking_issues", [])
            if not item.get("resolved", False)
        ]

    def resolve_blocking_issue(self, issue_id: str, evidence_refs: list[str]):
        """Close a blocker only after the framework has verified repair evidence."""
        if not evidence_refs:
            raise ValueError("关闭阻断问题必须提供修复证据")
        found = False
        for item in self.data.get("blocking_issues", []):
            if item.get("issue_id") == issue_id:
                found = True
                item["resolved"] = True
                item["status"] = "resolved"
                item["resolved_at"] = datetime.now().isoformat(timespec="seconds")
                item["resolution_evidence_refs"] = list(evidence_refs)
        if not found:
            raise KeyError(f"阻断问题不存在: {issue_id}")
        self._append_event("blocking_issue_resolved", {
            "issue_id": issue_id,
            "evidence_refs": list(evidence_refs),
        })
        self._save()

    def trigger_fallback(self, reason: str):
        self.data["fallback"] = {"triggered": True, "reason": reason}
        self._append_event("fallback_triggered", {"reason": reason})
        self._save()

    def finish(self, outcome: str):
        if outcome not in self.OUTCOMES - {"running"}:
            raise ValueError(f"无效运行结果: {outcome}")
        self.data["stage"] = "finish"
        self.data["outcome"] = outcome
        self.data["finished"] = True
        self.data["finished_at"] = datetime.now().isoformat(timespec="seconds")
        self._append_event("run_finished", {"outcome": outcome})
        self.refresh_artifacts(save=False)
        self._save()
