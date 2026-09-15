import unittest

from orchestration.planning.hierarchical_planner import (
    _ensure_parent_compounds,
    _merge_patch,
    _normalize_stage_plan,
    resolve_planning_policy,
)
from orchestration.planning.plan_flattener import flatten_plan_ir_to_semantic_graph
from orchestration.planning.plan_format_adapter import adapt_plan_ir, adapt_plan_patch
from orchestration.planning.plan_ir import (
    InputRef,
    PlanIR,
    PlanNode,
    PlanNodeType,
    PlanPatch,
    PlanningContract,
    PlanningPolicy,
)
from orchestration.planning.plan_validator import validate_plan_ir


def output(output_id: str) -> dict:
    return {"output_id": output_id, "output_type": "structured_result", "description": output_id}


class HierarchicalPlanningTests(unittest.TestCase):
    def test_orphan_children_get_real_compound_parent(self):
        plan = PlanIR(
            global_goal="repair hierarchy",
            nodes=[
                PlanNode(
                    node_id="child_a", objective="define variables",
                    node_type=PlanNodeType.PRIMITIVE,
                    parent_node_id="missing_root",
                    primary_capability="math_modeling",
                    expected_output=output("a"),
                    acceptance_criteria=[{"type": "present"}],
                ),
                PlanNode(
                    node_id="child_b", objective="define constraints",
                    node_type=PlanNodeType.PRIMITIVE,
                    parent_node_id="missing_root",
                    primary_capability="math_modeling",
                    dependencies=["child_a"],
                    expected_output=output("b"),
                    acceptance_criteria=[{"type": "present"}],
                ),
            ],
        )
        repaired, parents = _ensure_parent_compounds(plan)
        self.assertEqual(parents, ["missing_root"])
        root = repaired.nodes[-1]
        self.assertEqual(root.node_id, "missing_root")
        self.assertEqual(root.node_type, PlanNodeType.COMPOUND)
        self.assertEqual({node.parent_node_id for node in repaired.nodes[:2]}, {"missing_root"})

    def test_unknown_edge_endpoint_reaches_validator_instead_of_crashing(self):
        plan = PlanIR(
            global_goal="validate malformed dependency",
            nodes=[
                PlanNode(
                    node_id="known",
                    objective="读取输入",
                    node_type=PlanNodeType.PRIMITIVE,
                    primary_capability="document_extraction",
                    expected_output=output("known_output"),
                    acceptance_criteria=[{"type": "present"}],
                ),
            ],
            edges=[("known", "missing_target")],
        )

        normalized, _ = _ensure_parent_compounds(plan)
        self.assertIn(("known", "missing_target"), normalized.edges)
        validation = validate_plan_ir(
            normalized,
            PlanningContract(),
            PlanningPolicy(),
        )
        self.assertFalse(validation.passed)
        self.assertTrue(any(
            "missing_target" in item
            for item in validation.dependency_errors
        ))

    def make_plan(self) -> PlanIR:
        return PlanIR(
            global_goal="完成一个复杂数学建模任务并生成可复核结果",
            deliverables=["artifacts/result.json"],
            nodes=[
                PlanNode(
                    node_id="solution",
                    objective="构建完整解决方案",
                    node_type=PlanNodeType.COMPOUND,
                    primary_capability="reasoning",
                ),
                PlanNode(
                    node_id="model",
                    objective="整理模型输入参数",
                    node_type=PlanNodeType.PRIMITIVE,
                    parent_node_id="solution",
                    primary_capability="reasoning",
                    expected_output=output("model_inputs"),
                    acceptance_criteria=[{"type": "model_inputs_are_normalized"}],
                ),
                PlanNode(
                    node_id="solve",
                    objective="按照上游规格实现求解代码并保存结构化结果",
                    node_type=PlanNodeType.PRIMITIVE,
                    parent_node_id="solution",
                    primary_capability="code",
                    dependencies=["model"],
                    required_inputs=[InputRef(
                        source_type="node_output", name="model_inputs",
                        producer_node_id="model", fields=["variables"],
                    )],
                    expected_output=output("solver_result"),
                    output_artifacts=["artifacts/result.json", "artifacts/code_pipeline.py"],
                    acceptance_criteria=[{"type": "execution_exit_code", "equals": 0}],
                    supporting_capabilities=["reasoning"],
                ),
            ],
            edges=[("model", "solve")],
        )

    def test_validator_separates_graph_and_postprocess_deliverables(self):
        plan = self.make_plan()
        result = validate_plan_ir(
            plan,
            PlanningContract(
                graph_deliverables=["artifacts/result.json"],
                postprocess_deliverables=["final_report.md"],
                framework_artifacts=["run_state.json"],
            ),
            PlanningPolicy(),
            authoritative_goal=plan.global_goal,
        )
        self.assertTrue(result.passed, result)

    def test_multiresponsibility_math_modeling_primitive_is_rejected(self):
        plan = self.make_plan()
        model = plan.nodes[1]
        model.objective = "构建完整数学优化模型"
        model.primary_capability = "math_modeling"
        model.expected_output = type(model.expected_output).model_validate(output("model_spec"))
        model.acceptance_criteria = [{
            "type": "模型包含决策变量、目标函数、多类约束和建模假设",
        }]
        result = validate_plan_ir(
            plan,
            PlanningContract(graph_deliverables=["artifacts/result.json"]),
            PlanningPolicy(),
            authoritative_goal=plan.global_goal,
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.problem_node_ids, ["model"])
        self.assertIn("model", result.oversized_nodes)
        self.assertTrue(any(
            "从 primitive 转为 compound" in item
            for item in result.revision_instructions
        ))

    def test_cross_stage_code_primitive_is_rejected(self):
        plan = self.make_plan()
        solve = plan.nodes[2]
        solve.objective = "设计求解策略、实现代码并验证业务结果"
        solve.acceptance_criteria = [{
            "type": "完成算法设计、代码实现和业务结果验证",
        }]
        result = validate_plan_ir(
            plan,
            PlanningContract(graph_deliverables=["artifacts/result.json"]),
            PlanningPolicy(),
            authoritative_goal=plan.global_goal,
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.problem_node_ids, ["solve"])
        self.assertIn("solve", result.oversized_nodes)
        self.assertTrue(any(
            "唯一 code primitive" in item
            for item in result.revision_instructions
        ))

    def test_single_code_primitive_implementing_existing_spec_is_valid(self):
        plan = self.make_plan()
        solve = plan.nodes[2]
        solve.objective = "按照上游算法规格实现可执行代码包并产生结构化结果"
        solve.acceptance_criteria = [
            {"type": "execution_exit_code", "equals": 0},
            {"type": "implements_declared_input_output_contract"},
        ]
        result = validate_plan_ir(
            plan,
            PlanningContract(graph_deliverables=["artifacts/result.json"]),
            PlanningPolicy(),
            authoritative_goal=plan.global_goal,
        )
        self.assertTrue(result.passed, result)
        self.assertEqual(result.problem_node_ids, [])

    def test_problem_node_ids_target_only_coarse_primitives(self):
        plan = self.make_plan()
        model, solve = plan.nodes[1], plan.nodes[2]
        model.objective = "设计数学模型"
        model.primary_capability = "math_modeling"
        model.acceptance_criteria = [{
            "type": "定义决策变量、目标函数及全部物理约束",
        }]
        solve.objective = "设计求解算法并实现代码和验证业务结果"
        solve.acceptance_criteria = [{
            "type": "算法设计、代码实现和业务结果验证全部完成",
        }]
        result = validate_plan_ir(
            plan,
            PlanningContract(graph_deliverables=["artifacts/result.json"]),
            PlanningPolicy(),
            authoritative_goal=plan.global_goal,
        )
        self.assertEqual(result.problem_node_ids, ["model", "solve"])
        self.assertNotIn("solution", result.problem_node_ids)

    def test_child_cannot_use_compound_parent_as_data_dependency(self):
        plan = self.make_plan()
        plan.nodes[1].dependencies = ["solution"]
        result = validate_plan_ir(
            plan,
            PlanningContract(graph_deliverables=["artifacts/result.json"]),
            PlanningPolicy(),
            authoritative_goal=plan.global_goal,
        )
        self.assertFalse(result.passed)
        self.assertIn("model", result.problem_node_ids)
        self.assertTrue(any("parent_node_id" in item for item in result.dependency_errors))

    def test_child_cannot_use_indirect_compound_ancestor_as_dependency(self):
        plan = self.make_plan()
        nested = PlanNode(
            node_id="nested",
            objective="嵌套分析阶段",
            node_type=PlanNodeType.COMPOUND,
            parent_node_id="solution",
            decomposition_depth=1,
            primary_capability="analysis",
        )
        plan.nodes.insert(1, nested)
        model = next(node for node in plan.nodes if node.node_id == "model")
        model.parent_node_id = "nested"
        model.decomposition_depth = 2
        model.dependencies = ["solution"]

        result = validate_plan_ir(
            plan,
            PlanningContract(graph_deliverables=["artifacts/result.json"]),
            PlanningPolicy(),
            authoritative_goal=plan.global_goal,
        )

        self.assertFalse(result.passed)
        self.assertIn("model", result.problem_node_ids)
        self.assertTrue(any(
            "层级祖先 solution" in item
            for item in result.dependency_errors
        ))

    def test_primitive_cannot_be_a_hierarchy_parent(self):
        plan = self.make_plan()
        plan.nodes[0].node_type = PlanNodeType.PRIMITIVE
        plan.nodes[0].expected_output = type(
            plan.nodes[1].expected_output
        ).model_validate(output("coarse_parent_output"))
        plan.nodes[0].acceptance_criteria = [{"type": "coarse_complete"}]

        result = validate_plan_ir(
            plan,
            PlanningContract(graph_deliverables=["artifacts/result.json"]),
            PlanningPolicy(),
            authoritative_goal=plan.global_goal,
        )

        self.assertFalse(result.passed)
        self.assertIn("solution", result.problem_node_ids)
        self.assertTrue(any(
            "primitive 节点 solution 不能作为层级父节点" in item
            for item in result.invalid_decompositions
        ))

    def test_plan_rejects_a_second_code_primitive(self):
        plan = self.make_plan()
        plan.nodes.append(PlanNode(
            node_id="second_code",
            objective="实现既定规格的辅助代码",
            node_type=PlanNodeType.PRIMITIVE,
            primary_capability="code",
            expected_output=output("second_code_result"),
            output_artifacts=[],
            acceptance_criteria=[{"type": "implemented"}],
        ))
        result = validate_plan_ir(
            plan,
            PlanningContract(graph_deliverables=["artifacts/result.json"]),
            PlanningPolicy(),
            authoritative_goal=plan.global_goal,
        )
        self.assertFalse(result.passed)
        self.assertIn("second_code", result.problem_node_ids)
        self.assertIn("solve", result.problem_node_ids)

    def test_compound_capability_is_not_required_at_runtime(self):
        plan = self.make_plan()
        plan.nodes[0].primary_capability = "planning"
        result = validate_plan_ir(
            plan,
            PlanningContract(
                graph_deliverables=[
                    "artifacts/result.json",
                    "artifacts/code_pipeline.py",
                ],
            ),
            PlanningPolicy(),
            authoritative_goal=plan.global_goal,
            available_capabilities={"reasoning", "code"},
        )
        self.assertTrue(result.passed, result.model_dump())

    def test_missing_graph_deliverable_and_input_producer_are_rejected(self):
        plan = self.make_plan()
        plan.nodes[2].required_inputs[0].producer_node_id = "missing"
        result = validate_plan_ir(
            plan,
            PlanningContract(graph_deliverables=["artifacts/unknown.json"]),
            PlanningPolicy(),
            authoritative_goal=plan.global_goal,
        )
        self.assertFalse(result.passed)
        self.assertIn("artifacts/unknown.json", result.missing_deliverables)
        self.assertTrue(any("producer" in item for item in result.dependency_errors))

    def test_flatten_preserves_parallelism_metadata_and_input_refs(self):
        flattened = flatten_plan_ir_to_semantic_graph(self.make_plan())
        self.assertEqual(flattened.flattened_edges, [("model", "solve")])
        self.assertEqual([node.node_id for node in flattened.semantic_graph.nodes], ["model", "solve"])
        solve = flattened.semantic_graph.nodes[1]
        self.assertEqual(solve.capability, "code")
        self.assertEqual(solve.metadata["supporting_capabilities"], ["reasoning"])
        self.assertEqual(solve.metadata["hierarchy_path"], ["solution", "solve"])
        self.assertEqual(solve.required_inputs, {"model": ["/variables"]})
        self.assertEqual(solve.output_artifacts, ["artifacts/result.json", "artifacts/code_pipeline.py"])

    def test_flatten_ignores_explicit_parent_containment_edges(self):
        plan = self.make_plan()
        plan.edges.extend([
            ("solution", "model"),
            ("solution", "solve"),
            ("model", "solution"),
            ("solve", "solution"),
        ])
        flattened = flatten_plan_ir_to_semantic_graph(plan)
        self.assertEqual(flattened.flattened_edges, [("model", "solve")])

    def test_adapter_removes_untyped_containment_edges_in_both_directions(self):
        payload = self.make_plan().model_dump(mode="json")
        payload["edges"].extend([
            ["solution", "model"],
            ["solve", "solution"],
        ])

        adapted, report, _ = adapt_plan_ir(payload, {})

        self.assertEqual(adapted.edges, [("model", "solve")])
        removals = [
            repair for repair in report.repairs
            if repair["action"] == "containment_edge_removed"
        ]
        self.assertEqual(len(removals), 2)

    def test_validator_rejects_descendant_to_ancestor_data_edge(self):
        plan = self.make_plan()
        plan.edges.append(("model", "solution"))

        result = validate_plan_ir(
            plan,
            PlanningContract(
                graph_deliverables=[
                    "artifacts/result.json",
                    "artifacts/code_pipeline.py",
                ],
            ),
            PlanningPolicy(),
            authoritative_goal=plan.global_goal,
        )

        self.assertFalse(result.passed)
        self.assertIn("solution", result.problem_node_ids)
        self.assertTrue(any(
            "model->solution" in item
            for item in result.dependency_errors
        ))

    def test_adapter_removes_compound_dependency_on_its_descendant(self):
        payload = self.make_plan().model_dump(mode="json")
        compound = next(
            node for node in payload["nodes"] if node["node_id"] == "solution"
        )
        compound["dependencies"] = ["model"]

        adapted, report, _ = adapt_plan_ir(payload, {})

        adapted_compound = next(
            node for node in adapted.nodes if node.node_id == "solution"
        )
        self.assertEqual(adapted_compound.dependencies, [])
        self.assertTrue(any(
            repair["action"] == "containment_dependency_removed"
            for repair in report.repairs
        ))

    def test_compound_required_input_is_mapped_to_primitive_exit(self):
        plan = PlanIR(
            global_goal="flatten compound data contract",
            nodes=[
                PlanNode(
                    node_id="phase",
                    objective="完成分析阶段",
                    node_type=PlanNodeType.COMPOUND,
                    primary_capability="analysis",
                ),
                PlanNode(
                    node_id="phase_entry",
                    objective="读取数据",
                    node_type=PlanNodeType.PRIMITIVE,
                    parent_node_id="phase",
                    primary_capability="analysis",
                    expected_output=output("raw"),
                    acceptance_criteria=[{"type": "present"}],
                ),
                PlanNode(
                    node_id="phase_exit",
                    objective="汇总阶段结果",
                    node_type=PlanNodeType.PRIMITIVE,
                    parent_node_id="phase",
                    primary_capability="analysis",
                    dependencies=["phase_entry"],
                    expected_output=output("summary"),
                    acceptance_criteria=[{"type": "present"}],
                ),
                PlanNode(
                    node_id="consumer",
                    objective="消费阶段结果",
                    node_type=PlanNodeType.PRIMITIVE,
                    primary_capability="reasoning",
                    dependencies=["phase"],
                    required_inputs=[InputRef(
                        source_type="node_output",
                        name="phase_summary",
                        producer_node_id="phase",
                        fields=["summary"],
                    )],
                    expected_output=output("result"),
                    acceptance_criteria=[{"type": "present"}],
                ),
            ],
            edges=[
                ("phase_entry", "phase_exit"),
                ("phase", "consumer"),
            ],
        )

        flattened = flatten_plan_ir_to_semantic_graph(plan)
        consumer = next(
            node for node in flattened.semantic_graph.nodes
            if node.node_id == "consumer"
        )
        self.assertEqual(consumer.dependencies, ["phase_exit"])
        self.assertEqual(
            consumer.required_inputs,
            {"phase_exit": ["/summary"]},
        )

    def test_local_refinement_preserves_external_boundary(self):
        plan = self.make_plan()
        plan.nodes.append(PlanNode(
            node_id="report_input", objective="整理结果", node_type=PlanNodeType.PRIMITIVE,
            primary_capability="analysis", dependencies=["solve"],
            expected_output=output("report_input"), acceptance_criteria=[{"type": "present"}],
        ))
        patch = PlanPatch(
            target_node_id="solve",
            replacement_nodes=[PlanNode(
                node_id="solve", objective="执行求解", node_type=PlanNodeType.PRIMITIVE,
                parent_node_id="solution", dependencies=["model"], primary_capability="code",
                expected_output=output("solver_result"), output_artifacts=["artifacts/result.json"],
                acceptance_criteria=[{"type": "exit_code"}],
            )],
        )
        merged = _merge_patch(plan, patch)
        self.assertIn(("model", "solve"), set(merged.edges))
        self.assertIn(("solve", "report_input"), set(merged.edges))

    def test_refinement_attaches_omitted_children_to_compound_target(self):
        plan = PlanIR(
            global_goal="refine coarse node",
            nodes=[PlanNode(
                node_id="coarse", objective="coarse work",
                node_type=PlanNodeType.PRIMITIVE,
                primary_capability="analysis",
                expected_output=output("coarse_result"),
                acceptance_criteria=[{"type": "semantic_check"}],
            )],
        )
        patch = PlanPatch(
            target_node_id="coarse",
            replacement_nodes=[
                PlanNode(
                    node_id="coarse", objective="group refined work",
                    node_type=PlanNodeType.COMPOUND,
                    primary_capability="analysis",
                ),
                PlanNode(
                    node_id="part_a", objective="produce part a",
                    node_type=PlanNodeType.PRIMITIVE,
                    primary_capability="analysis",
                    expected_output=output("part_a_result"),
                    acceptance_criteria=[{"type": "semantic_check"}],
                ),
                PlanNode(
                    node_id="part_b", objective="produce part b",
                    node_type=PlanNodeType.PRIMITIVE,
                    dependencies=["part_a"],
                    primary_capability="analysis",
                    expected_output=output("part_b_result"),
                    acceptance_criteria=[{"type": "semantic_check"}],
                ),
            ],
            replacement_edges=[("part_a", "part_b")],
        )
        merged = _merge_patch(plan, patch)
        children = [
            node for node in merged.nodes
            if node.node_id in {"part_a", "part_b"}
        ]
        self.assertTrue(all(
            node.parent_node_id == "coarse" for node in children
        ))

    def test_refinement_repairs_only_missing_local_output_acceptance(self):
        plan = PlanIR(
            global_goal="refine coarse node",
            nodes=[PlanNode(
                node_id="coarse", objective="coarse work",
                node_type=PlanNodeType.PRIMITIVE,
                primary_capability="analysis",
                expected_output=output("coarse_result"),
                acceptance_criteria=[{"type": "semantic_check"}],
            )],
        )
        patch = PlanPatch(
            target_node_id="coarse",
            replacement_nodes=[
                PlanNode(
                    node_id="coarse", objective="group refined work",
                    node_type=PlanNodeType.COMPOUND,
                    primary_capability="analysis",
                ),
                PlanNode(
                    node_id="part_a", objective="produce part a",
                    node_type=PlanNodeType.PRIMITIVE,
                    primary_capability="analysis",
                    expected_output=output("part_a_result"),
                ),
                PlanNode(
                    node_id="part_b", objective="produce part b",
                    node_type=PlanNodeType.PRIMITIVE,
                    primary_capability="analysis",
                    expected_output=output("part_b_result"),
                    acceptance_criteria=[{"type": "domain_specific_check"}],
                ),
            ],
        )

        merged = _merge_patch(plan, patch)
        by_id = {node.node_id: node for node in merged.nodes}

        self.assertEqual(
            by_id["part_a"].acceptance_criteria[0]["type"],
            "logical_output_present",
        )
        self.assertTrue(
            by_id["part_a"].metadata["framework_local_acceptance_repaired"],
        )
        self.assertEqual(
            by_id["part_b"].acceptance_criteria,
            [{"type": "domain_specific_check"}],
        )

    def test_refinement_removes_dependencies_on_deleted_old_children(self):
        plan = self.make_plan()
        patch = PlanPatch(
            target_node_id="solution",
            replacement_nodes=[
                PlanNode(
                    node_id="solution",
                    objective="重新组织解决方案",
                    node_type=PlanNodeType.COMPOUND,
                    dependencies=["model"],
                    primary_capability="reasoning",
                ),
                PlanNode(
                    node_id="new_model",
                    objective="整理新模型输入",
                    node_type=PlanNodeType.PRIMITIVE,
                    parent_node_id="solution",
                    primary_capability="reasoning",
                    expected_output=output("new_model_output"),
                    acceptance_criteria=[{"type": "present"}],
                ),
                PlanNode(
                    node_id="new_code",
                    objective="实现既定规格",
                    node_type=PlanNodeType.PRIMITIVE,
                    parent_node_id="solution",
                    dependencies=["new_model"],
                    primary_capability="code",
                    expected_output=output("new_code_output"),
                    output_artifacts=[
                        "artifacts/result.json",
                        "artifacts/code_pipeline.py",
                    ],
                    acceptance_criteria=[{"type": "exit_code"}],
                ),
            ],
            replacement_edges=[("new_model", "new_code")],
        )

        merged = _merge_patch(plan, patch)
        replacement = next(
            node for node in merged.nodes if node.node_id == "solution"
        )
        self.assertNotIn("model", replacement.dependencies)
        self.assertNotIn("solve", {
            dependency
            for node in merged.nodes
            for dependency in node.dependencies
        })

    def test_auto_policy_selects_hierarchy_only_for_complex_contract(self):
        simple, score_simple = resolve_planning_policy({"task_type": "general", "input": {"files": []}})
        self.assertEqual(simple.mode, "auto")
        self.assertLess(score_simple, 4)
        complex_spec = {
            "task_type": "math_modeling",
            "code_policy": {"mode": "lightweight"},
            "input": {"files": ["a.txt", "b.txt"]},
            "artifact_contract": {"intermediate_artifacts": ["a.json", "b.json"]},
            "requirements": {"must_review_artifacts": True},
        }
        _, score_complex = resolve_planning_policy(complex_spec, {
            "recommended_capabilities": ["reasoning", "code", "analysis"],
            "constraints": ["a", "b", "c", "d", "e"],
        })
        self.assertGreaterEqual(score_complex, 4)

    def test_stage_normalization_removes_external_ids_and_restores_acceptance(self):
        plan = PlanIR(
            global_goal="stage two",
            nodes=[
                PlanNode(
                    node_id="wrapper", objective="group",
                    node_type=PlanNodeType.COMPOUND,
                    primary_capability="analysis",
                    requirement_ids=["REQ-1"],
                    acceptance_refs=["AC-1"],
                    acceptance_criteria=[{"type": "wrong_owner"}],
                ),
                PlanNode(
                    node_id="check", objective="check constraint",
                    node_type=PlanNodeType.PRIMITIVE,
                    parent_node_id="wrapper",
                    dependencies=["s001_previous"],
                    primary_capability="analysis",
                    required_inputs=[InputRef(
                        source_type="node_output", name="previous",
                        producer_node_id="s001_previous",
                    )],
                    expected_output=output("checked"),
                    requirement_ids=["REQ-1"],
                    acceptance_refs=["AC-1"],
                ),
            ],
            edges=[("s001_previous", "check")],
        )
        contract = PlanningContract(requirement_contract={
            "contract_id": "req", "global_goal": "goal",
            "requirements": [{
                "requirement_id": "REQ-1",
                "acceptance_criteria": [{
                    "criterion_id": "AC-1", "method": "evidence_supported",
                    "target": "node:check", "condition": "supported",
                    "params": {"keywords": ["constraint"]},
                    "severity": "blocking",
                }, {
                    "criterion_id": "AC-2", "method": "evidence_supported",
                    "target": "node:check", "condition": "complete",
                    "params": {"keywords": ["complete"]},
                    "severity": "blocking",
                }],
            }],
        })
        normalized = _normalize_stage_plan(plan, {
            "completed_stage_context": [{"node_id": "s001_previous"}],
        }, contract)
        self.assertEqual([node.node_id for node in normalized.nodes], ["check"])
        check = normalized.nodes[0]
        self.assertEqual(check.dependencies, [])
        self.assertEqual(check.required_inputs, [])
        self.assertEqual(check.acceptance_refs, ["AC-1", "AC-2"])
        self.assertEqual(
            [item["criterion_id"] for item in check.acceptance_criteria],
            ["AC-1", "AC-2"],
        )
        self.assertEqual(check.metadata["framework_external_stage_inputs"], [
            "s001_previous",
        ])

    def test_stage_normalization_removes_unknown_alias_and_compound_ancestor(self):
        plan = PlanIR(
            global_goal="stage boundary",
            nodes=[
                PlanNode(
                    node_id="root", objective="root grouping",
                    node_type=PlanNodeType.COMPOUND,
                    primary_capability="analysis",
                ),
                PlanNode(
                    node_id="group", objective="nested grouping",
                    node_type=PlanNodeType.COMPOUND,
                    parent_node_id="root", primary_capability="analysis",
                ),
                PlanNode(
                    node_id="leaf", objective="consume prior stage context",
                    node_type=PlanNodeType.PRIMITIVE,
                    parent_node_id="group",
                    dependencies=["root", "input_normalization"],
                    primary_capability="analysis",
                    required_inputs=[
                        InputRef(
                            source_type="node_output", name="root result",
                            producer_node_id="root",
                        ),
                        InputRef(
                            source_type="node_output", name="normalized input",
                            producer_node_id="input_normalization",
                        ),
                    ],
                    expected_output=output("leaf_result"),
                    acceptance_criteria=[{"type": "semantic_check"}],
                ),
            ],
            edges=[
                ("root", "leaf"),
                ("input_normalization", "leaf"),
            ],
        )
        normalized = _normalize_stage_plan(plan, {
            "completed_stage_context": [{"node_id": "s001_previous"}],
        }, PlanningContract())
        leaf = normalized.nodes[0]
        self.assertEqual(leaf.node_id, "leaf")
        self.assertEqual(leaf.dependencies, [])
        self.assertEqual(leaf.required_inputs, [])
        self.assertEqual(leaf.metadata["framework_external_stage_inputs"], [
            "input_normalization",
        ])
        self.assertEqual(normalized.edges, [])

    def test_stage_normalization_removes_containment_edges_in_both_directions(self):
        plan = PlanIR(
            global_goal="refine a coarse model",
            nodes=[
                PlanNode(
                    node_id="model", objective="group model responsibilities",
                    node_type=PlanNodeType.COMPOUND,
                    dependencies=["variables", "constraints"],
                    primary_capability="math_modeling",
                ),
                PlanNode(
                    node_id="variables", objective="define variables",
                    node_type=PlanNodeType.PRIMITIVE,
                    parent_node_id="model", dependencies=["model"],
                    primary_capability="math_modeling",
                    expected_output=output("variables"),
                    acceptance_criteria=[{"type": "present"}],
                ),
                PlanNode(
                    node_id="constraints", objective="define constraints",
                    node_type=PlanNodeType.PRIMITIVE,
                    parent_node_id="model", dependencies=["variables"],
                    primary_capability="math_modeling",
                    expected_output=output("constraints"),
                    acceptance_criteria=[{"type": "present"}],
                ),
            ],
            edges=[
                ("variables", "model"),
                ("constraints", "model"),
                ("model", "variables"),
                ("variables", "constraints"),
            ],
        )

        normalized = _normalize_stage_plan(plan, {}, PlanningContract())

        by_id = {node.node_id: node for node in normalized.nodes}

        self.assertEqual(by_id["model"].dependencies, [])
        self.assertEqual(by_id["variables"].dependencies, [])
        self.assertEqual(
            by_id["constraints"].dependencies, ["variables"],
        )
        self.assertEqual(normalized.edges, [("variables", "constraints")])

    def test_stage_normalization_assigns_duplicate_code_requirement_once(self):
        plan = PlanIR(
            global_goal="implement frozen specification",
            nodes=[
                PlanNode(
                    node_id="support", objective="prepare implementation detail",
                    node_type=PlanNodeType.PRIMITIVE,
                    primary_capability="analysis",
                    expected_output=output("implementation_detail"),
                    requirement_ids=["REQ-CODE"],
                    acceptance_criteria=[{"type": "semantic_check"}],
                ),
                PlanNode(
                    node_id="writer", objective="write executable package",
                    node_type=PlanNodeType.PRIMITIVE,
                    dependencies=["support"], primary_capability="code",
                    expected_output=output("executable_package"),
                    output_artifacts=["artifacts/code_pipeline.py"],
                    requirement_ids=["REQ-CODE"],
                    acceptance_criteria=[{"type": "artifact_exists"}],
                ),
            ],
            edges=[("support", "writer")],
        )
        contract = PlanningContract(requirement_contract={
            "requirements": [{
                "requirement_id": "REQ-CODE",
                "acceptance_criteria": [{
                    "criterion_id": "AC-CODE",
                    "method": "artifact_exists",
                    "target": "artifacts/code_pipeline.py",
                    "condition": "code exists",
                    "severity": "blocking",
                }],
            }],
        })
        normalized = _normalize_stage_plan(plan, {}, contract)
        support = next(n for n in normalized.nodes if n.node_id == "support")
        writer = next(n for n in normalized.nodes if n.node_id == "writer")
        self.assertEqual(support.requirement_ids, [])
        self.assertEqual(writer.requirement_ids, ["REQ-CODE"])
        self.assertEqual(writer.acceptance_refs, ["AC-CODE"])

    def test_stage_normalization_removes_obsolete_empty_compound(self):
        plan = PlanIR(
            global_goal="remove obsolete refinement shell",
            nodes=[
                PlanNode(
                    node_id="upstream", objective="produce specification",
                    node_type=PlanNodeType.PRIMITIVE,
                    primary_capability="analysis",
                    expected_output=output("specification"),
                    acceptance_criteria=[{"type": "semantic_check"}],
                ),
                PlanNode(
                    node_id="obsolete", objective="old grouping shell",
                    node_type=PlanNodeType.COMPOUND,
                    dependencies=["upstream"],
                    primary_capability="analysis",
                ),
                PlanNode(
                    node_id="consumer", objective="consume specification",
                    node_type=PlanNodeType.PRIMITIVE,
                    dependencies=["obsolete"],
                    primary_capability="analysis",
                    expected_output=output("consumer_result"),
                    acceptance_criteria=[{"type": "semantic_check"}],
                ),
            ],
            edges=[("upstream", "obsolete"), ("obsolete", "consumer")],
        )
        normalized = _normalize_stage_plan(plan, {}, PlanningContract())
        self.assertEqual(
            [node.node_id for node in normalized.nodes],
            ["upstream", "consumer"],
        )
        consumer = normalized.nodes[1]
        self.assertEqual(consumer.dependencies, ["upstream"])
        self.assertEqual(normalized.edges, [("upstream", "consumer")])

    def test_format_adapter_repairs_realistic_model_shapes(self):
        raw = {
            "global_goal": "完成复杂数学建模任务并生成可复核结果",
            "deliverables": [{
                "artifact_name": "artifacts/result.json",
                "description": "模型生成的结构化业务结果",
            }],
            "nodes": [
                {
                    "node_id": "root_task", "objective": "完成建模任务",
                    "node_type": "compound", "primary_capability": "reasoning",
                    "required_inputs": ["outputs/runs/x/inputs/problem.pdf"],
                },
                {
                    "node_id": "model", "objective": "建立模型",
                    "node_type": "primitive", "primary_capability": "reasoning",
                    "required_inputs": ["normalized_input"],
                    "expected_output": "model_spec",
                    "acceptance_criteria": ["模型定义完整"],
                },
                {
                    "node_id": "solve", "objective": "运行求解器",
                    "node_type": "primitive", "primary_capability": "code",
                    "required_inputs": ["model_spec", "artifacts/code_pipeline.py"],
                    "expected_output": {
                        "output_id": "solver_result", "output_type": "structured_result",
                    },
                    "output_artifacts": ["artifacts/result.json"],
                    "acceptance_criteria": [{"type": "exit_code"}],
                },
            ],
            "edges": [
                {"from_node": "root_task", "to_node": "model", "relationship": "subtask_of"},
                {"from_node": "root_task", "to_node": "solve", "relationship": "subtask_of"},
                {"from_node": "model", "to_node": "solve", "relationship": "provides_input_to"},
            ],
        }
        plan, report, payload = adapt_plan_ir(raw, {
            "input": {"files": ["outputs/runs/x/inputs/problem.pdf"]},
        })
        self.assertGreater(report.repair_count, 0)
        self.assertEqual(plan.nodes[1].parent_node_id, "root_task")
        self.assertEqual(plan.nodes[2].parent_node_id, "root_task")
        self.assertEqual(plan.nodes[2].dependencies, ["model"])
        self.assertEqual(plan.nodes[2].required_inputs[0].source_type, "node_output")
        self.assertEqual(plan.nodes[2].required_inputs[0].producer_node_id, "model")
        self.assertEqual(plan.nodes[2].required_inputs[1].source_type, "artifact")
        self.assertEqual(plan.deliverables, ["artifacts/result.json"])
        self.assertTrue(any(
            repair["action"] == "deliverable_from_object"
            for repair in report.repairs
        ))
        self.assertEqual(payload["edges"], [("model", "solve")])

    def test_format_adapter_repairs_list_shaped_logical_output(self):
        raw = {
            "target_node_id": "coarse",
            "replacement_nodes": [{
                "node_id": "coarse",
                "objective": "produce one independently verifiable result",
                "node_type": "primitive",
                "primary_capability": "analysis",
                "expected_output": ["models/model_spec.json"],
                "acceptance_criteria": [{"type": "semantic_check"}],
            }],
            "replacement_edges": [],
        }
        patch, report, _ = adapt_plan_patch(raw, {})
        self.assertEqual(
            patch.replacement_nodes[0].expected_output.output_id,
            "model_spec",
        )
        self.assertTrue(any(
            item["action"] == "logical_output_from_list"
            for item in report.repairs
        ))


if __name__ == "__main__":
    unittest.main()
