import unittest

from orchestration.graph.recovery_controller import (
    RecoveryAction,
    RecoveryController,
    RecoveryLimitExceededError,
    ReplanValidationError,
    merge_full_replacement_graph,
)
from orchestration.graph.task_graph import NodeStatus, TaskGraph, TaskNode


def node(
    node_id: str,
    dependencies: list[str] | None = None,
    *,
    capability: str = "analysis",
    description: str | None = None,
) -> TaskNode:
    return TaskNode(
        node_id=node_id,
        description=description or f"execute {node_id}",
        capability=capability,
        dependencies=dependencies or [],
        success_criteria=[{"type": "node_result"}],
        max_retries=1,
    )


def failed_graph() -> TaskGraph:
    graph = TaskGraph(
        graph_id="recovery_graph",
        version=3,
        goal="complete a recoverable task",
        nodes=[node("A"), node("B", ["A"]), node("C", ["B"])],
    )
    graph.ready_nodes()
    graph.mark_running("A", "analysis_primary")
    graph.mark_completed("A", {"summary": "trusted", "value": 42})
    graph.ready_nodes()
    graph.mark_running("B", "analysis_primary")
    graph.mark_failed("B", {"error_type": "quality"})
    graph.block_failed_dependencies()
    return graph


class RecoveryDecisionTests(unittest.TestCase):
    def setUp(self):
        self.controller = RecoveryController({
            "recovery_policy": {
                "max_node_retries": 1,
                "max_replans": 1,
            },
            "team_policy": {"max_executor_switches": 1},
        })

    def test_decision_order_and_attempt_history_are_deterministic(self):
        first = self.controller.record_attempt("analyze", "primary")
        self.assertEqual(first.executor_attempts, ["primary"])

        retry = self.controller.decide_after_failure(
            "analyze", "primary", ["primary", "backup"]
        )
        self.assertEqual(retry.action, RecoveryAction.SAME_EXECUTOR_RETRY)
        self.assertEqual(retry.selected_executor_id, "primary")

        self.controller.record_attempt("analyze", retry.selected_executor_id)
        switch = self.controller.decide_after_failure(
            "analyze", "primary", ["primary", "backup"]
        )
        self.assertEqual(switch.action, RecoveryAction.SWITCH_EXECUTOR)
        self.assertEqual(switch.selected_executor_id, "backup")

        history = self.controller.record_attempt("analyze", switch.selected_executor_id)
        self.assertEqual(history.executor_attempts, ["primary", "primary", "backup"])
        self.assertEqual(history.tried_executors, ["primary", "backup"])
        self.assertEqual(history.same_executor_retries_used, 1)
        self.assertEqual(history.executor_switches_used, 1)

        replan = self.controller.decide_after_failure(
            "analyze", "backup", ["primary", "backup"]
        )
        self.assertEqual(replan.action, RecoveryAction.REPLAN)
        self.controller.begin_replan()
        failed = self.controller.decide_after_failure(
            "analyze", "backup", ["primary", "backup"]
        )
        self.assertEqual(failed.action, RecoveryAction.FAIL)
        self.assertIn("重规划额度耗尽", failed.reason)

    def test_task_spec_limits_cannot_be_exceeded(self):
        self.controller.record_attempt("N", "one")
        self.controller.record_attempt("N", "one")
        with self.assertRaises(RecoveryLimitExceededError):
            self.controller.record_attempt("N", "one")

        self.controller.record_attempt("N", "two")
        with self.assertRaises(RecoveryLimitExceededError):
            self.controller.record_attempt("N", "three")

        self.assertEqual(self.controller.begin_replan(), 1)
        with self.assertRaises(RecoveryLimitExceededError):
            self.controller.begin_replan()

    def test_node_limit_and_error_recommendation_can_only_narrow_policy(self):
        self.controller.record_attempt("N", "one")
        decision = self.controller.decide_after_failure(
            "N",
            "one",
            ["one", "two"],
            node_retry_limit=0,
        )
        self.assertEqual(decision.action, RecoveryAction.SWITCH_EXECUTOR)

        decision = self.controller.decide_after_failure(
            "N",
            "one",
            ["one"],
            retry_allowed=False,
            switch_allowed=False,
            replan_allowed=False,
        )
        self.assertEqual(decision.action, RecoveryAction.FAIL)

    def test_failure_must_correspond_to_recorded_attempt(self):
        with self.assertRaisesRegex(ValueError, "record_attempt"):
            self.controller.decide_after_failure("N", "unrecorded")
        self.controller.record_attempt("N", "one")
        with self.assertRaisesRegex(ValueError, "record_attempt"):
            self.controller.decide_after_failure("N", "different")

    def test_policy_sections_must_be_objects(self):
        with self.assertRaisesRegex(ValueError, "recovery_policy"):
            RecoveryController({"recovery_policy": []})
        with self.assertRaisesRegex(ValueError, "team_policy"):
            RecoveryController({"team_policy": "dynamic"})

    def test_snapshot_is_serializable_and_defensive(self):
        self.controller.record_attempt("N", "one")
        snapshot = self.controller.snapshot()
        snapshot["nodes"]["N"]["executor_attempts"].append("tampered")
        self.assertEqual(self.controller.history_for("N").executor_attempts, ["one"])
        self.assertEqual(snapshot["policy"]["max_executor_switches"], 1)


class ReplanMergeTests(unittest.TestCase):
    def test_completed_node_is_immutable_and_remaining_graph_is_reset(self):
        current = failed_graph()
        replacement = TaskGraph(
            graph_id="planner_chosen_id",
            version=99,
            goal="repaired route",
            nodes=[
                node("A"),
                node("B_repaired", ["A"], capability="reasoning"),
                node("C_repaired", ["B_repaired"]),
            ],
        )
        # Runtime claims from the planner must never survive the merge.
        replacement.ready_nodes()
        replacement.mark_running("A", "untrusted")
        replacement.mark_completed("A", {"summary": "forged"})
        replacement.ready_nodes()
        replacement.mark_running("B_repaired", "untrusted")

        merged = merge_full_replacement_graph(
            current,
            replacement,
            available_capabilities={"analysis", "reasoning"},
        )
        self.assertEqual(merged.graph_id, current.graph_id)
        self.assertEqual(merged.version, 4)
        self.assertEqual(merged.goal, "repaired route")

        completed = merged.get_node("A")
        self.assertEqual(completed.status, NodeStatus.COMPLETED)
        self.assertEqual(completed.assigned_executor, "analysis_primary")
        self.assertEqual(completed.attempts, 1)
        self.assertEqual(completed.result, {"summary": "trusted", "value": 42})

        repaired = merged.get_node("B_repaired")
        self.assertEqual(repaired.status, NodeStatus.PENDING)
        self.assertIsNone(repaired.assigned_executor)
        self.assertEqual(repaired.attempts, 0)
        self.assertIsNone(repaired.result)
        self.assertIsNone(repaired.error)
        self.assertEqual([item.node_id for item in merged.ready_nodes()], ["B_repaired"])

    def test_completed_node_must_exist_with_same_definition(self):
        current = failed_graph()
        missing = TaskGraph(
            graph_id="new",
            goal="x",
            nodes=[node("replacement")],
        )
        with self.assertRaisesRegex(ReplanValidationError, "缺少已完成"):
            merge_full_replacement_graph(current, missing)

        changed = TaskGraph(
            graph_id="new",
            goal="x",
            nodes=[
                node("A", description="changed immutable definition"),
                node("replacement", ["A"]),
            ],
        )
        with self.assertRaisesRegex(ReplanValidationError, "description"):
            merge_full_replacement_graph(current, changed)

    def test_unfinished_nodes_must_have_available_capability(self):
        current = failed_graph()
        replacement = TaskGraph(
            graph_id="new",
            goal="x",
            nodes=[node("A"), node("new_work", ["A"], capability="unavailable")],
        )
        with self.assertRaisesRegex(ReplanValidationError, "没有可用执行器"):
            merge_full_replacement_graph(
                current, replacement, available_capabilities={"analysis"}
            )

    def test_controller_consumes_replan_even_when_replacement_is_invalid(self):
        controller = RecoveryController({
            "recovery_policy": {
                "max_node_retries": 0,
                "max_executor_switches": 0,
                "max_replans": 1,
            }
        })
        current = failed_graph()
        invalid = TaskGraph(graph_id="new", goal="x", nodes=[node("replacement")])
        with self.assertRaises(ReplanValidationError):
            controller.merge_replanned_graph(current, invalid)
        self.assertEqual(controller.replans_used, 1)
        with self.assertRaises(RecoveryLimitExceededError):
            controller.merge_replanned_graph(current, invalid)

    def test_replan_reusing_failed_node_id_starts_new_recovery_epoch(self):
        current = failed_graph()
        controller = RecoveryController({
            "recovery_policy": {
                "max_node_retries": 0,
                "max_executor_switches": 0,
                "max_replans": 1,
            }
        })
        controller.record_attempt("B", "analysis_primary")
        replacement = TaskGraph(
            graph_id="new",
            goal="repair same logical node",
            nodes=[node("A"), node("B", ["A"]), node("C", ["B"])],
        )

        merged = controller.merge_replanned_graph(current, replacement)

        self.assertEqual(merged.get_node("B").status, NodeStatus.PENDING)
        self.assertEqual(controller.history_for("B").executor_attempts, [])
        controller.record_attempt("B", "analysis_primary")
        snapshot = controller.snapshot()
        self.assertEqual(
            snapshot["archived_epochs"][0]["nodes"]["B"]["executor_attempts"],
            ["analysis_primary"],
        )


if __name__ == "__main__":
    unittest.main()
