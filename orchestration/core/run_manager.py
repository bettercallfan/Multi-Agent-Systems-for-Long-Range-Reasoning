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
