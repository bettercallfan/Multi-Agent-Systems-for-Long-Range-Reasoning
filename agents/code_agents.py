from pathlib import Path

from autogen_ext.agents.magentic_one import MagenticOneCoderAgent
from autogen_agentchat.agents import CodeExecutorAgent
from autogen_ext.code_executors.local import LocalCommandLineCodeExecutor


def create_code_agents(model_client, work_dir):
    """
    创建代码生成 Agent 和代码执行 Agent。
    work_dir 必须是当前 run 目录，例如 outputs/runs/20260623_152406。
    """

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    (work_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    (work_dir / "review").mkdir(parents=True, exist_ok=True)

    code_modeling_agent = MagenticOneCoderAgent(
        name="CodeModelingAgent",
        model_client=model_client,
    )

    code_executor_agent = CodeExecutorAgent(
        name="CodeExecutorAgent",
        description=(
            "负责在当前 run 目录下执行 CodeModelingAgent 生成的代码块，"
            "并返回 stdout、stderr、exit_code 和错误信息。"
        ),
        code_executor=LocalCommandLineCodeExecutor(work_dir=work_dir),
        sources=["CodeModelingAgent"],
        supported_languages=["python", "bash", "sh"],
    )

    return code_modeling_agent, code_executor_agent
