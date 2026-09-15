"""Runtime statistics and pluggable scoring policies for node executors.

The module implements a deliberately small subset of DyLAN's dynamic-team
idea: routing can learn from observed executor outcomes without allowing an
LLM to change framework control flow.  Static descriptor values remain the
prior, and runtime history is introduced gradually to keep cold starts stable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from orchestration.execution.node_executor import ExecutorDescriptor


class ExecutorStats(BaseModel):
    """Serializable aggregate for one ``executor_id`` and ``task_type`` pair."""

    model_config = ConfigDict(extra="forbid")

    executor_id: str
    task_type: str
    runs: int = Field(default=0, ge=0)
    successes: int = Field(default=0, ge=0)
    quality_checks: int = Field(default=0, ge=0)
    quality_passes: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    total_latency_seconds: float = Field(default=0.0, ge=0)
    avg_tokens: float = Field(default=0.0, ge=0)
    avg_latency_seconds: float = Field(default=0.0, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)

    @field_validator("executor_id", "task_type")
    @classmethod
    def _require_identity(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("executor_id/task_type 不能为空")
        return normalized

    @property
    def success_rate(self) -> float:
        return self.successes / self.runs if self.runs else 0.0

    @property
    def quality_pass_rate(self) -> float:
        return self.quality_passes / self.quality_checks if self.quality_checks else 0.0

    def snapshot(self) -> dict[str, Any]:
        """Return a routing-friendly snapshot including derived rates."""
        payload = self.model_dump(mode="json")
        payload["success_rate"] = self.success_rate
        payload["quality_pass_rate"] = self.quality_pass_rate
        return payload

    def record(
        self,
        *,
        success: bool,
        quality_passed: bool | None = None,
        tokens: int = 0,
        latency_seconds: float = 0.0,
    ) -> None:
        """Update this aggregate using numerically stable cumulative totals."""
        if tokens < 0:
            raise ValueError("tokens 不能为负数")
        if latency_seconds < 0:
            raise ValueError("latency_seconds 不能为负数")

        self.runs += 1
        if success:
            self.successes += 1
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1
        if quality_passed is not None:
            self.quality_checks += 1
            if quality_passed:
                self.quality_passes += 1

        self.total_tokens += tokens
        self.total_latency_seconds += latency_seconds
        self.avg_tokens = self.total_tokens / self.runs
        self.avg_latency_seconds = self.total_latency_seconds / self.runs


class ExecutorStatsStore:
    """In-memory statistics table with optional atomic JSON persistence."""

    schema_version = "1.0"

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._records: dict[tuple[str, str], ExecutorStats] = {}
        if self.path is not None and self.path.exists():
            self.load()

    @staticmethod
    def _key(executor_id: str, task_type: str) -> tuple[str, str]:
        executor = executor_id.strip()
        task = task_type.strip() or "unknown"
        if not executor:
            raise ValueError("executor_id 不能为空")
        return executor, task

    def get(self, executor_id: str, task_type: str = "unknown") -> ExecutorStats:
        """Return a copy of one aggregate; reads never mutate the table."""
        key = self._key(executor_id, task_type)
        stats = self._records.get(key)
        if stats is None:
            return ExecutorStats(executor_id=key[0], task_type=key[1])
        return stats.model_copy(deep=True)

    def record(
        self,
        executor_id: str,
        task_type: str,
        *,
        success: bool,
        quality_passed: bool | None = None,
        tokens: int = 0,
        latency_seconds: float = 0.0,
    ) -> ExecutorStats:
        key = self._key(executor_id, task_type)
        stats = self._records.setdefault(
            key, ExecutorStats(executor_id=key[0], task_type=key[1])
        )
        stats.record(
            success=success,
            quality_passed=quality_passed,
            tokens=tokens,
            latency_seconds=latency_seconds,
        )
        if self.path is not None:
            self.save()
        return stats.model_copy(deep=True)

    def all(self) -> list[ExecutorStats]:
        return [
            self._records[key].model_copy(deep=True)
            for key in sorted(self._records)
        ]

    def save(self, path: str | Path | None = None) -> None:
        destination = Path(path) if path is not None else self.path
        if destination is None:
            raise ValueError("未配置统计持久化路径")
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": self.schema_version,
            "records": [record.model_dump(mode="json") for record in self.all()],
        }
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        temporary.replace(destination)

    def load(self, path: str | Path | None = None) -> None:
        source = Path(path) if path is not None else self.path
        if source is None:
            raise ValueError("未配置统计持久化路径")
        if not source.exists():
            self._records = {}
            return
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
            if payload.get("schema_version") != self.schema_version:
                raise ValueError(f"不支持的 schema_version: {payload.get('schema_version')}")
            records = [ExecutorStats.model_validate(item) for item in payload.get("records", [])]
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError) as exc:
            raise ValueError(f"执行器统计文件无效: {source}: {exc}") from exc

        loaded: dict[tuple[str, str], ExecutorStats] = {}
        for record in records:
            key = (record.executor_id, record.task_type)
            if key in loaded:
                raise ValueError(f"执行器统计包含重复记录: {record.executor_id}/{record.task_type}")
            loaded[key] = record
        self._records = loaded


class ExecutorScore(BaseModel):
    """Explainable result returned by an :class:`ExecutorScorer`."""

    effective_score: float
    components: dict[str, float] = Field(default_factory=dict)
    reason: str


@runtime_checkable
class ExecutorScorer(Protocol):
    """Policy interface used by ``CapabilityRegistry``."""

    name: str

    def score(
        self,
        descriptor: ExecutorDescriptor,
        history: ExecutorStats,
    ) -> ExecutorScore:
        """Score one eligible executor for the current task type."""
        ...


class StaticExecutorScorer:
    """Deterministic policy using descriptor quality, cost and latency only."""

    name = "static"

    def score(
        self,
        descriptor: ExecutorDescriptor,
        history: ExecutorStats,
    ) -> ExecutorScore:
        # The registry retains the original lexicographic ordering for this
        # policy.  This scalar is primarily an auditable summary.
        effective = (
            descriptor.quality_score
            - descriptor.cost_score * 1e-6
            - descriptor.latency_score * 1e-9
        )
        return ExecutorScore(
            effective_score=effective,
            components={
                "quality_score": descriptor.quality_score,
                "cost_score": descriptor.cost_score,
                "latency_score": descriptor.latency_score,
                "history_confidence": 0.0,
            },
            reason="使用静态质量、成本和延迟；尚未让运行历史影响路由。",
        )


class DyLANLiteScorer:
    """Stable online scorer inspired by DyLAN's agent-importance idea.

    History receives zero weight at cold start and approaches ``history_weight``
    only as observations accumulate.  This avoids one early failure causing a
    large and difficult-to-reproduce routing change.
    """

    name = "dylan_lite"

    def __init__(self, *, cold_start_runs: int = 8, history_weight: float = 0.35) -> None:
        if cold_start_runs <= 0:
            raise ValueError("cold_start_runs 必须大于 0")
        if not 0 <= history_weight <= 1:
            raise ValueError("history_weight 必须位于 [0, 1]")
        self.cold_start_runs = cold_start_runs
        self.history_weight = history_weight

    @staticmethod
    def _positive_bounded(value: float) -> float:
        return value / (1.0 + value)

    def score(
        self,
        descriptor: ExecutorDescriptor,
        history: ExecutorStats,
    ) -> ExecutorScore:
        quality_prior = self._positive_bounded(descriptor.quality_score)
        cost_efficiency = 1.0 / (1.0 + descriptor.cost_score)
        latency_efficiency = 1.0 / (1.0 + descriptor.latency_score)
        static_score = (
            0.70 * quality_prior
            + 0.15 * cost_efficiency
            + 0.15 * latency_efficiency
        )

        confidence = history.runs / (history.runs + self.cold_start_runs)
        quality_rate = (
            history.quality_pass_rate
            if history.quality_checks
            else history.success_rate
        )
        token_efficiency = 1.0 / (1.0 + history.avg_tokens / 2000.0)
        observed_latency_efficiency = 1.0 / (
            1.0 + history.avg_latency_seconds / 10.0
        )
        runtime_score = (
            0.45 * history.success_rate
            + 0.30 * quality_rate
            + 0.10 * token_efficiency
            + 0.15 * observed_latency_efficiency
        )
        learned_weight = self.history_weight * confidence
        failure_penalty = confidence * min(0.04 * history.consecutive_failures, 0.16)
        effective = (
            (1.0 - learned_weight) * static_score
            + learned_weight * runtime_score
            - failure_penalty
        )

        return ExecutorScore(
            effective_score=effective,
            components={
                "static_score": static_score,
                "runtime_score": runtime_score,
                "history_confidence": confidence,
                "learned_weight": learned_weight,
                "failure_penalty": failure_penalty,
                "success_rate": history.success_rate,
                "quality_pass_rate": quality_rate,
                "token_efficiency": token_efficiency,
                "observed_latency_efficiency": observed_latency_efficiency,
            },
            reason=(
                "静态能力作为先验；运行次数越多，成功率、质量通过率、"
                "Token 效率和实测耗时的影响越大，并惩罚连续失败。"
            ),
        )


def token_count(token_usage: int | dict[str, int] | None) -> int:
    """Normalize common token-usage payloads without double counting totals."""
    if token_usage is None:
        return 0
    if isinstance(token_usage, bool):
        raise TypeError("token_usage 不能是布尔值")
    if isinstance(token_usage, int):
        if token_usage < 0:
            raise ValueError("token_usage 不能为负数")
        return token_usage
    if not isinstance(token_usage, dict):
        raise TypeError("token_usage 必须是整数或字典")
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in token_usage.values()):
        raise ValueError("token_usage 字典只能包含非负整数")
    for total_key in ("total_tokens", "total_token_count"):
        if total_key in token_usage:
            return token_usage[total_key]
    return sum(
        value for key, value in token_usage.items()
        if key in {"prompt_tokens", "completion_tokens", "input_tokens", "output_tokens"}
    )
