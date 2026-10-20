import unittest

from otelite import normalize

TRACE_ID = "5b8efff798038103d269b633813fc60c"
SPAN_ID = "eee19b7ec3c1b174"
PARENT_ID = "f2e78b4f7c3b2a19"


def traces_payload():
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "checkout"}},
                        {"key": "deployment.environment.name", "value": {"stringValue": "dev"}},
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "my.app", "version": "1.2.3"},
                        "spans": [
                            {
                                "traceId": TRACE_ID,
                                "spanId": SPAN_ID,
                                "parentSpanId": PARENT_ID,
                                "name": "POST /checkout",
                                "kind": 2,
                                "startTimeUnixNano": "1729328732051000000",
                                "endTimeUnixNano": 1729328732099000000,
                                "attributes": [
                                    {"key": "http.route", "value": {"stringValue": "/checkout"}},
                                    {"key": "retries", "value": {"intValue": "3"}},
                                    {"key": "ok", "value": {"boolValue": True}},
                                    {
                                        "key": "tags",
                                        "value": {"arrayValue": {"values": [{"stringValue": "a"}, {"intValue": 1}]}},
                                    },
                                ],
                                "events": [
                                    {
                                        "timeUnixNano": 1729328732060000000,
                                        "name": "cart.loaded",
                                        "attributes": [{"key": "cart.id", "value": {"intValue": 42}}],
                                    }
                                ],
                                "links": [{"traceId": TRACE_ID, "spanId": PARENT_ID}],
                                "status": {"code": "STATUS_CODE_ERROR", "message": "boom"},
                                "droppedAttributesCount": 1,
                            }
                        ],
                    }
                ],
            }
        ]
    }


def metrics_payload():
    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "checkout"}}]},
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": "checkout.requests",
                                "description": "requests",
                                "unit": "1",
                                "data": {
                                    "sum": {
                                        "dataPoints": [
                                            {
                                                "timeUnixNano": "1729328732051000000",
                                                "asInt": "42",
                                                "attributes": [{"key": "route", "value": {"stringValue": "/checkout"}}],
                                            }
                                        ],
                                        "isMonotonic": True,
                                        "aggregationTemporality": 2,
                                    }
                                },
                            },
                            {
                                "name": "queue.depth",
                                "data": {
                                    "gauge": {"dataPoints": [{"timeUnixNano": 1729328732051000000, "asDouble": 3.5}]}
                                },
                            },
                            {
                                "name": "latency.histogram",
                                "data": {"histogram": {"dataPoints": [{"timeUnixNano": 1729328732051000000}]}},
                            },
                        ]
                    }
                ],
            }
        ]
    }


def logs_payload():
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "checkout"}}]},
                "scopeLogs": [
                    {
                        "scope": {"name": "my.app"},
                        "logRecords": [
                            {
                                "timeUnixNano": 1729328732051000000,
                                "severityNumber": 17,
                                "severityText": "ERROR",
                                "body": {"stringValue": "checkout failed for cart 42"},
                                "attributes": [{"key": "cart.id", "value": {"intValue": 42}}],
                                "traceId": TRACE_ID.upper(),
                                "spanId": SPAN_ID.upper(),
                            },
                            {
                                "observedTimeUnixNano": 1729328732100000000,
                                "severityNumber": 9,
                                "severityText": "INFO",
                                "body": {"stringValue": "recovered"},
                            },
                        ],
                    }
                ],
            }
        ]
    }


class TestNormalize(unittest.TestCase):
    def test_span_row(self):
        rows = normalize.spans_from(traces_payload(), received_ns=1)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        (
            trace_id,
            span_id,
            parent_id,
            name,
            kind,
            status_code,
            status_msg,
            service_name,
            start_ns,
            end_ns,
            attrs,
            events,
            links,
            resource,
            scope_name,
            scope_ver,
            dropped,
            received_ns,
        ) = row
        self.assertEqual(trace_id, bytes.fromhex(TRACE_ID))
        self.assertEqual(span_id, bytes.fromhex(SPAN_ID))
        self.assertEqual(parent_id, bytes.fromhex(PARENT_ID))
        self.assertEqual(name, "POST /checkout")
        self.assertEqual(kind, 2)
        self.assertEqual(status_code, 2)
        self.assertEqual(status_msg, "boom")
        self.assertEqual(service_name, "checkout")
        self.assertEqual(start_ns, 1729328732051000000)
        self.assertEqual(end_ns, 1729328732099000000)
        self.assertIn('"http.route":"/checkout"', attrs)
        self.assertIn('"retries":3', attrs)
        self.assertIn('"ok":true', attrs)
        self.assertIn('"tags":["a",1]', attrs)
        self.assertIn("cart.loaded", events)
        self.assertIn(TRACE_ID, links)
        self.assertIn('"service.name":"checkout"', resource)
        self.assertEqual(scope_name, "my.app")
        self.assertEqual(scope_ver, "1.2.3")
        self.assertEqual(dropped, 1)
        self.assertEqual(received_ns, 1)

    def test_span_dropped_when_ids_bad(self):
        payload = traces_payload()
        payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["spanId"] = "not-hex"
        self.assertEqual(normalize.spans_from(payload, received_ns=1), [])

    def test_legacy_instrumentation_library_key(self):
        payload = traces_payload()
        group = payload["resourceSpans"][0].pop("scopeSpans")
        group[0]["instrumentationLibrary"] = group[0].pop("scope")
        payload["resourceSpans"][0]["instrumentationLibrarySpans"] = group
        rows = normalize.spans_from(payload, received_ns=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][14], "my.app")

    def test_metrics_sum_and_gauge(self):
        metrics = normalize.metrics_from(metrics_payload())
        self.assertEqual([m["name"] for m in metrics], ["checkout.requests", "queue.depth"])
        sum_metric = metrics[0]
        self.assertEqual(sum_metric["kind"], "sum")
        self.assertEqual(sum_metric["monotonic"], 1)
        self.assertEqual(sum_metric["unit"], "1")
        self.assertEqual(sum_metric["service_name"], "checkout")
        self.assertEqual(sum_metric["points"], [(1729328732051, "42", {"route": "/checkout"})])
        gauge = metrics[1]
        self.assertEqual(gauge["kind"], "gauge")
        self.assertIsNone(gauge["monotonic"])
        self.assertEqual(gauge["points"][0][1], 3.5)

    def test_histogram_metric_skipped(self):
        metrics = normalize.metrics_from(metrics_payload())
        self.assertNotIn("latency.histogram", [m["name"] for m in metrics])

    def test_log_records(self):
        records = normalize.log_records_from(logs_payload())
        self.assertEqual(len(records), 2)
        error = records[0]
        self.assertEqual(error["time_unix_nano"], 1729328732051000000)
        self.assertEqual(error["severity_number"], 17)
        self.assertEqual(error["severity_text"], "ERROR")
        self.assertEqual(error["body"], "checkout failed for cart 42")
        self.assertEqual(error["attributes"], {"cart.id": 42})
        self.assertEqual(error["trace_id"], TRACE_ID)
        self.assertEqual(error["span_id"], SPAN_ID)
        self.assertEqual(error["service_name"], "checkout")
        self.assertEqual(error["scope"], "my.app")
        self.assertEqual(error["day"], "2024-10-19")
        info = records[1]
        self.assertEqual(info["body"], "recovered")


if __name__ == "__main__":
    unittest.main()
