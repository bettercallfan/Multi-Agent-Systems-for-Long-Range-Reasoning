import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

import pandas as pd
from autogen_ext.models.replay import ReplayChatCompletionClient

from agents.code_agents import execute_code_file, save_generated_code
from orchestration.input_loader import load_input
from orchestration.run_state import RunState
from orchestration.schemas import CodeGenerationResult, ExecutionResult, ReviewDecision
from orchestration.task_normalizer import finalize_task_spec, normalize_to_task_spec
from orchestration.workflow import run_explicit_workflow
from task_plugins.expense_reimbursement import ExpenseReimbursementPlugin
from utils.file_reader import build_file_previews
from utils.output import save_agent_trace
from utils.review_artifacts import final_validation, pre_report_review


class TaskSpecTests(unittest.TestCase):
    def test_expense_task_has_single_authoritative_contract(self):
        raw = load_input("examples/expense_reimbursement")
        spec = normalize_to_task_spec(raw)
        spec = finalize_task_spec(spec, build_file_previews(spec["input"]["files"]))
        self.assertEqual(spec["task_type"], "document_analysis")
        self.assertEqual(spec["code_policy"]["mode"], "lightweight")
        self.assertIn("artifacts/expense_summary.json", spec["required_artifacts"])
        self.assertIn("artifacts/code_pipeline.py", spec["required_artifacts"])
        self.assertEqual(spec["plugin_id"], "expense_reimbursement")
        self.assertEqual(spec["artifact_contract"]["final_artifacts"], ["final_report.md"])
        self.assertIn("artifacts/expense_summary.json", spec["artifact_contract"]["intermediate_artifacts"])
        self.assertIn("run_state.json", spec["artifact_contract"]["framework_artifacts"])
        self.assertEqual(spec["required_report_sections"], [
            "输入文件清单", "出差信息摘要", "报销规则摘要",
            "报销明细汇总", "发现的问题", "后续处理建议",
        ])


class RunStateTests(unittest.TestCase):
    def test_artifact_missing_does_not_overwrite_upstream_execution_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = RunState(tmp)
            state.start_stage("execute")
            state.set_failure("execution_failure", "code", "execute", ["连接失败"])
            state.record_artifact_quality({
                "status": "failed",
                "checked_artifacts": ["artifacts/result.json"],
                "issues": ["缺少中间产物"],
                "failure_type": "artifact_missing",
                "repair_target": "code",
                "resume_stage": "execute",
            })
            self.assertEqual(state.get("failure")["failure_type"], "execution_failure")
            self.assertTrue(any(
                event["type"] == "derived_failure_recorded" for event in state.get("events")
            ))

    def test_real_execution_result_controls_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = RunState(directory, code_policy={"mode": "lightweight", "max_retries": 1})
            state.start_stage("classify")
            state.start_stage("plan")
            state.start_stage("execute")
            state.record_execution(ExecutionResult(
                attempt=1, command=["python", "x.py"], exit_code=1, stderr="boom"
            ))
            self.assertTrue(state.to_dict()["execution"]["failed"])
            self.assertEqual(state.to_dict()["execution"]["exit_code"], 1)


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_executor_uses_run_dir_without_cd(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "artifacts").mkdir()
            (run_dir / "artifacts" / "code_pipeline.py").write_text(
                "from pathlib import Path\n"
                "Path('artifacts/result.json').write_text('{\"ok\": true}', encoding='utf-8')\n",
                encoding="utf-8",
            )
            result = await execute_code_file(run_dir, "artifacts/code_pipeline.py", attempt=1)
            self.assertEqual(result.exit_code, 0)
            self.assertIn("artifacts/result.json", result.produced_files)


class CodePersistenceTests(unittest.TestCase):
    def test_save_generated_code_accepts_relative_run_dir(self):
        with tempfile.TemporaryDirectory(dir=".") as directory:
            run_dir = Path(directory).resolve()
            result = CodeGenerationResult(
                code="from pathlib import Path\nPath('artifacts/result.json').write_text('{}')",
                entrypoint="artifacts/code_pipeline.py",
                expected_outputs=["artifacts/result.json"],
            )
            relative_run_dir = run_dir.relative_to(Path.cwd())
            saved = save_generated_code(
                result, relative_run_dir, max_lines=160, allowed_outputs=["artifacts/result.json"]
            )
            self.assertEqual(saved, "artifacts/code_pipeline.py")
            self.assertTrue((run_dir / saved).is_file())

    def test_save_generated_code_rejects_invalid_or_framework_owned_output(self):
        with tempfile.TemporaryDirectory() as directory:
            invalid = CodeGenerationResult(
                code="import pd\nPath('agent_trace.md').write_text('bad')",
                entrypoint="artifacts/code_pipeline.py",
            )
            with self.assertRaisesRegex(ValueError, "框架专属文件"):
                save_generated_code(invalid, Path(directory), 160, ["artifacts/result.json"])

            syntax_error = CodeGenerationResult(
                code="def broken(:\n    pass",
                entrypoint="artifacts/code_pipeline.py",
            )
            with self.assertRaisesRegex(ValueError, "编译预检"):
                save_generated_code(syntax_error, Path(directory), 160, ["artifacts/result.json"])

            undeclared_dependency = CodeGenerationResult(
                code="from pypdf import PdfReader",
                entrypoint="artifacts/code_pipeline.py",
            )
            with self.assertRaisesRegex(ValueError, "未声明的 pypdf"):
                save_generated_code(undeclared_dependency, Path(directory), 160, ["artifacts/result.json"])


class TraceTests(unittest.TestCase):
    def test_trace_excludes_model_thought_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent_trace.md"
            messages = [
                SimpleNamespace(source="Agent", type="ThoughtEvent", content="private reasoning"),
                SimpleNamespace(source="Agent", type="TextMessage", content="final result"),
            ]
            save_agent_trace(messages, path)
            trace = path.read_text(encoding="utf-8")
            self.assertNotIn("private reasoning", trace)
            self.assertIn("final result", trace)


class ValidationTests(unittest.TestCase):
    def _spec(self, run_dir: Path):
        return {
            "run_dir": str(run_dir),
            "code_policy": {"mode": "lightweight"},
            "required_artifacts": [
                "artifacts/code_pipeline.py", "artifacts/result.json",
                "task_spec.json", "agent_trace.md", "final_report.md",
            ],
            "required_report_sections": ["任务理解", "结果分析"],
        }

    def test_nonzero_exit_can_only_be_partial_before_report(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "artifacts").mkdir()
            (run_dir / "artifacts" / "code_pipeline.py").write_text("pass\n", encoding="utf-8")
            (run_dir / "artifacts" / "result.json").write_text("{}", encoding="utf-8")
            (run_dir / "task_spec.json").write_text("{}", encoding="utf-8")
            (run_dir / "agent_trace.md").write_text("trace", encoding="utf-8")
            decision = pre_report_review(self._spec(run_dir), {"execution": {"exit_code": 1}})
            self.assertEqual(decision.status, "partial")
            self.assertTrue(decision.can_generate_final_report)

    def test_final_report_cannot_be_task_spec(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "artifacts").mkdir()
            for name, content in {
                "artifacts/code_pipeline.py": "pass\n",
                "artifacts/result.json": "{}",
                "task_spec.json": "{}",
                "agent_trace.md": "trace",
                "final_report.md": "# task_spec.json\n```json\n{}\n```" * 20,
            }.items():
                path = run_dir / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            decision = final_validation(self._spec(run_dir), {
                "execution": {"exit_code": 0},
                "review": {"status": "passed"},
            })
            self.assertEqual(decision.status, "failed")
            self.assertTrue(any("不是业务报告" in issue for issue in decision.issues))


class PluginValidationTests(unittest.TestCase):
    def test_expense_plugin_normalizes_side_by_side_summary_without_empty_rows(self):
        plugin = ExpenseReimbursementPlugin()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "inputs").mkdir()
            frame = pd.DataFrame([
                {"日期": pd.Timestamp("2026-06-01"), "费用类型": "高铁", "金额": 320,
                 "发票状态": "已提供", "备注": "去程", "汇总项": "总金额", "金额/数量": 320},
                {"日期": pd.NaT, "费用类型": None, "金额": None,
                 "发票状态": None, "备注": None, "汇总项": "未提供发票条数", "金额/数量": 0},
            ])
            frame.to_excel(run_dir / "inputs" / "expense.xlsx", index=False)
            prepared = plugin.prepare_inputs({}, run_dir)
            self.assertEqual(len(prepared["expense_rows"]), 1)
            self.assertEqual(prepared["expense_rows"][0]["日期"], "2026-06-01")
            self.assertEqual(len(prepared["provided_summary"]), 2)

    def test_expense_plugin_normalizes_fullwidth_colon_and_two_column_table(self):
        facts = ExpenseReimbursementPlugin._extract_travel_facts({
            "paragraphs": ["申请编号：TR-2026-0601", "申请人：张三"],
            "tables": [[
                ["员工姓名", "张三"],
                ["出差时间", "2026-06-01 至 2026-06-03"],
            ]],
        })
        self.assertEqual(facts, {
            "申请编号": "TR-2026-0601",
            "申请人": "张三",
            "出差开始": "2026-06-01",
            "出差结束": "2026-06-03",
        })

    def test_expense_plugin_normalizes_pdf_policy_line_breaks(self):
        rules = ExpenseReimbursementPlugin._extract_policy_rules(
            "一线城市住宿费不超过 500 元/晚；其他城市住宿费不超过 350\n元/晚。"
            "高铁二等座、动车二等座、飞机经济舱可报销。"
            "餐补标准为每人每天 100 元。所有报销项目必须提供合规发票。"
        )
        self.assertNotIn("缺失", rules.values())
        self.assertIn("350 元/晚", rules["住宿标准"])

    def _spec(self, run_dir: Path):
        return {
            "run_dir": str(run_dir),
            "artifact_contract": {
                "intermediate_artifacts": ["artifacts/expense_summary.json"],
                "final_artifacts": ["final_report.md"],
                "framework_artifacts": [],
            },
        }

    def test_expense_plugin_rejects_nan_and_inconsistent_totals(self):
        plugin = ExpenseReimbursementPlugin()
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "artifacts").mkdir()
            path = run_dir / "artifacts" / "expense_summary.json"
            path.write_text('{"summary":{"total_amount":NaN}}', encoding="utf-8")
            result = plugin.validate_intermediate(self._spec(run_dir), run_dir)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.failure_type, "artifact_json_invalid")

            invalid_total = {
                "trip_info": {},
                "policy_rules": {"发票要求": "必须提供"},
                "expense_records": [{
                    "date": "2026-06-01", "category": "住宿", "amount": 100.0,
                    "invoice_status": "已提供", "note": "", "issues": [],
                }],
                "summary": {
                    "total_amount": 200.0, "by_category": {"住宿": 100.0},
                    "missing_invoice_count": 0,
                },
                "issues": [],
            }
            path.write_text(json.dumps(invalid_total, ensure_ascii=False), encoding="utf-8")
            result = plugin.validate_intermediate(self._spec(run_dir), run_dir)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.resume_stage, "execute")
            self.assertTrue(any("明细合计" in issue for issue in result.issues))

    def test_expense_plugin_accepts_valid_artifact(self):
        plugin = ExpenseReimbursementPlugin()
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "artifacts").mkdir()
            artifact = {
                "trip_info": {"申请人": "张三"},
                "policy_rules": {"发票要求": "必须提供"},
                "expense_records": [{
                    "date": "2026-06-01", "category": "住宿", "amount": 100.0,
                    "invoice_status": "已提供", "note": "", "issues": [],
                }],
                "summary": {
                    "total_amount": 100.0, "by_category": {"住宿": 100.0},
                    "missing_invoice_count": 0,
                },
                "issues": [],
            }
            (run_dir / "artifacts" / "expense_summary.json").write_text(
                json.dumps(artifact, ensure_ascii=False), encoding="utf-8"
            )
            result = plugin.validate_intermediate(self._spec(run_dir), run_dir)
            self.assertEqual(result.status, "passed", result.issues)

    def test_expense_plugin_cross_checks_trip_and_row_issues(self):
        plugin = ExpenseReimbursementPlugin()
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "artifacts").mkdir()
            normalized = {
                "expense_rows": [{
                    "日期": "2026-06-04", "费用类型": "住宿", "金额": 480,
                    "发票状态": "未提供", "备注": "超出日期",
                }],
                "travel_application": {
                    "paragraphs": ["申请编号：TR-1", "申请人：张三"],
                    "tables": [[["员工姓名", "张三"], ["出差时间", "2026-06-01 至 2026-06-03"]]],
                },
                "policy_text": "住宿标准：一线城市不超过500元",
            }
            artifact = {
                "trip_info": {"申请编号": "", "申请人": "", "出差开始": "2026-06-01", "出差结束": ""},
                "policy_rules": {"住宿标准": "见文档说明"},
                "expense_records": [{
                    "date": "2026-06-04", "category": "住宿", "amount": 480,
                    "invoice_status": "未提供", "note": "", "issues": [],
                }],
                "summary": {"total_amount": 480, "by_category": {"住宿": 480}, "missing_invoice_count": 1},
                "issues": [],
            }
            (run_dir / "normalized_input.json").write_text(
                json.dumps(normalized, ensure_ascii=False), encoding="utf-8"
            )
            (run_dir / "artifacts" / "expense_summary.json").write_text(
                json.dumps(artifact, ensure_ascii=False), encoding="utf-8"
            )
            result = plugin.validate_intermediate(self._spec(run_dir), run_dir)
            self.assertEqual(result.status, "failed")
            self.assertTrue(any("trip_info.申请人" in issue for issue in result.issues))
            self.assertTrue(any("超出出差日期" in issue for issue in result.issues))
            self.assertTrue(any("未标记" in issue for issue in result.issues))


class WorkflowIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_workflow_reaches_success_and_reports_once(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "artifacts").mkdir()
            spec = {
                "task_id": "test_run",
                "task_name": "测试任务",
                "task_type": "document_analysis",
                "run_dir": str(run_dir),
                "input": {"type": "directory", "files": [], "text": ""},
                "file_previews": [],
                "code_policy": {
                    "mode": "lightweight", "allows_complex": False,
                    "allows_lightweight": True, "max_lines": 80, "max_retries": 1,
                },
                "required_artifacts": [
                    "artifacts/result.json", "artifacts/code_pipeline.py",
                    "task_spec.json", "agent_trace.md", "final_report.md",
                ],
                "required_report_sections": ["任务理解", "输入文件清单", "分析过程", "结果分析", "风险与改进建议", "最终交付物"],
                "success_criteria": {"required_artifacts": [], "required_report_sections": []},
                "artifacts": {
                    "code": "artifacts/code_pipeline.py",
                    "results": ["artifacts/result.json"],
                    "report": "final_report.md", "trace": "agent_trace.md",
                },
            }
            (run_dir / "task_spec.json").write_text(json.dumps(spec), encoding="utf-8")
            state = RunState(
                str(run_dir), task_type=spec["task_type"], code_policy=spec["code_policy"],
                required_artifacts=spec["required_artifacts"],
            )
            state.start_stage("classify")
            state.record_artifact("task_spec.json")

            plan = {
                "graph_id": "test_run_graph", "version": 1, "goal": "生成并验证结构化结果",
                "nodes": [
                    {
                        "node_id": "generate_result", "description": "生成并执行结果代码",
                        "capability": "code", "dependencies": [], "input_artifacts": [],
                        "output_artifacts": ["artifacts/result.json", "artifacts/code_pipeline.py"],
                        "success_criteria": [{"type": "execution_exit_code", "equals": 0}],
                        "max_retries": 0,
                    },
                    {
                        "node_id": "validate_result", "description": "确定性校验中间产物",
                        "capability": "artifact_validation", "dependencies": ["generate_result"],
                        "input_artifacts": ["artifacts/result.json"], "output_artifacts": [],
                        "success_criteria": [{"type": "artifact_quality", "equals": "passed"}],
                        "max_retries": 0,
                    },
                ],
            }
            code = {
                "language": "python",
                "code": "from pathlib import Path\nPath('artifacts/result.json').write_text('{\\\"value\\\": 1}', encoding='utf-8')",
                "entrypoint": "artifacts/code_pipeline.py",
                "expected_outputs": ["artifacts/result.json"], "notes": "",
            }
            review = {
                "blocking_issues": ["语义审查认为需要人工复核，但不拥有流程裁决权"],
                "advisory_issues": ["建议补充说明"],
                "evidence_references": ["artifacts/result.json:value"],
                "repair_recommended": True,
                "repair_target": "human",
            }
            report = """# 任务理解
这是一次用于验证显式工作流的业务测试，内容完全来源于框架提供的证据。
## 输入文件清单
本测试没有外部输入文件，使用内置的确定性测试数据完成验证。
## 分析过程
流程生成可复现脚本，执行后产生结构化 JSON，再进行报告前审查。
## 结果分析
结构化结果中的 value 为 1，脚本正常退出，所有必需产物均可读取。
## 风险与改进建议
生产环境仍应为模型超时、网络异常和无效 JSON 配置重试与监控。
## 最终交付物
本轮交付结构化结果、可复现脚本和业务报告，所有文件均已通过存在性检查。
"""
            client = ReplayChatCompletionClient([
                json.dumps(plan, ensure_ascii=False),
                json.dumps(code, ensure_ascii=False),
                json.dumps(review, ensure_ascii=False),
                report,
            ])
            decision, _ = await run_explicit_workflow(spec, state, client)
            self.assertEqual(decision.status, "passed", decision.issues)
            self.assertEqual(state.get("outcome"), "success")
            self.assertTrue((run_dir / "artifacts" / "code_pipeline.py").is_file())
            self.assertTrue((run_dir / "artifacts" / "result.json").is_file())
            self.assertTrue((run_dir / "final_report.md").read_text(encoding="utf-8").startswith("# 任务理解"))

    async def test_artifact_quality_failure_routes_back_to_code(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "artifacts").mkdir()
            spec = {
                "task_id": "retry_run", "task_name": "报销重试测试",
                "task_type": "document_analysis", "plugin_id": "expense_reimbursement",
                "run_dir": str(run_dir),
                "input": {"type": "directory", "files": [], "text": ""}, "file_previews": [],
                "code_policy": {"mode": "lightweight", "max_lines": 160, "max_retries": 1},
                "artifact_contract": {
                    "intermediate_artifacts": ["artifacts/expense_summary.json", "artifacts/code_pipeline.py"],
                    "final_artifacts": ["final_report.md"],
                    "framework_artifacts": ["task_spec.json", "agent_trace.md", "run_state.json"],
                },
                "required_artifacts": [
                    "artifacts/expense_summary.json", "artifacts/code_pipeline.py",
                    "final_report.md", "task_spec.json", "agent_trace.md", "run_state.json",
                ],
                "required_report_sections": ["任务理解", "结果分析"],
                "artifacts": {"code": "artifacts/code_pipeline.py", "results": ["artifacts/expense_summary.json"]},
            }
            (run_dir / "task_spec.json").write_text(json.dumps(spec), encoding="utf-8")
            state = RunState(
                str(run_dir), task_type=spec["task_type"], code_policy=spec["code_policy"],
                required_artifacts=spec["required_artifacts"],
            )
            state.start_stage("classify")
            state.configure(spec)

            plan = {
                "graph_id": "retry_run_graph", "version": 1, "goal": "生成并验证报销产物",
                "nodes": [
                    {
                        "node_id": "generate_expense", "description": "生成并执行报销处理代码",
                        "capability": "code", "dependencies": [], "input_artifacts": [],
                        "output_artifacts": [
                            "artifacts/expense_summary.json", "artifacts/code_pipeline.py"
                        ],
                        "success_criteria": [{"type": "execution_exit_code", "equals": 0}],
                        "max_retries": 0,
                    },
                    {
                        "node_id": "validate_expense", "description": "确定性校验报销产物",
                        "capability": "artifact_validation", "dependencies": ["generate_expense"],
                        "input_artifacts": ["artifacts/expense_summary.json"], "output_artifacts": [],
                        "success_criteria": [{"type": "artifact_quality", "equals": "passed"}],
                        "max_retries": 0,
                    },
                ],
            }
            invalid_code = {
                "language": "python",
                "code": "from pathlib import Path\nPath('artifacts/expense_summary.json').write_text('{\\\"summary\\\": {\\\"total_amount\\\": NaN}}')",
                "entrypoint": "artifacts/code_pipeline.py",
                "expected_outputs": ["artifacts/expense_summary.json"], "notes": "first attempt",
            }
            valid_artifact = {
                "trip_info": {"申请人": "张三"},
                "policy_rules": {"发票要求": "必须提供"},
                "expense_records": [{
                    "date": "2026-06-01", "category": "住宿", "amount": 100.0,
                    "invoice_status": "已提供", "note": "", "issues": [],
                }],
                "summary": {"total_amount": 100.0, "by_category": {"住宿": 100.0}, "missing_invoice_count": 0},
                "issues": [],
            }
            valid_code_text = (
                "import json\nfrom pathlib import Path\n"
                f"data = {valid_artifact!r}\n"
                "Path('artifacts/expense_summary.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')"
            )
            valid_code = {
                "language": "python", "code": valid_code_text,
                "entrypoint": "artifacts/code_pipeline.py",
                "expected_outputs": ["artifacts/expense_summary.json"], "notes": "repaired",
            }
            semantic_review = {
                "blocking_issues": [], "advisory_issues": [], "evidence_references": [],
                "repair_recommended": False, "repair_target": "none",
            }
            report = (
                "# 任务理解\n本报告验证产物失败后能返回代码阶段完成一次局部修复。\n"
                "框架使用固定状态机推进，所有业务事实均来自通过 Schema 校验的中间产物。\n"
                "## 结果分析\n修复后的结构化结果金额为100元，分类金额与明细合计完全一致。\n"
                "代码真实执行成功，产物通过标准 JSON、字段结构和数值不变量检查。\n"
                "该验证说明失败被精确路由到 execute 阶段，没有重新执行无关的准备与规划工作。\n"
                "最终报告仅消费经过框架验证的数据，因此不会把第一次失败产生的非法数值带入交付结果。\n"
            )
            client = ReplayChatCompletionClient([
                json.dumps(plan, ensure_ascii=False),
                json.dumps(invalid_code, ensure_ascii=False),
                json.dumps(valid_code, ensure_ascii=False),
                json.dumps(semantic_review, ensure_ascii=False),
                report,
            ])
            decision, _ = await run_explicit_workflow(spec, state, client)
            self.assertEqual(decision.status, "passed", decision.issues)
            current = state.to_dict()
            self.assertEqual(current["execution"]["attempts"], 2)
            self.assertEqual([item["status"] for item in current["artifact_quality"]["history"]], ["failed", "passed"])
            self.assertIsNone(current["failure"])
            event_types = [event["type"] for event in current["events"]]
            self.assertIn("retry_scheduled", event_types)
            self.assertIn("failure_cleared", event_types)


if __name__ == "__main__":
    unittest.main()
