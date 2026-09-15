import json
import tempfile
import unittest
from pathlib import Path

from orchestration.graph.repair_loop import business_validation_issues
from orchestration.graph.task_graph import TaskGraph, TaskNode
from orchestration.validation.acceptance_contract import (
    compile_acceptance_contract,
)
from orchestration.validation.acceptance_runner import (
    run_acceptance_contract,
)
from orchestration.validation.runner import run_business_validation


def graph(requirement_ids=None, acceptance_refs=None):
    return TaskGraph(
        graph_id="acceptance-test",
        goal="produce a verified result",
        nodes=[TaskNode(
            node_id="producer",
            description="produce result",
            capability="analysis",
            output_artifacts=["artifacts/result.json"],
            requirement_ids=requirement_ids or ["REQ-1"],
            acceptance_refs=acceptance_refs or ["AC-1"],
            success_criteria=[{"type": "node_result"}],
        )],
    )


def requirement_contract(method, target, *, params=None):
    return {
        "contract_id": "requirements-test",
        "version": 1,
        "global_goal": "produce a real result",
        "requirements": [{
            "requirement_id": "REQ-1",
            "requirement_type": "deliverable",
            "statement": "result must contain independently checkable records",
            "mandatory": True,
            "owner": "planner",
            "expected_outputs": ["artifacts/result.json"],
            "acceptance_criteria": [{
                "criterion_id": "AC-1",
                "method": method,
                "target": target,
                "condition": "machine check",
                "params": params or {},
                "severity": "blocking",
            }],
        }],
    }


def spec(directory, contract):
    return {
        "run_dir": directory,
        "code_policy": {"mode": "none"},
        "required_artifacts": ["artifacts/result.json"],
        "artifact_contract": {
            "intermediate_artifacts": ["artifacts/result.json"],
            "final_artifacts": [],
        },
        "business_validation": {
            "mode": "optional",
            "validators": [],
        },
        "requirement_contract": contract,
    }


class AcceptanceContractTests(unittest.TestCase):
    def _write_result(self, directory, value):
        path = Path(directory) / "artifacts"
        path.mkdir(parents=True, exist_ok=True)
        (path / "result.json").write_text(
            json.dumps(value), encoding="utf-8",
        )

    def test_missing_required_field_fails_despite_self_reported_success(self):
        with tempfile.TemporaryDirectory() as directory:
            self._write_result(directory, {"status": "success"})
            requirements = requirement_contract(
                "required_fields", "artifacts/result.json",
                params={"fields": ["/records", "/metrics/total"]},
            )
            contract = compile_acceptance_contract(
                requirements, graph(), spec(directory, requirements),
            )
            ledger = run_acceptance_contract(directory, contract)
            self.assertEqual(ledger.status, "failed")
            self.assertFalse(ledger.mandatory_requirements_passed)
            self.assertIn("AC-1", ledger.failed_blocking_criteria)
            self.assertIn("/records", ledger.entries[0].summary)

    def test_unsupported_method_is_unverified_and_blocks_closure(self):
        with tempfile.TemporaryDirectory() as directory:
            self._write_result(directory, {"records": []})
            requirements = requirement_contract(
                "model_says_complete", "artifacts/result.json",
            )
            contract = compile_acceptance_contract(
                requirements, graph(), spec(directory, requirements),
            )
            ledger = run_acceptance_contract(directory, contract)
            self.assertEqual(ledger.status, "unverified")
            self.assertEqual(
                ledger.unverified_blocking_criteria, ["AC-1"],
            )

    def test_valid_structured_output_passes_and_hashes_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            self._write_result(directory, {
                "records": [{"id": "A"}], "metrics": {"total": 1},
            })
            requirements = requirement_contract(
                "required_fields", "artifacts/result.json",
                params={"fields": ["/records", "/metrics/total"]},
            )
            contract = compile_acceptance_contract(
                requirements, graph(), spec(directory, requirements),
            )
            ledger = run_acceptance_contract(directory, contract)
            self.assertEqual(ledger.status, "passed")
            self.assertTrue(ledger.mandatory_requirements_passed)
            self.assertIn(
                "artifacts/result.json", ledger.entries[0].evidence_hashes,
            )

    def test_requirement_mapping_survives_compilation(self):
        requirements = requirement_contract(
            "artifact_exists", "artifacts/result.json",
        )
        contract = compile_acceptance_contract(
            requirements, graph(), spec(".", requirements),
        )
        self.assertEqual(contract.criteria[0].producer_node_id, "producer")
        self.assertEqual(contract.criteria[0].requirement_id, "REQ-1")

    def test_parent_requirement_with_own_criterion_is_not_dropped(self):
        requirements = requirement_contract(
            "required_fields", "artifacts/result.json",
            params={"fields": ["/parent_metric"]},
        )
        requirements["requirements"].append({
            "requirement_id": "REQ-CHILD",
            "parent_id": "REQ-1",
            "requirement_type": "deliverable",
            "statement": "child detail",
            "mandatory": True,
            "owner": "planner",
            "expected_outputs": ["artifacts/result.json"],
            "acceptance_criteria": [{
                "criterion_id": "AC-CHILD",
                "method": "required_fields",
                "target": "artifacts/result.json",
                "condition": "child field",
                "params": {"fields": ["/child_metric"]},
                "severity": "blocking",
            }],
        })
        task_graph = graph(
            requirement_ids=["REQ-1", "REQ-CHILD"],
            acceptance_refs=["AC-1", "AC-CHILD"],
        )
        contract = compile_acceptance_contract(
            requirements, task_graph, spec(".", requirements),
        )
        self.assertEqual(
            [criterion.criterion_id for criterion in contract.criteria],
            ["AC-1", "AC-CHILD"],
        )

    def test_artifact_check_routes_to_physical_writer_not_semantic_owner(self):
        requirements = requirement_contract(
            "required_fields", "artifacts/result.json",
            params={"fields": ["/records"]},
        )
        task_graph = TaskGraph(
            graph_id="artifact-routing", goal="route repair",
            nodes=[
                TaskNode(
                    node_id="model",
                    description="define model",
                    capability="analysis",
                    requirement_ids=["REQ-1"],
                    acceptance_refs=["AC-1"],
                    success_criteria=[{"type": "node_result"}],
                ),
                TaskNode(
                    node_id="code",
                    description="write result",
                    capability="code",
                    dependencies=["model"],
                    output_artifacts=["artifacts/result.json"],
                    success_criteria=[{"type": "execution_exit_code", "equals": 0}],
                ),
            ],
        )
        contract = compile_acceptance_contract(
            requirements, task_graph, spec(".", requirements),
        )
        self.assertEqual(contract.criteria[0].producer_node_id, "code")

    def test_runner_upgrades_non_code_mandatory_contract_to_required(self):
        with tempfile.TemporaryDirectory() as directory:
            self._write_result(directory, {"status": "success"})
            requirements = requirement_contract(
                "required_fields", "artifacts/result.json",
                params={"fields": ["/records"]},
            )
            result = run_business_validation(
                directory, spec(directory, requirements),
                task_graph=graph(), persist=False,
            )
            self.assertEqual(result.mode, "required")
            self.assertEqual(result.status, "failed")
            self.assertIn("acceptance_contract", result.failed_required_checks)

    def test_failed_criterion_routes_to_declared_producer(self):
        with tempfile.TemporaryDirectory() as directory:
            self._write_result(directory, {"status": "success"})
            requirements = requirement_contract(
                "required_fields", "artifacts/result.json",
                params={"fields": ["/records"]},
            )
            task_graph = graph()
            result = run_business_validation(
                directory, spec(directory, requirements),
                task_graph=task_graph, persist=False,
            )
            issues = business_validation_issues(result, task_graph)
            acceptance_issues = [
                item for item in issues
                if item["validator_id"] == "acceptance_contract"
            ]
            self.assertEqual(len(acceptance_issues), 1)
            self.assertEqual(
                acceptance_issues[0]["target_node_id"], "producer",
            )

    def test_numeric_and_record_count_methods_use_json_pointers(self):
        with tempfile.TemporaryDirectory() as directory:
            self._write_result(directory, {
                "records": [{"id": "A"}, {"id": "B"}],
                "expected_records": ["A", "B"],
                "metrics": {"score": 0.95},
            })
            requirements = requirement_contract(
                "record_count_compare", "artifacts/result.json#/records",
                params={
                    "left": "artifacts/result.json#/records",
                    "right": "artifacts/result.json#/expected_records",
                    "operator": "eq",
                },
            )
            requirements["requirements"].append({
                "requirement_id": "REQ-2",
                "requirement_type": "metric",
                "statement": "score reaches threshold",
                "mandatory": True,
                "owner": "planner",
                "acceptance_criteria": [{
                    "criterion_id": "AC-2",
                    "method": "numeric_compare",
                    "target": "artifacts/result.json#/metrics/score",
                    "condition": "score >= 0.9",
                    "params": {
                        "left": "artifacts/result.json#/metrics/score",
                        "value": 0.9,
                        "operator": "ge",
                    },
                    "severity": "blocking",
                }],
            })
            task_graph = graph()
            task_graph.get_node("producer").requirement_ids.append("REQ-2")
            task_graph.get_node("producer").acceptance_refs.append("AC-2")
            contract = compile_acceptance_contract(
                requirements, task_graph, spec(directory, requirements),
            )
            ledger = run_acceptance_contract(directory, contract)
            self.assertEqual(ledger.status, "passed")
            self.assertEqual([item.status for item in ledger.entries], [
                "passed", "passed",
            ])

    def test_evidence_supported_requires_specific_keywords(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence_dir = (
                Path(directory) / "artifacts" / "tool_results" / "verify"
            )
            evidence_dir.mkdir(parents=True)
            evidence = {
                "passed": True,
                "catalog": [{
                    "evidence_ref": "artifacts/result.json",
                    "verified": True,
                }],
                "decision": {"claims": [{
                    "claim_id": "claim_001",
                    "source_node_ids": ["producer"],
                    "claim": "全部输入记录均已覆盖",
                    "verdict": "supported",
                    "evidence_refs": ["artifacts/result.json"],
                }]},
            }
            (evidence_dir / "evidence_verification.json").write_text(
                json.dumps(evidence, ensure_ascii=False), encoding="utf-8",
            )
            self._write_result(directory, {"records": [1]})
            requirements = requirement_contract(
                "evidence_supported", "artifacts/result.json",
                params={"keywords": ["输入", "覆盖"]},
            )
            contract = compile_acceptance_contract(
                requirements, graph(), spec(directory, requirements),
            )
            ledger = run_acceptance_contract(directory, contract)
            self.assertEqual(ledger.status, "passed")
            self.assertIn(
                "artifacts/result.json", ledger.entries[0].evidence_hashes,
            )

    def test_markdown_report_fields_are_delegated_to_final_validator(self):
        requirements = requirement_contract(
            "required_fields", "final_report.md",
            params={"fields": ["/required_sections/results"]},
        )
        requirements["requirements"][0]["owner"] = "outer_workflow"
        task_spec = spec(".", requirements)
        task_spec["artifact_contract"]["final_artifacts"] = [
            "final_report.md",
        ]

        contract = compile_acceptance_contract(
            requirements, graph(), task_spec,
        )

        self.assertEqual(contract.criteria[0].phase, "delivery")
        self.assertEqual(contract.criteria[0].method, "artifact_exists")

    def test_supported_claim_survives_unrelated_global_verifier_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence_dir = (
                Path(directory) / "artifacts" / "tool_results" / "verify"
            )
            evidence_dir.mkdir(parents=True)
            self._write_result(directory, {"records": [1]})
            evidence = {
                "passed": False,
                "catalog": [{
                    "evidence_ref": "artifacts/result.json",
                    "verified": True,
                }],
                "decision": {"claims": [
                    {
                        "claim_id": "claim_supported",
                        "source_node_ids": ["producer"],
                        "claim": "全部输入记录均已覆盖",
                        "verdict": "supported",
                        "evidence_refs": ["artifacts/result.json"],
                    },
                    {
                        "claim_id": "claim_other",
                        "source_node_ids": ["other-producer"],
                        "claim": "另一项事实缺少证据",
                        "verdict": "insufficient",
                        "evidence_refs": [],
                    },
                ]},
            }
            (evidence_dir / "evidence_verification.json").write_text(
                json.dumps(evidence, ensure_ascii=False), encoding="utf-8",
            )
            requirements = requirement_contract(
                "evidence_supported", "artifacts/result.json",
                params={"keywords": ["输入", "覆盖"]},
            )
            contract = compile_acceptance_contract(
                requirements, graph(), spec(directory, requirements),
            )

            ledger = run_acceptance_contract(directory, contract)

            self.assertEqual(ledger.status, "passed")
            self.assertEqual(ledger.entries[0].status, "passed")


if __name__ == "__main__":
    unittest.main()
