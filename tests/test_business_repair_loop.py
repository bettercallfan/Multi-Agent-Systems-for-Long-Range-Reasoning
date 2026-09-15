import tempfile
import unittest

from orchestration.core.run_state import RunState
from orchestration.graph.repair_loop import (
    BusinessRepairLoop,
    business_validation_issues,
    evidence_verification_issues,
)
from orchestration.graph.task_graph import NodeStatus, TaskGraph, TaskNode
from orchestration.validation.models import (
    BusinessCheckResult,
    BusinessValidationResult,
)


def node(node_id, dependencies=None, *, capability="analysis", outputs=None):
    return TaskNode(
        node_id=node_id,
        description=f"execute {node_id}",
        capability=capability,
        dependencies=dependencies or [],
        output_artifacts=outputs or [],
        success_criteria=[{"type": "node_result"}],
    )


def completed_graph():
    graph = TaskGraph(
        graph_id="business-repair",
        goal="produce and verify a result",
        nodes=[
            node("input", outputs=["normalized_input.json"]),
            node(
                "code",
                ["input"],
                capability="code",
                outputs=[
                    "artifacts/code_pipeline.py",
                    "artifacts/result.json",
                ],
            ),
            node("verify", ["code"], capability="evidence_verification"),
            node("unrelated", capability="research"),
        ],
    )
    for node_id in ["input", "unrelated", "code", "verify"]:
        graph.ready_nodes()
        graph.mark_running(node_id, f"{node_id}_executor")
        graph.mark_completed(node_id, {
            "node_id": node_id,
            "executor_id": f"{node_id}_executor",
            "status": "completed",
        })
    return graph


def failed_validation():
    check = BusinessCheckResult(
        check_id="code_shortcut_scan",
        validator_id="code_shortcut_scan",
        status="failed",
        summary="generated code used a default data shortcut",
        failed_checks=[{
            "check": "no_default_data_on_missing_input",
            "type": "default_data_on_missing_input",
            "path": "artifacts/code_pipeline.py",
            "reason": "default DataFrame detected",
        }],
        detected_shortcuts=[{
            "type": "default_data_on_missing_input",
            "path": "artifacts/code_pipeline.py",
            "severity": "fatal",
        }],
        evidence_refs=["artifacts/code_pipeline.py"],
    )
    return BusinessValidationResult(
        status="failed",
        mode="required",
        checks=[check],
        failed_required_checks=["code_shortcut_scan"],
        evidence_refs=["artifacts/code_pipeline.py"],
        started_at="x",
        finished_at="x",
    )


def passed_validation():
    return BusinessValidationResult(
        status="passed",
        mode="required",
        checks=[BusinessCheckResult(
            check_id="code_shortcut_scan",
            validator_id="code_shortcut_scan",
            status="passed",
            summary="independent validation passed",
            evidence_refs=[
                "artifacts/code_pipeline.py",
                "artifacts/result.json",
            ],
        )],
        evidence_refs=[
            "artifacts/code_pipeline.py",
            "artifacts/result.json",
        ],
        started_at="x",
        finished_at="x",
    )


def graph_with_failed_evidence():
    graph = completed_graph()
    graph.reopen_subgraph(["verify"], {})
    graph.ready_nodes()
    graph.mark_running("verify", "evidence")
    graph.mark_failed("verify", {
        "node_id": "verify",
        "executor_id": "evidence",
        "status": "failed",
        "summary": "independent evidence contradicted the result",
        "structured_output": {
            "verification": {
                "status": "failed",
                "issues": [{
                    "issue_type": "critical_contradiction",
                    "description": "result metric exceeds its valid range",
                    "recommendation": "recompute from normalized input",
                }],
            },
        },
        "evidence_refs": [
            "normalized_input.json",
            "artifacts/result.json",
        ],
        "error_type": "evidence_verification_failure",
        "error_message": "result metric exceeds its valid range",
    })
    return graph


class TaskGraphBusinessReopenTests(unittest.TestCase):
    def test_reopens_producer_and_descendants_only(self):
        graph = completed_graph()
        affected = graph.reopen_subgraph(
            ["code"],
            {"code": {
                "repair_instruction": "remove default data",
                "issue_ids": ["ISSUE-1"],
            }},
        )
        self.assertEqual(affected, ["code", "verify"])
        self.assertEqual(graph.get_node("input").status, NodeStatus.COMPLETED)
        self.assertEqual(graph.get_node("unrelated").status, NodeStatus.COMPLETED)
        self.assertEqual(graph.get_node("code").status, NodeStatus.PENDING)
        self.assertEqual(graph.get_node("verify").status, NodeStatus.PENDING)
        self.assertIn(
            "remove default data",
            graph.get_node("code").recovery_context["repair_instruction"],
        )
        self.assertEqual(graph.get_node("verify").recovery_context, {})

    def test_business_issue_is_routed_by_artifact_ownership(self):
        issues = business_validation_issues(
            failed_validation(), completed_graph(),
        )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["target_node_id"], "code")
        self.assertEqual(issues[0]["repair_target"], "code")
        self.assertTrue(issues[0]["issue_id"].startswith("business-"))

    def test_evidence_failure_is_routed_to_unique_code_producer(self):
        issues = evidence_verification_issues(graph_with_failed_evidence())
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["target_node_id"], "code")
        self.assertIn("valid range", issues[0]["description"])
        self.assertTrue(issues[0]["issue_id"].startswith("evidence-"))


class BusinessRepairLoopTests(unittest.IsolatedAsyncioTestCase):
    def _configure(self, state):
        state.configure({
            "task_type": "generic_test",
            "code_policy": {"mode": "lightweight", "max_retries": 0},
            "artifact_contract": {},
            "business_validation": {"mode": "required"},
            "communication_policy": {},
            "routing_policy": {},
            "team_policy": {},
            "recovery_policy": {"max_business_repair_rounds": 2},
            "external_tools_policy": {},
            "capability_contract": {},
            "memory_policy": {},
        })
        state.start_stage("classify")
        state.start_stage("plan")
        state.start_stage("execute")

    async def test_failed_business_result_replays_and_resolves_issue(self):
        with tempfile.TemporaryDirectory() as directory:
            state = RunState(directory)
            self._configure(state)
            graph = completed_graph()
            state.record_task_graph(graph)
            initial = failed_validation()
            state.record_business_validation(
                initial.model_dump(mode="json"),
                "review/business_validation.json",
            )
            replay_calls = []

            async def replay(reopened):
                replay_calls.append([
                    item.node_id for item in reopened.nodes
                    if item.status == NodeStatus.PENDING
                ])
                self.assertIn(
                    "default DataFrame",
                    reopened.get_node("code").recovery_context[
                        "repair_instruction"
                    ],
                )
                reopened.ready_nodes()
                reopened.mark_running("code", "code_pipeline_executor")
                reopened.mark_completed("code", {
                    "node_id": "code",
                    "executor_id": "code_pipeline_executor",
                    "status": "completed",
                })
                reopened.ready_nodes()
                reopened.mark_running("verify", "evidence")
                reopened.mark_completed("verify", {
                    "node_id": "verify",
                    "executor_id": "evidence",
                    "status": "completed",
                })
                return reopened

            loop = BusinessRepairLoop(state, max_rounds=2)
            outcome = await loop.run(
                graph,
                initial,
                replay=replay,
                validate=passed_validation,
            )

            self.assertEqual(outcome.status, "repaired")
            self.assertEqual(outcome.rounds_used, 1)
            self.assertEqual(replay_calls, [["code", "verify"]])
            self.assertEqual(state.get_unresolved_blocking_issues(), [])
            self.assertIsNone(state.get("failure"))
            repairs = state.get("recovery")["business_repairs"]
            self.assertEqual(repairs["rounds_used"], 1)
            self.assertEqual(repairs["history"][0]["status"], "repaired")

    async def test_unroutable_input_failure_is_blocked_without_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            state = RunState(directory)
            self._configure(state)
            graph = completed_graph()
            validation = BusinessValidationResult(
                status="failed",
                mode="required",
                checks=[BusinessCheckResult(
                    check_id="input_coverage",
                    validator_id="input_coverage",
                    status="failed",
                    summary="normalized input missing",
                    failed_checks=[{
                        "check": "normalized_input_exists",
                        "reason": "missing",
                    }],
                )],
                failed_required_checks=["input_coverage"],
                started_at="x",
                finished_at="x",
            )

            async def replay(_):
                self.fail("unroutable issue must not replay the graph")

            outcome = await BusinessRepairLoop(
                state, max_rounds=2,
            ).run(
                graph,
                validation,
                replay=replay,
                validate=passed_validation,
            )
            self.assertEqual(outcome.status, "blocked")
            self.assertEqual(outcome.rounds_used, 0)
            self.assertEqual(
                len(state.get_unresolved_blocking_issues()), 1,
            )

    async def test_failed_evidence_gate_reopens_code_and_verifier(self):
        with tempfile.TemporaryDirectory() as directory:
            state = RunState(directory)
            self._configure(state)
            graph = graph_with_failed_evidence()
            state.record_task_graph(graph)
            replay_calls = []

            async def replay(reopened):
                replay_calls.append([
                    item.node_id for item in reopened.nodes
                    if item.status == NodeStatus.PENDING
                ])
                instruction = reopened.get_node(
                    "code"
                ).recovery_context["repair_instruction"]
                self.assertIn("valid range", instruction)
                for node_id in ["code", "verify"]:
                    reopened.ready_nodes()
                    reopened.mark_running(node_id, f"{node_id}_executor")
                    reopened.mark_completed(node_id, {
                        "node_id": node_id,
                        "executor_id": f"{node_id}_executor",
                        "status": "completed",
                    })
                return reopened

            outcome = await BusinessRepairLoop(
                state, max_rounds=2,
            ).run(
                graph,
                passed_validation(),
                replay=replay,
                validate=passed_validation,
            )

            self.assertEqual(outcome.status, "repaired")
            self.assertEqual(outcome.rounds_used, 1)
            self.assertEqual(replay_calls, [["code", "verify"]])


if __name__ == "__main__":
    unittest.main()
