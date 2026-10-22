#!/usr/bin/env python3
"""Send real Sentry SDK error events into otelite.

This is the real-SDK check for the Sentry endpoints: sentry_sdk captures
exceptions, the DSN points at otelite (key and project id are labels there),
and the OTEL instrumenter runs Sentry on top of the same OTEL tracer
provider whose spans export to otelite, so error events share trace ids
with the OTLP traces and the page's error-to-waterfall join works.

Usage: sentry_send.py --url http://127.0.0.1:4318 --count 20
Needs sentry-sdk in the generator venv:
  generator/.venv/bin/pip install sentry-sdk
"""

import argparse
import random
import sys
import time

try:
    import sentry_sdk
    from sentry_sdk.integrations.opentelemetry.span_processor import SentrySpanProcessor
except ImportError:
    sys.exit("sentry-sdk is not installed; run: generator/.venv/bin/pip install sentry-sdk")

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

sys.path.insert(0, str(__file__).rsplit("/", 1)[0])
from gen import JsonTraceExporter  # noqa: E402

OPERATIONS = [
    ("reconcile cart", "divide stock by zero", ZeroDivisionError),
    ("charge card", "card token expired", ValueError),
]


def send_errors(url, count, seed):
    host = url.split("//", 1)[-1]
    provider = TracerProvider(resource=Resource.create({"service.name": "sentry-demo"}))
    provider.add_span_processor(SimpleSpanProcessor(JsonTraceExporter(url)))
    provider.add_span_processor(SentrySpanProcessor())
    trace.set_tracer_provider(provider)

    sentry_sdk.init(
        dsn=f"http://mykey@{host}/42",
        traces_sample_rate=1.0,
        instrumenter="otel",
    )

    rng = random.Random(seed)
    tracer = trace.get_tracer("otelite-sentry-sender")
    sent = 0
    for _ in range(count):
        name, detail, exc_type = rng.choice(OPERATIONS)
        with tracer.start_as_current_span(f"sentry-demo {name}") as span:
            try:
                raise exc_type(detail)
            except (ZeroDivisionError, ValueError):
                sentry_sdk.capture_exception()
                sent += 1
        time.sleep(0.05)
    sentry_sdk.flush()
    print(f"sent {sent} error events via the real sentry_sdk to {url}/api/42/")
    print("open the page, Errors panel: two groups, each expandable into its trace")


def main():
    parser = argparse.ArgumentParser(description="Send real Sentry SDK errors into otelite")
    parser.add_argument("--url", default="http://127.0.0.1:4318")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--seed", type=int, default=13)
    args = parser.parse_args()
    send_errors(args.url, args.count, args.seed)


if __name__ == "__main__":
    main()
