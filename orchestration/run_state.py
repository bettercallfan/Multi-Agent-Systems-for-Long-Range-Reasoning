"""Persisted, framework-owned runtime state machine."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
from orchestration.schemas import ArtifactQualityResult, ExecutionResult, ReviewDecision


class RunState:
    STAGES = ["prepare", "classify", "plan", "execute", "review", "report", "final_validate", "finish"]
    OUTCOMES = {"running", "success", "partial", "failed"}

    def __init__(
        self,
        run_dir: str,
        task_type: str = "general_complex_task",
        code_policy: dict | None = None,
        required_artifacts: list[str] | None = None,
    ):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "run_state.json"
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
            "artifacts": {
                "required": required_artifacts or [],
                "produced": [],
                "missing": list(required_artifacts or []),
                "metadata": {},
            },
            "artifact_contract": {},
            "artifact_quality": {"current": None, "history": []},
            "plan": None,
            "task_graph": None,
            "nodes": {},
            "routing": [],
            "graph_outcome": "pending",
            "communication": {
                "context_deliveries": 0,
                "dependency_edges_used": [],
                "planned_edge_count": 0,
                "possible_full_connection_edges": 0,
                "topology_density": 0.0,
                "full_history_broadcasts": 0,
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

    def _save(self):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

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
        self._append_event("task_configured", {
            "task_type": task_spec["task_type"],
            "plugin_id": task_spec.get("plugin_id", "generic"),
            "artifact_contract": self.data["artifact_contract"],
        })
        self.refresh_artifacts()

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

    def record_node_context(self, node_id: str, dependency_ids: list[str]):
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
        })
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

    def retry_node(self, node_id: str, attempts: int, max_retries: int):
        node = self.data["nodes"].setdefault(node_id, {})
        node["status"] = "pending"
        node["attempts"] = attempts
        self._append_event("graph_node_retry_scheduled", {
            "node_id": node_id,
            "attempts": attempts,
            "max_retries": max_retries,
        })
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
        if outcome not in {"success", "failed"}:
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
        for relative in self.data["artifacts"]["produced"]:
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
        self.data["review"] = parsed.model_dump()
        self._append_event("review_recorded", parsed.model_dump())
        self._save()

    def allow_report(self) -> bool:
        review = self.data["review"]
        return bool(review["can_generate_final_report"] and review["status"] in {"passed", "partial"})

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

    # Compatibility helper for older callers. New code should use the methods above.
    def set_stage(self, stage: str):
        self.start_stage(stage)
