"""Uniform extension boundary for business validators."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from orchestration.validation.models import (
    BusinessCheckResult,
    BusinessValidationContext,
)


@runtime_checkable
class BusinessValidator(Protocol):
    validator_id: str
    validator_type: str

    def supports(self, task_spec: dict, validator_config: dict) -> bool:
        ...

    def validate(
        self, context: BusinessValidationContext,
    ) -> BusinessCheckResult:
        ...

