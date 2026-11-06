"""Typical query cases through the /ql HTTP endpoints.

The everyday queries a developer actually types, wired end to end: SDK
payloads in over HTTP, questions out over HTTP. Interpreter unit tests
live in test_ql.py; these pin the realistic shapes.
"""

import unittest

from fixtures import logs_payload, metrics_payload, traces_payload
from test_http import HttpTestBase, wait_count, wait_file

FROM_MS = 1729296000000  # 2024-10-19 00:00:00 UTC
TO_MS = 1729382400000  # 2024-10-20 00:00:00 UTC
TRACE_ID = "5b8efff798038103d269b633813fc60c"


class TestTypicalQueries(HttpTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.post("/v1/traces", traces_payload())
        self.post("/v1/metrics", metrics_payload())
        self.post("/v1/logs", logs_payload())
        wait_count(self.cfg.db_path, "SELECT count(*) FROM spans", 1)
        wait_count(self.cfg.db_path, "SELECT count(*) FROM samples", 2)
        wait_file(self.cfg.logs_dir / "2024-10-19" / "checkout.jsonl", "checkout failed")

    def ql(self, language, query):
        status, _, data = self.get(f"/ql/{language}", {"q": query, "from": FROM_MS, "to": TO_MS})
        self.assertEqual(status, 200, data)
        return data

    # logql: the things people type after clicking around the logs panel

    def test_logql_grep_one_service(self):
        data = self.ql("logql", '{service="checkout"} |= "failed"')
        self.assertEqual(len(data["logs"]), 1)
        self.assertIn("checkout failed", data["logs"][0]["body"])

    def test_logql_negation(self):
        data = self.ql("logql", '{service="checkout"} != "recovered"')
        self.assertEqual(len(data["logs"]), 1)

    def test_logql_regex_and_severity(self):
        data = self.ql("logql", '{service="checkout", severity=~"E.*"} |~ "cart 42"')
        self.assertEqual(len(data["logs"]), 1)
        self.assertEqual(data["logs"][0]["trace_id"], TRACE_ID)

    def test_logql_pipeline_json_field(self):
        self.post(
            "/v1/logs",
            {
                "resourceLogs": [
                    {
                        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "api"}}]},
                        "scopeLogs": [
                            {
                                "logRecords": [
                                    {
                                        "timeUnixNano": "1729328732151000000",
                                        "severityText": "WARN",
                                        "body": {"stringValue": '{"event": "cache_miss", "shard": 7}'},
                                    }
                                ]
                            }
                        ],
                    }
                ]
            },
        )
        wait_file(self.cfg.logs_dir / "2024-10-19" / "api.jsonl", "cache_miss")
        data = self.ql("logql", '{service="api"} | json | shard="7"')
        self.assertEqual(len(data["logs"]), 1)

    # traceql: the things people type after staring at the waterfall

    def test_traceql_errors_only(self):
        data = self.ql("traceql", "{ status = error }")
        self.assertEqual([t["trace_id"] for t in data["traces"]], [TRACE_ID])

    def test_traceql_slow_spans(self):
        data = self.ql("traceql", "{ duration > 10ms }")
        self.assertEqual(len(data["traces"]), 1)

    def test_traceql_route_and_regex(self):
        data = self.ql("traceql", '{ .http.route =~ "/check.*" && name = "POST /checkout" }')
        self.assertEqual(len(data["traces"]), 1)

    def test_traceql_structural_child_of(self):
        self.post("/v1/traces", child_trace_payload())
        wait_count(self.cfg.db_path, "SELECT count(*) FROM spans", 2)
        data = self.ql("traceql", '{ name = "POST /checkout" } >> { name = "db query" }')
        self.assertEqual([t["trace_id"] for t in data["traces"]], [TRACE_ID])

    def test_traceql_aggregate_rows(self):
        # aggregate pipes return rows, not spans; the endpoint hands them
        # back as rows so the page can say something sane
        data = self.ql("traceql", "{ } | count() by (resource.service.name)")
        self.assertEqual(data["rows"], [{"resource.service.name": "checkout", "count": 1}])
        data = self.ql("traceql", "{ }")
        self.assertEqual(len(data["traces"]), 1)

    # promql: the things people type when the picker is not enough

    def test_promql_sum_by_service(self):
        data = self.ql("promql", "sum by (service) (checkout.requests)")
        self.assertEqual(
            data["rows"],
            [{"metric": "", "labels": {"service": "checkout"}, "value": 42.0}],
        )

    def test_promql_gauge_selector(self):
        data = self.ql("promql", 'queue.depth{service="checkout"}')
        self.assertEqual(data["rows"][0]["value"], 3.5)

    def test_promql_rate(self):
        self.post("/v1/metrics", counter_payload())
        wait_count(self.cfg.db_path, "SELECT count(*) FROM samples", 4)
        # the [5m] window must actually cover the fixture points (09:05 UTC)
        status, _, data = self.get("/ql/promql", {"q": "rate(http.hits[5m])", "from": FROM_MS, "to": 1729328733051})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["rows"]), 1)
        self.assertGreater(data["rows"][0]["value"], 0)

    def test_promql_topk(self):
        self.post("/v1/metrics", two_series_payload())
        wait_count(self.cfg.db_path, "SELECT count(*) FROM samples", 6)
        data = self.ql("promql", "topk(1, latency.check)")
        self.assertEqual(len(data["rows"]), 1)
        self.assertEqual(data["rows"][0]["labels"]["dc"], "b")

    # broken queries answer 400 with the language named, not a crash

    def test_bad_queries_are_400s(self):
        for language, query in (("traceql", "{ name = }"), ("logql", "{service=}"), ("promql", "sum by (")):
            status, _, body = self.get(f"/ql/{language}", {"q": query, "from": FROM_MS, "to": TO_MS})
            self.assertEqual(status, 400, language)
            self.assertIn(f"{language}:", body["message"])


def child_trace_payload():
    payload = traces_payload()
    span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    span["name"] = "db query"
    span["spanId"] = "00f067aa0ba902b7"
    span["parentSpanId"] = "eee19b7ec3c1b174"
    span["status"] = {"code": "STATUS_CODE_OK"}
    return payload


def counter_payload():
    base = 1729328732051000000
    points = [
        {"timeUnixNano": str(base - 2_000_000_000), "asInt": "90"},
        {"timeUnixNano": str(base), "asInt": "100"},
    ]
    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "checkout"}}]},
                "scopeMetrics": [
                    {"metrics": [{"name": "http.hits", "data": {"sum": {"dataPoints": points, "isMonotonic": True}}}]}
                ],
            }
        ]
    }


def two_series_payload():
    base = 1729328732051000000
    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "edge"}}]},
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": "latency.check",
                                "data": {
                                    "gauge": {
                                        "dataPoints": [
                                            {
                                                "timeUnixNano": str(base),
                                                "asDouble": 12.0,
                                                "attributes": [{"key": "dc", "value": {"stringValue": "a"}}],
                                            },
                                            {
                                                "timeUnixNano": str(base),
                                                "asDouble": 48.0,
                                                "attributes": [{"key": "dc", "value": {"stringValue": "b"}}],
                                            },
                                        ]
                                    }
                                },
                            }
                        ]
                    }
                ],
            }
        ]
    }


if __name__ == "__main__":
    unittest.main()
