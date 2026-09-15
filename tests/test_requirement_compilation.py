import unittest

from orchestration.core.schemas import (
    RequirementAcceptance,
    RequirementItem,
    RequirementSet,
    RequirementSourceCandidate,
    RequirementSourceRef,
)
from orchestration.planning.plan_ir import (
    PlanIR,
    PlanNode,
    PlanNodeType,
    PlanningContract,
    PlanningPolicy,
)
from orchestration.planning.plan_validator import validate_plan_ir
from orchestration.planning.hierarchical_planner import _merge_patch
from orchestration.planning.plan_ir import PlanPatch
from orchestration.planning.requirement_compiler import (
    compile_requirement_set,
    extract_requirement_source_candidates,
    preserve_requirement_history,
    validate_requirement_set,
)


class RequirementCompilationTests(unittest.TestCase):
    def test_missing_parent_acceptance_routes_to_domain_validation_log(self):
        task_spec = {
            "task_id": "run",
            "task_name": "domain task",
            "artifact_contract": {
                "intermediate_artifacts": [
                    "artifacts/result.json",
                    "artifacts/validation_log.json",
                ],
                "final_artifacts": ["final_report.md"],
                "framework_artifacts": [],
            },
            "domain_result_contract": {
                "semantic_validation_target": "artifacts/validation_log.json",
                "semantic_validation_fields": [
                    "/passed", "/failures", "/recomputed",
                ],
                "allowed_acceptance_targets": {
                    "artifacts/result.json": ["/summary"],
                    "artifacts/validation_log.json": [
                        "/passed", "/failures", "/recomputed",
                    ],
                },
            },
            "input": {"files": ["inputs/task.md"]},
        }

        requirements = compile_requirement_set(task_spec, {
            "summary": "完成领域分析",
            "requirements": [{
                "requirement_id": "REQ-PARENT",
                "requirement_type": "goal",
                "statement": "完成由子需求定义的复合领域分析",
                "mandatory": True,
                "owner": "planner",
                "acceptance_criteria": [],
            }],
        })

        criterion = requirements.requirements[0].acceptance_criteria[0]
        self.assertEqual(criterion.method, "required_fields")
        self.assertEqual(criterion.target, "artifacts/validation_log.json")
        self.assertEqual(
            criterion.params["fields"],
            ["/passed", "/failures", "/recomputed"],
        )

    def test_planner_acceptance_uses_declared_json_and_explicit_comparator(self):
        task_spec = {
            "task_id": "run",
            "task_name": "generic optimization",
            "artifact_contract": {
                "intermediate_artifacts": [
                    "artifacts/result.json", "artifacts/code_pipeline.py",
                ],
                "final_artifacts": ["final_report.md"],
                "framework_artifacts": ["review/validation_report.md"],
            },
            "input": {"files": ["inputs/spec.pdf"]},
        }
        reqs = compile_requirement_set(task_spec, {
            "summary": "完成优化",
            "requirements": [{
                "requirement_id": "REQ-CHECK",
                "requirement_type": "constraint",
                "statement": "安全间隙不小于 3",
                "mandatory": True,
                "owner": "planner",
                "expected_outputs": ["validation_report.md"],
                "acceptance_criteria": [{
                    "criterion_id": "AC-CHECK",
                    "method": "numeric_compare",
                    "target": "validation_report.md#/gap_margin",
                    "condition": "值>=3",
                    "params": {"value": 3},
                    "severity": "blocking",
                }],
            }],
        })

        item = reqs.requirements[0]
        criterion = item.acceptance_criteria[0]
        self.assertEqual(item.expected_outputs, ["artifacts/result.json"])
        self.assertEqual(
            criterion.target, "artifacts/result.json#/gap_margin",
        )
        self.assertEqual(criterion.params["operator"], "ge")

    def test_comparator_pointer_and_numeric_string_become_executable(self):
        task_spec = {
            "task_id": "run",
            "task_name": "generic optimization",
            "artifact_contract": {
                "intermediate_artifacts": ["artifacts/result.json"],
                "final_artifacts": ["final_report.md"],
                "framework_artifacts": [],
            },
            "input": {"files": ["inputs/spec.pdf"]},
        }
        reqs = compile_requirement_set(task_spec, {
            "summary": "完成优化",
            "requirements": [{
                "requirement_id": "REQ-RATE",
                "statement": "空间利用率不小于零",
                "mandatory": True,
                "owner": "planner",
                "acceptance_criteria": [{
                    "criterion_id": "AC-RATE",
                    "method": "numeric_compare",
                    "target": "artifacts/result.json",
                    "condition": "空间利用率>=0",
                    "params": {
                        "left": "/problem1/space_utilization_rate",
                        "operator": "ge",
                        "right": "0",
                    },
                }],
            }],
        })
        params = reqs.requirements[0].acceptance_criteria[0].params
        self.assertEqual(
            params["left"],
            "artifacts/result.json#/problem1/space_utilization_rate",
        )
        self.assertEqual(params["value"], 0)
        self.assertNotIn("right", params)

    def test_optional_outer_workflow_quality_item_is_not_forced_to_split(self):
        reqs = RequirementSet(
            contract_id="r",
            global_goal="完成任务",
            requirements=[RequirementItem(
                requirement_id="REQ-OPTIONAL-REPORT",
                statement="报告需包含模型通用性验证思路说明",
                mandatory=False,
                owner="outer_workflow",
                acceptance_criteria=[RequirementAcceptance(
                    criterion_id="AC-OPTIONAL",
                    method="evidence_supported",
                    condition="报告包含相关说明",
                    severity="warning",
                )],
            )],
        )
        validation = validate_requirement_set(reqs)
        self.assertTrue(validation.passed)
        self.assertEqual(validation.oversized_requirement_ids, [])

    def test_source_candidates_expose_uncovered_long_document_obligations(self):
        task_spec = {
            "task_id": "run",
            "task_name": "generic",
            "file_previews": [{
                "path": "inputs/spec.pdf",
                "text_preview": (
                    "背景介绍不构成任务。必须生成结构化结果。"
                    "分别比较方案甲与方案乙，并计算成本。"
                ),
            }],
        }
        candidates = extract_requirement_source_candidates(task_spec)
        self.assertGreaterEqual(len(candidates), 2)
        reqs = compile_requirement_set(task_spec, {
            "summary": "完成任务",
            "requirements": [{
                "requirement_id": "REQ-ONE",
                "statement": "生成结构化结果",
                "source_refs": [{
                    "artifact": "spec.pdf",
                    "locator": candidates[0].candidate_id,
                }],
                "acceptance_criteria": [{
                    "criterion_id": "AC-ONE",
                    "method": "schema_check",
                    "condition": "结果存在",
                }],
            }],
        })
        validation = validate_requirement_set(reqs)
        self.assertFalse(validation.passed)
        self.assertIn(
            candidates[1].candidate_id,
            validation.uncovered_source_candidate_ids,
        )

    def test_all_source_candidates_can_be_closed_by_trace_refs(self):
        candidate = RequirementSourceCandidate(
            candidate_id="SRC-01-001",
            artifact="spec.pdf",
            locator="preview:chars:0-10",
            text="必须生成结果",
        )
        reqs = RequirementSet(
            contract_id="r",
            global_goal="完成任务",
            source_candidates=[candidate],
            requirements=[RequirementItem(
                requirement_id="REQ-ONE",
                statement="生成结果",
                source_refs=[RequirementSourceRef(
                    artifact="spec.pdf", locator="SRC-01-001",
                )],
                acceptance_criteria=[RequirementAcceptance(
                    criterion_id="AC-ONE", method="check",
                    condition="存在",
                )],
            )],
        )
        self.assertTrue(validate_requirement_set(reqs).passed)

    def test_exact_artifact_and_span_also_closes_source_candidate(self):
        candidate = RequirementSourceCandidate(
            candidate_id="SRC-01-001",
            artifact="spec.pdf",
            locator="preview:chars:12-34",
            text="必须生成结果",
        )
        reqs = RequirementSet(
            contract_id="r",
            global_goal="完成任务",
            source_candidates=[candidate],
            requirements=[RequirementItem(
                requirement_id="REQ-ONE",
                statement="生成结果",
                source_refs=[RequirementSourceRef(
                    artifact="inputs/spec.pdf",
                    locator="preview:chars:12-34",
                )],
                acceptance_criteria=[RequirementAcceptance(
                    criterion_id="AC-ONE", method="check",
                    condition="存在",
                )],
            )],
        )

        self.assertTrue(validate_requirement_set(reqs).passed)

    def test_model_aliases_are_normalized_without_losing_requirements(self):
        item = RequirementItem.model_validate({
            "requirement_id": "REQ-X",
            "requirement_type": "delivery",
            "statement": "生成结果",
            "acceptance_criteria": [{
                "criterion_id": "AC-X",
                "method": "schema_check",
                "condition": "结果存在",
                "severity": "critical",
            }],
        })
        self.assertEqual(item.requirement_type, "deliverable")
        self.assertEqual(item.acceptance_criteria[0].severity, "blocking")
        analysis = RequirementItem.model_validate({
            "requirement_id": "REQ-A",
            "requirement_type": "analysis",
            "statement": "分析结果",
            "acceptance_criteria": [{
                "criterion_id": "AC-A",
                "method": "review",
                "condition": "分析完整",
                "severity": "major",
            }],
        })
        report = RequirementItem.model_validate({
            "requirement_id": "REQ-R",
            "requirement_type": "report",
            "statement": "生成报告",
        })
        self.assertEqual(analysis.requirement_type, "objective")
        self.assertEqual(analysis.acceptance_criteria[0].severity, "blocking")
        self.assertEqual(report.requirement_type, "deliverable")
        derived = RequirementItem.model_validate({
            "requirement_id": "REQ-D",
            "requirement_type": "derived",
            "statement": "来源候选仅为背景说明",
        })
        self.assertEqual(derived.requirement_type, "quality")

    def test_model_self_parent_and_dependency_are_removed_without_dropping_item(self):
        task_spec = {
            "task_id": "run", "task_name": "generic",
            "input": {"files": []},
            "artifact_contract": {"intermediate_artifacts": []},
        }
        reqs = compile_requirement_set(task_spec, {
            "summary": "完成任务",
            "requirements": [{
                "requirement_id": "REQ-SELF",
                "parent_id": "REQ-SELF",
                "depends_on": ["REQ-SELF"],
                "statement": "可选通用性验证",
                "mandatory": False,
            }],
        })
        self.assertEqual(len(reqs.requirements), 1)
        self.assertIsNone(reqs.requirements[0].parent_id)
        self.assertEqual(reqs.requirements[0].depends_on, [])
        self.assertTrue(validate_requirement_set(reqs).passed)

    def test_computational_requirement_cannot_be_misowned_by_outer_workflow(self):
        task_spec = {
            "task_id": "run",
            "task_name": "generic",
            "artifact_contract": {
                "intermediate_artifacts": ["artifacts/result.json"],
                "final_artifacts": ["final_report.md"],
                "framework_artifacts": [],
            },
            "input": {"files": ["inputs/spec.pdf"]},
        }
        reqs = compile_requirement_set(task_spec, {
            "summary": "完成任务",
            "requirements": [
                {
                    "requirement_id": "REQ-COMPUTE",
                    "requirement_type": "objective",
                    "statement": "计算方案指标并分析参数变化",
                    "mandatory": True,
                    "owner": "outer_workflow",
                    "expected_outputs": ["final_report.md"],
                    "acceptance_criteria": [{
                        "criterion_id": "AC-COMPUTE",
                        "method": "metric_check",
                        "condition": "指标可复算",
                        "severity": "warning",
                    }],
                },
                {
                    "requirement_id": "REQ-REPORT",
                    "requirement_type": "deliverable",
                    "statement": "撰写最终报告",
                    "mandatory": True,
                    "owner": "outer_workflow",
                    "expected_outputs": ["final_report.md"],
                    "acceptance_criteria": [{
                        "criterion_id": "AC-REPORT",
                        "method": "section_check",
                        "condition": "章节存在",
                    }],
                },
            ],
        })
        by_id = {item.requirement_id: item for item in reqs.requirements}
        self.assertEqual(by_id["REQ-COMPUTE"].owner, "planner")
        self.assertEqual(
            by_id["REQ-COMPUTE"].acceptance_criteria[0].severity,
            "blocking",
        )
        self.assertEqual(by_id["REQ-REPORT"].owner, "outer_workflow")

    def test_report_section_goal_is_owned_by_outer_workflow(self):
        task_spec = {
            "task_id": "run",
            "task_name": "generic",
            "artifact_contract": {
                "intermediate_artifacts": ["artifacts/result.json"],
                "final_artifacts": ["final_report.md"],
                "framework_artifacts": [],
            },
            "input": {"files": ["inputs/spec.pdf"]},
        }
        reqs = compile_requirement_set(task_spec, {
            "summary": "完成任务",
            "requirements": [{
                "requirement_id": "REQ-REPORT-SECTION",
                "requirement_type": "goal",
                "statement": "技术报告包含模型性能分析章节",
                "mandatory": True,
                "owner": "planner",
                "expected_outputs": ["final_report.md"],
                "acceptance_criteria": [{
                    "criterion_id": "AC-REPORT-SECTION",
                    "method": "artifact_exists",
                    "target": "final_report.md",
                    "condition": "章节存在",
                    "severity": "blocking",
                }],
            }],
        })

        item = reqs.requirements[0]
        self.assertEqual(item.owner, "outer_workflow")
        self.assertTrue(validate_requirement_set(reqs).passed)

    def test_cross_responsibility_leaf_requirement_requires_refinement(self):
        reqs = RequirementSet(
            contract_id="r",
            global_goal="完成任务",
            requirements=[RequirementItem(
                requirement_id="REQ-COARSE",
                statement="设计算法并输出结果坐标",
                acceptance_criteria=[RequirementAcceptance(
                    criterion_id="AC-X", method="schema_check",
                    condition="结果存在",
                )],
            )],
        )
        validation = validate_requirement_set(reqs)
        self.assertFalse(validation.passed)
        self.assertEqual(
            validation.oversized_requirement_ids, ["REQ-COARSE"],
        )

    def test_compute_and_emit_one_metric_is_an_atomic_requirement(self):
        reqs = RequirementSet(
            contract_id="r",
            global_goal="完成任务",
            requirements=[RequirementItem(
                requirement_id="REQ-METRIC",
                statement="计算并输出空间利用率指标（单辆，有效范围验证）",
                acceptance_criteria=[
                    RequirementAcceptance(
                        criterion_id="AC-MIN", method="numeric_compare",
                        condition="空间利用率不小于零",
                    ),
                    RequirementAcceptance(
                        criterion_id="AC-MAX", method="numeric_compare",
                        condition="空间利用率不大于一",
                    ),
                ],
            )],
        )

        validation = validate_requirement_set(reqs)

        self.assertTrue(validation.passed, validation.model_dump())

    def test_duplicate_acceptance_ids_are_namespaced_during_compilation(self):
        task_spec = {
            "task_id": "generic",
            "artifact_contract": {
                "intermediate_artifacts": ["artifacts/result.json"],
                "final_artifacts": [],
                "framework_artifacts": [],
            },
        }
        criterion = {
            "criterion_id": "AC-SHARED",
            "method": "required_fields",
            "target": "artifacts/result.json",
            "condition": "字段存在",
            "params": {"fields": ["/value"]},
            "severity": "blocking",
        }

        compiled = compile_requirement_set(task_spec, {
            "summary": "计算两个独立指标",
            "requirements": [
                {
                    "requirement_id": "REQ-A",
                    "statement": "计算指标 A",
                    "expected_outputs": ["artifacts/result.json"],
                    "acceptance_criteria": [criterion],
                },
                {
                    "requirement_id": "REQ-B",
                    "statement": "计算指标 B",
                    "expected_outputs": ["artifacts/result.json"],
                    "acceptance_criteria": [criterion],
                },
            ],
        })

        criterion_ids = [
            criterion.criterion_id
            for item in compiled.requirements
            for criterion in item.acceptance_criteria
        ]
        self.assertEqual(
            set(criterion_ids),
            {"AC-SHARED--REQ-A", "AC-SHARED--REQ-B"},
        )

    def test_provider_parent_relation_is_conservatively_normalized(self):
        item = RequirementItem.model_validate({
            "requirement_id": "REQ-GROUP",
            "relation": "parent",
            "statement": "汇总一组必须全部完成的子需求",
        })

        self.assertEqual(item.relation, "and")

    def test_compound_status_is_provenance_alias_not_hierarchy(self):
        item = RequirementItem.model_validate({
            "requirement_id": "REQ-GROUP",
            "statement": "一组显式需求的父级描述",
            "status": "compound",
        })

        self.assertEqual(item.status, "explicit")

    def test_file_exists_acceptance_alias_is_executable(self):
        criterion = RequirementAcceptance.model_validate({
            "criterion_id": "AC-FILE",
            "method": "file_exists",
            "target": "artifacts/result.json",
            "condition": "结果文件存在",
        })

        self.assertEqual(criterion.method, "artifact_exists")

    def test_json_parseable_is_not_applied_to_python_deliverable(self):
        task_spec = {
            "task_id": "run",
            "task_name": "generic implementation",
            "artifact_contract": {
                "intermediate_artifacts": ["artifacts/code_pipeline.py"],
                "final_artifacts": [],
                "framework_artifacts": [],
            },
            "input": {"files": ["inputs/spec.txt"]},
        }
        contract = compile_requirement_set(task_spec, {
            "requirements": [{
                "requirement_id": "REQ-CODE",
                "requirement_type": "deliverable",
                "statement": "provide executable Python code",
                "mandatory": True,
                "owner": "planner",
                "expected_outputs": ["artifacts/code_pipeline.py"],
                "acceptance_criteria": [{
                    "criterion_id": "AC-CODE",
                    "method": "json_parseable",
                    "target": "artifacts/code_pipeline.py",
                    "condition": "Python source is valid",
                    "severity": "blocking",
                }],
            }],
        })

        self.assertEqual(
            contract.requirements[0].acceptance_criteria[0].method,
            "artifact_exists",
        )

    def test_separate_models_for_multiple_entities_requires_refinement(self):
        reqs = RequirementSet(
            contract_id="r",
            global_goal="完成任务",
            requirements=[RequirementItem(
                requirement_id="REQ-MODELS",
                statement="为车型 1 和车型 2 分别建立三维装箱优化模型",
                acceptance_criteria=[RequirementAcceptance(
                    criterion_id="AC-MODELS", method="model_check",
                    condition="两种车型模型存在",
                )],
            )],
        )
        validation = validate_requirement_set(reqs)
        self.assertFalse(validation.passed)
        self.assertEqual(
            validation.oversized_requirement_ids, ["REQ-MODELS"],
        )

    def test_refinement_cannot_drop_previous_requirements(self):
        previous = RequirementSet(
            contract_id="r",
            version=1,
            global_goal="完成任务",
            requirements=[
                RequirementItem(
                    requirement_id="REQ-KEEP",
                    statement="保留需求",
                    acceptance_criteria=[RequirementAcceptance(
                        criterion_id="AC-KEEP", method="check",
                        condition="保留",
                    )],
                ),
                RequirementItem(
                    requirement_id="REQ-COARSE",
                    statement="粗需求",
                    acceptance_criteria=[RequirementAcceptance(
                        criterion_id="AC-COARSE", method="check",
                        condition="细化",
                    )],
                ),
            ],
        )
        refined = RequirementSet(
            contract_id="model-changed-id",
            version=2,
            global_goal="完成任务",
            requirements=[RequirementItem(
                requirement_id="REQ-CHILD",
                parent_id="REQ-COARSE",
                statement="细化后的子需求",
                acceptance_criteria=[RequirementAcceptance(
                    criterion_id="AC-CHILD", method="check",
                    condition="完成",
                )],
            )],
        )
        merged = preserve_requirement_history(previous, refined)
        self.assertEqual(merged.contract_id, "r")
        self.assertEqual(
            {item.requirement_id for item in merged.requirements},
            {"REQ-KEEP", "REQ-COARSE", "REQ-CHILD"},
        )

    def test_refinement_recovers_unambiguous_intermediate_parent(self):
        previous = RequirementSet(
            contract_id="r", global_goal="完成任务",
            requirements=[RequirementItem(
                requirement_id="REQ-005", statement="计算两类利用率",
            )],
        )
        refined = RequirementSet(
            contract_id="r", global_goal="完成任务",
            requirements=[
                RequirementItem(
                    requirement_id="REQ-005a", parent_id="REQ-005",
                    statement="计算并输出空间利用率",
                ),
                RequirementItem(
                    requirement_id="REQ-005a-1", parent_id="REQ-005",
                    statement="空间利用率字段存在",
                ),
            ],
        )
        merged = preserve_requirement_history(previous, refined)
        child = next(
            item for item in merged.requirements
            if item.requirement_id == "REQ-005a-1"
        )
        self.assertEqual(child.parent_id, "REQ-005a")

    def test_coarse_parent_is_allowed_after_real_child_decomposition(self):
        reqs = RequirementSet(
            contract_id="r",
            global_goal="完成任务",
            requirements=[
                RequirementItem(
                    requirement_id="REQ-PARENT",
                    statement="设计算法并输出结果坐标",
                    acceptance_criteria=[RequirementAcceptance(
                        criterion_id="AC-P", method="evidence_review",
                        condition="子需求闭环",
                    )],
                ),
                RequirementItem(
                    requirement_id="REQ-ALGORITHM",
                    parent_id="REQ-PARENT",
                    statement="设计求解算法",
                    acceptance_criteria=[RequirementAcceptance(
                        criterion_id="AC-A", method="specification_check",
                        condition="算法规格存在",
                    )],
                ),
                RequirementItem(
                    requirement_id="REQ-COORDINATES",
                    parent_id="REQ-PARENT",
                    statement="输出结果坐标",
                    acceptance_criteria=[RequirementAcceptance(
                        criterion_id="AC-C", method="schema_check",
                        condition="坐标字段存在",
                    )],
                ),
            ],
        )
        self.assertTrue(validate_requirement_set(reqs).passed)

    def test_legacy_understanding_gets_a_non_empty_generic_contract(self):
        task_spec = {
            "task_id": "run-1",
            "task_name": "generic task",
            "artifact_contract": {
                "intermediate_artifacts": ["artifacts/result.json"],
            },
            "input": {"files": ["inputs/source.pdf"]},
        }
        reqs = compile_requirement_set(task_spec, {
            "summary": "完成一个通用任务",
            "goals": ["分析输入并生成结果"],
            "constraints": ["结果必须可追踪"],
        })
        self.assertGreaterEqual(len(reqs.requirements), 4)
        self.assertTrue(validate_requirement_set(reqs).passed)

    def test_plan_coverage_requires_mandatory_leaf_requirements(self):
        reqs = RequirementSet(
            contract_id="r",
            global_goal="完成任务",
            requirements=[
                RequirementItem(
                    requirement_id="ROOT",
                    requirement_type="goal",
                    statement="完成任务",
                    acceptance_criteria=[RequirementAcceptance(
                        criterion_id="AC-ROOT", method="evidence_review",
                        condition="完成",
                    )],
                ),
                RequirementItem(
                    requirement_id="LEAF-A",
                    parent_id="ROOT",
                    requirement_type="deliverable",
                    statement="产出 A",
                    expected_outputs=["a"],
                    acceptance_criteria=[RequirementAcceptance(
                        criterion_id="AC-A", method="schema_check",
                        condition="存在 A",
                    )],
                ),
                RequirementItem(
                    requirement_id="LEAF-B",
                    parent_id="ROOT",
                    requirement_type="deliverable",
                    statement="产出 B",
                    expected_outputs=["b"],
                    acceptance_criteria=[RequirementAcceptance(
                        criterion_id="AC-B", method="schema_check",
                        condition="存在 B",
                    )],
                ),
            ],
        )
        plan = PlanIR(
            global_goal="完成任务",
            nodes=[
                PlanNode(
                    node_id="root",
                    objective="组织任务",
                    node_type=PlanNodeType.COMPOUND,
                    primary_capability="reasoning",
                ),
                PlanNode(
                    node_id="a",
                    objective="产出 A",
                    node_type=PlanNodeType.PRIMITIVE,
                    parent_node_id="root",
                    primary_capability="analysis",
                    requirement_ids=["LEAF-A"],
                    acceptance_refs=["AC-A"],
                    expected_output={
                        "output_id": "a",
                        "description": "a",
                    },
                    acceptance_criteria=[{"type": "schema_check"}],
                ),
                PlanNode(
                    node_id="b",
                    objective="产出 B",
                    node_type=PlanNodeType.PRIMITIVE,
                    parent_node_id="root",
                    primary_capability="analysis",
                    requirement_ids=["LEAF-B"],
                    acceptance_refs=["AC-B"],
                    expected_output={
                        "output_id": "b",
                        "description": "b",
                    },
                    acceptance_criteria=[{"type": "schema_check"}],
                ),
            ],
        )
        contract = PlanningContract(
            requirement_contract=reqs.model_dump(mode="json"),
        )
        result = validate_plan_ir(
            plan, contract, PlanningPolicy(),
            authoritative_goal="完成任务",
            available_capabilities={"reasoning", "analysis"},
        )
        self.assertTrue(result.passed, result)
        self.assertEqual(set(result.covered_requirement_ids), {"LEAF-A", "LEAF-B"})

    def test_missing_requirement_is_a_planning_error(self):
        reqs = RequirementSet(
            contract_id="r",
            global_goal="完成任务",
            requirements=[
                RequirementItem(
                    requirement_id="REQ-A",
                    statement="必须产出 A",
                    acceptance_criteria=[RequirementAcceptance(
                        criterion_id="AC-A", method="schema_check",
                        condition="存在 A",
                    )],
                ),
            ],
        )
        plan = PlanIR(
            global_goal="完成任务",
            nodes=[PlanNode(
                node_id="n",
                objective="做一些分析",
                node_type=PlanNodeType.PRIMITIVE,
                primary_capability="analysis",
                expected_output={"output_id": "x", "description": "x"},
                acceptance_criteria=[{"type": "present"}],
            )],
        )
        result = validate_plan_ir(
            plan,
            PlanningContract(requirement_contract=reqs.model_dump(mode="json")),
            PlanningPolicy(),
            authoritative_goal="完成任务",
            available_capabilities={"analysis"},
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.missing_requirement_ids, ["REQ-A"])

    def test_outer_workflow_requirement_does_not_create_business_node(self):
        reqs = RequirementSet(
            contract_id="r",
            global_goal="完成任务",
            requirements=[RequirementItem(
                requirement_id="REQ-REPORT",
                requirement_type="deliverable",
                statement="生成最终报告",
                owner="outer_workflow",
                expected_outputs=["final_report.md"],
                acceptance_criteria=[RequirementAcceptance(
                    criterion_id="AC-REPORT", method="section_check",
                    condition="章节完整",
                )],
            )],
        )
        plan = PlanIR(
            global_goal="完成任务",
            nodes=[PlanNode(
                node_id="analysis",
                objective="分析输入",
                node_type=PlanNodeType.PRIMITIVE,
                primary_capability="analysis",
                expected_output={"output_id": "analysis", "description": "analysis"},
                acceptance_criteria=[{"type": "present"}],
            )],
        )
        result = validate_plan_ir(
            plan,
            PlanningContract(requirement_contract=reqs.model_dump(mode="json")),
            PlanningPolicy(),
            authoritative_goal="完成任务",
            available_capabilities={"analysis"},
        )
        self.assertTrue(result.passed, result)

    def test_dangling_explicit_edge_is_rejected_before_flatten(self):
        plan = PlanIR(
            global_goal="完成任务",
            nodes=[PlanNode(
                node_id="target",
                objective="分析输入",
                node_type=PlanNodeType.PRIMITIVE,
                primary_capability="analysis",
                expected_output={"output_id": "analysis", "description": "analysis"},
                acceptance_criteria=[{"type": "present"}],
            )],
            edges=[("removed_node", "target")],
        )
        result = validate_plan_ir(
            plan, PlanningContract(), PlanningPolicy(),
            authoritative_goal="完成任务",
            available_capabilities={"analysis"},
        )
        self.assertFalse(result.passed)
        self.assertIn(
            "PlanIR 边引用不存在节点: removed_node->target",
            result.dependency_errors,
        )

    def test_local_patch_drops_dangling_external_edge(self):
        plan = PlanIR(
            global_goal="完成任务",
            nodes=[PlanNode(
                node_id="target",
                objective="旧任务",
                node_type=PlanNodeType.PRIMITIVE,
                primary_capability="analysis",
                expected_output={"output_id": "old", "description": "old"},
                acceptance_criteria=[{"type": "present"}],
            )],
            edges=[("removed_node", "target")],
        )
        patch = PlanPatch(
            target_node_id="target",
            replacement_nodes=[PlanNode(
                node_id="target",
                objective="新任务",
                node_type=PlanNodeType.PRIMITIVE,
                primary_capability="analysis",
                expected_output={"output_id": "new", "description": "new"},
                acceptance_criteria=[{"type": "present"}],
            )],
        )
        merged = _merge_patch(plan, patch)
        self.assertNotIn(("removed_node", "target"), merged.edges)


if __name__ == "__main__":
    unittest.main()
