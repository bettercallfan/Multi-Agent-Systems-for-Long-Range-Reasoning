import json
from pathlib import Path
from datetime import datetime


class RunState:
    """
    轻量级运行后状态记录。

    不要求 Agent 写文件，只在执行结束后由 main.py 根据
    result.messages 和真实产物做一次状态汇总。
    """

    def __init__(self, run_dir: str, task_type: str = "general_complex_task"):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "run_state.json"

        self.data = {
            "task_type": task_type,
            "started_at": datetime.now().isoformat(timespec="seconds"),

            # 哪些 Agent 被调用了（从 messages 提取）
            "agents_called": [],

            # 代码执行相关
            "code_execution_attempts": 0,
            "code_execution_failures": 0,
            "code_retry_count": 0,
            "max_code_retries": 3,

            # 错误摘要
            "errors": [],

            # 降级
            "fallback_triggered": False,
            "fallback_reason": "",

            # 产物
            "artifacts_found": [],
            "artifacts_missing": [],

            # 是否有最终报告（ReportAgent 生成的内容，非 CodeModelingAgent 输出）
            "final_report_generated": False,
            "final_report_source": "",

            # 综合判断
            "outcome": "unknown",
            # "success": 代码成功执行 + 报告由 ReportAgent 生成
            # "partial": 有产物但代码未完美执行 / 报告非 ReportAgent 生成
            # "fallback": 触发了降级机制
            # "failed": 关键产物缺失

            "finished": False,
            "finished_at": None,
        }

        self._save()

    def _save(self):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # --- 读取 ---

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def to_dict(self) -> dict:
        return dict(self.data)

    # --- 写入（只在 main.py 中调用） ---

    def set_stage(self, stage: str):
        self.data["current_stage"] = stage

    def summarize_from_execution(self, messages, run_dir: Path):
        """
        执行结束后，扫描 result.messages 和实际文件系统，汇总运行状态。
        """

        # 1. 提取调用了哪些 Agent
        seen_agents = []
        for msg in messages:
            source = getattr(msg, "source", "unknown")
            if source not in ("unknown", "user", "MagenticOneOrchestrator"):
                if source not in seen_agents:
                    seen_agents.append(source)
        self.data["agents_called"] = seen_agents

        # 2. 统计代码执行情况
        # CodeExecutorAgent 输出 TextMessage，不是 CodeExecutionEvent
        code_exec_attempts = 0
        for m in messages:
            source = getattr(m, "source", "")
            msg_type = getattr(m, "type", m.__class__.__name__)
            if source == "CodeExecutorAgent" or msg_type == "CodeExecutionEvent":
                code_exec_attempts += 1

        # 从 CodeExecutorAgent 的 TextMessage 内容判断成功/失败
        code_failures = 0
        error_summaries = []
        for m in messages:
            source = getattr(m, "source", "")
            if source == "CodeExecutorAgent":
                content = getattr(m, "content", "")
                if isinstance(content, str):
                    has_error = False
                    if "exit_code" in content:
                        for line in content.split("\n"):
                            line_lower = line.lower()
                            if "exit_code" in line_lower:
                                # exit_code: 0 表示成功
                                code_str = line.split(":")[-1].strip()
                                try:
                                    if int(code_str) != 0:
                                        has_error = True
                                except ValueError:
                                    pass
                            if "error" in line_lower or "traceback" in line_lower:
                                has_error = True
                    if has_error:
                        code_failures += 1
            if source == "ErrorAttributionAgent":
                content = getattr(m, "content", "")
                if isinstance(content, str) and len(content) > 20:
                    error_summaries.append(content[:120])

        self.data["code_execution_attempts"] = code_exec_attempts
        self.data["code_execution_failures"] = code_failures
        self.data["code_retry_count"] = max(0, code_exec_attempts - 1)  # 第一次不算重试
        self.data["errors"] = error_summaries

        # 3. 检测降级
        if self.data["code_retry_count"] >= self.data["max_code_retries"]:
            self.data["fallback_triggered"] = True
            self.data["fallback_reason"] = f"代码重试 {self.data['code_retry_count']} 轮，达到上限"
        elif error_summaries and self.data["code_retry_count"] >= 2:
            self.data["fallback_triggered"] = True
            self.data["fallback_reason"] = f"代码多轮失败后继续执行"

        # 4. 扫描真实产物
        artifacts_dir = run_dir / "artifacts"
        if artifacts_dir.exists():
            for f in artifacts_dir.iterdir():
                if f.is_file():
                    self.data["artifacts_found"].append(f.name)

        # 检查预期产物哪些缺失
        expected = [
            "final_report.md", "agent_trace.md", "task_spec.json"
        ]
        for name in expected:
            if not (run_dir / name).exists():
                self.data["artifacts_missing"].append(name)

        # 5. 判断最终报告是否由 ReportAgent 生成
        report_source = ""
        report_content = ""
        for msg in reversed(messages):
            source = getattr(msg, "source", "")
            content = getattr(msg, "content", "")
            if source == "ReportAgent" and isinstance(content, str) and len(content) > 100:
                report_source = "ReportAgent"
                report_content = content
                break

        if not report_source:
            # ReportAgent 没被调用，看最后一个 agent 的输出
            for msg in reversed(messages):
                source = getattr(msg, "source", "")
                content = getattr(msg, "content", "")
                if source not in ("user", "MagenticOneOrchestrator") and isinstance(content, str) and len(content) > 100:
                    report_source = source
                    report_content = content
                    break

        self.data["final_report_generated"] = bool(report_content)
        self.data["final_report_source"] = report_source

        # 6. 综合判断 outcome
        if not self.data["final_report_generated"]:
            self.data["outcome"] = "failed"
        elif self.data["fallback_triggered"]:
            self.data["outcome"] = "fallback"
        elif report_source != "ReportAgent":
            self.data["outcome"] = "partial"
        elif code_failures > 0 and self.data["code_retry_count"] >= 2:
            self.data["outcome"] = "partial"
        else:
            self.data["outcome"] = "success"

        self._save()

    def finish(self):
        self.data["finished"] = True
        self.data["finished_at"] = datetime.now().isoformat(timespec="seconds")
        self._save()
