"""Serve a read-only local dashboard for persisted workflow runs."""

from __future__ import annotations

import argparse
import hmac
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
from threading import Lock
from datetime import datetime
from urllib.parse import quote, unquote, urlparse

try:
    from utils.runtime_injection_queue import enqueue_injection, read_injection_queue
except ModuleNotFoundError:
    from runtime_injection_queue import enqueue_injection, read_injection_queue

try:
    from utils.run_audit import audit_run
except ModuleNotFoundError:  # Direct execution: python utils/dashboard_server.py
    from run_audit import audit_run


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def _summary(run_dir: Path) -> dict:
    state = _load(run_dir / "run_state.json", {})
    spec = _load(run_dir / "task_spec.json", {})
    audit = audit_run(run_dir) if (run_dir / "run_state.json").is_file() else {
        "passed": False, "checks": {}, "failed_nodes": [],
        "unresolved_blocking_issues": [], "review_issues": [],
    }
    nodes = state.get("nodes") or {}
    status_counts = {}
    for node in nodes.values():
        status = str(node.get("status", "unknown"))
        status_counts[status] = status_counts.get(status, 0) + 1
    model = state.get("model_calls") or {}
    plan = state.get("plan") or {}
    recovery = state.get("recovery") or {}
    repairs = recovery.get("business_repairs") or {}
    return {
        "run_id": run_dir.name,
        "task_name": spec.get("task_name", run_dir.name),
        "task_type": spec.get("task_type", ""),
        "outcome": state.get("outcome", "unknown"),
        "finished": bool(state.get("finished")),
        "audit_passed": bool(audit.get("passed")),
        "updated_at": state.get("updated_at") or state.get("finished_at") or "",
        "node_count": len(nodes),
        "status_counts": status_counts,
        "model_calls": model.get("count", 0),
        "total_tokens": model.get("total_tokens", 0),
        "topology_density": plan.get("topology_density", 0),
        "repair_rounds": repairs.get("rounds_used", 0),
        "logical_steps": (state.get("horizon") or {}).get("logical_step_count", 0),
    }


def _detail(run_dir: Path) -> dict:
    state = _load(run_dir / "run_state.json", {})
    spec = _load(run_dir / "task_spec.json", {})
    graph = _load(run_dir / "task_graph.json", {})
    audit = audit_run(run_dir)
    nodes = []
    graph_nodes = graph.get("nodes") or []
    state_nodes = state.get("nodes") or {}
    if isinstance(graph_nodes, list):
        for node in graph_nodes:
            item = dict(node)
            runtime = state_nodes.get(item.get("node_id"), {})
            item["status"] = runtime.get("status", item.get("status", "unknown"))
            item["attempts"] = runtime.get("attempts", item.get("attempts", 0))
            item["executor_id"] = runtime.get("executor_id", item.get("assigned_executor", ""))
            nodes.append({key: item.get(key) for key in (
                "node_id", "description", "capability", "dependencies", "status",
                "attempts", "executor_id", "output_artifacts",
            )})
    artifact_contract = spec.get("artifact_contract") or {}
    produced = (state.get("artifacts") or {}).get("produced") or []
    return {
        "graph_version": graph.get("version"),
        "injection_audit": _load(run_dir / "injections/recovery_trace.json", {}),
        "injection_queue": read_injection_queue(run_dir),
        "accept_runtime_injections": bool(spec.get("accept_runtime_injections")),
        "input_files": [Path(item).name for item in spec.get("input", {}).get("files", [])],
        "injection_evidence": {
            key: _load(run_dir / "injections" / key / "evidence.json", {})
            for key in (state.get("runtime_injections", {}).get("items", {}))
            if re.fullmatch(r"[A-Za-z0-9_-]{1,100}", key)
        },
        "summary": _summary(run_dir),
        "audit": audit,
        "task_spec": {
            "task_name": spec.get("task_name", ""),
            "task_type": spec.get("task_type", ""),
            "goal": graph.get("goal", ""),
            "required_artifacts": spec.get("required_artifacts", []),
            "intermediate_artifacts": artifact_contract.get("intermediate_artifacts", []),
        },
        "nodes": nodes,
        "artifacts": {
            "produced": produced,
            "required": state.get("artifacts", {}).get("required", []),
            "missing": state.get("artifacts", {}).get("missing", []),
        },
        "metrics": {
            "model_calls": state.get("model_calls", {}),
            "communication": state.get("communication", {}),
            "execution": state.get("execution", {}),
            "recovery": state.get("recovery", {}),
            "horizon": state.get("horizon", {}),
        },
        "review": state.get("review", {}),
        "business_validation": state.get("business_validation", {}),
        "requirement_acceptance": state.get("requirement_acceptance", {}),
        "routing": state.get("routing", []),
        "runtime_memory": state.get("runtime_memory", {}),
        "runtime_injections": state.get("runtime_injections", {}),
        "events": state.get("events", []),
    }


def _demo_payload(project_root: Path, runs_root: Path) -> dict:
    metrics = _load(project_root / "evidence/metrics/cross_scenario_metrics.json", {})
    long_horizon = _load(project_root / "evidence/metrics/long_horizon_1000.json", {})
    business_long_horizon = _load(
        project_root / "evidence/metrics/urban_business_1000.json", {}
    )
    mathorcup_long_horizon = _load(
        project_root / "evidence/metrics/mathorcup_business_1000.json", {}
    )
    recovery = _load(project_root / "evidence/fault_recovery_city.json", {})
    official_runs = []
    for item in metrics.get("runs", []):
        run_id = str(item.get("run_id", ""))
        run_dir = runs_root / run_id
        if run_id and (run_dir / "run_state.json").is_file():
            official_runs.append({**item, "detail": _detail(run_dir)})
    return {
        "schema_version": "1.0",
        "official_runs": official_runs,
        "metrics": metrics,
        "long_horizon": long_horizon,
        "business_long_horizon": business_long_horizon,
        "mathorcup_long_horizon": mathorcup_long_horizon,
        "recovery": recovery.get("urban_fault_recovery", {}),
        "limitations": recovery.get("limitations", []),
        "regression_tests": 287,
    }


def _artifact_preview(run_dir: Path, relative_path: str) -> dict:
    if not relative_path or Path(relative_path).is_absolute():
        raise ValueError("invalid artifact path")
    target = (run_dir / relative_path).resolve()
    try:
        target.relative_to(run_dir.resolve())
    except ValueError as exc:
        raise ValueError("invalid artifact path") from exc
    if not target.is_file():
        raise FileNotFoundError(relative_path)
    allowed = {".json", ".md", ".txt", ".py", ".csv"}
    if target.suffix.lower() not in allowed:
        raise ValueError("artifact type is not previewable")
    limit = 120_000
    content = target.read_text(encoding="utf-8", errors="replace")
    return {
        "run_id": run_dir.name,
        "path": target.relative_to(run_dir).as_posix(),
        "content": content[:limit],
        "truncated": len(content) > limit,
        "size": target.stat().st_size,
    }


SCENARIOS = {
    "mathorcup": {"label": "MathorCup 装箱优化", "input": "examples/mathorcup_d"},
    "expense": {"label": "差旅报销审核", "input": "examples/expense_reimbursement"},
    "urban": {"label": "城市多模态治理", "input": "城市多模态数据集"},
    "urban_long": {
        "label": "城市多模态千步长程任务",
        "input": "城市多模态数据集",
        "command": ["utils/urban_long_horizon.py", "--live-reasoning"],
    },
    "mathorcup_long": {
        "label": "MathorCup 千步优化任务",
        "input": "examples/mathorcup_d",
        "command": ["utils/mathorcup_long_horizon.py", "--live-reasoning"],
    },
    "recovery_demo": {
        "label": "三类动态注入恢复演示",
        "input": "examples/expense_reimbursement",
        "command": [
            "main.py", "--input", "examples/expense_reimbursement",
            "--injection-profile", "recovery-demo",
        ],
    },
    "interactive_demo": {
        "label": "交互式扰动演示", "input": "examples/expense_reimbursement",
        "command": ["main.py", "--input", "examples/expense_reimbursement",
                    "--accept-runtime-injections"],
    },
}
RUN_DIR_PATTERN = re.compile(r"(?:运行目录|运行完成):\s*(outputs/runs/[A-Za-z0-9_-]+)")


def _runtime_preflight(project_root: Path, enabled: bool, active_jobs: int) -> dict:
    checks = {
        "model_api_key": bool(os.environ.get("MODEL_API_KEY")),
        "model_base_url": bool(os.environ.get("MODEL_BASE_URL")),
        "model_name": bool(os.environ.get("MODEL_NAME")),
        "outputs_writable": os.access(project_root / "outputs", os.W_OK),
        "single_job_available": active_jobs == 0,
    }
    scenarios = []
    for scenario_id, config in SCENARIOS.items():
        path = project_root / config["input"]
        scenarios.append({
            "id": scenario_id,
            "label": config["label"],
            "input": config["input"],
            "available": path.is_dir(),
            "file_count": sum(1 for item in path.rglob("*") if item.is_file()) if path.is_dir() else 0,
        })
    return {
        "execution_enabled": enabled,
        "ready": enabled and all(checks.values()) and all(item["available"] for item in scenarios),
        "checks": checks,
        "model_name": os.environ.get("MODEL_NAME", "未配置"),
        "scenarios": scenarios,
        "active_jobs": active_jobs,
    }


class DashboardHandler(SimpleHTTPRequestHandler):
    runs_root: Path
    dashboard_root: Path
    project_root: Path
    enable_execution: bool = False
    jobs: dict = {}
    processes: dict = {}
    jobs_lock = Lock()
    launch_lock = Lock()
    execution_token: str = ""

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not self.execution_token:
            return False
        provided = self.headers.get("X-Dashboard-Token", "")
        return hmac.compare_digest(provided, self.execution_token)

    @staticmethod
    def _public_job(job: dict) -> dict:
        return {key: value for key, value in job.items() if not key.startswith("_")}

    def _refresh_job(self, job_id: str) -> dict | None:
        with self.jobs_lock:
            job = self.jobs.get(job_id)
            process = self.processes.get(job_id)
            if job is None:
                return None
            log_path = self.project_root / job["log_path"]
            log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
            match = RUN_DIR_PATTERN.search(log_text)
            if match and not job.get("run_dir"):
                job["run_dir"] = match.group(1)
            if job.get("run_dir"):
                run_dir = self.project_root / job["run_dir"]
                state = _load(run_dir / "run_state.json", {})
                job["stage"] = state.get("stage", job.get("stage", "running"))
                job["current_nodes"] = [
                    node_id for node_id, node in (state.get("nodes") or {}).items()
                    if node.get("status") == "running"
                ]
                ledgers = (
                    run_dir / "artifacts/work_unit_ledger.jsonl",
                    run_dir / "artifacts/candidate_evaluation_ledger.jsonl",
                )
                ledger = next((path for path in ledgers if path.is_file()), ledgers[0])
                if not state and ledger.is_file():
                    completed = sum(1 for line in ledger.open(encoding="utf-8") if line.strip())
                    job["stage"] = "execute"
                    job["current_nodes"] = [f"WorkUnit {completed}/1000"]
                    job["logical_steps_completed"] = completed
                job["node_status_counts"] = _summary(run_dir).get("status_counts", {}) if (run_dir / "run_state.json").is_file() else {}
            return_code = process.poll() if process is not None else job.get("return_code")
            if return_code is None:
                job["status"] = "running"
            elif job.get("status") != "cancelled":
                job["return_code"] = return_code
                job["finished_at"] = datetime.now().isoformat(timespec="seconds")
                if job.get("run_dir"):
                    run_dir = self.project_root / job["run_dir"]
                    audit = audit_run(run_dir)
                    job["audit_passed"] = bool(audit.get("passed"))
                    job["audit_checks"] = audit.get("checks", {})
                    job["status"] = "success" if audit.get("passed") else "failed"
                    job["stage"] = "finished"
                else:
                    job["status"] = "failed"
                    job["error"] = "process finished without a run directory"
            job["updated_at"] = datetime.now().isoformat(timespec="seconds")
            return self._public_job(job)

    def _active_job_count(self) -> int:
        count = 0
        for job_id in list(self.jobs):
            job = self._refresh_job(job_id)
            if job and job.get("status") in {"queued", "running"}:
                count += 1
        return count

    def do_GET(self):  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        if parsed.path == "/api/presentation":
            candidates = [
                ("20260910_163108", "三类扰动 · 自主恢复"),
                ("20260908_125323", "差旅报销 · 跨文档核验"),
                ("20260908_124006", "MathorCup · 装箱优化"),
                ("20260908_173502", "城市多模态 · 自动修复"),
                ("20260909_urban_business_1000", "城市业务 · 千步证据"),
                ("20260911_mathorcup_business_1000", "装箱优化 · 千步证据"),
            ]
            for run in sorted(self.runs_root.iterdir(), reverse=True):
                if run.is_dir() and (run / "run_state.json").is_file() and _load(
                    run / "task_spec.json", {}
                ).get("accept_runtime_injections"):
                    candidates.append((run.name, f"交互式注入 · {run.name}"))
            self._json({"runs": [
                {"run_id": key, "label": label, **_summary(self.runs_root / key)}
                for key, label in candidates
                if (self.runs_root / key / "run_state.json").is_file()
            ], "execution_enabled": self.enable_execution})
            return
        if parsed.path == "/api/demo":
            self._json(_demo_payload(self.project_root, self.runs_root))
            return
        if parsed.path == "/api/demo/config":
            self._json({"execution_enabled": self.enable_execution,
                        "token_required": self.enable_execution})
            return
        if parsed.path == "/api/runtime/preflight":
            self._json(_runtime_preflight(
                self.project_root, self.enable_execution, self._active_job_count()
            ))
            return
        if parsed.path == "/api/demo/jobs":
            jobs = [self._refresh_job(job_id) for job_id in list(self.jobs)]
            self._json({"jobs": jobs})
            return
        if parsed.path.startswith("/api/runtime/jobs/"):
            suffix = unquote(parsed.path[len("/api/runtime/jobs/"):]).strip("/")
            parts = suffix.split("/", 1)
            job = self._refresh_job(parts[0])
            if job is None:
                self._json({"error": "job not found"}, 404)
                return
            if len(parts) == 2 and parts[1] == "log":
                log_path = self.project_root / job["log_path"]
                text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
                self._json({"job_id": parts[0], "content": text[-80_000:], "truncated": len(text) > 80_000})
                return
            self._json(job)
            return
        if parsed.path == "/api/runs":
            runs = sorted(
                (path for path in self.runs_root.iterdir()
                 if path.is_dir() and (path / "run_state.json").is_file()),
                key=lambda path: path.name, reverse=True,
            )
            self._json({"runs": [_summary(path) for path in runs]})
            return
        if parsed.path.startswith("/api/runs/"):
            suffix = unquote(parsed.path[len("/api/runs/"):]).strip("/")
            parts = suffix.split("/", 2)
            run_id = parts[0]
            if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
                self._json({"error": "invalid run id"}, 400)
                return
            run_dir = (self.runs_root / run_id).resolve()
            try:
                run_dir.relative_to(self.runs_root.resolve())
            except ValueError:
                self._json({"error": "invalid run id"}, 400)
                return
            if not (run_dir / "run_state.json").is_file():
                self._json({"error": "run not found"}, 404)
                return
            if len(parts) == 3 and parts[1] == "artifacts":
                try:
                    self._json(_artifact_preview(run_dir, parts[2]))
                except FileNotFoundError:
                    self._json({"error": "artifact not found"}, 404)
                except ValueError as exc:
                    self._json({"error": str(exc)}, 400)
                return
            if len(parts) == 3 and parts[1] == "download":
                target = (run_dir / parts[2]).resolve()
                try:
                    target.relative_to(run_dir)
                    if Path(parts[2]).is_absolute() or not target.is_file():
                        raise ValueError("invalid artifact path")
                    if target.suffix.lower() not in {".md", ".json", ".csv", ".txt", ".py", ".pdf", ".docx", ".xlsx", ".png"}:
                        raise ValueError("unsupported artifact type")
                    if target.stat().st_size > 32_000_000:
                        raise ValueError("artifact exceeds 32 MB download limit")
                except ValueError as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(target.stat().st_size))
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(target.name))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                with target.open("rb") as stream:
                    while chunk := stream.read(64 * 1024):
                        self.wfile.write(chunk)
                return
            self._json(_detail(run_dir))
            return
        if parsed.path in {"/demo", "/demo/"}:
            self.path = "/demo.html"
        elif parsed.path == "/" or parsed.path == "":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        if path not in {"/api/demo/run", "/api/runtime/jobs"} and not (
            path.startswith("/api/runtime/jobs/") and path.endswith(("/cancel", "/injections"))
        ):
            self._json({"error": "not found"}, 404)
            return
        if not self.enable_execution:
            self._json({"error": "execution is disabled; restart with --enable-execution"}, 403)
            return
        if not self._authorized():
            self._json({"error": "invalid dashboard execution token"}, 401)
            return
        if path.endswith("/injections"):
            job_id = path[len("/api/runtime/jobs/"):-len("/injections")].strip("/")
            job = self._refresh_job(job_id)
            if not job or job.get("status") != "running" or not job.get("run_dir"):
                self._json({"error": "任务尚未就绪或已结束"}, 409)
                return
            run_dir = self.project_root / job["run_dir"]
            spec = _load(run_dir / "task_spec.json", {})
            state = _load(run_dir / "run_state.json", {})
            if not spec.get("accept_runtime_injections") or state.get("finished"):
                self._json({"error": "该运行未开放动态注入"}, 409)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    raise ValueError("invalid request size")
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                queued = enqueue_injection(run_dir, request)
                self._json(queued, 202)
            except (ValueError, TypeError) as exc:
                self._json({"error": str(exc)}, 400)
            return
        if path.endswith("/cancel"):
            job_id = unquote(path[len("/api/runtime/jobs/"):-len("/cancel")]).strip("/")
            with self.jobs_lock:
                job = self.jobs.get(job_id)
                process = self.processes.get(job_id)
                if job is None:
                    self._json({"error": "job not found"}, 404)
                    return
                if process is not None and process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                job["status"] = "cancelled"
                job["finished_at"] = datetime.now().isoformat(timespec="seconds")
            self._json(self._public_job(job))
            return
        with self.launch_lock:
            self._launch_job()

    def _launch_job(self):
        if self._active_job_count() > 0:
            self._json({"error": "another dashboard job is already running"}, 409)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 4096:
                raise ValueError("invalid request size")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("request must be an object")
            scenario = str(body.get("scenario", ""))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, 400)
            return
        config = SCENARIOS.get(scenario)
        if config is None:
            self._json({"error": "unknown scenario"}, 400)
            return
        input_path = config["input"]
        job_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        log_dir = self.project_root / "outputs/dashboard_jobs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{job_id}.log"
        stream = log_path.open("w", encoding="utf-8")
        command = config.get("command") or ["main.py", "--input", input_path]
        process = subprocess.Popen(
            [sys.executable, "-u", *command],
            cwd=self.project_root, stdout=stream, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        stream.close()
        now = datetime.now().isoformat(timespec="seconds")
        job = {"job_id": job_id, "scenario": scenario, "scenario_label": config["label"],
               "input_path": input_path, "pid": process.pid, "status": "running",
               "stage": "prepare", "started_at": now, "updated_at": now,
               "log_path": log_path.relative_to(self.project_root).as_posix()}
        with self.jobs_lock:
            self.jobs[job_id] = job
            self.processes[job_id] = process
        self._json(job, 202)

    def log_message(self, format, *args):  # noqa: A003 - stdlib handler API
        return


def serve(host: str, port: int, runs_root: Path, enable_execution: bool = False,
          execution_token: str = "") -> None:
    dashboard_root = Path(__file__).resolve().parent.parent / "dashboard"
    project_root = dashboard_root.parent
    handler = type("BoundDashboardHandler", (DashboardHandler,), {
        "runs_root": runs_root.resolve(),
        "dashboard_root": dashboard_root.resolve(),
        "project_root": project_root.resolve(),
        "enable_execution": enable_execution,
        "jobs": {},
        "processes": {},
        "execution_token": execution_token,
    })
    # SimpleHTTPRequestHandler serves from cwd; keep the server's working
    # directory independent by constructing the handler with an explicit path.
    def factory(*args, **kwargs):
        return handler(*args, directory=str(dashboard_root), **kwargs)
    server = ThreadingHTTPServer((host, port), factory)
    print(f"Dashboard: http://{host}:{port}/")
    if enable_execution:
        print(f"Dashboard execution token: {execution_token}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--runs-root", default="outputs/runs")
    parser.add_argument("--enable-execution", action="store_true",
                        help="allow only the predefined demo scenarios to be started")
    parser.add_argument("--execution-token", default="",
                        help="token required by write endpoints; generated when omitted")
    args = parser.parse_args()
    root = Path(args.runs_root).resolve()
    if not root.is_dir():
        parser.error(f"runs root does not exist: {root}")
    token = args.execution_token or (secrets.token_urlsafe(18) if args.enable_execution else "")
    serve(args.host, args.port, root, args.enable_execution, token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
