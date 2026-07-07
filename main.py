import argparse
import asyncio

from config import create_model_client, create_file_surfer_model_client
from orchestration.base_team import create_base_team
from orchestration.input_loader import load_input
from orchestration.task_normalizer import normalize_to_task_spec
from orchestration.prompt_builder import build_task_prompt
from orchestration.run_manager import create_run_dir, copy_inputs_to_run, save_task_spec
from orchestration.run_context import RunContext
from orchestration.run_state import RunState
from utils.output import get_final_report, save_agent_trace, save_final_report
from utils.review_artifacts import review_artifacts
from utils.file_reader import build_file_previews, save_file_previews
from memory.memory_store import MemoryStore


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        type=str,
        required=True,
        help="输入文件或文件夹路径，例如 examples/mathorcup_d"
    )
    return parser.parse_args()


async def main():
    args = parse_args()

    # 1. 创建 run 目录
    run_id, run_dir = create_run_dir()

    # 2. 读取输入 → 生成 TaskSpec
    raw_input = load_input(args.input)
    task_spec = normalize_to_task_spec(raw_input)
    task_spec["run_id"] = run_id
    task_spec["run_dir"] = str(run_dir)

    # 3. 复制输入文件 + 预读取
    task_spec = copy_inputs_to_run(task_spec, run_dir)
    file_previews = build_file_previews(task_spec["input"]["files"])
    task_spec["file_previews"] = file_previews
    save_file_previews(file_previews, run_dir / "file_previews.json")
    save_task_spec(task_spec, run_dir)

    # 4. 创建 RunContext（任务静态快照，注入 Agent prompt）
    run_context = RunContext(
        task_spec=task_spec,
        file_previews=file_previews,
        run_dir=str(run_dir),
    )

    # 5. 初始化 RunState（执行前轻量占位）
    run_state = RunState(
        run_dir=str(run_dir),
        task_type=task_spec["task_type"],
    )

    # 6. Memory
    memory = MemoryStore()
    memory.start_run(
        run_id=run_id,
        task_name=task_spec["task_name"],
        task_type=task_spec["task_type"],
        task_spec_path=str(run_dir / "task_spec.json")
    )
    memory.add_progress(run_id, f"创建运行目录: {run_dir}")
    memory.add_progress(run_id, "完成输入读取、TaskSpec 生成、RunContext 构建")

    # 7. 创建模型客户端和团队
    model_client = create_model_client()
    file_model_client = create_file_surfer_model_client()

    team = create_base_team(
        model_client=model_client,
        work_dir=run_dir,
        file_model_client=file_model_client,
        run_context=run_context,
    )

    # 8. 构建任务 prompt
    task_prompt = build_task_prompt(task_spec)

    # 9. 执行
    memory.add_progress(run_id, "开始多智能体执行")
    result = await team.run(task=task_prompt)

    # 10. 保存产物
    final_report = get_final_report(result.messages)

    trace_path = run_dir / "agent_trace.md"
    report_path = run_dir / "final_report.md"

    save_agent_trace(result.messages, str(trace_path))
    save_final_report(final_report, str(report_path))

    # 11. 执行后汇总 RunState（扫描 messages + 文件系统）
    run_state.summarize_from_execution(result.messages, run_dir)

    # 12. 通用审查（参考 run_state.json）
    review_result = review_artifacts(task_spec)

    # 12b. 审查后更新 RunState：outcome 必须与 validation_report 一致
    if review_result["status"] == "PASS":
        run_state.data["outcome"] = "success"
    elif review_result["status"] in ("PARTIAL", "PASS_ANALYSIS_ONLY"):
        run_state.data["outcome"] = "partial"
    elif review_result["status"] == "FAIL":
        run_state.data["outcome"] = "failed"
    run_state.data["review_status"] = review_result["status"]
    run_state._save()

    memory.add_artifact(run_id, str(trace_path), "本次运行的多智能体执行轨迹")
    memory.add_artifact(run_id, str(report_path), "本次运行的最终报告")
    memory.add_artifact(run_id, review_result["report_path"], "本次运行的通用产物审查报告")
    memory.set_review(run_id, review_result)
    memory.add_progress(
        run_id,
        f"产物审查完成，状态：{review_result['status']}，通过项：{review_result['passed']}/{review_result['total']}"
    )

    # 13. 收尾
    run_state.finish()
    memory.add_progress(run_id, "系统完成任务执行")
    memory.finish_run(run_id, status="completed")

    await model_client.close()
    if file_model_client is not model_client:
        await file_model_client.close()

    print(f"运行完成: {run_dir}")
    print(f"执行轨迹: {trace_path}")
    print(f"最终报告: {report_path}")
    print(f"运行状态: {run_state.path}")


if __name__ == "__main__":
    asyncio.run(main())
