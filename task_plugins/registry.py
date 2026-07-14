from __future__ import annotations

from task_plugins.expense_reimbursement import ExpenseReimbursementPlugin
from task_plugins.generic import GenericTaskPlugin


_PLUGINS = {
    "generic": GenericTaskPlugin(),
    "expense_reimbursement": ExpenseReimbursementPlugin(),
}


def get_task_plugin(plugin_id: str | None):
    return _PLUGINS.get(plugin_id or "generic", _PLUGINS["generic"])


def detect_plugin_id(task_spec: dict, text: str) -> str:
    artifacts = task_spec.get("artifacts", {}).get("results", [])
    if "expense_summary.json" in text or "报销" in text or any("expense_summary" in path for path in artifacts):
        return "expense_reimbursement"
    return "generic"
