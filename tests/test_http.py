import asyncio
import gzip
import http.client
import json
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from test_normalize import TRACE_ID, logs_payload, metrics_payload, traces_payload

from otelite.config import Config
from otelite.server import serve
from otelite.writer import Writer


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_count(db_path, sql, expected, timeout=10.0):
    deadline = time.time() + timeout
    count = 0
    while time.time() < deadline:
        conn = sqlite3.connect(str(db_path))
        try:
            count = conn.execute(sql).fetchone()[0]
        except sqlite3.OperationalError:
            count = 0
        conn.close()
        if count >= expected:
            return count
        time.sleep(0.05)
    return count


class ServerThread:
    def __init__(self, cfg, writer):
        self.cfg = cfg
        self.writer = writer
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.error = None

    def start(self):
        self.thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.error is not None:
                raise RuntimeError(f"server failed to start: {self.error}")
            try:
                socket.create_connection(("127.0.0.1", self.cfg.port), timeout=0.1).close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("server did not start")

    def _run(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(serve(self.cfg, self.writer))
        except asyncio.CancelledError:
            pass  # stop() cancels the serve_forever task; that is the normal exit
        except Exception as exc:  # surfaced by start() and visible in output
            self.error = exc
            print(f"server thread died: {exc!r}")

    def stop(self):
        def shutdown():
            for task in asyncio.all_tasks(self.loop):
                task.cancel()
            # let cancelled connection handlers run their cleanup before stopping
            self.loop.call_later(0.1, self.loop.stop)

        self.loop.call_soon_threadsafe(shutdown)
        self.thread.join(timeout=5)


class HttpTestBase:
    """Shared harness: writer + server per test, torn down after. Plain class, not collected."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="otelite-test-"))
        self.cfg = Config(root=self.tmp, port=free_port(), flush_ms=5)
        self.cfg.logs_dir.mkdir(parents=True)
        self.writer = Writer(self.cfg)
        self.writer.start()
        self.server = ServerThread(self.cfg, self.writer)
        self.server.start()

    def tearDown(self):
        self.server.stop()
        self.writer.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, path, payload, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.cfg.port, timeout=5)
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        conn.request("POST", path, body=body, headers=request_headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, json.loads(data) if data else {}

    def get(self, path, params=None):
        import urllib.parse

        conn = http.client.HTTPConnection("127.0.0.1", self.cfg.port, timeout=5)
        qs = ("?" + urllib.parse.urlencode(params)) if params else ""
        conn.request("GET", path + qs)
        response = conn.getresponse()
        data = response.read()
        content_type = response.getheader("Content-Type")
        conn.close()
        parsed = json.loads(data) if data and "json" in (content_type or "") else data
        return response.status, content_type, parsed


class TestIngestHttp(HttpTestBase, unittest.TestCase):
    def test_traces_200_then_rows(self):
        status, body = self.post("/v1/traces", traces_payload())
        self.assertEqual(status, 200)
        self.assertEqual(body, {})
        count = wait_count(self.cfg.db_path, "SELECT count(*) FROM spans", 1)
        self.assertEqual(count, 1)
        conn = sqlite3.connect(str(self.cfg.db_path))
        row = conn.execute("SELECT hex(trace_id), name, service_name FROM spans").fetchone()
        conn.close()
        self.assertEqual(row[0], TRACE_ID.upper())
        self.assertEqual(row[1], "POST /checkout")
        self.assertEqual(row[2], "checkout")

    def test_metrics_200_then_rows(self):
        status, _ = self.post("/v1/metrics", metrics_payload())
        self.assertEqual(status, 200)
        count = wait_count(self.cfg.db_path, "SELECT count(*) FROM samples", 2)
        self.assertEqual(count, 2)

    def test_metrics_same_metric_two_services_same_ms(self):
        def payload(service):
            return {
                "resourceMetrics": [
                    {
                        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
                        "scopeMetrics": [
                            {
                                "metrics": [
                                    {
                                        "name": "shared.metric",
                                        "unit": "1",
                                        "data": {
                                            "gauge": {
                                                "dataPoints": [{"timeUnixNano": "1729328732051000000", "asDouble": 1.0}]
                                            }
                                        },
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }

        self.post("/v1/metrics", payload("svcA"))
        self.post("/v1/metrics", payload("svcB"))
        self.assertEqual(wait_count(self.cfg.db_path, "SELECT count(*) FROM samples", 2), 2)

    def test_logs_200_then_file(self):
        status, _ = self.post("/v1/logs", logs_payload())
        self.assertEqual(status, 200)
        path = self.cfg.logs_dir / "2024-10-19" / "checkout.jsonl"
        deadline = time.time() + 10
        content = ""
        while time.time() < deadline:
            if path.exists():
                content = path.read_text()
                if TRACE_ID in content:
                    break
            time.sleep(0.05)
        self.assertIn(TRACE_ID, content)
        self.assertIn("checkout failed for cart 42", content)
        self.assertIn('"resource"', content)

    def test_gzip_body_accepted(self):
        raw = json.dumps(traces_payload()).encode()
        status, _ = self.post("/v1/traces", gzip.compress(raw), headers={"Content-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertEqual(wait_count(self.cfg.db_path, "SELECT count(*) FROM spans", 1), 1)

    def test_invalid_json_400(self):
        status, body = self.post("/v1/traces", b"not json")
        self.assertEqual(status, 400)
        self.assertIn("message", body)

    def test_unknown_path_404(self):
        status, _ = self.post("/v1/other", {})
        self.assertEqual(status, 404)

    def test_get_on_ingest_path_405(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.cfg.port, timeout=5)
        conn.request("GET", "/v1/traces")
        response = conn.getresponse()
        response.read()
        conn.close()
        self.assertEqual(response.status, 405)

    def test_healthz(self):
        status, _, data = self.get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")

    def test_keep_alive_pipelining(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.cfg.port, timeout=5)
        for _ in range(3):
            conn.request("POST", "/v1/traces", body=json.dumps(traces_payload()).encode())
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
        conn.close()
        self.assertEqual(wait_count(self.cfg.db_path, "SELECT count(*) FROM spans", 3), 3)
