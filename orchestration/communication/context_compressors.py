"""Pluggable free-text compressors with deterministic hard-budget guards."""

from __future__ import annotations

import importlib
import importlib.util
import math
from typing import Any, Callable, Protocol, runtime_checkable

from orchestration.communication.communication_models import (
    CommunicationBudget,
    CompressionResult,
    estimate_tokens,
    utf8_size,
)


class CompressionUnavailableError(RuntimeError):
    """Raised when an optional compressor cannot be loaded or invoked."""


def _fits(text: str, budget: CommunicationBudget) -> bool:
    return (
        len(text) <= budget.max_summary_chars
        and utf8_size(text) <= budget.max_message_bytes
        and estimate_tokens(text) <= budget.max_message_tokens
    )


def _make_result(
    original: str,
    delivered: str,
    *,
    compressor: str,
    budget: CommunicationBudget,
    truncated: bool = False,
    fallback_used: bool = False,
    warnings: list[str] | None = None,
    attempted_compressor: str | None = None,
) -> CompressionResult:
    return CompressionResult(
        text=delivered,
        compressor=compressor,
        original_bytes=utf8_size(original),
        delivered_bytes=utf8_size(delivered),
        original_tokens=estimate_tokens(original),
        delivered_tokens=estimate_tokens(delivered),
        within_budget=_fits(delivered, budget),
        compressed=delivered != original,
        truncated=truncated,
        fallback_used=fallback_used,
        warnings=warnings or [],
        attempted_compressor=attempted_compressor,
    )


@runtime_checkable
class ContextCompressor(Protocol):
    """Compress one natural-language field; never accepts structured data."""

    def compress(self, text: str, budget: CommunicationBudget) -> CompressionResult:
        ...


class DeterministicCompressor:
    """Whitespace folding plus stable head/tail truncation.

    It has no model dependency and is also the final safety guard after a
    semantic compressor.  Empty output is allowed when a caller allocates no
    useful room to free text; protected structured fields live outside here.
    """

    name = "deterministic"
    marker = " …[已压缩]… "

    def compress(self, text: str, budget: CommunicationBudget) -> CompressionResult:
        original = str(text or "")
        if _fits(original, budget):
            return _make_result(original, original, compressor=self.name, budget=budget)

        normalized = " ".join(original.split())
        if _fits(normalized, budget):
            return _make_result(
                original, normalized, compressor=self.name, budget=budget,
            )

        char_limit = min(len(normalized), budget.max_summary_chars)

        def head_tail(kept: int) -> str:
            if kept <= 0:
                return ""
            if kept >= len(normalized):
                return normalized
            # Reserve most space for the leading conclusion and retain a small
            # tail where qualifications and references often appear.
            if kept <= len(self.marker):
                return normalized[:kept]
            content = kept - len(self.marker)
            head = max(1, (content * 3) // 4)
            tail = max(0, content - head)
            return normalized[:head] + self.marker + (normalized[-tail:] if tail else "")

        low, high = 0, char_limit
        best = ""
        while low <= high:
            middle = (low + high) // 2
            candidate = head_tail(middle)
            if _fits(candidate, budget):
                best = candidate
                low = middle + 1
            else:
                high = middle - 1

        result = _make_result(
            original,
            best,
            compressor=self.name,
            budget=budget,
            truncated=True,
        )
        # This assertion is an implementation invariant, not an optional check.
        if not result.within_budget:  # pragma: no cover - guards future changes
            raise AssertionError("确定性压缩器未满足硬预算")
        return result


class LLMLinguaCompressor:
    """Lazy optional LLMLingua adapter with a deterministic safety guard."""

    name = "llmlingua"

    def __init__(
        self,
        *,
        model_name: str = "microsoft/llmlingua-2-xlm-roberta-large-meetingbank",
        device_map: str = "cpu",
        allow_download: bool = False,
        backend_factory: Callable[[], Any] | None = None,
        fallback: ContextCompressor | None = None,
        strict: bool = False,
    ) -> None:
        self.model_name = model_name
        self.device_map = device_map
        self.allow_download = allow_download
        self.backend_factory = backend_factory
        self.fallback = fallback or DeterministicCompressor()
        self.strict = strict
        self._backend: Any | None = None
        self._load_error: str | None = None

    def _load_backend(self) -> Any:
        if self._backend is not None:
            return self._backend
        if self._load_error is not None:
            raise CompressionUnavailableError(
                f"LLMLingua 本轮已降级，不重复初始化: {self._load_error}"
            )
        try:
            if self.backend_factory is not None:
                self._backend = self.backend_factory()
            else:
                module = importlib.import_module("llmlingua")
                compressor_type = getattr(module, "PromptCompressor")
                self._backend = compressor_type(
                    model_name=self.model_name,
                    device_map=self.device_map,
                    model_config={"local_files_only": not self.allow_download},
                    use_llmlingua2=True,
                )
            return self._backend
        except Exception as exc:  # optional import/model loading can fail widely
            self._load_error = str(exc)
            raise CompressionUnavailableError(f"LLMLingua 不可用: {exc}") from exc

    def _fallback_result(
        self, text: str, budget: CommunicationBudget, exc: Exception,
    ) -> CompressionResult:
        if self.strict:
            if isinstance(exc, CompressionUnavailableError):
                raise exc
            raise CompressionUnavailableError(f"LLMLingua 压缩失败: {exc}") from exc
        result = self.fallback.compress(text, budget)
        return result.model_copy(update={
            "fallback_used": True,
            "warnings": [*result.warnings, f"LLMLingua 回退: {exc}"],
            "attempted_compressor": self.name,
        })

    def compress(self, text: str, budget: CommunicationBudget) -> CompressionResult:
        original = str(text or "")
        if _fits(original, budget):
            return _make_result(original, original, compressor=self.name, budget=budget)
        try:
            backend = self._load_backend()
            # LLMLingua-2 can preserve numeric evidence and split Chinese
            # prose more reliably with these options.  The compatibility
            # retry keeps the small adapter usable with older/custom backends
            # that only implement ``text`` and ``target_token``.
            try:
                response = backend.compress_prompt(
                    original,
                    target_token=max(1, budget.max_message_tokens),
                    force_reserve_digit=True,
                    chunk_end_tokens=[".", "\n", "。", "！", "？"],
                )
            except TypeError as exc:
                if not any(
                    name in str(exc)
                    for name in ("force_reserve_digit", "chunk_end_tokens")
                ):
                    raise
                response = backend.compress_prompt(
                    original,
                    target_token=max(1, budget.max_message_tokens),
                )
            if isinstance(response, dict):
                compressed = response.get("compressed_prompt")
            else:
                compressed = response
            if not isinstance(compressed, str) or not compressed.strip():
                raise CompressionUnavailableError("LLMLingua 返回空或非文本结果")

            # LLMLingua's target token count is approximate.  A deterministic
            # pass makes bytes, local token estimate, and character limits hard.
            guarded = DeterministicCompressor().compress(compressed, budget)
            return _make_result(
                original,
                guarded.text,
                compressor=self.name,
                budget=budget,
                truncated=guarded.truncated,
                warnings=guarded.warnings,
                attempted_compressor=self.name,
            )
        except Exception as exc:
            return self._fallback_result(original, budget, exc)


class AutoCompressor:
    """Use LLMLingua for long text and deterministic compression otherwise."""

    def __init__(
        self,
        *,
        threshold_tokens: int = 512,
        llmlingua_model: str = "microsoft/llmlingua-2-xlm-roberta-large-meetingbank",
        llmlingua_target_ratio: float = 0.6,
        llmlingua_device_map: str = "cpu",
        llmlingua_allow_download: bool = False,
        semantic: ContextCompressor | None = None,
        deterministic: ContextCompressor | None = None,
    ) -> None:
        self.threshold_tokens = threshold_tokens
        self.target_ratio = llmlingua_target_ratio
        self.deterministic = deterministic or DeterministicCompressor()
        self.semantic = semantic or LLMLinguaCompressor(
            model_name=llmlingua_model,
            device_map=llmlingua_device_map,
            allow_download=llmlingua_allow_download,
            strict=True,
        )

    def _semantic_budget(
        self, text: str, hard_budget: CommunicationBudget,
    ) -> CommunicationBudget:
        """Create a useful compression target below the transport hard limit."""

        target_tokens = max(1, math.ceil(estimate_tokens(text) * self.target_ratio))
        target_bytes = max(1, math.ceil(utf8_size(text) * self.target_ratio))
        target_chars = max(1, math.ceil(len(text) * self.target_ratio))
        return hard_budget.model_copy(update={
            "max_message_tokens": min(hard_budget.max_message_tokens, target_tokens),
            "max_message_bytes": min(hard_budget.max_message_bytes, target_bytes),
            "max_summary_chars": min(hard_budget.max_summary_chars, target_chars),
        })

    def compress(self, text: str, budget: CommunicationBudget) -> CompressionResult:
        if estimate_tokens(text) >= self.threshold_tokens:
            try:
                result = self.semantic.compress(text, self._semantic_budget(text, budget))
                return result.model_copy(update={
                    "attempted_compressor": "llmlingua",
                })
            except Exception as exc:
                # Missing optional packages/models must not force lossy
                # truncation to the semantic target.  Fall back against the
                # original transport budget instead.
                result = self.deterministic.compress(text, budget)
                return result.model_copy(update={
                    "fallback_used": True,
                    "attempted_compressor": "llmlingua",
                    "warnings": [*result.warnings, f"LLMLingua 自动降级: {exc}"],
                })
        return self.deterministic.compress(text, budget)


def build_compressor(
    name: str,
    *,
    llmlingua_threshold_tokens: int = 512,
    llmlingua_model: str = "microsoft/llmlingua-2-xlm-roberta-large-meetingbank",
    llmlingua_target_ratio: float = 0.6,
    llmlingua_device_map: str = "cpu",
    llmlingua_allow_download: bool = False,
) -> ContextCompressor:
    """Create a configured compressor without importing optional packages."""

    if name == "deterministic":
        return DeterministicCompressor()
    if name == "llmlingua":
        return LLMLinguaCompressor(
            model_name=llmlingua_model,
            device_map=llmlingua_device_map,
            allow_download=llmlingua_allow_download,
        )
    if name == "auto":
        return AutoCompressor(
            threshold_tokens=llmlingua_threshold_tokens,
            llmlingua_model=llmlingua_model,
            llmlingua_target_ratio=llmlingua_target_ratio,
            llmlingua_device_map=llmlingua_device_map,
            llmlingua_allow_download=llmlingua_allow_download,
        )
    raise ValueError(f"不支持的通信压缩器: {name}")


def llmlingua_runtime_status(policy: Any) -> dict[str, Any]:
    """Return a no-model-download readiness record for RunState."""

    compressor = getattr(policy, "compressor", "deterministic")
    enabled = compressor in {"auto", "llmlingua"}
    installed = importlib.util.find_spec("llmlingua") is not None if enabled else False
    importable = False
    import_error = None
    if installed:
        try:
            module = importlib.import_module("llmlingua")
            importable = callable(getattr(module, "PromptCompressor", None))
            if not importable:
                import_error = "llmlingua.PromptCompressor 不存在"
        except Exception as exc:  # optional ML stacks fail for many platform reasons
            import_error = f"{type(exc).__name__}: {exc}"
    return {
        "configured_compressor": compressor,
        "llmlingua_enabled": enabled,
        "llmlingua_installed": installed,
        "llmlingua_importable": importable,
        "llmlingua_import_error": import_error,
        "llmlingua_model": getattr(policy, "llmlingua_model", None),
        "llmlingua_threshold_tokens": getattr(
            policy, "llmlingua_threshold_tokens", None,
        ),
        "llmlingua_target_ratio": getattr(policy, "llmlingua_target_ratio", None),
        "llmlingua_device_map": getattr(policy, "llmlingua_device_map", None),
        "llmlingua_allow_download": getattr(
            policy, "llmlingua_allow_download", False,
        ),
        "fallback": "deterministic",
    }
