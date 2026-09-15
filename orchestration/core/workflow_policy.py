"""Versioned workflow-policy interface with an offline AFlow extension point.

Online execution consumes a plain, deterministic policy snapshot.  A future
AFlow experiment may generate such snapshots offline, but it is deliberately
not allowed to search over or mutate the active task graph here.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Protocol, runtime_checkable


DEFAULT_COMMUNICATION_POLICY: dict[str, Any] = {
    "compressor": "auto",
    "max_message_bytes": 12_000,
    "max_message_tokens": 1_500,
    "max_node_context_bytes": 48_000,
    "max_node_context_tokens": 5_000,
    # Optional provider fact; unset means the framework budget remains soft.
    "model_input_limit_tokens": None,
    "max_control_prompt_tokens": 8_000,
    "max_summary_chars": 2_000,
    "max_inline_structured_bytes": 8_000,
    "llmlingua_threshold_tokens": 512,
    "llmlingua_model": "microsoft/llmlingua-2-xlm-roberta-large-meetingbank",
    "llmlingua_target_ratio": 0.6,
    "llmlingua_device_map": "cpu",
    # Stable runs must not download model weights implicitly.  A deployment or
    # an explicit smoke test can opt in after choosing its compute resource.
    "llmlingua_allow_download": False,
    "preserve_structured_fields": True,
    "deduplicate_by_hash": True,
    "deduplicate_across_receivers": True,
    "externalize_large_payloads": True,
    "persist_message_payloads": True,
}

DEFAULT_ROUTING_POLICY: dict[str, Any] = {
    "mode": "agent_prune_lite",
    "direct_dependencies_only": True,
    "allow_optional_routes": False,
}

DEFAULT_TEAM_POLICY: dict[str, Any] = {
    "mode": "dylan_lite",
    "use_runtime_history": True,
    "stats_path": "memory/executor_stats.json",
    "early_stop_on_success": True,
}

DEFAULT_RECOVERY_POLICY: dict[str, Any] = {
    "max_node_retries": 1,
    "max_executor_switches": 1,
    "max_replans": 1,
    # A business repair round is different from an executor retry.  It is
    # consumed only when independently validated outputs are wrong after the
    # producing node itself completed successfully.
    "max_business_repair_rounds": 2,
}

DEFAULT_EXTERNAL_TOOLS_POLICY: dict[str, Any] = {
    "enabled": True,
    "max_files_per_node": 8,
    "max_file_excerpt_chars": 12_000,
    "max_web_steps": 4,
    "web_timeout_seconds": 90,
    "web_read_only": True,
    "allow_web_downloads": False,
}

DEFAULT_VERIFICATION_POLICY: dict[str, Any] = {
    "enabled": True,
    "max_claims": 32,
    # Evidence verification needs enough room to inspect the complete
    # normalized structure and ordinary generated scripts.  These are
    # evidence-specific bounds; the model-call budget remains a soft cost
    # target and large binary files are still represented by metadata.
    "max_evidence_excerpt_chars": 24_000,
    "max_evidence_catalog_chars": 40_000,
    "require_evidence_for_supported_claims": True,
}

DEFAULT_MEMORY_POLICY: dict[str, Any] = {
    "enabled": True,
    "store_path": "memory/workflow_skills.json",
    "max_retrieved_skills": 3,
    "max_candidates_per_run": 2,
    "min_candidate_confidence": 0.7,
    # CoALA/MemGPT-inspired runtime memory is scoped to one run.  The
    # existing workflow skill store above remains cross-run procedural memory.
    "runtime_enabled": True,
    "runtime_store": "memory/runtime_memory.sqlite3",
    "capsule_dir": "memory/capsules",
    "metrics_path": "memory/metrics.json",
    "max_runtime_memories": 8,
    "max_capsule_memory_tokens": 1200,
    "max_capsule_memory_chars": 6000,
    "retrieval_weights": {
        "relevance": 0.40,
        "recency": 0.20,
        "importance": 0.25,
        "graph_proximity": 0.10,
        "evidence_quality": 0.05,
    },
}

DEFAULT_TERMINAL_POLICY: dict[str, Any] = {
    "enabled": True,
    "backend": "bounded_local",
    "timeout_seconds": 30,
    "allowed_checks": ["python_compile", "python_execute"],
}

DEFAULT_TASK_UNDERSTANDING_POLICY: dict[str, Any] = {
    "enabled": True,
    "max_preview_chars_per_file": 1_500,
    # One bounded retry protects the entire long-running workflow from a
    # malformed provider JSON response without turning understanding into an
    # open-ended chat loop.
    "max_understanding_attempts": 2,
    # Requirement refinement is bounded and local.  This is independent from
    # PlanIR revision rounds because it happens before PlanningAgent runs.
    "max_requirement_revision_rounds": 2,
}

# Planning remains a frontend concern: it may produce a verified hierarchical
# PlanIR, but it never mutates the runtime graph after compilation.  ``auto``
# keeps simple tasks on the legacy one-shot planner and selects hierarchy only
# when the deterministic complexity score warrants it.
DEFAULT_PLANNING_POLICY: dict[str, Any] = {
    "mode": "auto",
    "hierarchical_validation_enabled": True,
    # A bounded extra local revision lets a validator-targeted primitive be
    # repaired after an earlier parent/format repair, without unbounded
    # planning expansion.
    "max_revision_rounds": 3,
    "max_decomposition_depth": 3,
    "max_plan_nodes": 32,
    "max_new_nodes_per_refinement": 8,
    "max_refinement_prompt_tokens": 4_000,
    # A deterministic graph is a bounded recovery path when a provider emits
    # malformed PlanIR. It never marks business work complete; downstream
    # execution and acceptance gates remain authoritative.
    "fallback_to_legacy_plan": True,
    "enable_llm_plan_critic": False,
    "preserve_hierarchy_metadata": True,
}

DEFAULT_HORIZON_POLICY: dict[str, Any] = {
    # ``auto`` keeps short document tasks on the existing one-shot path while
    # allowing explicitly long/iterative tasks to use bounded execution
    # windows.  This is an execution policy, not a fixed node-count target.
    "mode": "auto",
    "progressive_execution": True,
    "max_active_nodes": 8,
    "checkpoint_each_epoch": True,
    # Full run-state/task-graph snapshots are expensive for thousand-step
    # chains; keep a bounded recovery window while preserving epoch files.
    "persist_every_epochs": 4,
    "max_epochs": 128,
    "requirement_driven_expansion": True,
    "min_requirements_for_staging": 8,
    "max_requirements_per_stage": 6,
    "max_stage_prompt_tokens": 6_000,
    "max_stages": 64,
}

DEFAULT_WORKFLOW_POLICY: dict[str, Any] = {
    "schema_version": "1.0",
    "policy_id": "default-v1",
    "communication_policy": DEFAULT_COMMUNICATION_POLICY,
    "routing_policy": DEFAULT_ROUTING_POLICY,
    "team_policy": DEFAULT_TEAM_POLICY,
    "recovery_policy": DEFAULT_RECOVERY_POLICY,
    "external_tools_policy": DEFAULT_EXTERNAL_TOOLS_POLICY,
    "verification_policy": DEFAULT_VERIFICATION_POLICY,
    "memory_policy": DEFAULT_MEMORY_POLICY,
    "terminal_policy": DEFAULT_TERMINAL_POLICY,
    "task_understanding_policy": DEFAULT_TASK_UNDERSTANDING_POLICY,
    "planning_policy": DEFAULT_PLANNING_POLICY,
    "horizon_policy": DEFAULT_HORIZON_POLICY,
}


@runtime_checkable
class WorkflowPolicyProvider(Protocol):
    """Read-only source of one policy snapshot for a normalized TaskSpec."""

    def get_policy(self, task_spec: Mapping[str, Any]) -> dict[str, Any]:
        ...


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


class DefaultWorkflowPolicy:
    """Return framework defaults plus explicit TaskSpec policy overrides."""

    def __init__(self, defaults: Mapping[str, Any] | None = None) -> None:
        self._defaults = _deep_merge(
            DEFAULT_WORKFLOW_POLICY,
            defaults or {},
        )

    def get_policy(self, task_spec: Mapping[str, Any]) -> dict[str, Any]:
        override = task_spec.get("workflow_policy", {})
        if not isinstance(override, Mapping):
            raise TypeError("task_spec.workflow_policy 必须是对象")
        return _deep_merge(self._defaults, override)


class AFlowPolicyAdapter:
    """Consume a precomputed AFlow policy; never run online optimization.

    This adapter is the intentional integration seam for a later offline AFlow
    training pipeline.  It accepts an immutable policy snapshot produced
    elsewhere and performs the same TaskSpec override merge as the default
    provider.  It has no graph argument and exposes no search/mutation method.
    """

    def __init__(self, precomputed_policy: Mapping[str, Any]) -> None:
        if not precomputed_policy:
            raise ValueError("precomputed_policy 不能为空")
        self._provider = DefaultWorkflowPolicy(precomputed_policy)

    def get_policy(self, task_spec: Mapping[str, Any]) -> dict[str, Any]:
        policy = self._provider.get_policy(task_spec)
        policy["policy_source"] = "aflow_offline_snapshot"
        return policy
