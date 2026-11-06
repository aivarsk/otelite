r"""Query languages over the otelite data: TraceQL, LogQL, PromQL subsets.

    python3 -m otelite.ql --data-dir /path/to/data traceql '{ .http.route = "/checkout" } | avg(duration)'
    python3 -m otelite.ql --data-dir /path/to/data logql '{service="checkout"} |= "failed"'
    python3 -m otelite.ql --data-dir /path/to/data promql 'sum by (service) (demo_requests)'

The same three languages drive the query boxes on the page (Traces, Logs,
Metrics panels) through the /ql/traceql, /ql/logql, /ql/promql endpoints.
Grep and SQL stay the old-school path; these are the modern one.

Compatibility, stated honestly: the grammars accept a subset of what
Tempo/Loki/Prometheus accept, kept syntactically faithful so that a query
which parses here and avoids our extensions will almost certainly parse
there. The reverse is not true. Known divergences to keep in mind:

- regexes run through Python's `re`: lookaheads and backreferences parse
  here but RE2 (the real engines) rejects them;
- string literals are lenient about unknown escapes (\d stays \d), the
  real languages require valid Go escapes;
- metric names may contain dots here, Prometheus proper forbids them;
- rate/increase are naive (first-to-last over the window, no reset
  handling, no extrapolation) and TraceQL aggregates return plain rows
  rather than spansets filtered by the aggregate;
- label sets are otelite's (service, severity, plus attributes), not the
  ecosystem's canonical names.
"""

from . import logql, promql, traceql

LANGUAGES = {
    "traceql": traceql,
    "logql": logql,
    "promql": promql,
}
