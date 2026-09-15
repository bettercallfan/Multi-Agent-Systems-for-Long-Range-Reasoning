"""Framework-owned registry for端-边-云 resources and model routes."""

from __future__ import annotations

from typing import Literal
import os
from pydantic import BaseModel, Field, field_validator


class ResourceDescriptor(BaseModel):
    resource_id: str
    location: Literal["device", "edge", "cloud"]
    # A resource may point at a real OpenAI-compatible deployment.  The
    # registry remains backend-neutral: availability and routing are still
    # auditable even when all deployments share one endpoint.
    model_name: str = ""
    base_url: str = ""
    compute_score: float = Field(default=0.5, ge=0, le=1)
    latency_ms: float = Field(default=100, ge=0)
    cost_score: float = Field(default=0.5, ge=0)
    security_levels: list[str] = Field(default_factory=lambda: ["internal"])
    available: bool = True
    max_concurrency: int = Field(default=1, ge=1)
    active_load: int = Field(default=0, ge=0)

    @field_validator("resource_id")
    @classmethod
    def non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("resource_id 不能为空")
        return value

    def can_accept(self) -> bool:
        return self.available and self.active_load < self.max_concurrency


class ResourceRegistry:
    """Deterministic registry; real infrastructure can replace this backend."""

    def __init__(self, resources: list[ResourceDescriptor] | None = None):
        self._resources: dict[str, ResourceDescriptor] = {}
        for resource in resources or []:
            self.register(resource)

    def register(self, resource: ResourceDescriptor) -> None:
        if resource.resource_id in self._resources:
            raise ValueError(f"resource_id 已注册: {resource.resource_id}")
        self._resources[resource.resource_id] = resource

    def get(self, resource_id: str) -> ResourceDescriptor:
        try:
            return self._resources[resource_id]
        except KeyError as exc:
            raise KeyError(f"resource 未注册: {resource_id}") from exc

    def set_available(self, resource_id: str, available: bool) -> None:
        self.get(resource_id).available = available

    def list_available(self) -> list[ResourceDescriptor]:
        return [item for item in self._resources.values() if item.can_accept()]

    def has_available_location(self, location: str) -> bool:
        return any(item.can_accept() and item.location == location for item in self._resources.values())

    def snapshot(self) -> list[dict]:
        return [item.model_dump(mode="json") for item in self._resources.values()]


def default_resource_registry() -> ResourceRegistry:
    base_url = os.getenv("MODEL_BASE_URL", "")
    device_model = os.getenv("DEVICE_MODEL", "qwen3.5-flash")
    edge_model = os.getenv("EDGE_MODEL", device_model)
    cloud_model = os.getenv("CLOUD_MODEL", os.getenv("MODEL_NAME", "qwen3.5-35b-a3b"))
    device_url = os.getenv("DEVICE_BASE_URL", base_url)
    edge_url = os.getenv("EDGE_BASE_URL", base_url)
    cloud_url = os.getenv("CLOUD_BASE_URL", base_url)
    return ResourceRegistry([
        ResourceDescriptor(resource_id="device-local", location="device", model_name=device_model, base_url=device_url, compute_score=.35, latency_ms=20, cost_score=0.0, security_levels=["internal", "confidential"]),
        ResourceDescriptor(resource_id="edge-local", location="edge", model_name=edge_model, base_url=edge_url, compute_score=.65, latency_ms=80, cost_score=.2, security_levels=["internal", "confidential"]),
        ResourceDescriptor(resource_id="cloud-qwen", location="cloud", model_name=cloud_model, base_url=cloud_url, compute_score=1.0, latency_ms=500, cost_score=.8, security_levels=["internal"]),
    ])
