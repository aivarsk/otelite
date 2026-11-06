import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from fixtures import logs_payload, traces_payload

from otelite import db
from otelite.config import Config
from otelite.normalize import log_records_from, spans_from
from otelite.ql import logql, promql, traceql
from otelite.ql.combinators import ParseError

FIXTURE_NS = 1729328732051000000
FIXTURE_MS = 1729328732051


def make_cfg(tmp):
    root = Path(tmp) / "data"
    root.mkdir(parents=True)
    return Config(root=root, port=0)


class TestCombinators(unittest.TestCase):
    def test_sequence_choice_and_many(self):
        from otelite.ql.combinators import lexeme, lit, regex, run, sep_by, token

        csv = sep_by(lexeme(regex(r"[a-z]+")), token(","))
        self.assertEqual(run(csv, "a, a ,a"), ["a", "a", "a"])
        self.assertEqual(run(csv, "a"), ["a"])
        self.assertEqual(run(lit("x") | lit("y"), "y"), "y")

    def test_failure_points_at_position(self):
        from otelite.ql.combinators import lit

        with self.assertRaises(ParseError) as ctx:
            lit("hello")("say hi", 0)
        self.assertIn("hello", str(ctx.exception))


class TestTraceQL(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="otelite-ql-"))
        self.cfg = make_cfg(self.tmp)
        conn = db.connect(self.cfg.db_path)
        db.insert_spans(conn, spans_from(traces_payload(), time.time_ns()))
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_selector_by_name(self):
        rows = traceql.execute(self.cfg, '{ name = "POST /checkout" }')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["service_name"], "checkout")
        self.assertEqual(rows[0]["duration_ms"], 48)

    def test_selector_attrs_and_status(self):
        rows = traceql.execute(self.cfg, '{ .http.route = "/checkout" && status = error }')
        self.assertEqual(len(rows), 1)
        rows = traceql.execute(self.cfg, '{ .http.route = "/cart" || status = ok }')
        self.assertEqual(rows, [])

    def test_duration_and_regex(self):
        rows = traceql.execute(self.cfg, "{ duration > 10ms }")
        self.assertEqual(len(rows), 1)
        rows = traceql.execute(self.cfg, '{ .http.route =~ "/check.*" }')
        self.assertEqual(len(rows), 1)

    def test_resource_attr(self):
        rows = traceql.execute(self.cfg, '{ resource.service.name = "checkout" }')
        self.assertEqual(len(rows), 1)

    def test_aggregation_pipes(self):
        rows = traceql.execute(self.cfg, '{ .http.route = "/checkout" } | count()')
        self.assertEqual(rows, [{"count": 1}])
        rows = traceql.execute(self.cfg, '{ .http.route = "/checkout" } | avg(duration)')
        self.assertEqual(rows, [{"avg": 48_000_000.0}])

    def test_aggregation_by(self):
        rows = traceql.execute(self.cfg, "{ } | count() by (resource.service.name)")
        self.assertEqual(rows, [{"resource.service.name": "checkout", "count": 1}])

    def test_parse_error_is_readable(self):
        with self.assertRaises(ParseError):
            traceql.parse("{ name = }")


class TestTraceQLStructural(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="otelite-ql-"))
        self.cfg = make_cfg(self.tmp)
        conn = db.connect(self.cfg.db_path)
        db.insert_spans(conn, spans_from(tree_payload(), time.time_ns()))
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_descendant(self):
        rows = traceql.execute(self.cfg, '{ name = "root-op" } >> { name = "child-op" }')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "child-op")

    def test_ancestor(self):
        rows = traceql.execute(self.cfg, '{ name = "root-op" } << { name = "child-op" }')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "root-op")
        self.assertEqual(traceql.execute(self.cfg, '{ name = "child-op" } << { name = "root-op" }'), [])

    def test_sibling(self):
        rows = traceql.execute(self.cfg, '{ name = "child-op" } ~ { name = "child-two" }')
        self.assertEqual(sorted(row["name"] for row in rows), ["child-op", "child-two"])

    def test_struct_with_pipe(self):
        rows = traceql.execute(self.cfg, '{ name = "root-op" } << { } | count()')
        self.assertEqual(rows, [{"count": 1}])


def tree_payload():
    payload = traces_payload()
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    root = spans[0]
    root["name"] = "root-op"
    child = dict(root, name="child-op", spanId="00f067aa0ba902b7", parentSpanId=root["spanId"])
    child2 = dict(root, name="child-two", spanId="00f067aa0ba902b8", parentSpanId=root["spanId"])
    spans.extend([child, child2])
    return payload


class TestLogQL(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="otelite-ql-"))
        self.cfg = make_cfg(self.tmp)
        day_dir = self.cfg.logs_dir / "2024-10-19"
        day_dir.mkdir(parents=True)
        with open(day_dir / "checkout.jsonl", "w", encoding="utf-8") as handle:
            for record in log_records_from(logs_payload()):
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_selector_by_service(self):
        rows = logql.execute(self.cfg, '{service="checkout"}', from_ms=0, to_ms=FIXTURE_MS + 1000)
        self.assertEqual(len(rows), 2)

    def test_line_filters(self):
        rows = logql.execute(self.cfg, '{service="checkout"} |= "failed"', from_ms=0, to_ms=FIXTURE_MS + 1000)
        self.assertEqual(len(rows), 1)
        rows = logql.execute(self.cfg, '{service="checkout"} != "recovered"', from_ms=0, to_ms=FIXTURE_MS + 1000)
        self.assertEqual(len(rows), 1)
        rows = logql.execute(self.cfg, '{service="checkout"} |~ "fail.*cart"', from_ms=0, to_ms=FIXTURE_MS + 1000)
        self.assertEqual(len(rows), 1)

    def test_severity_matcher(self):
        rows = logql.execute(self.cfg, '{service="checkout", severity="ERROR"}', from_ms=0, to_ms=FIXTURE_MS + 1000)
        self.assertEqual(len(rows), 1)
        rows = logql.execute(self.cfg, '{service="checkout", severity=~"I.*"}', from_ms=0, to_ms=FIXTURE_MS + 1000)
        self.assertEqual(len(rows), 1)

    def test_attribute_label_and_field_stage(self):
        rows = logql.execute(
            self.cfg,
            '{service="checkout", cart.id="42"}',
            from_ms=0,
            to_ms=FIXTURE_MS + 1000,
        )
        self.assertEqual(len(rows), 1)

    def test_line_filter_hits_body_not_envelope(self):
        # a body substring matches even when the record's timestamps would
        # also match the pattern (the JSON envelope must not be the line)
        rows = logql.execute(self.cfg, '{service="checkout"} |~ "51.*000000"', from_ms=0, to_ms=FIXTURE_MS + 1000)
        self.assertEqual(rows, [])

    def test_logfmt_and_regexp_extractors(self):
        day_dir = self.cfg.logs_dir / "2024-10-19"
        with open(day_dir / "applog.jsonl", "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "time_unix_nano": FIXTURE_NS,
                        "severity_text": "INFO",
                        "service_name": "app",
                        "body": 'level=error msg="slow query" dur=1.5',
                    }
                )
                + "\n"
            )
            handle.write(
                json.dumps(
                    {
                        "time_unix_nano": FIXTURE_NS + 1000,
                        "severity_text": "INFO",
                        "service_name": "app",
                        "body": "status=500 took=12ms",
                    }
                )
                + "\n"
            )
        rows = logql.execute(self.cfg, '{service="app"} | logfmt | level="error"', from_ms=0, to_ms=FIXTURE_MS + 10)
        self.assertEqual(len(rows), 1)
        rows = logql.execute(
            self.cfg,
            '{service="app"} | regexp "status=(?P<status>\\d+)" | status="500"',
            from_ms=0,
            to_ms=FIXTURE_MS + 10,
        )
        self.assertEqual(len(rows), 1)
        # a regexp that does not match drops the line, like Loki
        rows = logql.execute(
            self.cfg,
            '{service="app"} | regexp "nomatch(?P<x>\\d+)"',
            from_ms=0,
            to_ms=FIXTURE_MS + 10,
        )
        self.assertEqual(rows, [])

    def test_range_excludes_outside(self):
        rows = logql.execute(self.cfg, '{service="checkout"}', from_ms=0, to_ms=FIXTURE_MS - 1000)
        self.assertEqual(rows, [])


class TestPromQL(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="otelite-ql-"))
        self.cfg = make_cfg(self.tmp)
        conn = db.connect(self.cfg.db_path)
        for metric in promql_metrics():
            db.insert_metric(conn, metric)
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_selector(self):
        rows = promql.execute(self.cfg, 'checkout_requests{route="/checkout"}', to_ms=FIXTURE_MS + 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], 42.0)

    def test_selector_regex(self):
        rows = promql.execute(self.cfg, 'checkout_requests{route=~"/ch.*"}', to_ms=FIXTURE_MS + 10)
        self.assertEqual(len(rows), 1)
        rows = promql.execute(self.cfg, 'checkout_requests{route="/cart"}', to_ms=FIXTURE_MS + 10)
        self.assertEqual(rows, [])

    def test_rate(self):
        rows = promql.execute(self.cfg, "rate(page_hits[5m])", from_ms=0, to_ms=FIXTURE_MS + 10)
        self.assertEqual(len(rows), 1)
        # two samples two seconds apart, 10 apart in value -> 5/s
        self.assertAlmostEqual(rows[0]["value"], 5.0)

    def test_aggregation_by(self):
        rows = promql.execute(self.cfg, "sum by (service) (queue_depth)", to_ms=FIXTURE_MS + 10)
        # instant-vector semantics: last sample per series, then grouped
        self.assertEqual(
            rows,
            [{"metric": "", "labels": {"service": "checkout"}, "time_ms": None, "value": 2.5}],
        )
        rows = promql.execute(self.cfg, "sum(queue_depth)", to_ms=FIXTURE_MS + 10)
        self.assertEqual(rows[0]["value"], 2.5)

    def test_increase(self):
        rows = promql.execute(self.cfg, "increase(page_hits[5m])", from_ms=0, to_ms=FIXTURE_MS + 10)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["value"], 10.0)

    def test_over_time(self):
        rows = promql.execute(self.cfg, "avg_over_time(queue_depth[5m])", to_ms=FIXTURE_MS + 10)
        self.assertEqual(rows[0]["value"], 3.0)
        rows = promql.execute(self.cfg, "sum_over_time(queue_depth[5m])", to_ms=FIXTURE_MS + 10)
        self.assertEqual(rows[0]["value"], 6.0)
        rows = promql.execute(self.cfg, "count_over_time(queue_depth[5m])", to_ms=FIXTURE_MS + 10)
        self.assertEqual(rows[0]["value"], 2)

    def test_topk_bottomk_quantile(self):
        rows = promql.execute(self.cfg, "topk(1, latency)", to_ms=FIXTURE_MS + 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], 40.0)
        self.assertEqual(rows[0]["labels"], {"service": "checkout", "p": "b"})
        rows = promql.execute(self.cfg, "bottomk(1, latency)", to_ms=FIXTURE_MS + 10)
        self.assertEqual(rows[0]["value"], 20.0)
        rows = promql.execute(self.cfg, "quantile(0.5, latency)", to_ms=FIXTURE_MS + 10)
        self.assertEqual(rows, [{"metric": "", "labels": {}, "time_ms": None, "value": 30.0}])

    def test_comparison_filter_and_bool(self):
        rows = promql.execute(self.cfg, "latency > 25", to_ms=FIXTURE_MS + 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], 40.0)
        rows = promql.execute(self.cfg, "latency > bool 25", to_ms=FIXTURE_MS + 10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(row["value"] for row in rows), [0.0, 1.0])

    def test_offset(self):
        rows = promql.execute(self.cfg, "sum(page_hits offset 5s)", to_ms=FIXTURE_MS + 10)
        # shifted 5s back, only the older sample is visible -> 100 not 110
        self.assertEqual(rows[0]["value"], 100.0)

    def test_parse_error(self):
        with self.assertRaises(ParseError):
            promql.parse("sum by (")


def promql_metrics():
    base = FIXTURE_MS - 5000
    return [
        {
            "name": "checkout_requests",
            "description": None,
            "unit": "1",
            "kind": "sum",
            "monotonic": 1,
            "service_name": "checkout",
            "points": [(FIXTURE_MS, 42, {"route": "/checkout"})],
        },
        {
            "name": "page_hits",
            "description": None,
            "unit": "1",
            "kind": "sum",
            "monotonic": 1,
            "service_name": "frontend",
            "points": [(base, 100, {}), (base + 2000, 110, {})],
        },
        {
            "name": "queue_depth",
            "description": None,
            "unit": "1",
            "kind": "gauge",
            "monotonic": None,
            "service_name": "checkout",
            "points": [(FIXTURE_MS, 3.5, {}), (FIXTURE_MS + 1, 2.5, {})],
        },
        {
            "name": "latency",
            "description": None,
            "unit": "ms",
            "kind": "gauge",
            "monotonic": None,
            "service_name": "checkout",
            "points": [
                (FIXTURE_MS, 10, {"p": "a"}),
                (FIXTURE_MS, 30, {"p": "b"}),
                (FIXTURE_MS + 1, 20, {"p": "a"}),
                (FIXTURE_MS + 1, 40, {"p": "b"}),
            ],
        },
    ]


if __name__ == "__main__":
    unittest.main()
