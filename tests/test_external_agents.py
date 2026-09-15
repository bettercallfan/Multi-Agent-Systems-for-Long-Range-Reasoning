import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

from autogen_core import FunctionCall
from autogen_core.models import CreateResult, RequestUsage
from autogen_ext.models.replay import ReplayChatCompletionClient

from orchestration.execution.capability_registry import CapabilityRegistry
from orchestration.integrations.external_agent_executors import (
    FileSurferNodeExecutor,
    MarkItDownDocumentExecutor,
    WebSurferNodeExecutor,
)
from orchestration.graph.graph_planner import build_fallback_graph, validate_planned_graph
from orchestration.graph.graph_scheduler import GraphScheduler
from orchestration.execution.node_executor import ExecutorDescriptor, NodeExecutionResult
from orchestration.core.run_state import RunState
from orchestration.graph.task_graph import GraphValidationError, TaskGraph, TaskNode
from orchestration.task.task_normalizer import finalize_task_spec, normalize_to_task_spec
from orchestration.execution.default_executors import ArtifactValidationExecutor
from orchestration.core.workflow import run_explicit_workflow


MODEL_INFO = {
    "vision": False,
    "function_calling": True,
    "json_output": True,
    "structured_output": False,
    "family": "unknown",
}


def completion(content, finish_reason="stop"):
    return CreateResult(
        finish_reason=finish_reason,
        content=content,
        usage=RequestUsage(prompt_tokens=10, completion_tokens=5),
        cached=False,
    )


def tool_task_spec(run_dir, capability, input_file, *, text=""):
    return {
        "task_id": "external-tool-test",
        "task_name": "external tool integration",
        "task_type": "document_analysis",
        "run_dir": str(run_dir),
        "input": {"files": [str(input_file)], "text": text},
        "file_previews": [{
            "path": str(input_file),
            "type": input_file.suffix.lstrip("."),
            "status": "error" if capability in {"document_recovery", "document_conversion"} else "success",
            "text_preview": "",
        }],
        "code_policy": {"mode": "none", "max_retries": 0},
        "artifact_contract": {"intermediate_artifacts": []},
        "capability_contract": {"required": [capability], "preferred": [], "reasons": {}},
        "communication_policy": {"compressor": "deterministic"},
        "routing_policy": {"mode": "agent_prune_lite"},
        "team_policy": {"mode": "static", "early_stop_on_success": True},
        "recovery_policy": {
            "max_node_retries": 1,
            "max_executor_switches": 1,
            "max_replans": 0,
        },
        "external_tools_policy": {
            "enabled": True,
            "max_files_per_node": 4,
            "max_file_excerpt_chars": 4_000,
            "max_web_steps": 2,
            "web_timeout_seconds": 30,
            "web_read_only": True,
            "allow_web_downloads": False,
        },
    }


def tool_graph(capability):
    return TaskGraph(
        graph_id="external-tool-graph",
        goal="prove an existing capability component is really invoked",
        nodes=[
            TaskNode(
                node_id="use_external_tool",
                description="read the supplied evidence with the registered external component",
                capability=capability,
                input_artifacts=[],
                success_criteria=[{"type": "node_result"}],
                max_retries=1,
            ),
            TaskNode(
                node_id="consume_evidence",
                description="consume the external evidence before validation",
                capability="analysis",
                dependencies=["use_external_tool"],
                success_criteria=[{"type": "node_result"}],
                max_retries=0,
            ),
            TaskNode(
                node_id="validate_artifacts",
                description="validate generic artifacts",
                capability="artifact_validation",
                dependencies=["consume_evidence"],
                success_criteria=[{"type": "artifact_quality", "equals": "passed"}],
                max_retries=0,
            ),
        ],
    )


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


class EvidenceConsumerExecutor:
    descriptor = ExecutorDescriptor(
        executor_id="test_evidence_consumer",
        executor_type="test_executor",
        capabilities=["analysis"],
        quality_score=1.0,
        cost_score=0.0,
        latency_score=0.0,
    )

    async def execute(self, context):
        dependency = context.dependency_results.get("use_external_tool") or {}
        if not dependency.get("evidence_refs"):
            return NodeExecutionResult(
                node_id=context.node.node_id,
                executor_id=self.descriptor.executor_id,
                status="failed",
                error_type="external_evidence_missing",
                error_message="外部节点没有提供证据引用",
            )
        return NodeExecutionResult(
            node_id=context.node.node_id,
            executor_id=self.descriptor.executor_id,
            status="completed",
            summary="外部证据已进入下游分析节点",
            evidence_refs=list(dependency["evidence_refs"]),
            structured_output={"quality_signal": {"status": "passed"}},
        )


class CapabilityContractTests(unittest.TestCase):
    def test_task_spec_detects_real_file_and_web_capabilities(self):
        with tempfile.TemporaryDirectory() as directory:
            presentation = Path(directory) / "slides.pptx"
            presentation.write_bytes(b"not needed for classification")
            spec = normalize_to_task_spec({
                "input_type": "file",
                "files": [str(presentation)],
                "text": "请联网查阅官方网站的最新资料",
            })
            spec = finalize_task_spec(spec, [{
                "path": str(presentation), "type": "pptx", "status": "error",
                "text_preview": "", "error": "native preview unsupported",
            }])

        self.assertEqual(
            set(spec["capability_contract"]["required"]),
            {"document_conversion", "web_research", "evidence_verification"},
        )

    def test_long_document_requires_file_navigation_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "long.pdf"
            source.write_bytes(b"classification only")
            spec = normalize_to_task_spec({
                "input_type": "file", "files": [str(source)],
                "text": "请定位长文档中的关键原文",
            })
            spec = finalize_task_spec(spec, [{
                "path": str(source), "type": "pdf", "status": "success",
                "page_count": 24, "text_preview": "可读取的前十页内容",
            }])

        self.assertIn("file_navigation", spec["capability_contract"]["required"])
        self.assertNotIn("document_conversion", spec["capability_contract"]["required"])

    def test_graph_cannot_omit_required_external_capability(self):
        spec = {
            "task_id": "required-capability",
            "code_policy": {"mode": "none"},
            "artifact_contract": {"intermediate_artifacts": []},
            "capability_contract": {"required": ["web_research"]},
            "recovery_policy": {"max_node_retries": 1},
        }
        graph = TaskGraph(
            graph_id="invalid",
            goal="invalid graph",
            nodes=[
                TaskNode(
                    node_id="analysis",
                    description="analysis only",
                    capability="analysis",
                    success_criteria=[{"type": "node_result"}],
                ),
                TaskNode(
                    node_id="validation",
                    description="validate",
                    capability="artifact_validation",
                    dependencies=["analysis"],
                    success_criteria=[{"type": "artifact_quality", "equals": "passed"}],
                ),
            ],
        )
        with self.assertRaisesRegex(GraphValidationError, "web_research"):
            validate_planned_graph(
                graph.model_dump(mode="json"),
                spec,
                ["analysis", "web_research", "artifact_validation"],
            )

    def test_external_node_must_feed_a_semantic_consumer(self):
        spec = {
            "task_id": "no-capability-padding",
            "code_policy": {"mode": "none"},
            "artifact_contract": {"intermediate_artifacts": []},
            "capability_contract": {"required": ["web_research"]},
            "recovery_policy": {"max_node_retries": 1},
        }
        graph = TaskGraph(
            graph_id="padded",
            goal="external node exists but its evidence is ignored",
            nodes=[
                TaskNode(
                    node_id="browse",
                    description="browse but do not use the result",
                    capability="web_research",
                    success_criteria=[{"type": "node_result"}],
                ),
                TaskNode(
                    node_id="analysis",
                    description="unrelated analysis",
                    capability="analysis",
                    success_criteria=[{"type": "node_result"}],
                ),
                TaskNode(
                    node_id="validation",
                    description="join both branches only at validation",
                    capability="artifact_validation",
                    dependencies=["browse", "analysis"],
                    success_criteria=[
                        {"type": "artifact_quality", "equals": "passed"},
                    ],
                ),
            ],
        )

        with self.assertRaisesRegex(GraphValidationError, "冗余凑数"):
            validate_planned_graph(
                graph.model_dump(mode="json"),
                spec,
                ["analysis", "web_research", "artifact_validation"],
            )

    def test_fallback_graph_invokes_required_external_capabilities(self):
        spec = {
            "task_id": "fallback-external",
            "task_name": "external fallback",
            "input": {"files": ["inputs/a.pptx"]},
            "code_policy": {"mode": "none"},
            "artifact_contract": {"intermediate_artifacts": []},
            "capability_contract": {
                "required": ["document_conversion", "file_navigation", "web_research"],
            },
            "recovery_policy": {"max_node_retries": 1},
        }
        graph = build_fallback_graph(
            spec,
            [
                "analysis", "document_conversion", "file_navigation",
                "web_research", "artifact_validation",
            ],
            "planner unavailable",
        )

        capabilities = [node.capability for node in graph.nodes]
        self.assertIn("document_conversion", capabilities)
        self.assertIn("file_navigation", capabilities)
        self.assertIn("web_research", capabilities)
        self.assertEqual(
            set(graph.get_node("analyze_task").dependencies),
            {"recover_documents", "navigate_documents", "research_public_web"},
        )


class ExistingAgentExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, directory, spec, graph, executors):
        state = RunState(directory)
        state.start_stage("execute")
        registry = CapabilityRegistry(use_runtime_history=False)
        for executor in executors:
            registry.register(executor)
        registry.register(ArtifactValidationExecutor(state))
        completed = await GraphScheduler(
            registry,
            state,
            directory,
            validate_task_contract=True,
        ).run(graph, spec)
        return state, completed

    async def test_real_markitdown_library_is_invoked_by_scheduler(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "inputs").mkdir()
            source = run_dir / "inputs" / "evidence.html"
            source.write_text(
                "<html><body><h1>真实转换证据</h1><p>value=42</p></body></html>",
                encoding="utf-8",
            )
            spec = tool_task_spec(run_dir, "document_conversion", source)
            state = RunState(directory)
            state.start_stage("execute")
            executor = MarkItDownDocumentExecutor(state, [])
            registry = CapabilityRegistry(use_runtime_history=False)
            registry.register(executor)
            registry.register(EvidenceConsumerExecutor())
            registry.register(ArtifactValidationExecutor(state))
            completed = await GraphScheduler(
                registry, state, directory, validate_task_contract=True,
            ).run(tool_graph("document_conversion"), spec)

            result = completed.get_node("use_external_tool").result
            self.assertEqual(result["executor_id"], "markitdown_document_executor")
            self.assertIn("value=42", result["structured_output"]["documents"][0]["content_excerpt"])
            metrics = state.to_dict()["external_tools"]
            self.assertEqual(metrics["total_invocations"], 1)
            self.assertEqual(metrics["successful_invocations"], 1)
            self.assertEqual(metrics["failed_invocations"], 0)
            self.assertEqual(
                metrics["providers"]["microsoft.markitdown"]["successes"], 1,
            )

    async def test_explicit_workflow_plans_and_calls_real_document_executor(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "inputs").mkdir()
            source = run_dir / "inputs" / "evidence.html"
            source.write_text(
                "<html><body><h1>工作流真实证据</h1><p>framework value=42</p></body></html>",
                encoding="utf-8",
            )
            graph = {
                "graph_id": "external-workflow-graph",
                "version": 1,
                "goal": "转换、分析并验证输入",
                "nodes": [
                    {
                        "node_id": "recover",
                        "description": "调用真实文档组件恢复 HTML 内容",
                        "capability": "document_conversion",
                        "dependencies": [],
                        "input_artifacts": ["inputs/evidence.html"],
                        "output_artifacts": [],
                        "success_criteria": [{"type": "node_result"}],
                        "max_retries": 1,
                    },
                    {
                        "node_id": "analyze",
                        "description": "根据恢复结果分析任务事实",
                        "capability": "analysis",
                        "dependencies": ["recover"],
                        "dependency_fields": {
                            "recover": ["/structured_output/documents"]
                        },
                        "input_artifacts": [],
                        "output_artifacts": [],
                        "success_criteria": [{"type": "node_result"}],
                        "max_retries": 0,
                    },
                    {
                        "node_id": "validate",
                        "description": "确定性校验产物契约",
                        "capability": "artifact_validation",
                        "dependencies": ["analyze"],
                        "input_artifacts": [],
                        "output_artifacts": [],
                        "success_criteria": [{"type": "artifact_quality", "equals": "passed"}],
                        "max_retries": 0,
                    },
                ],
            }
            analysis = {
                "summary": "已根据真实转换结果确认 framework value 为 42。",
                "findings": ["输入文件中记录 framework value=42。"],
                "evidence_refs": ["normalized_input.json"],
                "risks": [],
                "confidence": 0.95,
            }
            semantic_review = {
                "blocking_issues": [],
                "advisory_issues": [],
                "evidence_references": [],
                "repair_recommended": False,
                "repair_target": "none",
            }
            report = (
                "# 任务理解\n本任务验证现成文档转换组件在显式任务图中被真实调用。\n"
                "框架负责能力路由、状态推进和最终验收，外部组件只处理被授权的输入文件。\n"
                "## 结果分析\nMarkItDown 已读取 HTML 文件并恢复其中的结构化文本。\n"
                "后续分析节点依据恢复结果确认 framework value 为 42，并保留了输入和转换证据引用。\n"
                "任务图、节点路由、工具调用事件和证据文件均由框架持久化，因此该结论可审计。\n"
            )
            client = ReplayChatCompletionClient([
                json.dumps(graph, ensure_ascii=False),
                json.dumps(analysis, ensure_ascii=False),
                json.dumps(semantic_review, ensure_ascii=False),
                report,
            ])
            spec = {
                "task_id": "external_workflow",
                "task_name": "现成组件工作流验证",
                "task_type": "document_analysis",
                "run_dir": str(run_dir),
                "input": {
                    "type": "file", "files": [str(source)],
                    "text": "分析本地 HTML 文件",
                },
                "file_previews": [{
                    "path": str(source), "type": "html", "status": "error",
                    "text_preview": "", "error": "native preview unsupported",
                }],
                "code_policy": {"mode": "none", "max_lines": 0, "max_retries": 0},
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
                "capability_contract": {
                    "required": ["document_conversion"], "preferred": [], "reasons": {},
                },
                "external_tools_policy": {
                    "enabled": True, "max_files_per_node": 4,
                    "max_file_excerpt_chars": 4_000, "max_web_steps": 2,
                    "web_timeout_seconds": 30, "web_read_only": True,
                    "allow_web_downloads": False,
                },
                "communication_policy": {"compressor": "deterministic"},
                "routing_policy": {"mode": "agent_prune_lite"},
                "team_policy": {
                    "mode": "dylan_lite", "use_runtime_history": False,
                    "early_stop_on_success": True,
                },
                "recovery_policy": {
                    "max_node_retries": 1, "max_executor_switches": 1,
                    "max_replans": 0,
                },
            }
            (run_dir / "task_spec.json").write_text(
                json.dumps(spec, ensure_ascii=False), encoding="utf-8",
            )
            state = RunState(
                str(run_dir), task_type=spec["task_type"],
                code_policy=spec["code_policy"],
                required_artifacts=spec["required_artifacts"],
            )
            state.start_stage("classify")
            state.configure(spec)

            decision, _ = await run_explicit_workflow(spec, state, client)

            self.assertEqual(decision.status, "passed", decision.issues)
            routing = [item["selected_executor_id"] for item in state.get("routing")]
            self.assertEqual(routing, [
                "markitdown_document_executor",
                "reasoning_agent",
                "artifact_validation_executor",
            ])
            self.assertEqual(state.get("external_tools")["total_invocations"], 1)
            self.assertEqual(state.get("external_tools")["successful_invocations"], 1)
            self.assertTrue(
                (run_dir / "artifacts/tool_results/recover/markitdown_invocation.json").is_file()
            )
            await client.close()

    async def test_real_autogen_file_surfer_open_path_is_invoked_by_scheduler(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "inputs").mkdir()
            source = run_dir / "inputs" / "evidence.txt"
            source.write_text("真实 FileSurfer 证据：编号 E-42。", encoding="utf-8")
            replay = ReplayChatCompletionClient([
                completion([
                    FunctionCall(
                        id="open-1",
                        name="open_path",
                        arguments=json.dumps({"path": "evidence.txt"}),
                    )
                ], "function_calls"),
            ], model_info=MODEL_INFO)
            spec = tool_task_spec(run_dir, "file_navigation", source)
            state = RunState(directory)
            state.start_stage("execute")
            executor = FileSurferNodeExecutor(replay, state, [])
            registry = CapabilityRegistry(use_runtime_history=False)
            registry.register(executor)
            registry.register(EvidenceConsumerExecutor())
            registry.register(ArtifactValidationExecutor(state))
            completed = await GraphScheduler(
                registry, state, directory, validate_task_contract=True,
            ).run(tool_graph("file_navigation"), spec)

            result = completed.get_node("use_external_tool").result
            self.assertEqual(result["executor_id"], "autogen_file_surfer")
            self.assertIn(
                "E-42",
                result["structured_output"]["results"][0]["content_excerpt"],
            )
            evidence = json.loads(
                (run_dir / "artifacts/tool_results/use_external_tool/file_surfer_invocation.json")
                .read_text(encoding="utf-8")
            )
            self.assertTrue(evidence["tool_invoked"])
            metrics = state.to_dict()["external_tools"]
            self.assertEqual(metrics["total_invocations"], 1)
            self.assertEqual(metrics["successful_invocations"], 1)
            self.assertIn("autogen_ext.agents.file_surfer.FileSurfer", metrics["providers"])
            await replay.close()

    async def test_explicit_workflow_plans_and_calls_real_web_surfer(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "inputs").mkdir()
            source = run_dir / "inputs" / "evidence.txt"
            source.write_text("web task", encoding="utf-8")
            page = run_dir / "public.html"
            page.write_text(
                "<html><head><title>Evidence</title></head>"
                "<body><h1>Public evidence</h1><p>verified value is 42</p></body></html>",
                encoding="utf-8",
            )
            handler = functools.partial(QuietHandler, directory=directory)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}/public.html"
            web_client = ReplayChatCompletionClient([
                completion([
                    FunctionCall(
                        id="visit-1",
                        name="visit_url",
                        arguments=json.dumps({"url": url}),
                    )
                ], "function_calls"),
                completion(f"网页事实：verified value is 42。来源 {url}"),
            ], model_info=MODEL_INFO)
            graph = {
                "graph_id": "web-workflow-graph",
                "version": 1,
                "goal": "获取公开网页证据、分析并验证交付物",
                "nodes": [
                    {
                        "node_id": "browse_public_source",
                        "description": f"访问公开网页 {url} 并提取可引用事实",
                        "capability": "web_research",
                        "dependencies": [],
                        "input_artifacts": [],
                        "output_artifacts": [],
                        "success_criteria": [{"type": "node_result"}],
                        "max_retries": 1,
                    },
                    {
                        "node_id": "analyze_web_evidence",
                        "description": "只根据上游网页证据形成分析结论",
                        "capability": "analysis",
                        "dependencies": ["browse_public_source"],
                        "dependency_fields": {
                            "browse_public_source": [
                                "/summary", "/structured_output/sources",
                            ],
                        },
                        "input_artifacts": [],
                        "output_artifacts": [],
                        "success_criteria": [{"type": "node_result"}],
                        "max_retries": 0,
                    },
                    {
                        "node_id": "validate",
                        "description": "确定性校验通用交付契约",
                        "capability": "artifact_validation",
                        "dependencies": ["analyze_web_evidence"],
                        "input_artifacts": [],
                        "output_artifacts": [],
                        "success_criteria": [
                            {"type": "artifact_quality", "equals": "passed"},
                        ],
                        "max_retries": 0,
                    },
                ],
            }
            analysis = {
                "summary": "公开网页证据显示 verified value 为 42。",
                "findings": ["页面正文明确记录 verified value is 42。"],
                "evidence_refs": [url],
                "risks": ["该测试只验证本轮公开页面内容。"],
                "confidence": 0.95,
            }
            semantic_review = {
                "blocking_issues": [],
                "advisory_issues": [],
                "evidence_references": [url],
                "repair_recommended": False,
                "repair_target": "none",
            }
            report = (
                "# 任务理解\n本任务需要通过受控浏览器读取公开网页，并基于真实来源形成可审计结论。\n"
                "## 结果分析\n公开页面显示 verified value 为 42。该事实由网页工具实际访问后取得，"
                f"来源为 {url}。框架保存了浏览动作、来源地址和节点路由，随后完成语义审查与最终验收。\n"
                "本轮没有根据任务文本直接臆测网页内容：任务图先建立 web_research 节点，浏览器组件实际"
                "执行访问动作并返回页面事实，分析节点只接收该直接依赖的摘要与来源。最终结论因此同时"
                "具备原始公开地址、结构化节点结果和框架持久化调用记录三类证据。\n"
                "该验证采用通用能力契约，不包含特定行业字段。其他任务只有在 TaskSpec 明确要求外部网页"
                "信息时才会生成同类节点；不需要联网的任务不会注册或调用浏览器组件，从而避免无效通信、"
                "不必要成本以及与任务无关的信息噪声。\n"
            )
            model_client = ReplayChatCompletionClient([
                json.dumps(graph, ensure_ascii=False),
                json.dumps(analysis, ensure_ascii=False),
                json.dumps(semantic_review, ensure_ascii=False),
                report,
            ])
            try:
                spec = {
                    "task_id": "web_workflow",
                    "task_name": "公开网页能力工作流验证",
                    "task_type": "general_complex_task",
                    "run_dir": str(run_dir),
                    "input": {
                        "type": "file", "files": [str(source)],
                        "text": f"请访问 {url} 获取公开证据",
                    },
                    "file_previews": [{
                        "path": str(source), "type": "txt", "status": "success",
                        "text_preview": "web task", "error": None,
                    }],
                    "code_policy": {"mode": "none", "max_lines": 0, "max_retries": 0},
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
                    "capability_contract": {
                        "required": ["web_research"], "preferred": [], "reasons": {},
                    },
                    "external_tools_policy": {
                        "enabled": True, "max_files_per_node": 4,
                        "max_file_excerpt_chars": 4_000, "max_web_steps": 2,
                        "web_timeout_seconds": 30, "web_read_only": True,
                        "allow_web_downloads": False,
                    },
                    "communication_policy": {"compressor": "deterministic"},
                    "routing_policy": {"mode": "agent_prune_lite"},
                    "team_policy": {
                        "mode": "dylan_lite", "use_runtime_history": False,
                        "early_stop_on_success": True,
                    },
                    "recovery_policy": {
                        "max_node_retries": 1, "max_executor_switches": 1,
                        "max_replans": 0,
                    },
                }
                (run_dir / "task_spec.json").write_text(
                    json.dumps(spec, ensure_ascii=False), encoding="utf-8",
                )
                state = RunState(
                    str(run_dir), task_type=spec["task_type"],
                    code_policy=spec["code_policy"],
                    required_artifacts=spec["required_artifacts"],
                )
                state.start_stage("classify")
                state.configure(spec)

                decision, _ = await run_explicit_workflow(
                    spec, state, model_client, web_model_client=web_client,
                )

                self.assertEqual(decision.status, "passed", decision.issues)
                routing = [item["selected_executor_id"] for item in state.get("routing")]
                self.assertEqual(routing, [
                    "autogen_multimodal_web_surfer",
                    "reasoning_agent",
                    "artifact_validation_executor",
                ])
                evidence = json.loads(
                    (run_dir / "artifacts/tool_results/browse_public_source/web_surfer_invocation.json")
                    .read_text(encoding="utf-8")
                )
                self.assertTrue(evidence["tool_invoked"])
                self.assertIn(url, evidence["sources"])
                self.assertTrue(evidence["read_only_enforced"])
                self.assertNotIn("input_text", evidence["exposed_tool_names"])
                metrics = state.to_dict()["external_tools"]
                self.assertEqual(metrics["total_invocations"], 1)
                self.assertEqual(metrics["successful_invocations"], 1)
                self.assertIn(
                    "autogen_ext.agents.web_surfer.MultimodalWebSurfer",
                    metrics["providers"],
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                await web_client.close()
                await model_client.close()


if __name__ == "__main__":
    unittest.main()
