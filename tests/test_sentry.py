import gzip
import json
import time
import unittest

from test_http import HttpTestBase

TRACE_ID = "5b8efff798038103d269b633813fc60c"
SPAN_ID = "eee19b7ec3c1b174"
AUTH = {"X-Sentry-Auth": "Sentry sentry_version=7, sentry_client=test/1.0, sentry_key=deadbeef"}
EVENT_ID = "11112222333344445555666677778888"

FRAMES = [
    {"filename": "app/x.py", "function": "boom", "lineno": 7, "in_app": True},
    {"filename": "lib/y.py", "function": "call", "lineno": 42},
]


def sentry_event(event_id=EVENT_ID, kind="ValueError", value="bad input", fingerprint=None):
    event = {
        "event_id": event_id,
        "timestamp": time.time(),
        "level": "error",
        "platform": "python",
        "release": "test-1.0",
        "environment": "ci",
        "message": value,
        "exception": {"values": [{"type": kind, "value": value, "stacktrace": {"frames": FRAMES}}]},
        "contexts": {"trace": {"trace_id": TRACE_ID, "span_id": SPAN_ID}},
    }
    if fingerprint:
        event["fingerprint"] = fingerprint
    return event


def envelope_bytes(event, key="deadbeef"):
    header = {"event_id": event["event_id"], "dsn": f"http://{key}@127.0.0.1/1"}
    return (
        json.dumps(header).encode()
        + b"\n"
        + json.dumps({"type": "event"}).encode()
        + b"\n"
        + json.dumps(event).encode()
        + b"\n"
    )


def today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def wait_file(path, needle, timeout=10.0):
    deadline = time.time() + timeout
    content = ""
    while time.time() < deadline:
        if path.exists():
            content = path.read_text()
            if needle in content:
                return content
        time.sleep(0.05)
    return content


def hour_range():
    now_ms = int(time.time() * 1000)
    return {"from": now_ms - 3600_000, "to": now_ms + 60_000}


class TestSentryIngest(HttpTestBase, unittest.TestCase):
    def test_store_endpoint_writes_jsonl(self):
        status, body = self.post("/api/1/store/", sentry_event(), headers=AUTH)
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], EVENT_ID)
        content = wait_file(self.cfg.errors_dir / today() / "1-deadbeef.jsonl", EVENT_ID)
        self.assertIn('"kind":"ValueError"', content)
        self.assertIn('"group"', content)
        self.assertIn(TRACE_ID, content)
        self.assertIn('"event":', content)

    def test_envelope_endpoint_key_from_dsn(self):
        # no X-Sentry-Auth header: the key falls back to the envelope dsn
        status, body = self.post(
            "/api/7/envelope/",
            envelope_bytes(sentry_event(), key="cafef00d"),
            headers={"Content-Type": "application/x-sentry-envelope"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], EVENT_ID)
        content = wait_file(self.cfg.errors_dir / today() / "7-cafef00d.jsonl", EVENT_ID)
        self.assertIn("bad input", content)

    def test_no_key_accepted(self):
        status, _ = self.post("/api/1/store/", sentry_event(), headers={})
        self.assertEqual(status, 200)
        content = wait_file(self.cfg.errors_dir / today() / "1-nokey.jsonl", EVENT_ID)
        self.assertIn("ValueError", content)

    def test_auth_header_with_key_first(self):
        # regression: real SDKs send "Sentry sentry_key=..., sentry_version=7, ..."
        # where the scheme prefix glues to the first parameter name
        auth = {"X-Sentry-Auth": "Sentry sentry_key=deadbeef, sentry_version=7, sentry_client=test/1.0"}
        status, _ = self.post("/api/1/store/", sentry_event(), headers=auth)
        self.assertEqual(status, 200)
        content = wait_file(self.cfg.errors_dir / today() / "1-deadbeef.jsonl", EVENT_ID)
        self.assertIn("ValueError", content)

    def test_gzip_envelope_accepted(self):
        raw = envelope_bytes(sentry_event())
        status, _ = self.post(
            "/api/1/envelope/",
            gzip.compress(raw),
            headers={"Content-Type": "application/x-sentry-envelope", "Content-Encoding": "gzip"},
        )
        self.assertEqual(status, 200)
        self.assertIn(EVENT_ID, wait_file(self.cfg.errors_dir / today() / "1-deadbeef.jsonl", EVENT_ID))

    def test_path_prefix_accepted(self):
        # self-hosted style dsn: <prefix>/api/<project>/store/
        status, _ = self.post("/sentry/api/1/store/", sentry_event(), headers=AUTH)
        self.assertEqual(status, 200)
        self.assertIn(EVENT_ID, wait_file(self.cfg.errors_dir / today() / "1-deadbeef.jsonl", EVENT_ID))

    def test_bad_payload_400(self):
        status, body = self.post("/api/1/store/", b"not json")
        self.assertEqual(status, 400)
        self.assertIn("message", body)

    def test_get_on_sentry_path_405(self):
        status, _, _ = self.get("/api/1/store/")
        self.assertEqual(status, 405)

    def test_grouping_by_kind_and_fingerprint(self):
        self.post("/api/1/store/", sentry_event(event_id="aaaa1111aaaa1111aaaa1111aaaa1111"), headers=AUTH)
        self.post(
            "/api/1/store/",
            sentry_event(event_id="bbbb2222bbbb2222bbbb2222bbbb2222", value="bad input again"),
            headers=AUTH,
        )
        self.post(
            "/api/1/store/",
            sentry_event(event_id="cccc3333cccc3333cccc3333cccc3333", kind="KeyError", value="missing"),
            headers=AUTH,
        )
        # a shared client fingerprint wins over the exception shape
        self.post(
            "/api/1/store/",
            sentry_event(
                event_id="dddd4444dddd4444dddd4444dddd4444", kind="KeyError", value="other", fingerprint=["grp1"]
            ),
            headers=AUTH,
        )
        self.post(
            "/api/1/store/",
            sentry_event(
                event_id="eeee5555eeee5555eeee5555eeee5555", kind="TypeError", value="other", fingerprint=["grp1"]
            ),
            headers=AUTH,
        )
        groups = []
        deadline = time.time() + 10
        while time.time() < deadline:
            _, _, data = self.get("/grep/errors", hour_range())
            groups = data["errors"]
            if len(groups) == 3 and sum(g["count"] for g in groups) == 5:
                break
            time.sleep(0.05)
        self.assertEqual(len(groups), 3)
        self.assertEqual(sorted(g["count"] for g in groups), [1, 2, 2])
        by_kind = {g["kind"]: g for g in groups}
        self.assertEqual(by_kind["ValueError"]["count"], 2)
        self.assertEqual(by_kind["ValueError"]["trace_id"], TRACE_ID)
        self.assertIn("1-deadbeef", by_kind["ValueError"]["services"])
        self.assertEqual(len(by_kind["ValueError"]["group"]), 16)

    def test_errors_series_buckets_by_kind(self):
        self.post("/api/1/store/", sentry_event(event_id="aaaa1111aaaa1111aaaa1111aaaa1111"), headers=AUTH)
        self.post(
            "/api/1/store/",
            sentry_event(event_id="bbbb2222bbbb2222bbbb2222bbbb2222", kind="KeyError", value="missing"),
            headers=AUTH,
        )
        kinds = {}
        deadline = time.time() + 10
        while time.time() < deadline:
            _, _, data = self.get("/grep/errors/series", hour_range())
            kinds = data["kinds"]
            if len(kinds) == 2 and sum(sum(v) for v in kinds.values()) == 2:
                break
            time.sleep(0.05)
        self.assertEqual(sorted(kinds), ["KeyError", "ValueError"])
        self.assertEqual(sum(kinds["ValueError"]), 1)
        self.assertEqual(sum(kinds["KeyError"]), 1)

    def test_stats_count_sentry_errors(self):
        self.post("/api/1/store/", sentry_event(), headers=AUTH)
        services = []
        deadline = time.time() + 10
        while time.time() < deadline:
            _, _, data = self.get("/perl/stats", hour_range())
            services = data["services"]
            if any(s["sentry"] for s in services):
                break
            time.sleep(0.05)
        rows = [s for s in services if s["sentry"]]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["service"], "1-deadbeef")
        self.assertEqual(rows[0]["sentry"], 1)
