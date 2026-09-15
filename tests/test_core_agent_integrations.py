import json
from pathlib import Path
import tempfile
import unittest

from autogen_ext.models.replay import ReplayChatCompletionClient

from orchestration.execution.capability_registry import CapabilityRegistry
from orchestration.execution.default_executors import ArtifactValidationExecutor
from orchestration.integrations.evidence_verifier import EvidenceVerificationExecutor
from orchestration.graph.graph_planner import build_fallback_graph, validate_planned_graph
from orchestration.graph.graph_scheduler import GraphScheduler
from orchestration.execution.node_executor import ExecutorDescriptor, NodeExecutionResult
from orchestration.core.run_state import RunState
from orchestration.core.schemas import EvidenceVerificationResult
from orchestration.graph.task_graph import GraphValidationError, TaskGraph, TaskNode
from orchestration.task.task_normalizer import normalize_to_task_spec
from orchestration.integrations.terminal_executor import SandboxedTerminalNodeExecutor
from orchestration.core.workflow import run_explicit_workflow


class EvidenceProducerExecutor:
    descriptor = ExecutorDescriptor(
        executor_id="test_evidence_producer",
        executor_type="test_executor",
        capabilities=["analysis"],
        quality_score=1.0,
        cost_score=0.0,
        latency_score=0.0,
    )

    async def execute(self, context):
        return NodeExecutionResult(
            node_id=context.node.node_id,
            executor_id=self.descriptor.executor_id,
            status="completed",
            summary="标准化输入中的 total 为 42",
            structured_output={
                "findings": ["total=42"],
                "quality_signal": {"status": "passed"},
            },
            evidence_refs=["normalized_input.json#/total"],
        )


class CodeProducerExecutor:
    descriptor = ExecutorDescriptor(
        executor_id="test_code_producer",
        executor_type="test_executor",
        capabilities=["code"],
        quality_score=1.0,
        cost_score=0.0,
        latency_score=0.0,
    )

    async def execute(self, context):
        relative = "artifacts/code_pipeline.py"
        destination = Path(context.run_dir) / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            "from pathlib import Path\nVALUE = 42\n",
            encoding="utf-8",
        )
        return NodeExecutionResult(
            node_id=context.node.node_id,
            executor_id=self.descriptor.executor_id,
            status="completed",
            summary="测试代码已由框架测试执行器落盘",
            output_artifacts=[relative],
            evidence_refs=[relative],
            structured_output={
                "execution_result": {"exit_code": 0},
                "quality_signal": {"status": "passed"},
            },
        )


def enter_execute_stage(state):
    state.start_stage("classify")
    state.start_stage("plan")
    state.start_stage("execute")


class CoreAgentContractTests(unittest.TestCase):
    def test_normalized_task_spec_enables_agents_by_task_semantics(self):
        no_code = normalize_to_task_spec({
            "input_type": "text",
            "files": [],
            "text": "请分析这项通用任务，不需要代码",
        })
        code = normalize_to_task_spec({
            "input_type": "text",
            "files": [],
            "text": "请结构化输出 artifacts/result.json",
        })

        self.assertIn(
            "evidence_verification",
            no_code["capability_contract"]["required"],
        )
        self.assertNotIn(
            "terminal_execution",
            no_code["capability_contract"]["required"],
        )
        self.assertTrue(no_code["memory_policy"]["enabled"])
        self.assertTrue(no_code["task_understanding_policy"]["enabled"])
        self.assertEqual(
            set(code["capability_contract"]["required"]),
            {"evidence_verification", "terminal_execution"},
        )
        fallback = build_fallback_graph(
            code,
            [
                "analysis", "code", "terminal_execution",
                "evidence_verification", "artifact_validation",
            ],
            "test fallback",
        )
        capabilities = [node.capability for node in fallback.nodes]
        self.assertEqual(capabilities, [
            "analysis", "code", "terminal_execution",
            "evidence_verification", "artifact_validation",
        ])

    def test_verifier_must_directly_receive_every_business_node(self):
        spec = {
            "task_id": "direct-verification-contract",
            "code_policy": {"mode": "none"},
            "artifact_contract": {"intermediate_artifacts": []},
            "capability_contract": {"required": ["evidence_verification"]},
            "recovery_policy": {"max_node_retries": 0},
        }
        graph = TaskGraph(
            graph_id="indirect-verification",
            goal="证明间接祖先不能冒充直接核验输入",
            nodes=[
                TaskNode(
                    node_id="first_analysis",
                    description="第一项业务分析",
                    capability="analysis",
                    success_criteria=[{"type": "node_result"}],
                ),
                TaskNode(
                    node_id="second_analysis",
                    description="第二项业务分析",
                    capability="analysis",
                    dependencies=["first_analysis"],
                    success_criteria=[{"type": "node_result"}],
                ),
                TaskNode(
                    node_id="verify",
                    description="只直接接收第二个节点，属于不完整核验",
                    capability="evidence_verification",
                    dependencies=["second_analysis"],
                    success_criteria=[{"type": "node_result"}],
                ),
                TaskNode(
                    node_id="validate",
                    description="确定性产物校验",
                    capability="artifact_validation",
                    dependencies=["verify"],
                    success_criteria=[
                        {"type": "artifact_quality", "equals": "passed"}
                    ],
                ),
            ],
        )

        with self.assertRaisesRegex(GraphValidationError, "缺少直接依赖"):
            validate_planned_graph(
                graph.model_dump(mode="json"),
                spec,
                ["analysis", "evidence_verification", "artifact_validation"],
            )


class CoreAgentSchedulerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def test_malformed_evidence_claim_is_preserved_but_forced_insufficient(self):
        decision = EvidenceVerificationResult.model_validate({
            "status": "passed",
            "claims": [{
                "claim_id": "claim_016",
                "source_node_ids": ["analyze"],
                "verdict": "supported",
                "evidence_refs": ["normalized_input.json"],
                "rationale": "模型遗漏了原子事实字段",
                "confidence": 0.9,
            }],
            "issues": [],
            "additional_evidence_requests": [],
            "can_continue": True,
        })

        self.assertEqual(decision.claims[0].verdict, "insufficient")
        self.assertIn("模型遗漏了原子事实字段", decision.claims[0].claim)
        self.assertIn("缺少 claim 字段", decision.claims[0].rationale)

    async def test_evidence_verifier_reads_real_evidence_and_is_routed(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "normalized_input.json").write_text(
                json.dumps({"total": 42}), encoding="utf-8"
            )
            spec = {
                "task_id": "evidence-agent-test",
                "task_type": "document_analysis",
                "run_dir": str(run_dir),
                "code_policy": {"mode": "none"},
                "artifact_contract": {"intermediate_artifacts": []},
                "capability_contract": {
                    "required": ["evidence_verification"],
                    "preferred": [],
                },
                "verification_policy": {
                    "enabled": True,
                    "max_claims": 8,
                    "max_evidence_excerpt_chars": 1_000,
                    "max_evidence_catalog_chars": 2_000,
                    "require_evidence_for_supported_claims": True,
                },
                "routing_policy": {"mode": "agent_prune_lite"},
                "recovery_policy": {
                    "max_node_retries": 0,
                    "max_executor_switches": 0,
                    "max_replans": 0,
                },
                "team_policy": {"early_stop_on_success": True},
                "communication_policy": {
                    "compressor": "deterministic",
                    "max_node_context_tokens": 5_000,
                },
            }
            graph = TaskGraph(
                graph_id="evidence-agent-graph",
                goal="核验真实文件中的事实",
                nodes=[
                    TaskNode(
                        node_id="analyze",
                        description="从标准化输入形成待核验事实",
                        capability="analysis",
                        success_criteria=[{"type": "node_result"}],
                        max_retries=0,
                    ),
                    TaskNode(
                        node_id="verify_evidence",
                        description="逐条核验分析事实",
                        capability="evidence_verification",
                        dependencies=["analyze"],
                        success_criteria=[{"type": "node_result"}],
                        max_retries=0,
                    ),
                    TaskNode(
                        node_id="validate",
                        description="确定性校验中间产物",
                        capability="artifact_validation",
                        dependencies=["verify_evidence"],
                        success_criteria=[
                            {"type": "artifact_quality", "equals": "passed"}
                        ],
                        max_retries=0,
                    ),
                ],
            )
            client = ReplayChatCompletionClient([
                json.dumps({
                    "status": "passed",
                    "claims": [{
                        "claim_id": "claim_001",
                        "source_node_ids": ["analyze"],
                        "claim": "total 等于 42",
                        "verdict": "supported",
                        "evidence_refs": ["normalized_input.json#/total"],
                        "rationale": "框架读取的 JSON Pointer 值为 42",
                        "confidence": 1.0,
                    }],
                    "issues": [],
                    "additional_evidence_requests": [],
                    "can_continue": True,
                }, ensure_ascii=False)
            ])
            state = RunState(str(run_dir))
            enter_execute_stage(state)
            trace = []
            registry = CapabilityRegistry()
            registry.register(EvidenceProducerExecutor())
            registry.register(EvidenceVerificationExecutor(client, state, trace))
            registry.register(ArtifactValidationExecutor(state))

            result = await GraphScheduler(registry, state, run_dir).run(graph, spec)

            self.assertFalse(result.has_failed_nodes(), state.get("nodes"))
            self.assertEqual(len(client.create_calls), 1)
            self.assertEqual(
                state.get("nodes")["verify_evidence"]["result"]["executor_id"],
                "safe_critic_evidence_verifier",
            )
            record_path = (
                run_dir / "artifacts" / "tool_results" / "verify_evidence"
                / "evidence_verification.json"
            )
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertTrue(record["passed"])
            self.assertEqual(record["catalog"][0]["content"], "42")

    async def test_autogen_terminal_agent_executes_framework_check(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            spec = {
                "task_id": "terminal-agent-test",
                "task_type": "general_complex_task",
                "run_dir": str(run_dir),
                "code_policy": {"mode": "lightweight"},
                "artifacts": {"code": "artifacts/code_pipeline.py"},
                "artifact_contract": {
                    "intermediate_artifacts": ["artifacts/code_pipeline.py"]
                },
                "capability_contract": {
                    "required": ["terminal_execution"],
                    "preferred": [],
                },
                "terminal_policy": {
                    "enabled": True,
                    "backend": "bounded_local",
                    "timeout_seconds": 10,
                    "allowed_checks": ["python_compile"],
                },
                "routing_policy": {"mode": "agent_prune_lite"},
                "recovery_policy": {
                    "max_node_retries": 0,
                    "max_executor_switches": 0,
                    "max_replans": 0,
                },
                "team_policy": {"early_stop_on_success": True},
                "communication_policy": {"compressor": "deterministic"},
            }
            graph = TaskGraph(
                graph_id="terminal-agent-graph",
                goal="用真实 AutoGen CodeExecutorAgent 复核代码",
                nodes=[
                    TaskNode(
                        node_id="produce_code",
                        description="生成测试代码",
                        capability="code",
                        output_artifacts=["artifacts/code_pipeline.py"],
                        success_criteria=[
                            {"type": "execution_exit_code", "equals": 0}
                        ],
                        max_retries=0,
                    ),
                    TaskNode(
                        node_id="terminal_check",
                        description="在受控工作目录编译检查代码",
                        capability="terminal_execution",
                        dependencies=["produce_code"],
                        input_artifacts=["artifacts/code_pipeline.py"],
                        success_criteria=[
                            {"type": "execution_exit_code", "equals": 0}
                        ],
                        max_retries=0,
                    ),
                    TaskNode(
                        node_id="validate",
                        description="确定性校验代码产物",
                        capability="artifact_validation",
                        dependencies=["terminal_check"],
                        success_criteria=[
                            {"type": "artifact_quality", "equals": "passed"}
                        ],
                        max_retries=0,
                    ),
                ],
            )
            state = RunState(str(run_dir))
            enter_execute_stage(state)
            trace = []
            registry = CapabilityRegistry()
            registry.register(CodeProducerExecutor())
            registry.register(SandboxedTerminalNodeExecutor(state, trace))
            registry.register(ArtifactValidationExecutor(state))

            result = await GraphScheduler(registry, state, run_dir).run(graph, spec)

            self.assertFalse(result.has_failed_nodes(), state.get("nodes"))
            terminal_result = state.get("nodes")["terminal_check"]["result"]
            self.assertEqual(terminal_result["executor_id"], "autogen_controlled_terminal")
            self.assertEqual(
                terminal_result["structured_output"]["execution_result"]["exit_code"],
                0,
            )
            invocations = state.get("external_tools")["invocations"]
            self.assertEqual(len(invocations), 1)
            self.assertEqual(
                invocations[0]["provider"],
                "autogen_agentchat.agents.CodeExecutorAgent",
            )
            self.assertTrue(invocations[0]["tool_invoked"])
            self.assertFalse(any(run_dir.rglob("*.pyc")))


class WorkflowMemoryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_agent_is_called_before_planning_and_after_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            store_path = run_dir / "memory" / "workflow_skills.json"
            spec = {
                "task_id": "memory-agent-test",
                "task_name": "通用工作流记忆测试",
                "task_type": "document_analysis",
                "run_dir": str(run_dir),
                "input": {"type": "text", "files": [], "text": "分析通用任务"},
                "file_previews": [],
                "code_policy": {"mode": "none", "max_retries": 0},
                "artifact_contract": {
                    "intermediate_artifacts": [],
                    "final_artifacts": ["final_report.md"],
                    "framework_artifacts": [
                        "task_spec.json", "task_graph.json", "run_state.json",
                        "agent_trace.md", "normalized_input.json",
                    ],
                },
                "required_artifacts": [
                    "final_report.md", "task_spec.json", "task_graph.json",
                    "run_state.json", "agent_trace.md", "normalized_input.json",
                ],
                "required_report_sections": ["任务理解", "结果分析"],
                "success_criteria": {},
                "artifacts": {"code": None, "results": [], "report": "final_report.md"},
                "capability_contract": {"required": [], "preferred": []},
                "memory_policy": {
                    "enabled": True,
                    "store_path": str(store_path),
                    "max_retrieved_skills": 3,
                    "max_candidates_per_run": 2,
                    "min_candidate_confidence": 0.7,
                },
                "routing_policy": {"mode": "agent_prune_lite"},
                "team_policy": {
                    "mode": "static",
                    "early_stop_on_success": True,
                    "stats_path": str(run_dir / "executor_stats.json"),
                },
                "recovery_policy": {
                    "max_node_retries": 0,
                    "max_executor_switches": 0,
                    "max_replans": 0,
                },
                "communication_policy": {
                    "compressor": "deterministic",
                    "max_control_prompt_tokens": 8_000,
                    "max_node_context_tokens": 5_000,
                },
            }
            (run_dir / "task_spec.json").write_text(
                json.dumps(spec, ensure_ascii=False), encoding="utf-8"
            )
            store_path.parent.mkdir(parents=True, exist_ok=True)
            store_path.write_text(json.dumps({
                "schema_version": "1.0",
                "skills": [{
                    "skill_id": "skill_seed_document_analysis",
                    "title": "已有的通用分析技能",
                    "kind": "successful_workflow",
                    "applicable_task_types": ["document_analysis"],
                    "required_capabilities": ["analysis"],
                    "guidance": ["保持任务图稀疏，并让确定性校验节点收口"],
                    "confidence": 0.9,
                    "source_run_id": "seed",
                    "verified_outcome": "success",
                    "created_at": "2026-07-16T00:00:00",
                }],
            }, ensure_ascii=False), encoding="utf-8")
            state = RunState(
                str(run_dir),
                task_type=spec["task_type"],
                code_policy=spec["code_policy"],
                required_artifacts=spec["required_artifacts"],
            )
            state.start_stage("classify")
            state.configure(spec)
            graph = {
                "graph_id": "memory-agent-test-graph",
                "version": 1,
                "goal": "完成通用任务分析",
                "nodes": [
                    {
                        "node_id": "analyze",
                        "description": "分析通用任务目标",
                        "capability": "analysis",
                        "dependencies": [],
                        "input_artifacts": [],
                        "output_artifacts": [],
                        "success_criteria": [{"type": "node_result"}],
                        "max_retries": 0,
                    },
                    {
                        "node_id": "validate",
                        "description": "确定性校验中间产物",
                        "capability": "artifact_validation",
                        "dependencies": ["analyze"],
                        "input_artifacts": [],
                        "output_artifacts": [],
                        "success_criteria": [
                            {"type": "artifact_quality", "equals": "passed"}
                        ],
                        "max_retries": 0,
                    },
                ],
            }
            analysis = {
                "summary": "任务目标已经根据标准化输入完成分析。",
                "findings": ["本任务使用通用动态任务图完成分析。"],
                "evidence_refs": ["normalized_input.json"],
                "risks": [],
                "confidence": 0.9,
            }
            semantic_review = {
                "blocking_issues": [],
                "advisory_issues": [],
                "evidence_references": ["normalized_input.json"],
                "repair_recommended": False,
                "repair_target": "none",
            }
            report = (
                "# 任务理解\n本次测试验证通用多智能体框架能够在规划之前读取可复用工作流记忆，"
                "并且仍由当前任务契约决定实际任务图。框架没有把历史运行结果当作当前业务事实。\n\n"
                "## 结果分析\n分析节点和确定性产物校验节点按照稀疏依赖关系依次执行。"
                "最终报告仅消费本轮已经验证的结构化结果；最终验收完成后，框架再次调用记忆组件，"
                "由其提出可复用候选，再由 Python 过滤置信度和敏感信息后写入持久化技能库。"
                "运行状态同时记录两次真实调用、所选技能标识和新增技能标识，便于后续审计与复现。"
            )
            curation = {
                "candidates": [{
                    "title": "通用无代码分析收口流程",
                    "kind": "successful_workflow",
                    "applicable_task_types": ["document_analysis"],
                    "required_capabilities": ["analysis", "artifact_validation"],
                    "guidance": [
                        "先完成证据约束的分析，再执行确定性产物校验"
                    ],
                    "confidence": 0.9,
                }]
            }
            client = ReplayChatCompletionClient([
                json.dumps({
                    "selected_skill_ids": ["skill_seed_document_analysis"],
                    "planning_guidance": ["模型不能注入未授权的新建议"],
                    "confidence": 1.0,
                }, ensure_ascii=False),
                json.dumps(graph, ensure_ascii=False),
                json.dumps(analysis, ensure_ascii=False),
                json.dumps(semantic_review, ensure_ascii=False),
                report,
                json.dumps(curation, ensure_ascii=False),
            ])

            decision, _ = await run_explicit_workflow(spec, state, client)

            self.assertEqual(decision.status, "passed", decision.issues)
            self.assertEqual(len(client.create_calls), 6)
            memory_state = state.get("workflow_memory")
            self.assertEqual(memory_state["retrieval_calls"], 1)
            self.assertEqual(memory_state["curation_calls"], 1)
            self.assertEqual(
                memory_state["last_retrieval"]["planning_guidance"],
                ["保持任务图稀疏，并让确定性校验节点收口"],
            )
            stored = json.loads(store_path.read_text(encoding="utf-8"))
            self.assertEqual(len(stored["skills"]), 2)
            self.assertEqual(stored["skills"][1]["verified_outcome"], "success")
            agents = [call["agent"] for call in state.get("model_calls")["calls"]]
            self.assertEqual(agents.count("WorkflowMemoryAgent"), 2)
            self.assertLess(agents.index("WorkflowMemoryAgent"), agents.index("TaskPlanningAgent"))


if __name__ == "__main__":
    unittest.main()
