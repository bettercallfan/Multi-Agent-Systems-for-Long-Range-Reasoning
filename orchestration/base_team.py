from autogen_agentchat.teams import MagenticOneGroupChat
from autogen_ext.agents.file_surfer import FileSurfer

from agents.planning_agent import create_planning_agent
from agents.research_agent import create_research_agent
from agents.reasoning_agent import create_reasoning_agent
from agents.error_agent import create_error_agent
from agents.code_agents import create_code_modeling_agent


def create_base_team(model_client, work_dir, file_model_client=None, run_context=None):
    """
    创建 MagenticOneGroupChat 团队。

    run_context: RunContext 实例，会被注入到各 Agent 的 system prompt 中。
    """

    extra_context = run_context.to_prompt_context() if run_context else ""

    planning_agent = create_planning_agent(model_client, extra_context=extra_context)
    research_agent = create_research_agent(model_client, extra_context=extra_context)

    file_surfer = FileSurfer(
        name="FileSurfer",
        model_client=file_model_client or model_client,
        base_path=str(work_dir),
    )

    code_modeling_agent = create_code_modeling_agent(model_client)

    reasoning_agent = create_reasoning_agent(model_client, extra_context=extra_context)
    error_agent = create_error_agent(model_client, extra_context=extra_context)

    return MagenticOneGroupChat(
        participants=[
            planning_agent,
            file_surfer,
            research_agent,
            code_modeling_agent,
            reasoning_agent,
            error_agent,
        ],
        model_client=model_client,
    )
