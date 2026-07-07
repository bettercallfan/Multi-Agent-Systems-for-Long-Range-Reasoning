import json
from pathlib import Path
from datetime import datetime


class MemoryStore:
    def __init__(self, path="memory/memory.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {"runs": []}
            self.save()

        if "runs" not in self.data:
            self.data = {"runs": []}
            self.save()

    def save(self):
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    def _get_run(self, run_id):
        for run in self.data["runs"]:
            if run["run_id"] == run_id:
                return run
        raise ValueError(f"未找到 run_id: {run_id}")

    def start_run(self, run_id, task_name, task_type, task_spec_path):
        run = {
            "run_id": run_id,
            "task_name": task_name,
            "task_type": task_type,
            "task_spec_path": task_spec_path,
            "status": "running",
            "task_graph": [
                "INPUT: 读取用户输入",
                "NORMALIZE: 生成内部 TaskSpec",
                "PLAN: 任务理解与拆解",
                "CODE: 如需要则生成代码",
                "EXECUTE: 如需要则执行代码",
                "ERROR_ATTRIBUTION: 如失败则错误归因",
                "REVIEW: 审查真实产物",
                "REPORT: 生成最终报告"
            ],
            "progress": [],
            "errors": [],
            "artifacts": [],
            "review": {}
        }
        self.data["runs"].append(run)
        self.save()

    def add_progress(self, run_id, content):
        run = self._get_run(run_id)
        run["progress"].append({
            "time": datetime.now().isoformat(timespec="seconds"),
            "content": content
        })
        self.save()

    def add_artifact(self, run_id, path, description=""):
        run = self._get_run(run_id)
        existing = [x.get("path") for x in run["artifacts"]]

        if path not in existing:
            run["artifacts"].append({
                "path": path,
                "description": description,
                "time": datetime.now().isoformat(timespec="seconds")
            })

        self.save()

    def add_error(self, run_id, error_type, message, fix=""):
        run = self._get_run(run_id)
        run["errors"].append({
            "time": datetime.now().isoformat(timespec="seconds"),
            "error_type": error_type,
            "message": message,
            "fix": fix
        })
        self.save()

    def set_review(self, run_id, review_result):
        run = self._get_run(run_id)
        run["review"] = review_result
        self.save()

    def finish_run(self, run_id, status="completed"):
        run = self._get_run(run_id)
        run["status"] = status
        run["finished_at"] = datetime.now().isoformat(timespec="seconds")
        self.save()
