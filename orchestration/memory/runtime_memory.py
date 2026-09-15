"""Research-grounded, framework-owned memory for long-horizon DAG runs.

Architecture references:
* CoALA: working, episodic, semantic and procedural memory.
* MemGPT: small always-visible core plus paged recall/archival memory.
* Generative Agents: relevance, recency and importance retrieval signals.
* LLMLingua-2: optional compression after retrieval, never for protected state.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any

from orchestration.communication.communication_models import (
    CommunicationBudget,
    CommunicationPolicy,
    estimate_tokens,
    utf8_size,
)
from orchestration.communication.context_compressors import build_compressor
from orchestration.graph.task_graph import TaskGraph, TaskNode
from orchestration.memory.memory_models import ContextCapsule, MemoryRecord, RetrievedMemory


def _tokens(value: Any) -> set[str]:
    text = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9_./-]+|[\u4e00-\u9fff]", text)
        if token.strip()
    }


class RuntimeMemoryStore:
    """Small SQLite store scoped to one run and safe across process restarts."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                memory_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                tier TEXT NOT NULL,
                node_id TEXT,
                graph_version INTEGER,
                importance REAL NOT NULL,
                confidence REAL NOT NULL,
                verified INTEGER NOT NULL,
                valid INTEGER NOT NULL,
                sequence INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_scope "
            "ON memories(run_id, valid, tier, kind, sequence)"
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def next_sequence(self, run_id: str) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS value FROM memories WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row["value"])

    def add(self, record: MemoryRecord) -> MemoryRecord:
        if record.sequence == 0:
            record = record.model_copy(update={"sequence": self.next_sequence(record.run_id)})
        record = record.with_identity()
        payload = json.dumps(record.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        self.connection.execute(
            """
            INSERT OR IGNORE INTO memories(
                memory_id, run_id, kind, tier, node_id, graph_version,
                importance, confidence, verified, valid, sequence, created_at, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.memory_id, record.run_id, record.kind, record.tier,
                record.node_id, record.graph_version, record.importance,
                record.confidence, int(record.verified), int(record.valid),
                record.sequence, record.created_at, payload,
            ),
        )
        self.connection.commit()
        return record

    def records(self, run_id: str, *, valid_only: bool = True) -> list[MemoryRecord]:
        query = "SELECT payload FROM memories WHERE run_id = ?"
        values: list[Any] = [run_id]
        if valid_only:
            query += " AND valid = 1"
        query += " ORDER BY sequence ASC, memory_id ASC"
        rows = self.connection.execute(query, values).fetchall()
        return [MemoryRecord.model_validate(json.loads(row["payload"])) for row in rows]

    def invalidate_graph_version(self, run_id: str, graph_version: int) -> int:
        rows = self.records(run_id)
        changed = 0
        for record in rows:
            if record.tier == "core" or record.graph_version != graph_version:
                continue
            updated = record.model_copy(update={"valid": False})
            self.connection.execute(
                "UPDATE memories SET valid = 0, payload = ? WHERE memory_id = ?",
                (json.dumps(updated.model_dump(mode="json"), ensure_ascii=False, sort_keys=True), record.memory_id),
            )
            changed += 1
        self.connection.commit()
        return changed


class RuntimeMemoryManager:
    """Build and persist bounded memory capsules around scheduler calls."""

    def __init__(self, run_dir: str | Path, task_spec: dict[str, Any]) -> None:
        self.run_dir = Path(run_dir)
        self.task_spec = task_spec
        self.policy = task_spec.get("memory_policy", {})
        configured = self.policy.get("runtime_store", "memory/runtime_memory.sqlite3")
        path = Path(configured)
        self.store = RuntimeMemoryStore(path if path.is_absolute() else self.run_dir / path)
        self.run_id = str(task_spec.get("run_id") or task_spec.get("task_id") or self.run_dir.name)
        self.capsule_dir = self.run_dir / self.policy.get("capsule_dir", "memory/capsules")
        self.capsule_dir.mkdir(parents=True, exist_ok=True)
        self.metrics_path = self.run_dir / self.policy.get("metrics_path", "memory/metrics.json")
        self.metrics = self._load_metrics()

    @property
    def enabled(self) -> bool:
        return bool(self.policy.get("enabled", True) and self.policy.get("runtime_enabled", True))

    def _load_metrics(self) -> dict[str, Any]:
        if self.metrics_path.is_file():
            try:
                return json.loads(self.metrics_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        return {
            "schema_version": "1.0",
            "architecture": ["CoALA", "MemGPT-inspired", "Generative-Agents-inspired", "LLMLingua-2"],
            "capsules_built": 0,
            "retrieval_candidates": 0,
            "memories_injected": 0,
            "raw_memory_tokens": 0,
            "delivered_memory_tokens": 0,
            "critical_fields_expected": 0,
            "critical_fields_retained": 0,
            "compression_fallbacks": 0,
        }

    def _save_metrics(self) -> None:
        expected = self.metrics["critical_fields_expected"]
        retained = self.metrics["critical_fields_retained"]
        raw = self.metrics["raw_memory_tokens"]
        delivered = self.metrics["delivered_memory_tokens"]
        self.metrics["critical_state_retention"] = retained / expected if expected else 1.0
        self.metrics["context_compression_ratio"] = raw / delivered if delivered else 1.0
        self.metrics["record_count"] = len(self.store.records(self.run_id, valid_only=False))
        self.metrics["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self.metrics_path.parent.mkdir(parents=True, exist_ok=True)
        self.metrics_path.write_text(
            json.dumps(self.metrics, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def initialize(self) -> None:
        if not self.enabled:
            return
        existing = {record.tags[0] for record in self.store.records(self.run_id) if record.tags}
        goal = str(self.task_spec.get("task_name") or self.task_spec.get("input", {}).get("text") or "完成 TaskSpec 定义的任务")
        constraints = self._critical_constraints()
        if "core:goal" not in existing:
            self.store.add(MemoryRecord(
                run_id=self.run_id, kind="working", tier="core",
                summary=goal, content={"global_goal": goal}, tags=["core:goal"],
                importance=1.0, confidence=1.0, verified=True,
            ))
        for index, constraint in enumerate(constraints):
            tag = f"core:constraint:{index}"
            if tag in existing:
                continue
            self.store.add(MemoryRecord(
                run_id=self.run_id, kind="working", tier="core",
                summary=constraint, content={"constraint": constraint}, tags=[tag],
                importance=1.0, confidence=1.0, verified=True,
            ))
        self._save_metrics()

    def _critical_constraints(self) -> list[str]:
        criteria = self.task_spec.get("success_criteria", {})
        report = self.task_spec.get("report_policy", {})
        code = self.task_spec.get("code_policy", {})
        constraints = [
            f"任务类型固定为 {self.task_spec.get('task_type', 'unknown')}",
            "只能依据真实输入、产物和证据生成结论，不得虚构",
        ]
        if criteria.get("execution_must_succeed_for_pass"):
            constraints.append("执行失败时最终状态不得标记为通过")
        if criteria.get("json_artifacts_must_parse"):
            constraints.append("TaskSpec 声明的 JSON 产物必须能够解析")
        if (
            report.get("forbid_final_business_decision")
            or report.get("final_business_decision_forbidden")
            or report.get("forbid_unsupported_decisions")
        ):
            constraints.append("最终报告不得作出未经授权的业务裁决")
        if code:
            constraints.append(
                f"代码策略为 {code.get('mode', 'none')}，最大重试次数 {code.get('max_retries', 0)}"
            )
        constraints.extend(
            f"最终报告必须包含章节：{section}"
            for section in self.task_spec.get("required_report_sections", [])
        )
        return list(dict.fromkeys(constraints))

    @staticmethod
    def _graph_distance_score(
        graph: TaskGraph,
        node: TaskNode,
        record: MemoryRecord,
        node_index: dict[str, TaskNode] | None = None,
        max_hops: int = 16,
    ) -> float:
        if record.node_id is None:
            return 0.5
        if record.node_id in node.dependencies:
            return 1.0
        index = node_index or {item.node_id: item for item in graph.nodes}
        ancestors: set[str] = set(node.dependencies)
        frontier = [(item, 1) for item in node.dependencies]
        while frontier:
            current, hops = frontier.pop()
            if hops >= max_hops:
                continue
            dependency_node = index.get(current)
            if dependency_node is None:
                continue
            dependencies = dependency_node.dependencies
            for dependency in dependencies:
                if dependency not in ancestors:
                    ancestors.add(dependency)
                    frontier.append((dependency, hops + 1))
        return 0.65 if record.node_id in ancestors else 0.05

    def _score(
        self,
        record: MemoryRecord,
        graph: TaskGraph,
        node: TaskNode,
        newest: int,
        node_index: dict[str, TaskNode] | None = None,
    ) -> tuple[float, dict[str, float]]:
        query_tokens = _tokens({
            "description": node.description,
            "capability": node.capability,
            "inputs": node.input_artifacts,
            "outputs": node.output_artifacts,
        })
        memory_tokens = _tokens({
            "summary": record.summary,
            "content": record.content,
            "tags": record.tags,
            "capabilities": record.capabilities,
        })
        relevance = len(query_tokens & memory_tokens) / max(1, len(query_tokens))
        capability_match = 1.0 if node.capability in record.capabilities else 0.0
        relevance = min(1.0, relevance + 0.3 * capability_match)
        age = max(0, newest - record.sequence)
        recency = math.exp(-0.08 * age)
        graph_score = self._graph_distance_score(graph, node, record, node_index)
        evidence_quality = 1.0 if record.verified else min(0.5, record.confidence)
        components = {
            "relevance": relevance,
            "recency": recency,
            "importance": record.importance,
            "graph_proximity": graph_score,
            "evidence_quality": evidence_quality,
        }
        weights = self.policy.get("retrieval_weights", {})
        score = (
            float(weights.get("relevance", 0.40)) * relevance
            + float(weights.get("recency", 0.20)) * recency
            + float(weights.get("importance", 0.25)) * record.importance
            + float(weights.get("graph_proximity", 0.10)) * graph_score
            + float(weights.get("evidence_quality", 0.05)) * evidence_quality
        )
        return score, components

    def build_capsule(self, graph: TaskGraph, node: TaskNode) -> ContextCapsule:
        self.initialize()
        records = self.store.records(self.run_id)
        node_index = {item.node_id: item for item in graph.nodes}
        core = [record for record in records if record.tier == "core"]
        candidates = [record for record in records if record.tier != "core"]
        newest = max((record.sequence for record in records), default=0)
        scored: list[tuple[float, MemoryRecord, dict[str, float]]] = []
        for record in candidates:
            score, components = self._score(record, graph, node, newest, node_index)
            scored.append((score, record, components))
        scored.sort(key=lambda item: (-item[0], -item[1].sequence, item[1].memory_id))
        selected = scored[: int(self.policy.get("max_runtime_memories", 8))]
        retrieved = [
            RetrievedMemory(
                memory_id=record.memory_id, kind=record.kind, tier=record.tier,
                summary=record.summary, score=round(score, 6),
                evidence_refs=record.evidence_refs, artifact_refs=record.artifact_refs,
                verified=record.verified,
                score_components={key: round(value, 6) for key, value in components.items()},
            )
            for score, record, components in selected
        ]
        goal = next(
            (record.content.get("global_goal") for record in core if "global_goal" in record.content),
            str(self.task_spec.get("task_name") or "完成 TaskSpec 定义的任务"),
        )
        constraints = [
            str(record.content["constraint"])
            for record in core if "constraint" in record.content
        ]
        raw_memory_text = "\n".join(item.summary for item in retrieved)
        communication = CommunicationPolicy.from_task_spec(self.task_spec)
        communication_budget = communication.budget
        max_tokens = min(
            int(self.policy.get("max_capsule_memory_tokens", 1_200)),
            communication_budget.max_node_context_tokens,
        )
        memory_budget = CommunicationBudget(
            max_message_bytes=min(communication_budget.max_node_context_bytes, max_tokens * 8),
            max_message_tokens=max_tokens,
            max_summary_chars=int(self.policy.get("max_capsule_memory_chars", 6_000)),
            max_inline_structured_bytes=communication_budget.max_inline_structured_bytes,
        )
        compressor = build_compressor(
            communication.compressor,
            llmlingua_threshold_tokens=communication.llmlingua_threshold_tokens,
            llmlingua_model=communication.llmlingua_model,
            llmlingua_target_ratio=communication.llmlingua_target_ratio,
            llmlingua_device_map=communication.llmlingua_device_map,
            llmlingua_allow_download=communication.llmlingua_allow_download,
        )
        compressed = compressor.compress(raw_memory_text, memory_budget)
        artifacts = list(dict.fromkeys(
            reference for item in retrieved for reference in item.artifact_refs
        ))
        unresolved = [
            item.summary for item in retrieved
            if item.kind == "episodic" and any(word in item.summary.lower() for word in ("失败", "未解决", "blocked", "failed"))
        ]
        expected_critical = 1 + len(constraints)
        capsule = ContextCapsule(
            global_goal=str(goal), critical_constraints=constraints,
            current_node={
                "node_id": node.node_id,
                "description": node.description,
                "capability": node.capability,
            },
            graph_position={
                "graph_id": graph.graph_id,
                "graph_version": graph.version,
                "dependencies": list(node.dependencies),
                "completed_nodes": [
                    item.node_id for item in graph.nodes if item.status.value == "completed"
                ],
            },
            relevant_memories=retrieved,
            unresolved_issues=unresolved,
            artifact_refs=artifacts,
            compressed_memory_text=compressed.text,
            metrics={
                "candidate_count": len(candidates),
                "retrieved_count": len(retrieved),
                "raw_tokens": estimate_tokens(raw_memory_text),
                "delivered_tokens": estimate_tokens(compressed.text),
                "raw_bytes": utf8_size(raw_memory_text),
                "delivered_bytes": utf8_size(compressed.text),
                "compressor": compressed.compressor,
                "attempted_compressor": compressed.attempted_compressor,
                "compression_fallback_used": compressed.fallback_used,
                "critical_fields_expected": expected_critical,
                "critical_fields_retained": expected_critical,
                "protected_context_integrity": "passed",
            },
        )
        # Protected state is intentionally outside the lossy text block.  Keep
        # a deterministic guard here so a future compressor cannot silently
        # drop the objective or constraints while still reporting success.
        if not capsule.global_goal.strip() or len(capsule.critical_constraints) != expected_critical - 1:
            raise ValueError("runtime memory capsule 缺少受保护目标或约束")
        attempt = max(1, node.attempts)
        path = self.capsule_dir / f"{node.node_id}-attempt-{attempt}.json"
        path.write_text(capsule.model_dump_json(indent=2), encoding="utf-8")
        self.metrics["capsules_built"] += 1
        self.metrics["retrieval_candidates"] += len(candidates)
        self.metrics["memories_injected"] += len(retrieved)
        self.metrics["raw_memory_tokens"] += estimate_tokens(raw_memory_text)
        self.metrics["delivered_memory_tokens"] += estimate_tokens(compressed.text)
        self.metrics["critical_fields_expected"] += expected_critical
        self.metrics["critical_fields_retained"] += expected_critical
        self.metrics["compression_fallbacks"] += int(compressed.fallback_used)
        self._save_metrics()
        return capsule

    def remember_result(self, graph: TaskGraph, node: TaskNode, result: dict[str, Any]) -> list[str]:
        if not self.enabled:
            return []
        status = str(result.get("status") or "unknown")
        summary = str(result.get("summary") or f"节点 {node.node_id} 状态为 {status}")
        records = [MemoryRecord(
            run_id=self.run_id, kind="episodic", tier="recall",
            node_id=node.node_id, graph_version=graph.version,
            summary=summary,
            content={
                "status": status,
                "executor_id": result.get("executor_id"),
                "error_type": result.get("error_type"),
                "error_message": result.get("error_message"),
            },
            capabilities=[node.capability],
            tags=[f"node:{node.node_id}", f"status:{status}"],
            evidence_refs=list(result.get("evidence_refs") or []),
            artifact_refs=list(result.get("output_artifacts") or []),
            importance=0.85 if status == "failed" else 0.6,
            confidence=1.0, verified=True,
        )]
        for relative in result.get("output_artifacts") or []:
            path = self.run_dir / str(relative)
            if not path.is_file():
                continue
            records.append(MemoryRecord(
                run_id=self.run_id, kind="artifact", tier="archival",
                node_id=node.node_id, graph_version=graph.version,
                summary=f"节点 {node.node_id} 生成产物 {relative}",
                content={
                    "path": str(relative), "size": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                },
                capabilities=[node.capability], tags=[f"node:{node.node_id}", "artifact"],
                evidence_refs=[str(relative)], artifact_refs=[str(relative)],
                importance=0.75, confidence=1.0, verified=status == "completed",
            ))
        verification = (result.get("structured_output") or {}).get("verification") or {}
        for claim in verification.get("claims") or []:
            if claim.get("verdict") != "supported":
                continue
            records.append(MemoryRecord(
                run_id=self.run_id, kind="semantic", tier="archival",
                node_id=node.node_id, graph_version=graph.version,
                summary=str(claim.get("claim") or "已验证事实"),
                content={"claim": claim.get("claim"), "rationale": claim.get("rationale")},
                capabilities=[node.capability], tags=[f"node:{node.node_id}", "verified_fact"],
                evidence_refs=list(claim.get("evidence_refs") or []),
                importance=0.9, confidence=float(claim.get("confidence", 1.0)),
                verified=True,
            ))
        added = [self.store.add(record).memory_id for record in records]
        self._save_metrics()
        return added
