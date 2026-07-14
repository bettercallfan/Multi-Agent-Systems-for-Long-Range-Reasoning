import argparse
import asyncio
from config import create_file_surfer_model_client, create_model_client
from memory.memory_store import MemoryStore
from orchestration.input_loader import load_input
from orchestration.run_context import RunContext
from orchestration.run_manager import copy_inputs_to_run, create_run_dir, save_task_spec
from orchestration.run_state import RunState
from orchestration.task_normalizer import finalize_task_spec, normalize_to_task_spec
from orchestration.workflow import run_explicit_workflow
from utils.file_reader import build_file_previews, save_file_previews


def parse_args():
    parser = argparse.ArgumentParser(description="运行显式多智能体工作流")
    parser.add_argument("--input", type=str, required=True, help="输入文件或文件夹路径")
    return parser.parse_args()


async def main():
    args = parse_args()
    run_id, run_dir = create_run_dir()

    # PREPARE: all persistence in this stage is owned by the framework.
    raw_input = load_input(args.input)
    task_spec = normalize_to_task_spec(raw_input)
    task_spec["run_id"] = run_id
    task_spec["run_dir"] = str(run_dir)
    task_spec = copy_inputs_to_run(task_spec, run_dir)

    run_state = RunState(
        run_dir=str(run_dir),
        task_type=task_spec["task_type"],
        code_policy=task_spec["code_policy"],
        required_artifacts=task_spec.get("required_artifacts", []),
    )

    # CLASSIFY: previews refine TaskSpec once; all later stages only read it.
    run_state.start_stage("classify")
    file_previews = build_file_previews(task_spec["input"]["files"])
    save_file_previews(file_previews, run_dir / "file_previews.json")
    task_spec = finalize_task_spec(task_spec, file_previews)
    save_task_spec(task_spec, run_dir)
    run_state.configure(task_spec)
    run_state.record_artifact("task_spec.json")
    run_state.record_artifact("file_previews.json")

    # Keep the static prompt context available to integrations, but workflow
    # authority remains in RunState and orchestration.workflow.
    RunContext(task_spec=task_spec, file_previews=file_previews, run_dir=str(run_dir))

    memory = MemoryStore()
    memory.start_run(
        run_id=run_id,
        task_name=task_spec["task_name"],
        task_type=task_spec["task_type"],
        task_spec_path=str(run_dir / "task_spec.json"),
    )
    memory.add_progress(run_id, "prepare/classify 完成，TaskSpec 已冻结")

    model_client = None
    file_model_client = None
    try:
        model_client = create_model_client()
        if any(preview.get("status") != "success" for preview in file_previews):
            file_model_client = create_file_surfer_model_client()

        decision, _trace = await run_explicit_workflow(
            task_spec=task_spec,
            run_state=run_state,
            model_client=model_client,
            file_model_client=file_model_client,
        )

        for relative in run_state.to_dict()["artifacts"]["produced"]:
            memory.add_artifact(run_id, str(run_dir / relative), "框架验证的运行产物")
        memory.set_review(run_id, decision.model_dump())
        memory.add_progress(run_id, f"最终验收完成：{decision.status}")
        memory.finish_run(run_id, status=run_state.get("outcome"))
    except Exception as exc:
        if not run_state.get("finished", False):
            run_state.finish("failed")
        memory.add_error(run_id, "workflow", str(exc), "查看 run_state.json 和 agent_trace.md")
        memory.finish_run(run_id, status="failed")
        raise
    finally:
        if file_model_client is not None and file_model_client is not model_client:
            await file_model_client.close()
        if model_client is not None:
            await model_client.close()

    print(f"运行完成: {run_dir}")
    print(f"最终状态: {run_state.get('outcome')}")
    print(f"执行轨迹: {run_dir / 'agent_trace.md'}")
    if (run_dir / "final_report.md").exists():
        print(f"最终报告: {run_dir / 'final_report.md'}")
    print(f"验收报告: {run_dir / 'review' / 'validation_report.md'}")


if __name__ == "__main__":
    asyncio.run(main())
