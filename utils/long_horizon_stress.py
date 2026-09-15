"""Run an auditable 1000-step checkpoint and resume stress test."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from orchestration.core.run_state import RunState
from orchestration.core.workflow import (
    _execute_graph_in_horizons,
    load_horizon_checkpoint,
)
from orchestration.execution.capability_registry import CapabilityRegistry
from orchestration.execution.node_executor import (
    ExecutorDescriptor,
    NodeExecutionResult,
)
from orchestration.graph.task_graph import TaskGraph, TaskNode


GOAL = "preserve the immutable objective across 1000 resumable steps"


class StressExecutor:
    descriptor = ExecutorDescriptor(
        executor_id="long-horizon-stress",
        executor_type="deterministic_test",
        capabilities=["stress"],
    )

    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, context) -> NodeExecutionResult:
        self.executed.append(context.node.node_id)
        return NodeExecutionResult(
            node_id=context.node.node_id,
            executor_id=self.descriptor.executor_id,
            status="completed",
            summary=f"completed {context.node.node_id}",
            structured_output={"step": context.node.node_id},
        )


def build_chain(
    size: int,
    *,
    completed_prefix: TaskGraph | None = None,
    dependency_stride: int = 1,
) -> TaskGraph:
    completed = {
        node.node_id: node.model_copy(deep=True)
        for node in (completed_prefix.nodes if completed_prefix else [])
    }
    nodes = []
    for index in range(size):
        node_id = f"step_{index:04d}"
        nodes.append(completed.get(node_id) or TaskNode(
            node_id=node_id,
            description="execute one bounded deterministic step",
            capability="stress",
            dependencies=(
                [f"step_{index - dependency_stride:04d}"]
                if index >= dependency_stride else []
            ),
            success_criteria=[{"type": "node_result"}],
            max_retries=0,
        ))
    return TaskGraph(
        graph_id="long-horizon-1000",
        version=(completed_prefix.version + 1 if completed_prefix else 1),
        goal=GOAL,
        nodes=nodes,
    )


def _spec(total_steps: int, window_size: int) -> dict:
    return {
        "task_id": "long-horizon-1000-stress",
        "task_type": "general_complex_task",
        "required_artifacts": [],
        "success_criteria": {},
        "required_report_sections": [],
        "horizon_policy": {
            "mode": "progressive",
            "max_active_nodes": window_size,
            "max_epochs": total_steps + 10,
            "checkpoint_each_epoch": True,
            "persist_every_epochs": 5,
        },
        "recovery_policy": {
            "max_node_retries": 0,
            "max_executor_switches": 0,
            "max_replans": 0,
        },
    }


async def run_stress(
    run_dir: str | Path,
    *,
    total_steps: int = 1000,
    resume_after: int = 500,
    window_size: int = 25,
) -> dict:
    if not 0 < resume_after < total_steps:
        raise ValueError("resume_after must be between 1 and total_steps - 1")
    root = Path(run_dir)
    root.mkdir(parents=True, exist_ok=True)
    state = RunState(root)
    state.start_stage("classify")
    state.start_stage("plan")
    state.start_stage("execute")
    registry = CapabilityRegistry()
    executor = StressExecutor()
    registry.register(executor)
    spec = _spec(total_steps, window_size)
    started = time.perf_counter()

    first = await _execute_graph_in_horizons(
        task_graph=build_chain(
            resume_after, dependency_stride=window_size,
        ),
        task_spec=spec,
        registry=registry,
        run_state=state,
        run_dir=root,
        replan_callback=None,
        trace=[],
        validate_task_contract=False,
    )
    checkpoints = sorted((root / "checkpoints").glob("epoch-*.json"))
    resume_path = checkpoints[-1]
    restored_first, first_payload = load_horizon_checkpoint(
        root,
        checkpoint_ref=resume_path.relative_to(root).as_posix(),
        expected_goal=GOAL,
        expected_task_spec=spec,
    )

    async def expand(current: TaskGraph):
        if len(current.nodes) >= total_steps:
            return None
        return build_chain(
            total_steps,
            completed_prefix=current,
            dependency_stride=window_size,
        )

    final = await _execute_graph_in_horizons(
        task_graph=build_chain(total_steps, dependency_stride=window_size),
        task_spec=spec,
        registry=registry,
        run_state=state,
        run_dir=root,
        replan_callback=None,
        trace=[],
        validate_task_contract=False,
        expand_callback=expand,
        resume_checkpoint=resume_path.relative_to(root).as_posix(),
    )
    latest_checkpoint = sorted((root / "checkpoints").glob("epoch-*.json"))[-1]
    restored_final, final_payload = load_horizon_checkpoint(
        root,
        checkpoint_ref=latest_checkpoint.relative_to(root).as_posix(),
        expected_goal=GOAL,
        expected_task_spec=spec,
    )
    expected_order = [f"step_{index:04d}" for index in range(total_steps)]
    completed_count = sum(
        node.status.value == "completed" for node in final.nodes
    )
    horizon = state.get("horizon") or {}
    result = {
        "schema_version": "1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status": "passed" if (
            first.is_finished()
            and restored_first.is_finished()
            and final.is_finished()
            and restored_final.is_finished()
            and completed_count == total_steps
            and executor.executed == expected_order
            and final.goal == GOAL
            and first_payload.get("resume_supported") is True
            and final_payload.get("resume_supported") is True
        ) else "failed",
        "run_dir": root.as_posix(),
        "total_steps": total_steps,
        "resume_after_step": resume_after,
        "completed_steps": completed_count,
        "unique_executed_steps": len(set(executor.executed)),
        "duplicate_executions": len(executor.executed) - len(set(executor.executed)),
        "execution_order_preserved": executor.executed == expected_order,
        "goal_preserved": final.goal == GOAL == restored_final.goal,
        "resume_supported": bool(final_payload.get("resume_supported")),
        "checkpoint_integrity_algorithm": final_payload["integrity"]["algorithm"],
        "checkpoint_count": len(list((root / "checkpoints").glob("epoch-*.json"))),
        "horizon_epochs": int(horizon.get("epoch", 0)),
        "logical_event_steps": int(horizon.get("logical_step_count", 0)),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "resume_checkpoint": resume_path.relative_to(root).as_posix(),
        "final_checkpoint": latest_checkpoint.relative_to(root).as_posix(),
    }
    if result["status"] != "passed":
        raise AssertionError(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def render_markdown(result: dict) -> str:
    return "\n".join([
        "# 1000 步长程压力测试", "",
        f"- 状态：**{result['status'].upper()}**",
        f"- 完成步骤：{result['completed_steps']} / {result['total_steps']}",
        f"- 恢复点：第 {result['resume_after_step']} 步",
        f"- 重复执行：{result['duplicate_executions']}",
        f"- checkpoint 数量：{result['checkpoint_count']}",
        f"- checkpoint 完整性：{result['checkpoint_integrity_algorithm']}",
        f"- 目标保持：{result['goal_preserved']}",
        f"- 执行顺序保持：{result['execution_order_preserved']}",
        f"- 逻辑事件步数：{result['logical_event_steps']}",
        f"- 总耗时：{result['elapsed_seconds']} 秒", "",
        "该测试使用确定性执行器验证框架调度、checkpoint 和恢复能力，",
        "不代表进行了 1000 次真实大模型调用。", "",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", type=int, default=1000)
    parser.add_argument("--resume-after", type=int, default=500)
    parser.add_argument("--window-size", type=int, default=25)
    parser.add_argument("--run-dir")
    parser.add_argument("--output-dir", default="evidence/metrics")
    args = parser.parse_args()
    run_dir = args.run_dir or (
        "outputs/stress/" + datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    result = asyncio.run(run_stress(
        run_dir,
        total_steps=args.steps,
        resume_after=args.resume_after,
        window_size=args.window_size,
    ))
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "long_horizon_1000.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (output / "long_horizon_1000.md").write_text(
        render_markdown(result), encoding="utf-8",
    )
    print(output / "long_horizon_1000.md")
    print(f"Stress run: {result['run_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
