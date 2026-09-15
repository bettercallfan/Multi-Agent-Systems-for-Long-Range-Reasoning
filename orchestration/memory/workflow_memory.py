"""Framework-owned workflow memory with Agent-assisted selection and curation."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from agents.workflow_memory_agent import create_workflow_memory_agent
from orchestration.core.model_calls import run_agent
from orchestration.core.run_state import RunState
from orchestration.core.schemas import (
    WorkflowMemoryCuration,
    WorkflowMemorySelection,
    WorkflowSkillCandidate,
    parse_model,
)


class WorkflowSkillStore:
    """Small, deterministic store; language models never receive write access."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_file():
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("skills"), list):
                raise ValueError("workflow skill store 格式无效")
            self.data = payload
        else:
            self.data = {"schema_version": "1.0", "skills": []}

    def _save(self) -> None:
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    def retrieve(self, task_spec: dict, limit: int) -> list[dict[str, Any]]:
        task_type = str(task_spec.get("task_type", "unknown"))
        required = set(task_spec.get("capability_contract", {}).get("required", []))
        scored: list[tuple[int, str, dict[str, Any]]] = []
        for skill in self.data["skills"]:
            try:
                candidate = WorkflowSkillCandidate.model_validate(skill)
            except Exception:
                continue
            if not self._safe_candidate(candidate):
                continue
            skill_id = str(skill.get("skill_id", "")).strip()
            if not skill_id:
                continue
            score = 0
            if task_type in candidate.applicable_task_types:
                score += 10
            score += 2 * len(required & set(candidate.required_capabilities))
            if score > 0:
                public_record = {
                    "skill_id": skill_id,
                    **candidate.model_dump(mode="json"),
                }
                scored.append((score, skill_id, public_record))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [json.loads(json.dumps(item[2])) for item in scored[:max(0, limit)]]

    @staticmethod
    def _safe_candidate(candidate: WorkflowSkillCandidate) -> bool:
        text = json.dumps(
            candidate.model_dump(mode="json"), ensure_ascii=False,
        )
        forbidden = [
            r"(?i)api[_-]?key", r"(?i)secret", r"sk-[A-Za-z0-9]",
            r"(?m)(?:^|\s)/(?:home|root|tmp|var|mnt|nfs|workspace|Users|opt)/",
            r"(?i)[A-Z]:\\",
        ]
        return not any(re.search(pattern, text) for pattern in forbidden)

    def add_candidates(
        self,
        candidates: list[WorkflowSkillCandidate],
        *,
        run_id: str,
        outcome: str,
        min_confidence: float,
        limit: int,
    ) -> list[str]:
        added: list[str] = []
        existing = {str(item.get("skill_id")) for item in self.data["skills"]}
        for candidate in candidates[:max(0, limit)]:
            if candidate.confidence < min_confidence or not self._safe_candidate(candidate):
                continue
            canonical = json.dumps(
                candidate.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
            )
            skill_id = "skill_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
            if skill_id in existing:
                continue
            record = {
                "skill_id": skill_id,
                **candidate.model_dump(mode="json"),
                "source_run_id": run_id,
                "verified_outcome": outcome,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            self.data["skills"].append(record)
            existing.add(skill_id)
            added.append(skill_id)
        if added:
            self._save()
        return added


def _prompt_budget(task_spec: dict) -> int:
    return int(
        task_spec.get("communication_policy", {}).get(
            "max_control_prompt_tokens", 8_000,
        )
    )


async def retrieve_workflow_memory(
    task_spec: dict,
    run_state: RunState,
    model_client,
    trace: list,
) -> dict[str, Any]:
    policy = task_spec.get("memory_policy", {})
    if not policy.get("enabled", False):
        return {"selected_skills": [], "planning_guidance": []}
    store = WorkflowSkillStore(policy.get("store_path", "memory/workflow_skills.json"))
    candidates = store.retrieve(
        task_spec,
        int(policy.get("max_retrieved_skills", 3)),
    )
    if not candidates:
        payload = {
            "candidate_count": 0,
            "agent_invoked": False,
            "selected_skill_ids": [],
            "selected_skills": [],
            "planning_guidance": [],
            "confidence": 1.0,
        }
        run_state.record_memory_retrieval(payload)
        return payload
    task_summary = {
        "task_type": task_spec.get("task_type"),
        "code_policy": task_spec.get("code_policy", {}),
        "required_capabilities": task_spec.get(
            "capability_contract", {}
        ).get("required", []),
        "artifact_contract": task_spec.get("artifact_contract", {}),
    }
    prompt = f"""
为当前任务选择真正相关的历史工作流技能。只能选择候选列表中的 skill_id；没有相关技能时返回空列表。

任务契约摘要：
{json.dumps(task_summary, ensure_ascii=False, indent=2)}

候选技能：
{json.dumps(candidates, ensure_ascii=False, indent=2)}

严格返回：
{{"selected_skill_ids": [], "planning_guidance": [], "confidence": 0.0}}
planning_guidance 只能概括被选技能中的 guidance，不得创造新的业务事实。
""".strip()
    agent = create_workflow_memory_agent(model_client)
    raw = await run_agent(
        agent,
        prompt,
        trace,
        run_state=run_state,
        stage="plan",
        prompt_budget_tokens=_prompt_budget(task_spec),
    )
    selection = parse_model(WorkflowMemorySelection, raw)
    by_id = {str(item.get("skill_id")): item for item in candidates}
    invalid = sorted(set(selection.selected_skill_ids) - set(by_id))
    if invalid:
        raise ValueError("WorkflowMemoryAgent 选择了未提供的技能: " + ", ".join(invalid))
    selected = [by_id[skill_id] for skill_id in selection.selected_skill_ids]
    # The Agent selects IDs; it cannot inject arbitrary instructions into the
    # planner. Guidance is reconstructed only from framework-owned records.
    authorized_guidance = list(dict.fromkeys(
        str(item)
        for skill in selected
        for item in skill.get("guidance", [])
        if str(item).strip()
    ))
    payload = {
        "candidate_count": len(candidates),
        "agent_invoked": True,
        "selected_skill_ids": selection.selected_skill_ids,
        "selected_skills": selected,
        "planning_guidance": authorized_guidance,
        "confidence": selection.confidence,
    }
    run_state.record_memory_retrieval(payload)
    return payload


async def curate_workflow_memory(
    task_spec: dict,
    task_graph: dict,
    final_decision: dict,
    run_state: RunState,
    model_client,
    trace: list,
) -> list[str]:
    policy = task_spec.get("memory_policy", {})
    if not policy.get("enabled", False):
        return []
    compact_graph = {
        "graph_id": task_graph.get("graph_id"),
        "version": task_graph.get("version"),
        "nodes": [
            {
                "node_id": node.get("node_id"),
                "capability": node.get("capability"),
                "dependencies": node.get("dependencies", []),
                "status": node.get("status"),
                "executor_id": (node.get("result") or {}).get("executor_id"),
                "error_type": (node.get("error") or {}).get("error_type"),
            }
            for node in task_graph.get("nodes", [])
        ],
    }
    required_capabilities = task_spec.get(
        "capability_contract", {}
    ).get("required", [])
    prompt = f"""
从本轮已经由框架验收的执行轨迹中提炼可复用工作流技能或失败教训。
不得包含绝对路径、密钥、用户原文、具体人员信息或未经验证的业务事实。

任务类型：{task_spec.get('task_type', 'unknown')}
要求能力：{json.dumps(required_capabilities, ensure_ascii=False)}
任务图：{json.dumps(compact_graph, ensure_ascii=False, indent=2)}
最终验收：{json.dumps(final_decision, ensure_ascii=False, indent=2)}

严格返回：
{{"candidates": [{{
  "title": "可复用技能标题",
  "kind": "successful_workflow 或 failure_lesson",
  "applicable_task_types": ["任务类型"],
  "required_capabilities": [],
  "guidance": ["不包含具体业务事实的可复用步骤"],
  "confidence": 0.0
}}]}}
""".strip()
    agent = create_workflow_memory_agent(model_client)
    raw = await run_agent(
        agent,
        prompt,
        trace,
        run_state=run_state,
        stage="final_validate",
        prompt_budget_tokens=_prompt_budget(task_spec),
    )
    curation = parse_model(WorkflowMemoryCuration, raw)
    store = WorkflowSkillStore(policy.get("store_path", "memory/workflow_skills.json"))
    outcome = {"passed": "success", "partial": "partial", "failed": "failed"}.get(
        final_decision.get("status", "failed"), "failed",
    )
    added = store.add_candidates(
        curation.candidates,
        run_id=str(task_spec.get("run_id") or task_spec.get("task_id")),
        outcome=outcome,
        min_confidence=float(policy.get("min_candidate_confidence", 0.7)),
        limit=int(policy.get("max_candidates_per_run", 2)),
    )
    run_state.record_memory_curation({
        "candidate_count": len(curation.candidates),
        "added_skill_ids": added,
        "store_path": str(store.path),
    })
    return added
