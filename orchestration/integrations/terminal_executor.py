"""Bounded adapter around AutoGen's real CodeExecutorAgent."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import time

from autogen_agentchat.agents import ApprovalRequest, ApprovalResponse, CodeExecutorAgent
from autogen_agentchat.messages import TextMessage
from autogen_core import CancellationToken
from autogen_ext.code_executors.local import LocalCommandLineCodeExecutor

from orchestration.execution.node_executor import (
    ExecutorDescriptor,
    NodeExecutionContext,
    NodeExecutionResult,
)
from orchestration.core.run_state import RunState
from utils.output import TraceEvent, content_to_text


class SandboxedTerminalNodeExecutor:
    """Run framework-constructed checks; never execute model-authored shell text."""

    descriptor = ExecutorDescriptor(
        executor_id="autogen_controlled_terminal",
        executor_type="external_agent",
        capabilities=["terminal_execution", "test_execution"],
        quality_score=0.98,
        cost_score=0.1,
        latency_score=0.25,
        resource_location="local",
        security_levels=["bounded_local"],
    )

    def __init__(self, run_state: RunState, trace: list) -> None:
        self.run_state = run_state
        self.trace = trace

    @staticmethod
    def _entrypoint(context: NodeExecutionContext) -> str:
        relative = str(
            context.task_spec.get("artifacts", {}).get("code")
            or "artifacts/code_pipeline.py"
        )
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or path.suffix.lower() != ".py":
            raise ValueError(f"不安全的 Python 入口路径: {relative}")
        absolute = (Path(context.run_dir) / path).resolve()
        root = Path(context.run_dir).resolve()
        if root not in absolute.parents or not absolute.is_file():
            raise ValueError(f"代码入口不存在或超出 run_dir: {relative}")
        return path.as_posix()

    async def execute(self, context: NodeExecutionContext) -> NodeExecutionResult:
        started = time.monotonic()
        policy = context.task_spec.get("terminal_policy", {})
        if policy.get("backend", "bounded_local") != "bounded_local":
            return NodeExecutionResult(
                node_id=context.node.node_id,
                executor_id=self.descriptor.executor_id,
                status="failed",
                error_type="terminal_backend_unavailable",
                error_message="当前适配器只启用 bounded_local；Docker 后端尚未配置",
            )
        if "python_compile" not in policy.get("allowed_checks", ["python_compile"]):
            return NodeExecutionResult(
                node_id=context.node.node_id,
                executor_id=self.descriptor.executor_id,
                status="failed",
                error_type="terminal_check_not_allowed",
                error_message="terminal_policy 没有授权 python_compile",
            )
        try:
            entrypoint = self._entrypoint(context)
        except ValueError as exc:
            return NodeExecutionResult(
                node_id=context.node.node_id,
                executor_id=self.descriptor.executor_id,
                status="failed",
                error_type="terminal_entrypoint_invalid",
                error_message=str(exc),
            )

        timeout = max(1, int(policy.get("timeout_seconds", 30)))
        allowed_checks = set(policy.get("allowed_checks", ["python_compile"]))
        execute_script = "python_execute" in allowed_checks
        command = (["python", "-m", "py_compile", entrypoint]
                   if not execute_script else
                   ["python", "-m", "py_compile", entrypoint, "&&", "python", entrypoint])
        # This shell block is entirely framework-generated. The marker retains
        # the real exit code because model_client=None returns only command output.
        shell = (
            f"python -m py_compile {shlex.quote(entrypoint)}\n"
            "status=$?\n"
            + (f"if [ \"$status\" -eq 0 ]; then python {shlex.quote(entrypoint)}; status=$?; fi\n"
               if execute_script else "")
            + "printf '__FRAMEWORK_EXIT_CODE__=%s\\n' \"$status\"\n"
            + "exit \"$status\""
        )

        def approve_framework_check(request: ApprovalRequest) -> ApprovalResponse:
            match = re.fullmatch(
                r"```sh\s*\n([\s\S]*?)```",
                request.code.strip(),
                re.IGNORECASE,
            )
            approved = bool(match and match.group(1).strip() == shell.strip())
            return ApprovalResponse(
                approved=approved,
                reason=(
                    "命令与框架生成的固定语法复核完全一致"
                    if approved else "拒绝执行非框架生成的终端命令"
                ),
            )

        code_executor = LocalCommandLineCodeExecutor(
            timeout=timeout,
            work_dir=Path(context.run_dir),
            cleanup_temp_files=True,
        )
        agent = CodeExecutorAgent(
            name="SandboxedTerminalAgent",
            code_executor=code_executor,
            model_client=None,
            sources=["framework"],
            approval_func=approve_framework_check,
        )
        output = ""
        exit_code = 70
        error: str | None = None
        preexisting_bytecode = set(Path(context.run_dir).rglob("*.pyc"))
        try:
            await code_executor.start()
            response = await agent.on_messages(
                [TextMessage(content=f"```sh\n{shell}\n```", source="framework")],
                CancellationToken(),
            )
            output = content_to_text(response.chat_message.content)
            marker = re.search(r"__FRAMEWORK_EXIT_CODE__=(\d+)", output)
            if marker is None:
                raise ValueError("CodeExecutorAgent 输出缺少真实退出码标记")
            exit_code = int(marker.group(1))
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                await code_executor.stop()
            except Exception as exc:
                if error is None:
                    error = f"终端执行器关闭失败: {type(exc).__name__}: {exc}"
            # ``py_compile`` creates bytecode as an implementation detail. It
            # is not a TaskSpec artifact, so remove only files created by this
            # check and leave every pre-existing file untouched.
            for bytecode in set(Path(context.run_dir).rglob("*.pyc")) - preexisting_bytecode:
                bytecode.unlink(missing_ok=True)
                parent = bytecode.parent
                if parent.name == "__pycache__":
                    try:
                        parent.rmdir()
                    except OSError:
                        pass

        passed = error is None and exit_code == 0
        record = {
            "provider": "autogen_agentchat.agents.CodeExecutorAgent",
            "node_id": context.node.node_id,
            "backend": "bounded_local",
            "framework_constructed_command": command,
            "exit_code": exit_code,
            "output": output[:12_000],
            "error": error,
            "passed": passed,
        }
        destination = (
            Path(context.run_dir) / "artifacts" / "tool_results"
            / context.node.node_id / "terminal_execution.json"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        record_ref = destination.relative_to(Path(context.run_dir)).as_posix()
        self.run_state.record_artifact(record_ref)
        self.run_state.record_external_tool_invocation({
            "provider": record["provider"],
            "node_id": context.node.node_id,
            "capability": context.node.capability,
            "tool_invoked": True,
            "success": passed,
            "backend": "bounded_local",
            "exit_code": exit_code,
            "evidence_ref": record_ref,
        })
        self.trace.append(TraceEvent("SandboxedTerminalAgent", {
            "node_id": context.node.node_id,
            "exit_code": exit_code,
            "evidence_ref": record_ref,
        }))
        return NodeExecutionResult(
            node_id=context.node.node_id,
            executor_id=self.descriptor.executor_id,
            status="completed" if passed else "failed",
            summary=(
                f"AutoGen CodeExecutorAgent 已通过编译复核: {entrypoint}"
                if passed else f"受控终端复核失败: {entrypoint}"
            ),
            structured_output={
                "execution_result": {
                    "command": command,
                    "exit_code": exit_code,
                    "stdout": output,
                    "stderr": error or "",
                },
                "quality_signal": {
                    "status": "passed" if passed else "failed",
                    "checks": ["real_code_executor_agent_invoked", "python_compile"]
                    + (["python_execute"] if execute_script else []),
                },
            },
            evidence_refs=[entrypoint, record_ref],
            error_type=None if passed else "terminal_execution_failure",
            error_message=None if passed else error or output,
            duration_seconds=time.monotonic() - started,
        )
