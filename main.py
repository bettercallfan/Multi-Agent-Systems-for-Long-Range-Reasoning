import argparse
import asyncio
from config import (
    create_file_surfer_model_client,
    create_model_client,
    create_web_surfer_model_client,
)
from orchestration.task.input_loader import load_input
from orchestration.core.run_manager import copy_inputs_to_run, create_run_dir, save_task_spec
from orchestration.core.run_state import RunState
from orchestration.task.task_normalizer import finalize_task_spec, normalize_to_task_spec
from orchestration.core.workflow import run_explicit_workflow
from utils.file_reader import build_file_previews, save_file_previews
from utils.injection_audit import write_injection_audit


def parse_args():
    parser = argparse.ArgumentParser(description="运行显式多智能体工作流")
    parser.add_argument("--input", type=str, required=True, help="输入文件或文件夹路径")
    parser.add_argument("--accept-runtime-injections", action="store_true",
                        help="允许演示台在运行中提交注入，在节点边界应用")
    parser.add_argument(
        "--llmlingua-device-map",
        choices=["cpu", "cuda"],
        help="覆盖本轮 TaskSpec 的 LLMLingua 设备；GPU 作业内可设为 cuda",
    )
    parser.add_argument(
        "--llmlingua-allow-download",
        action="store_true",
        help="显式允许本轮下载 LLMLingua 模型；默认只读取本地缓存",
    )
    parser.add_argument(
        "--injection-profile",
        choices=["recovery-demo"],
        help="启用预设恢复演示；recovery-demo 会依次注入节点失效、需求变更和数据异常",
    )
    parser.add_argument(
        "--inject",
        action="append",
        choices=["node-failure", "requirement-change", "data-anomaly"],
        default=[],
        help="启用一种一次性运行期注入；可重复指定",
    )
    parser.add_argument(
        "--requirement-change-text",
        default="新增运行期要求：保持原始全局目标，并在本节点输出中保留可审计证据。",
        help="需求变更注入应用到尚未执行节点的追加要求",
    )
    return parser.parse_args()


def apply_runtime_policy_overrides(task_spec: dict, args) -> dict:
    """Freeze explicit CLI deployment choices into the authoritative TaskSpec."""

    policy = task_spec.setdefault("communication_policy", {})
    if getattr(args, "accept_runtime_injections", False):
        task_spec["accept_runtime_injections"] = True
        recovery = task_spec.setdefault("recovery_policy", {})
        recovery["max_node_retries"] = max(1, int(recovery.get("max_node_retries", 1)))
    if getattr(args, "llmlingua_device_map", None):
        policy["llmlingua_device_map"] = args.llmlingua_device_map
    if getattr(args, "llmlingua_allow_download", False):
        policy["llmlingua_allow_download"] = True
    requested = list(getattr(args, "inject", []) or [])
    if getattr(args, "injection_profile", None) == "recovery-demo":
        requested = [
            "node-failure", "requirement-change", "data-anomaly", *requested,
        ]
    requested = list(dict.fromkeys(item.replace("-", "_") for item in requested))
    if requested:
        defaults = {
            "node_failure": 0,
            "requirement_change": 1,
            "data_anomaly": 1,
        }
        task_spec["runtime_injection_policy"] = {
            "enabled": True,
            "profile": getattr(args, "injection_profile", None) or "custom",
            "injections": [
                {
                    "injection_id": f"demo-{injection_type}",
                    "type": injection_type,
                    "after_completed_nodes": defaults[injection_type],
                    **({"change_text": getattr(
                        args, "requirement_change_text",
                        "新增运行期要求：保持原始全局目标，并在本节点输出中保留可审计证据。",
                    )} if injection_type == "requirement_change" else {}),
                }
                for injection_type in requested
            ],
        }
        recovery = task_spec.setdefault("recovery_policy", {})
        recovery["max_node_retries"] = max(
            1, int(recovery.get("max_node_retries", 1)),
        )
    return task_spec


async def main():
    args = parse_args()
    run_id, run_dir = create_run_dir()
    print(f"运行目录: {run_dir}", flush=True)

    # PREPARE: all persistence in this stage is owned by the framework.
    raw_input = load_input(args.input)
    task_spec = normalize_to_task_spec(raw_input)
    task_spec = apply_runtime_policy_overrides(task_spec, args)
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

    model_client = None
    file_model_client = None
    web_model_client = None
    injection_audit = None
    try:
        model_client = create_model_client()
        required_capabilities = set(
            task_spec.get("capability_contract", {}).get("required", [])
        )
        preferred_capabilities = set(
            task_spec.get("capability_contract", {}).get("preferred", [])
        )
        requested_external = required_capabilities | preferred_capabilities
        if requested_external & {"document_recovery", "file_navigation"}:
            file_model_client = create_file_surfer_model_client()
        if "web_research" in requested_external:
            web_model_client = create_web_surfer_model_client()

        await run_explicit_workflow(
            task_spec=task_spec,
            run_state=run_state,
            model_client=model_client,
            file_model_client=file_model_client,
            web_model_client=web_model_client,
        )
        if task_spec.get("runtime_injection_policy", {}).get("enabled"):
            injection_audit = write_injection_audit(run_dir)
            run_state.record_artifact("injections/recovery_trace.json")
            run_state.record_artifact("injections/recovery_trace.md")
    except Exception:
        if not run_state.get("finished", False):
            run_state.finish("failed")
        raise
    finally:
        if (
            web_model_client is not None
            and web_model_client is not model_client
            and web_model_client is not file_model_client
        ):
            await web_model_client.close()
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
    if injection_audit is not None:
        print(
            "注入恢复审计: "
            f"{'PASS' if injection_audit['passed'] else 'FAIL'} "
            f"({run_dir / 'injections' / 'recovery_trace.md'})"
        )


if __name__ == "__main__":
    asyncio.run(main())
