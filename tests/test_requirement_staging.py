import unittest

from orchestration.core.schemas import (
    RequirementAcceptance,
    RequirementItem,
    RequirementSet,
)
from orchestration.graph.task_graph import NodeStatus, TaskGraph, TaskNode
from orchestration.planning.requirement_progress import (
    build_requirement_progress_ledger,
    requirement_needs_code,
    scoped_requirement_set,
    select_requirement_batch,
)
from orchestration.planning.staged_expansion import (
    append_governance_nodes,
    build_stage_task_spec,
    enforce_stage_graph_contract,
    merge_stage_graph,
    requirement_staging_enabled,
    StageContractError,
)


def requirement(
    requirement_id: str,
    *,
    parent_id: str | None = None,
    depends_on: list[str] | None = None,
    outputs: list[str] | None = None,
    mandatory: bool = True,
    requirement_type: str = "goal",
    acceptance: bool = False,
) -> RequirementItem:
    return RequirementItem(
        requirement_id=requirement_id,
        parent_id=parent_id,
        depends_on=depends_on or [],
        statement=f"complete {requirement_id}",
        mandatory=mandatory,
        requirement_type=requirement_type,
        expected_outputs=outputs or [],
        acceptance_criteria=([
            RequirementAcceptance(
                criterion_id=f"AC-{requirement_id}",
                method="artifact_exists" if outputs else "evidence_supported",
                target=(outputs or [requirement_id])[0],
                condition=f"verify {requirement_id}",
                params={} if outputs else {"keywords": [requirement_id]},
            )
        ] if acceptance else []),
    )


def graph_node(
    node_id: str,
    requirement_id: str,
    *,
    capability: str = "analysis",
    outputs: list[str] | None = None,
) -> TaskNode:
    return TaskNode(
        node_id=node_id,
        description=node_id,
        capability=capability,
        requirement_ids=[requirement_id],
        output_artifacts=outputs or [],
        success_criteria=[{"type": "node_result"}],
    )


class RequirementStagingTests(unittest.TestCase):
    def setUp(self):
        self.requirements = RequirementSet(
            contract_id="req-stage",
            global_goal="complete a long task",
            requirements=[
                requirement("ROOT", mandatory=True),
                requirement("R1", parent_id="ROOT"),
                requirement("R2", parent_id="ROOT", depends_on=["R1"]),
                requirement(
                    "R3", parent_id="ROOT", depends_on=["R2"],
                    outputs=["artifacts/code_pipeline.py"],
                    requirement_type="deliverable",
                ),
                requirement(
                    "R4", parent_id="ROOT", depends_on=["R2"],
                    outputs=["artifacts/code_pipeline.py"],
                    requirement_type="deliverable",
                ),
            ],
        )

    def test_only_dependency_ready_requirements_are_selected(self):
        ledger = build_requirement_progress_ledger(self.requirements)
        self.assertEqual(
            select_requirement_batch(
                ledger, self.requirements,
                max_requirements=2, max_prompt_tokens=4_000,
            ),
            ["R1"],
        )
        ledger.item("R1").status = "produced"
        self.assertEqual(
            select_requirement_batch(
                ledger, self.requirements,
                max_requirements=2, max_prompt_tokens=4_000,
            ),
            ["R2"],
        )
        ledger.item("R2").status = "produced"
        # A single physical implementation stage may exceed the soft item
        # target so code-producing requirements are not split into two writers.
        self.assertEqual(
            select_requirement_batch(
                ledger, self.requirements,
                max_requirements=1, max_prompt_tokens=4_000,
            ),
            ["R3", "R4"],
        )

    def test_scoped_contract_keeps_ancestors_but_only_leaf_is_mandatory(self):
        scoped = scoped_requirement_set(self.requirements, ["R2"])
        by_id = {item.requirement_id: item for item in scoped.requirements}
        self.assertEqual(set(by_id), {"ROOT", "R2"})
        self.assertFalse(by_id["ROOT"].mandatory)
        self.assertTrue(by_id["R2"].mandatory)

    def test_parent_with_own_acceptance_is_an_executable_progress_item(self):
        contract = RequirementSet(
            contract_id="parent-progress", global_goal="goal",
            requirements=[
                requirement("MODEL", acceptance=True),
                requirement(
                    "OUTPUT", parent_id="MODEL", depends_on=["MODEL"],
                    requirement_type="deliverable",
                    outputs=["artifacts/result.json"],
                ),
            ],
        )
        ledger = build_requirement_progress_ledger(contract)
        self.assertEqual([item.requirement_id for item in ledger.items], [
            "MODEL", "OUTPUT",
        ])
        self.assertEqual(ledger.item("OUTPUT").depends_on, ["MODEL"])
        self.assertEqual(ledger.ready_ids(), ["MODEL"])

    def test_constraint_citing_code_is_not_misclassified_as_code_stage(self):
        constraint = requirement(
            "CONSTRAINT", requirement_type="constraint",
            outputs=["artifacts/result.json", "artifacts/code_pipeline.py"],
        )
        deliverable = requirement(
            "DELIVERY", requirement_type="deliverable",
            outputs=["artifacts/result.json"],
        )
        validation_program = requirement(
            "PROGRAM", requirement_type="validation",
            outputs=["artifacts/code_pipeline.py"],
        )
        self.assertFalse(requirement_needs_code(constraint))
        self.assertTrue(requirement_needs_code(deliverable))
        self.assertTrue(requirement_needs_code(validation_program))

    def test_math_modeling_frontier_keeps_solution_output_after_model_goal(self):
        contract = RequirementSet(
            contract_id="math-frontier", global_goal="solve model",
            requirements=[
                requirement(
                    "C1", requirement_type="constraint", acceptance=True,
                    outputs=[
                        "artifacts/result.json",
                        "artifacts/code_pipeline.py",
                    ],
                ),
                requirement(
                    "MODEL", requirement_type="goal", acceptance=True,
                    depends_on=["C1"], outputs=["artifacts/result.json"],
                ),
                requirement(
                    "SOLUTION", parent_id="MODEL", depends_on=["MODEL"],
                    requirement_type="deliverable",
                    outputs=["artifacts/result.json"],
                ),
                requirement(
                    "JSON", requirement_type="deliverable",
                    outputs=["artifacts/result.json"],
                ),
            ],
        )
        ledger = build_requirement_progress_ledger(contract)
        first = select_requirement_batch(
            ledger, contract, max_requirements=6, max_prompt_tokens=4_000,
        )
        self.assertEqual(first, ["C1"])
        ledger.item("C1").status = "produced"
        second = select_requirement_batch(
            ledger, contract, max_requirements=6, max_prompt_tokens=4_000,
        )
        self.assertEqual(second, ["MODEL"])
        ledger.item("MODEL").status = "produced"
        third = select_requirement_batch(
            ledger, contract, max_requirements=6, max_prompt_tokens=4_000,
        )
        self.assertEqual(third, ["SOLUTION", "JSON"])

    def test_acceptance_results_promote_or_reject_produced_requirements(self):
        ledger = build_requirement_progress_ledger(self.requirements)
        ledger.mark_planned(
            ["R1", "R2"], 1, {"R1": ["N1"], "R2": ["N2"]},
        )
        ledger.item("R1").status = "produced"
        ledger.item("R2").status = "produced"
        ledger.apply_acceptance_ledger({"entries": [
            {
                "requirement_id": "R1", "status": "passed",
                "severity": "blocking", "mandatory": True,
            },
            {
                "requirement_id": "R2", "status": "unverified",
                "severity": "blocking", "mandatory": True,
            },
        ]})
        self.assertEqual(ledger.item("R1").status, "passed")
        self.assertEqual(ledger.item("R2").status, "unverified")

    def test_unplanned_or_planning_blocked_requirement_is_not_overwritten(self):
        ledger = build_requirement_progress_ledger(self.requirements)
        ledger.mark_stage_blocked(["R1"], 1, "planner validation failed")
        ledger.apply_acceptance_ledger({"entries": [{
            "requirement_id": "R1", "status": "failed",
            "severity": "blocking", "mandatory": True,
        }, {
            "requirement_id": "R2", "status": "failed",
            "severity": "blocking", "mandatory": True,
        }]})
        self.assertEqual(ledger.item("R1").status, "blocked")
        self.assertEqual(ledger.item("R2").status, "pending")

    def test_stage_spec_separates_analysis_from_single_code_stage(self):
        base = {
            "task_id": "task",
            "task_name": "task",
            "code_policy": {"mode": "required", "max_retries": 1},
            "artifact_contract": {
                "intermediate_artifacts": [
                    "artifacts/code_pipeline.py", "artifacts/result.json",
                ],
            },
            "capability_contract": {
                "required": ["terminal_execution", "evidence_verification"],
            },
            "planning_policy": {},
        }
        analysis = build_stage_task_spec(base, self.requirements, ["R1"], 1)
        code = build_stage_task_spec(base, self.requirements, ["R3", "R4"], 2)
        self.assertEqual(analysis["code_policy"]["mode"], "none")
        self.assertEqual(analysis["planning_contract"]["graph_deliverables"], [])
        self.assertEqual(code["code_policy"]["mode"], "required")
        self.assertEqual(
            set(code["planning_contract"]["graph_deliverables"]),
            {"artifacts/code_pipeline.py", "artifacts/result.json"},
        )
        self.assertEqual(code["capability_contract"]["required"], [])

    def test_merge_preserves_completed_nodes_and_bridges_next_stage(self):
        first = TaskGraph(
            graph_id="g1", goal="stage one",
            nodes=[graph_node("analyse", "R1")],
        )
        merged, mapping = merge_stage_graph(
            None, first, stage=1, global_goal="global",
            available_capabilities=["analysis"],
        )
        merged.get_node("s001_analyse").status = NodeStatus.COMPLETED
        merged.get_node("s001_analyse").result = {"ok": True}
        second = TaskGraph(
            graph_id="g2", goal="stage two",
            nodes=[graph_node("model", "R2")],
        )
        grown, second_mapping = merge_stage_graph(
            merged, second, stage=2, global_goal="global",
            available_capabilities=["analysis"],
        )
        self.assertEqual(mapping, {"R1": ["s001_analyse"]})
        self.assertEqual(second_mapping, {"R2": ["s002_model"]})
        self.assertEqual(
            grown.get_node("s001_analyse").status, NodeStatus.COMPLETED,
        )
        self.assertEqual(
            grown.get_node("s002_model").dependencies, ["s001_analyse"],
        )

    def test_stage_contract_converts_non_code_file_claim_to_logical_result(self):
        stage = TaskGraph(
            graph_id="stage", goal="model",
            nodes=[graph_node(
                "model", "R1", capability="analysis",
                outputs=["artifacts/model_spec.json"],
            )],
        )
        normalized, repairs = enforce_stage_graph_contract(
            stage,
            {"code_policy": {"mode": "none"}, "artifact_contract": {
                "intermediate_artifacts": [],
            }},
            ["analysis"],
        )
        self.assertEqual(normalized.get_node("model").output_artifacts, [])
        self.assertEqual(
            repairs[0]["action"], "convert_agent_artifact_to_logical_output",
        )

    def test_stage_contract_rejects_code_node_in_non_code_stage(self):
        stage = TaskGraph(
            graph_id="stage", goal="invalid code",
            nodes=[graph_node("code", "R1", capability="code")],
        )
        with self.assertRaisesRegex(StageContractError, "非代码阶段"):
            enforce_stage_graph_contract(
                stage,
                {"code_policy": {"mode": "none"}, "artifact_contract": {
                    "intermediate_artifacts": [],
                }},
                ["code"],
            )

    def test_stage_contract_requires_code_to_own_all_physical_artifacts(self):
        stage = TaskGraph(
            graph_id="stage", goal="code",
            nodes=[graph_node(
                "code", "R3", capability="code",
                outputs=["artifacts/code_pipeline.py", "artifacts/result.json"],
            )],
        )
        normalized, repairs = enforce_stage_graph_contract(
            stage,
            {"code_policy": {"mode": "required"}, "artifact_contract": {
                "intermediate_artifacts": [
                    "artifacts/code_pipeline.py", "artifacts/result.json",
                ],
            }},
            ["code"],
        )
        self.assertEqual(len(normalized.nodes), 1)
        self.assertEqual(repairs, [])

    def test_governance_is_appended_once_after_business_stages(self):
        graph = TaskGraph(
            graph_id="g", goal="global",
            nodes=[graph_node(
                "s001_code", "R3", capability="code",
                outputs=["artifacts/code_pipeline.py", "artifacts/result.json"],
            )],
        )
        spec = {
            "capability_contract": {
                "required": ["terminal_execution", "evidence_verification"],
            },
        }
        capabilities = [
            "code", "terminal_execution", "evidence_verification",
            "artifact_validation",
        ]
        governed = append_governance_nodes(graph, spec, capabilities)
        twice = append_governance_nodes(governed, spec, capabilities)
        self.assertEqual(len(governed.nodes), len(twice.nodes))
        self.assertEqual(
            [node.capability for node in governed.nodes][-3:],
            ["terminal_execution", "evidence_verification", "artifact_validation"],
        )

    def test_governance_synthesizes_missing_code_delivery_from_contract(self):
        graph = TaskGraph(
            graph_id="missing-code", goal="global",
            nodes=[graph_node("analyse", "R1")],
        )
        spec = {
            "artifact_contract": {"intermediate_artifacts": [
                "artifacts/code_pipeline.py", "artifacts/result.json",
            ]},
            "requirement_contract": {"requirements": [{
                "requirement_id": "R2", "owner": "planner",
                "expected_outputs": ["artifacts/result.json"],
                "acceptance_criteria": [{"criterion_id": "AC-R2"}],
            }]},
            "capability_contract": {
                "required": ["terminal_execution", "evidence_verification"],
            },
        }
        governed = append_governance_nodes(
            graph, spec,
            ["analysis", "code", "terminal_execution",
             "evidence_verification", "artifact_validation"],
        )

        code_nodes = [node for node in governed.nodes if node.capability == "code"]
        self.assertEqual(len(code_nodes), 1)
        code = code_nodes[0]
        self.assertEqual(code.node_id, "framework_code_delivery")
        self.assertEqual(code.dependencies, ["analyse"])
        self.assertEqual(code.requirement_ids, ["R2"])
        self.assertEqual(code.acceptance_refs, ["AC-R2"])
        self.assertEqual(
            governed.get_node("terminal_execution").dependencies,
            ["framework_code_delivery"],
        )

    def test_governance_coalesces_duplicate_code_nodes_without_losing_requirements(self):
        outputs = ["artifacts/code_pipeline.py", "artifacts/result.json"]
        primary = graph_node(
            "code_primary", "R3", capability="code", outputs=outputs,
        )
        duplicate = graph_node(
            "code_duplicate", "R4", capability="code", outputs=[],
        )
        consumer = graph_node("summarize", "R5")
        consumer.dependencies = ["code_duplicate"]
        graph = TaskGraph(
            graph_id="duplicate-code", goal="global",
            nodes=[primary, duplicate, consumer],
        )
        spec = {"capability_contract": {"required": [
            "terminal_execution", "evidence_verification",
        ]}}
        governed = append_governance_nodes(
            graph, spec,
            ["code", "analysis", "terminal_execution",
             "evidence_verification", "artifact_validation"],
        )

        code_nodes = [node for node in governed.nodes if node.capability == "code"]
        self.assertEqual(len(code_nodes), 1)
        self.assertEqual(set(code_nodes[0].requirement_ids), {"R3", "R4"})
        self.assertEqual(
            governed.get_node("summarize").dependencies,
            [code_nodes[0].node_id, "terminal_execution"],
        )
        self.assertEqual(
            governed.get_node("terminal_execution").dependencies,
            [code_nodes[0].node_id],
        )

    def test_staging_requires_hierarchical_nontrivial_contract(self):
        spec = {"horizon_policy": {
            "requirement_driven_expansion": True,
            "min_requirements_for_staging": 4,
        }}
        self.assertTrue(requirement_staging_enabled(
            spec, self.requirements, hierarchical_selected=True,
        ))
        self.assertFalse(requirement_staging_enabled(
            spec, self.requirements, hierarchical_selected=False,
        ))


if __name__ == "__main__":
    unittest.main()
