"""Small registry that keeps domain logic out of the workflow."""

from __future__ import annotations

from orchestration.validation.protocol import BusinessValidator


class BusinessValidatorRegistry:
    def __init__(self) -> None:
        self._validators: dict[str, BusinessValidator] = {}

    def register(self, validator: BusinessValidator) -> None:
        if validator.validator_id in self._validators:
            raise ValueError(f"业务验证器 ID 重复: {validator.validator_id}")
        self._validators[validator.validator_id] = validator

    def resolve(
        self,
        validator_id: str,
        task_spec: dict,
        config: dict,
    ) -> BusinessValidator | None:
        validator = self._validators.get(validator_id)
        if validator is None or not validator.supports(task_spec, config):
            return None
        return validator

    def list_ids(self) -> list[str]:
        return sorted(self._validators)
