"""CLI entry for otelite.ql: one query in, JSONL rows out."""

import argparse
import json
import sys
from pathlib import Path

from . import LANGUAGES, traceql
from ..config import Config


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="otelite.ql",
        description="Run a traceql/logql/promql query against an otelite data directory and print JSONL.",
    )
    parser.add_argument("--data-dir", type=Path, default=None, help="otelite data directory")
    parser.add_argument("--from", dest="from_ms", type=int, default=None, help="range start, epoch milliseconds")
    parser.add_argument("--to", dest="to_ms", type=int, default=None, help="range end, epoch milliseconds")
    parser.add_argument("--limit", type=int, default=1000, help="max rows (default 1000)")
    parser.add_argument("language", choices=sorted(LANGUAGES), help="query language")
    parser.add_argument("query", help="the query text")
    args = parser.parse_args(argv)

    cfg = Config.from_env(data_dir=args.data_dir)
    module = LANGUAGES[args.language]
    try:
        if module is traceql:
            rows = module.execute(cfg, args.query, limit=args.limit)
        else:
            rows = module.execute(cfg, args.query, from_ms=args.from_ms, to_ms=args.to_ms, limit=args.limit)
    except Exception as exc:  # ParseError and friends land here, message first
        print(str(exc), file=sys.stderr)
        return 2
    for row in rows:
        print(json.dumps(row, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
