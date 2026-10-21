import random
import unittest

from otelite import demo


class TestDemoPayloads(unittest.TestCase):
    def test_trace_batch_shape(self):
        rng = random.Random(1)
        batch, trace_id, root_id = demo.trace_batch("checkout", "POST /pay", rng, 1_729_328_732_051_000_000, 25, True)
        spans = batch["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[0]["traceId"], trace_id)
        self.assertEqual(spans[1]["parentSpanId"], root_id)
        self.assertEqual(spans[0]["status"]["code"], 2)

    def test_log_batch_correlates(self):
        batch = demo.log_batch("frontend", "ab" * 16, "cd" * 8, False, 12)
        record = batch["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
        self.assertEqual(record["traceId"], "ab" * 16)
        self.assertEqual(record["spanId"], "cd" * 8)
        self.assertIn("200", record["body"]["stringValue"])

    def test_metric_batch_has_latency_and_counter(self):
        batch = demo.metric_batch("frontend", 15, 42)
        names = [m["name"] for m in batch["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]]
        self.assertEqual(names, ["demo.request.latency", "demo.requests"])


if __name__ == "__main__":
    unittest.main()
