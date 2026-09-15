from functools import partial
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from utils.dashboard_server import DashboardHandler
from utils.runtime_injection_queue import close_injection_queue


class DemoHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = self.root / "outputs/runs/test_run"
        self.run.mkdir(parents=True)
        (self.run / "task_spec.json").write_text(json.dumps({"accept_runtime_injections": True}))
        (self.run / "run_state.json").write_text(json.dumps({"finished": False, "nodes": {}}))
        logs = self.root / "outputs/dashboard_jobs"
        logs.mkdir()
        (logs / "test.log").write_text("")
        handler = type("TestHandler", (DashboardHandler,), {
            "project_root": self.root, "runs_root": self.root / "outputs/runs",
            "enable_execution": True, "execution_token": "test-token",
            "jobs": {"test": {"job_id": "test", "status": "running", "run_dir": "outputs/runs/test_run",
                              "log_path": "outputs/dashboard_jobs/test.log"}}, "processes": {},
        })
        self.handler = handler
        self.server = ThreadingHTTPServer(("127.0.0.1",0),partial(handler,directory=str(self.root)))
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.client = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def send(self, body, token="test-token"):
        request = Request(f"http://127.0.0.1:{self.server.server_port}/api/runtime/jobs/test/injections",
                          data=json.dumps(body).encode(),
                          headers={"Content-Type":"application/json","X-Dashboard-Token":token})
        with self.client.open(request,timeout=5) as response:
            return response.status, json.load(response)

    def test_authentication_read_only_mode_and_closed_window(self):
        with self.assertRaises(HTTPError) as wrong:
            self.send({"type":"node_failure"},token="wrong")
        self.assertEqual(wrong.exception.code,401)
        self.handler.enable_execution = False
        with self.assertRaises(HTTPError) as readonly:
            self.send({"type":"node_failure"})
        self.assertEqual(readonly.exception.code,403)
        self.handler.enable_execution = True
        status, body = self.send({"type":"node_failure"})
        self.assertEqual(status,202)
        self.assertEqual(body["status"],"queued")
        # HTTP must not modify the workflow's state itself.
        self.assertEqual(json.loads((self.run / "run_state.json").read_text()),{"finished":False,"nodes":{}})
        close_injection_queue(self.run)
        with self.assertRaises(HTTPError) as closed:
            self.send({"type":"data_anomaly"})
        self.assertEqual(closed.exception.code,400)

    def test_malformed_request_and_finished_run_cannot_accept_injection(self):
        for body in (["not an object"], {"type":"command"}):
            with self.assertRaises(HTTPError) as invalid:
                self.send(body)
            self.assertEqual(invalid.exception.code,400)
        (self.run / "run_state.json").write_text(json.dumps({"finished":True,"nodes":{}}))
        with self.assertRaises(HTTPError) as ended:
            self.send({"type":"data_anomaly"})
        self.assertEqual(ended.exception.code,409)

    def test_download_is_exact_and_cannot_escape_run(self):
        (self.run / "final_report.md").write_text("核验报告", encoding="utf-8")
        base = f"http://127.0.0.1:{self.server.server_port}/api/runs/test_run/download/"
        with self.client.open(base + "final_report.md", timeout=5) as response:
            self.assertEqual(response.read(), "核验报告".encode())
            self.assertIn("attachment", response.headers["Content-Disposition"])
        with self.assertRaises(HTTPError) as traversal:
            self.client.open(base + "..%2F..%2Foutside.json", timeout=5)
        self.assertEqual(traversal.exception.code,400)

    def test_concurrent_launches_only_create_one_process(self):
        self.handler.jobs = {}
        self.handler.processes = {}
        process = Mock(pid=12345)
        process.poll.return_value = None

        def launch():
            request = Request(f"http://127.0.0.1:{self.server.server_port}/api/runtime/jobs",
                              data=json.dumps({"scenario":"interactive_demo"}).encode(),
                              headers={"Content-Type":"application/json","X-Dashboard-Token":"test-token"})
            try:
                with build_opener(ProxyHandler({})).open(request,timeout=5) as response:
                    return response.status
            except HTTPError as error:
                return error.code

        with patch("utils.dashboard_server.subprocess.Popen", return_value=process) as popen:
            with ThreadPoolExecutor(max_workers=2) as pool:
                statuses = list(pool.map(lambda _: launch(), range(2)))
            self.assertEqual(sorted(statuses),[202,409])
            self.assertEqual(popen.call_count,1)


if __name__ == "__main__":
    unittest.main()
