"""Verify HTTP launch -> interactive injection -> real workflow -> audits.

Explicitly invokes the configured model provider and consumes its normal quota.
All subprocess output stays in the run's dashboard log, not in this verifier.
"""
import argparse
from functools import partial
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sys
import threading
import time
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.dashboard_server import DashboardHandler
from utils.injection_audit import audit_runtime_injections


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--timeout", type=int, default=1500)
    args = parser.parse_args()
    token = secrets.token_urlsafe(24)
    handler = type("VerificationHandler", (DashboardHandler,), {
        "project_root": ROOT, "runs_root": ROOT / "outputs/runs",
        "dashboard_root": ROOT / "dashboard", "enable_execution": True,
        "execution_token": token, "jobs": {}, "processes": {},
    })
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(handler, directory=str(ROOT / "dashboard")))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    opener = build_opener(ProxyHandler({}))

    def request(path, body=None):
        payload = json.dumps(body).encode() if body is not None else None
        req = Request(base + path, data=payload, headers={"Content-Type": "application/json", "X-Dashboard-Token": token})
        with opener.open(req, timeout=20) as response:
            return json.load(response)

    job = None
    try:
        job = request("/api/runtime/jobs", {"scenario": "interactive_demo"})
        print("Live verification job:", job["job_id"], flush=True)
        submitted = False
        previous_stage = None
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            job = request(f"/api/runtime/jobs/{job['job_id']}")
            if job.get("stage") != previous_stage:
                previous_stage = job.get("stage")
                print("Stage:", previous_stage, flush=True)
            if job.get("run_dir") and not submitted:
                run = ROOT / job["run_dir"]
                spec_path = run / "task_spec.json"
                if spec_path.is_file():
                    spec = json.loads(spec_path.read_text(encoding="utf-8"))
                    if spec.get("accept_runtime_injections"):
                        for kind in ("node_failure", "requirement_change", "data_anomaly"):
                            response = request(f"/api/runtime/jobs/{job['job_id']}/injections", {"type": kind})
                            assert response["status"] == "queued"
                        submitted = True
                        print("Three requests submitted to the already running workflow.", flush=True)
            if job["status"] in {"success", "failed", "cancelled"}:
                run = ROOT / job["run_dir"]
                result = audit_runtime_injections(run)
                result["http_requests_submitted"] = submitted
                result["job_status"] = job["status"]
                output = run / "injections/interactive_http_verification.json"
                output.parent.mkdir(exist_ok=True)
                output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({"run_dir": job["run_dir"], "job_status": job["status"], "audit_passed": result["passed"], "checks": result["checks"]}, ensure_ascii=False), flush=True)
                return 0 if submitted and result["passed"] and job["status"] == "success" else 1
            time.sleep(2)
        print("Verification timed out; cancelling its own workflow.", flush=True)
        return 1
    finally:
        if job and job.get("status") == "running":
            request(f"/api/runtime/jobs/{job['job_id']}/cancel", {})
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
