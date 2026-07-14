"""Code generation and deterministic framework-owned execution."""

from __future__ import annotations

import asyncio
import ast
from pathlib import Path
import re
import sys

from autogen_agentchat.agents import AssistantAgent

from orchestration.schemas import CodeGenerationResult, ExecutionResult


def create_code_modeling_agent(model_client, extra_context: str = ""):
    return AssistantAgent(
        name="CodeModelingAgent",
        description="只生成结构化 Python 源码，不保存、不执行、不调度。",
        model_client=model_client,
        system_message="""
你是代码生成组件。严格按照调用方给出的 JSON 契约返回 Python 源码。
你不能执行代码、写入文件、切换工作目录、修改运行状态或宣称任务完成。
代码只能使用相对路径 inputs/、artifacts/ 和根目录 normalized_input.json，
不得包含 cd、绝对路径或 outputs/runs/ 路径。任务插件要求标准化输入时，必须直接读取
normalized_input.json，不能改写为 inputs/normalized_input.json。
只能写入调用方列出的业务产物，绝不能写入 trace、报告、TaskSpec、RunState 或验收报告。
PDF 使用 pdfplumber；Pandas 使用 `import pandas as pd`。
""" + extra_context,
    )


def _safe_run_path(run_dir: Path, relative_path: str) -> Path:
    run_dir = run_dir.resolve()
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"不安全的产物路径: {relative_path}")
    resolved = (run_dir / path).resolve()
    if run_dir.resolve() not in resolved.parents:
        raise ValueError(f"产物路径越界: {relative_path}")
    return resolved


def _validate_code(code: str, max_lines: int, allowed_outputs: list[str]) -> None:
    """Reject invalid or framework-invasive code before it reaches the executor."""
    line_count = len(code.splitlines())
    if max_lines and line_count > max_lines:
        raise ValueError(f"生成代码 {line_count} 行，超过上限 {max_lines} 行")
    if "outputs/runs/" in code or "os.chdir(" in code:
        raise ValueError("生成代码包含禁止的运行目录路径或工作目录切换")
    if "inputs/normalized_input.json" in code:
        raise ValueError("标准化输入位于 run 根目录；请读取 normalized_input.json")

    framework_owned = {"agent_trace.md", "final_report.md", "task_spec.json", "run_state.json", "validation_report.md"}
    violations = sorted(name for name in framework_owned if name in code)
    if violations:
        raise ValueError(f"代码不得写入框架专属文件: {', '.join(violations)}")

    try:
        tree = ast.parse(code)
        compile(tree, "<generated_code>", "exec")
    except (SyntaxError, ValueError) as exc:
        raise ValueError(f"生成代码未通过 Python 编译预检: {exc}") from exc

    invalid_imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            invalid_imports.extend(alias.name for alias in node.names if alias.name == "pd")
            invalid_imports.extend(alias.name for alias in node.names if alias.name == "pypdf")
        elif isinstance(node, ast.ImportFrom) and node.module == "pypdf":
            invalid_imports.append("pypdf")
    if invalid_imports:
        if "pd" in invalid_imports:
            raise ValueError("生成代码包含无效导入 'import pd'；请使用 'import pandas as pd'")
        raise ValueError("生成代码依赖未声明的 pypdf；请使用项目已有的 pdfplumber")

    # The model may only create business outputs declared by TaskSpec. Framework
    # files are rejected above; this guard makes the contract visible in errors.
    artifact_paths = set(re.findall(r"artifacts/[\w./-]+", code))
    undeclared = sorted(path for path in artifact_paths if path not in set(allowed_outputs))
    if undeclared:
        raise ValueError(f"代码引用了未声明的业务产物路径: {', '.join(undeclared)}")


def save_generated_code(
    result: CodeGenerationResult,
    run_dir: Path,
    max_lines: int,
    allowed_outputs: list[str] | None = None,
) -> str:
    if result.language != "python":
        raise ValueError("当前框架只允许执行 Python")
    if not result.entrypoint.startswith("artifacts/") or not result.entrypoint.endswith(".py"):
        raise ValueError("代码入口必须是 artifacts/ 下的 .py 文件")
    allowed_outputs = list(allowed_outputs or [])
    _validate_code(result.code, max_lines, allowed_outputs)

    resolved_run_dir = run_dir.resolve()
    path = _safe_run_path(resolved_run_dir, result.entrypoint)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.code.rstrip() + "\n", encoding="utf-8")
    return path.relative_to(resolved_run_dir).as_posix()


def list_run_files(run_dir: Path) -> list[str]:
    run_dir = run_dir.resolve()
    return sorted(
        path.relative_to(run_dir).as_posix()
        for path in run_dir.rglob("*")
        if path.is_file() and "inputs" not in path.relative_to(run_dir).parts
    )


async def execute_code_file(
    run_dir: Path,
    relative_path: str,
    attempt: int,
    timeout_seconds: int = 180,
) -> ExecutionResult:
    """Execute one persisted script and return the real process result."""
    run_dir = run_dir.resolve()
    script_path = _safe_run_path(run_dir, relative_path)
    if not script_path.exists():
        return ExecutionResult(
            attempt=attempt,
            phase="execution",
            command=[sys.executable, relative_path],
            exit_code=127,
            stderr=f"代码文件不存在: {relative_path}",
            produced_files=list_run_files(run_dir),
        )

    command = [sys.executable, relative_path]
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=run_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        exit_code = int(process.returncode or 0)
    except asyncio.TimeoutError:
        process.kill()
        stdout, stderr = await process.communicate()
        exit_code = 124
        stderr += f"\n执行超过 {timeout_seconds} 秒，已终止".encode()

    return ExecutionResult(
        attempt=attempt,
        command=command,
        exit_code=exit_code,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        produced_files=list_run_files(run_dir),
    )


def create_code_agents(model_client, work_dir):
    """Compatibility shim: execution is now owned by ``execute_code_file``."""
    return create_code_modeling_agent(model_client), None
