import gzip
import time
import unittest

from fixtures import SENTRY_AUTH, SENTRY_EVENT_ID, TRACE_ID, hour_range, sentry_envelope, sentry_event
from test_http import HttpTestBase, wait_file, wait_until


def today():
    return time.strftime("%Y-%m-%d", time.gmtime())


class TestSentryIngest(HttpTestBase, unittest.TestCase):
    def test_store_endpoint_writes_jsonl(self):
        status, body = self.post("/api/1/store/", sentry_event(), headers=SENTRY_AUTH)
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], SENTRY_EVENT_ID)
        content = wait_file(self.cfg.errors_dir / today() / "1-deadbeef.jsonl", SENTRY_EVENT_ID)
        self.assertIn('"kind":"ValueError"', content)
        self.assertIn('"group"', content)
        self.assertIn(TRACE_ID, content)
        self.assertIn('"event":', content)

    def test_envelope_endpoint_key_from_dsn(self):
        # no X-Sentry-Auth header: the key falls back to the envelope dsn
        status, body = self.post(
            "/api/7/envelope/",
            sentry_envelope(sentry_event(), key="cafef00d"),
            headers={"Content-Type": "application/x-sentry-envelope"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], SENTRY_EVENT_ID)
        content = wait_file(self.cfg.errors_dir / today() / "7-cafef00d.jsonl", SENTRY_EVENT_ID)
        self.assertIn("bad input", content)

    def test_no_key_accepted(self):
        status, _ = self.post("/api/1/store/", sentry_event(), headers={})
        self.assertEqual(status, 200)
        content = wait_file(self.cfg.errors_dir / today() / "1-nokey.jsonl", SENTRY_EVENT_ID)
        self.assertIn("ValueError", content)

    def test_auth_header_with_key_first(self):
        # regression: real SDKs send "Sentry sentry_key=..., sentry_version=7, ..."
        # where the scheme prefix glues to the first parameter name
        auth = {"X-Sentry-Auth": "Sentry sentry_key=deadbeef, sentry_version=7, sentry_client=test/1.0"}
        status, _ = self.post("/api/1/store/", sentry_event(), headers=auth)
        self.assertEqual(status, 200)
        content = wait_file(self.cfg.errors_dir / today() / "1-deadbeef.jsonl", SENTRY_EVENT_ID)
        self.assertIn("ValueError", content)

    def test_gzip_envelope_accepted(self):
        raw = sentry_envelope(sentry_event())
        status, _ = self.post(
            "/api/1/envelope/",
            gzip.compress(raw),
            headers={"Content-Type": "application/x-sentry-envelope", "Content-Encoding": "gzip"},
        )
        self.assertEqual(status, 200)
        self.assertIn(SENTRY_EVENT_ID, wait_file(self.cfg.errors_dir / today() / "1-deadbeef.jsonl", SENTRY_EVENT_ID))

    def test_path_prefix_accepted(self):
        # self-hosted style dsn: <prefix>/api/<project>/store/
        status, _ = self.post("/sentry/api/1/store/", sentry_event(), headers=SENTRY_AUTH)
        self.assertEqual(status, 200)
        self.assertIn(SENTRY_EVENT_ID, wait_file(self.cfg.errors_dir / today() / "1-deadbeef.jsonl", SENTRY_EVENT_ID))

    def test_bad_payload_400(self):
        status, body = self.post("/api/1/store/", b"not json")
        self.assertEqual(status, 400)
        self.assertIn("message", body)

    def test_get_on_sentry_path_405(self):
        status, _, _ = self.get("/api/1/store/")
        self.assertEqual(status, 405)

    def test_grouping_by_kind_and_fingerprint(self):
        self.post("/api/1/store/", sentry_event(event_id="aaaa1111aaaa1111aaaa1111aaaa1111"), headers=SENTRY_AUTH)
        self.post(
            "/api/1/store/",
            sentry_event(event_id="bbbb2222bbbb2222bbbb2222bbbb2222", value="bad input again"),
            headers=SENTRY_AUTH,
        )
        self.post(
            "/api/1/store/",
            sentry_event(event_id="cccc3333cccc3333cccc3333cccc3333", kind="KeyError", value="missing"),
            headers=SENTRY_AUTH,
        )
        # a shared client fingerprint wins over the exception shape
        self.post(
            "/api/1/store/",
            sentry_event(
                event_id="dddd4444dddd4444dddd4444dddd4444", kind="KeyError", value="other", fingerprint=["grp1"]
            ),
            headers=SENTRY_AUTH,
        )
        self.post(
            "/api/1/store/",
            sentry_event(
                event_id="eeee5555eeee5555eeee5555eeee5555", kind="TypeError", value="other", fingerprint=["grp1"]
            ),
            headers=SENTRY_AUTH,
        )

        def poll_groups():
            _, _, data = self.get("/grep/errors", hour_range())
            groups = data["errors"]
            return len(groups) == 3 and sum(g["count"] for g in groups) == 5 and groups

        groups = wait_until(poll_groups) or []
        self.assertEqual(len(groups), 3)
        self.assertEqual(sorted(g["count"] for g in groups), [1, 2, 2])
        by_kind = {g["kind"]: g for g in groups}
        self.assertEqual(by_kind["ValueError"]["count"], 2)
        self.assertEqual(by_kind["ValueError"]["trace_id"], TRACE_ID)
        self.assertIn("1-deadbeef", by_kind["ValueError"]["services"])
        self.assertEqual(len(by_kind["ValueError"]["group"]), 16)

    def test_errors_series_buckets_by_kind(self):
        self.post("/api/1/store/", sentry_event(event_id="aaaa1111aaaa1111aaaa1111aaaa1111"), headers=SENTRY_AUTH)
        self.post(
            "/api/1/store/",
            sentry_event(event_id="bbbb2222bbbb2222bbbb2222bbbb2222", kind="KeyError", value="missing"),
            headers=SENTRY_AUTH,
        )

        def poll_kinds():
            _, _, data = self.get("/grep/errors/series", hour_range())
            kinds = data["kinds"]
            return len(kinds) == 2 and sum(sum(v) for v in kinds.values()) == 2 and kinds

        kinds = wait_until(poll_kinds) or {}
        self.assertEqual(sorted(kinds), ["KeyError", "ValueError"])
        self.assertEqual(sum(kinds["ValueError"]), 1)
        self.assertEqual(sum(kinds["KeyError"]), 1)

    def test_stats_count_sentry_errors(self):
        self.post("/api/1/store/", sentry_event(), headers=SENTRY_AUTH)

        def poll_services():
            _, _, data = self.get("/perl/stats", hour_range())
            services = data["services"]
            return any(s["sentry"] for s in services) and services

        services = wait_until(poll_services) or []
        rows = [s for s in services if s["sentry"]]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["service"], "1-deadbeef")
        self.assertEqual(rows[0]["sentry"], 1)
