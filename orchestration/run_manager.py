import json
import shutil
from pathlib import Path
from datetime import datetime


def create_run_dir(base_dir="outputs/runs"):
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(base_dir) / run_id

    (run_dir / "inputs").mkdir(parents=True, exist_ok=True)
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    (run_dir / "review").mkdir(parents=True, exist_ok=True)

    return run_id, run_dir


def save_task_spec(task_spec: dict, run_dir: Path):
    path = run_dir / "task_spec.json"
    path.write_text(
        json.dumps(task_spec, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    return str(path)


def copy_inputs_to_run(task_spec: dict, run_dir: Path):
    input_dir = run_dir / "inputs"
    input_dir.mkdir(parents=True, exist_ok=True)

    copied_files = []

    for file_path in task_spec.get("input", {}).get("files", []):
        src = Path(file_path)
        if src.exists():
            dst = input_dir / src.name
            shutil.copy2(src, dst)
            copied_files.append(str(dst))

    task_spec["input"]["files"] = copied_files
    return task_spec


def init_run_state(run_dir: Path, task_type: str = "general_complex_task"):
    """在任务启动时写入初始 run_state.json。"""
    path = run_dir / "run_state.json"
    data = {
        "task_type": task_type,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "outcome": "unknown",
        "agents_called": [],
        "code_retry_count": 0,
        "fallback_triggered": False,
        "fallback_reason": "",
        "artifacts_found": [],
        "artifacts_missing": [],
        "final_report_generated": False,
        "final_report_source": "",
        "finished": False,
        "finished_at": None,
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(path)
