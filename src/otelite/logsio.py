import collections
import json
from pathlib import Path


class LogSink:
    """Append-only JSONL handles keyed by (day, service).

    One flattened JSON record per line, nanosecond timestamps, trace and
    span ids kept as hex strings so `grep <trace-id> logs/` is the trace
    join. No compression, ever: bare grep is the whole toolchain. A crash
    may leave a partial last line; readers skip unparseable tails.
    """

    def __init__(self, root):
        self.root = Path(root)
        self._handles = {}

    def write(self, records):
        by_key = collections.defaultdict(list)
        for record in records:
            key = (record["day"], record["service_name"] or "unknown")
            by_key[key].append(record)
        for key, group in by_key.items():
            handle = self._handle(key)
            for record in group:
                # records come from parsed JSON, they cannot be circular
                handle.write(json.dumps(record, separators=(",", ":"), check_circular=False) + "\n")
            handle.flush()

    def _handle(self, key):
        if key not in self._handles:
            day_dir = self.root / key[0]
            day_dir.mkdir(parents=True, exist_ok=True)
            self._handles[key] = open(day_dir / f"{key[1]}.jsonl", "a", encoding="utf-8")
        return self._handles[key]

    def close(self):
        for handle in self._handles.values():
            handle.close()
        self._handles.clear()
