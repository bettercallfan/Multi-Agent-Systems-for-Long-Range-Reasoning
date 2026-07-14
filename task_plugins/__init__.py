"""Task-specific extensions registered outside the core workflow."""

from task_plugins.registry import get_task_plugin

__all__ = ["get_task_plugin"]
