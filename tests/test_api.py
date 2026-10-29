import unittest

from fixtures import TRACE_ID, logs_payload, metrics_payload, traces_payload
from test_http import HttpTestBase, wait_count, wait_file, wait_until

# fixture timestamps are 2024-10-19T09:05:32Z
FROM_MS = 1729296000000  # 2024-10-19 00:00:00 UTC
TO_MS = 1729382400000  # 2024-10-20 00:00:00 UTC


class TestViewerApi(HttpTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.post("/v1/traces", traces_payload())
        self.post("/v1/metrics", metrics_payload())
        self.post("/v1/logs", logs_payload())
        # the APIs read committed data, so wait for the writer before asserting
        wait_count(self.cfg.db_path, "SELECT count(*) FROM spans", 1)
        wait_count(self.cfg.db_path, "SELECT count(*) FROM samples", 2)
        path = self.cfg.logs_dir / "2024-10-19" / "checkout.jsonl"
        self.assertTrue(wait_file(path, "checkout failed"), "log file never appeared")

    def test_root_serves_html_page(self):
        status, content_type, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        html = body.decode() if isinstance(body, bytes) else body
        self.assertIn("strace", html)
        self.assertIn("awk", html)
        self.assertIn("grep", html)
        self.assertIn("perl", html)

    def test_strace_traces_lists_trace(self):
        status, _, data = self.get("/strace/traces", {"from": FROM_MS, "to": TO_MS})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["traces"]), 1)
        trace = data["traces"][0]
        self.assertEqual(trace["trace_id"], TRACE_ID)
        self.assertEqual(trace["root"], "POST /checkout")
        self.assertEqual(trace["service"], "checkout")
        self.assertEqual(trace["errors"], 1)

    def test_strace_trace_waterfall(self):
        status, _, data = self.get("/strace/trace", {"id": TRACE_ID})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["spans"]), 1)
        span = data["spans"][0]
        self.assertEqual(span["name"], "POST /checkout")
        self.assertEqual(span["status_code"], 2)
        self.assertEqual(span["duration_ms"], 48)
        self.assertEqual(span["attrs"]["http.route"], "/checkout")
        self.assertEqual(span["attrs"]["retries"], 3)
        self.assertEqual(span["attrs"]["ok"], True)
        self.assertEqual(span["attrs"]["tags"], ["a", 1])
        self.assertEqual(span["events"][0]["name"], "cart.loaded")

    def test_strace_trace_bad_id_404(self):
        status, _, _ = self.get("/strace/trace", {"id": "not-hex"})
        self.assertEqual(status, 404)

    def test_awk_metrics_grouped_by_unit_and_kind(self):
        status, _, data = self.get("/awk/metrics")
        self.assertEqual(status, 200)
        keys = [(g["unit"], g["kind"]) for g in data["groups"]]
        self.assertIn(("1", "sum"), keys)
        self.assertIn(("unitless", "gauge"), keys)
        sum_group = next(g for g in data["groups"] if g["unit"] == "1" and g["kind"] == "sum")
        self.assertEqual(sum_group["metrics"][0]["name"], "checkout.requests")

    def test_metrics_accumulate_beyond_100(self):
        # "make sure we can accumulate 100 metrics" - ingest 120 distinct
        # names in one batch and expect all of them listed back.
        metrics = [
            {
                "name": f"bulk.metric.{i:03d}",
                "unit": "1",
                "data": {"gauge": {"dataPoints": [{"timeUnixNano": "1729328732051000000", "asDouble": 1.0}]}},
            }
            for i in range(120)
        ]
        status, _ = self.post("/v1/metrics", {"resourceMetrics": [{"scopeMetrics": [{"metrics": metrics}]}]})
        self.assertEqual(status, 200)

        def poll_total():
            _, _, data = self.get("/awk/metrics")
            total = sum(len(g["metrics"]) for g in data["groups"])
            return total >= 100 and total

        self.assertGreaterEqual(wait_until(poll_total) or 0, 100)

    def _metric_id_by_name(self, name):
        _, _, metrics = self.get("/awk/metrics")
        for group in metrics["groups"]:
            for metric in group["metrics"]:
                if metric["name"] == name:
                    return metric["id"]
        raise AssertionError(f"metric not found: {name}")

    def test_awk_series_returns_points(self):
        metric_id = self._metric_id_by_name("checkout.requests")
        status, _, data = self.get("/awk/series", {"id": str(metric_id), "from": FROM_MS, "to": TO_MS})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["series"]), 1)
        series = data["series"][0]
        self.assertEqual(series["service_name"], "checkout")
        self.assertEqual(series["points"], [[1729328732051, 42.0]])

    def test_awk_series_multiple_ids(self):
        _, _, metrics = self.get("/awk/metrics")
        ids = [m["id"] for g in metrics["groups"] for m in g["metrics"]]
        status, _, data = self.get("/awk/series", {"id": ",".join(str(i) for i in ids), "from": FROM_MS, "to": TO_MS})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["series"]), 2)

    def test_grep_logs_filter_by_query(self):
        status, _, data = self.get("/grep/logs", {"from": FROM_MS, "to": TO_MS, "q": "failed"})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["logs"]), 1)
        self.assertIn("checkout failed", data["logs"][0]["body"])
        self.assertEqual(data["logs"][0]["trace_id"], TRACE_ID)

    def test_grep_logs_filter_by_service(self):
        status, _, data = self.get("/grep/logs", {"from": FROM_MS, "to": TO_MS, "service": "checkout"})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["logs"]), 2)

    def test_grep_logs_sorted_by_timestamp_across_services(self):
        def payload(service, nano):
            return {
                "resourceLogs": [
                    {
                        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
                        "scopeLogs": [
                            {
                                "logRecords": [
                                    {
                                        "timeUnixNano": str(nano),
                                        "severityNumber": 9,
                                        "severityText": "INFO",
                                        "body": {"stringValue": "x"},
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }

        # zzz alphabetically after checkout but stamped earlier; aaa later.
        # sorted output must interleave by time, not group by service.
        self.post("/v1/logs", payload("zzz", 1729328732000000000))
        self.post("/v1/logs", payload("aaa", 1729328732100000000))

        def poll_times():
            _, _, data = self.get("/grep/logs", {"from": FROM_MS, "to": TO_MS})
            times = [entry["time_ms"] for entry in data["logs"]]
            return len(times) >= 4 and times

        times = wait_until(poll_times) or []
        self.assertGreaterEqual(len(times), 4)
        self.assertEqual(times, sorted(times))

    def test_grep_logs_empty_range(self):
        status, _, data = self.get("/grep/logs", {"from": 0, "to": 1})
        self.assertEqual(status, 200)
        self.assertEqual(data["logs"], [])

    def test_grep_logs_series_buckets_by_severity(self):
        status, _, data = self.get("/grep/logs/series", {"from": FROM_MS, "to": TO_MS})
        self.assertEqual(status, 200)
        self.assertGreater(data["bucket_ms"], 0)
        self.assertEqual(sum(sum(v) for v in data["severities"].values()), 2)
        self.assertIn("ERROR", data["severities"])
        self.assertIn("INFO", data["severities"])

    def test_perl_stats_per_service(self):
        status, _, data = self.get("/perl/stats", {"from": FROM_MS, "to": TO_MS})
        self.assertEqual(status, 200)
        services = {s["service"]: s for s in data["services"]}
        self.assertIn("checkout", services)
        checkout = services["checkout"]
        self.assertEqual(checkout["spans"], 1)
        self.assertEqual(checkout["errors"], 1)
        self.assertEqual(checkout["samples"], 2)
        self.assertEqual(checkout["logs"], 2)

    def test_unknown_api_404(self):
        status, _, _ = self.get("/netstat/open", {"from": FROM_MS, "to": TO_MS})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
