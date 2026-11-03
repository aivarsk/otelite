import shutil
import tempfile
import unittest
from pathlib import Path

from fixtures import traces_payload

from otelite import cli, db
from otelite.normalize import spans_from

OLD_NS = "1000000000000000000"  # 2001-09-09
NEW_NS = "4102444800000000000"  # 2100-01-01
OLD_MS = 1_000_000_000_000
NEW_MS = 4_102_444_800_000


def span_payload(name, nano):
    payload = traces_payload()
    span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    span["name"] = name
    span["startTimeUnixNano"] = nano
    span["endTimeUnixNano"] = nano
    return payload


class TestPrune(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="otelite-prune-test-"))
        self.data = self.tmp / "data"
        (self.data / "logs" / "2001-09-09").mkdir(parents=True)
        (self.data / "logs" / "2001-09-09" / "svc.jsonl").write_text("{}\n", encoding="utf-8")
        (self.data / "logs" / "2100-01-01").mkdir(parents=True)
        (self.data / "logs" / "2100-01-01" / "svc.jsonl").write_text("{}\n", encoding="utf-8")
        conn = db.connect(self.data / "otelite.sqlite")
        conn.executemany(
            "INSERT OR IGNORE INTO spans (trace_id, span_id, parent_id, name, kind, status_code, status_msg, service_name, start_ns, end_ns, attrs, events, links, resource, scope_name, scope_ver, dropped, received_ns) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', NULL, NULL, '{}', NULL, NULL, 0, 0)",
            [
                (row[0], row[1], None, row[3], 0, 0, None, "svc", row[8], row[9])
                for row in (
                    spans_from(span_payload("old-span", OLD_NS), 1) + spans_from(span_payload("new-span", NEW_NS), 1)
                )
            ],
        )
        conn.execute(
            "INSERT OR IGNORE INTO samples (ts, metric_id, attrset_id, service_name, value) VALUES (?, 1, 1, 'svc', 1.0)",
            (OLD_MS,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO samples (ts, metric_id, attrset_id, service_name, value) VALUES (?, 1, 1, 'svc', 1.0)",
            (NEW_MS,),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prune_removes_old_days_and_rows(self):
        rc = cli.main(["--data-dir", str(self.data), "--prune", "7"])
        self.assertEqual(rc, 0)
        self.assertFalse((self.data / "logs" / "2001-09-09").exists())
        self.assertTrue((self.data / "logs" / "2100-01-01").exists())
        conn = db.connect(self.data / "otelite.sqlite")
        names = [r[0] for r in conn.execute("SELECT name FROM spans ORDER BY name").fetchall()]
        sample_ts = [r[0] for r in conn.execute("SELECT ts FROM samples").fetchall()]
        conn.close()
        self.assertEqual(names, ["new-span"])
        self.assertEqual(sample_ts, [NEW_MS])

    def test_prune_on_empty_dir_is_fine(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertEqual(cli.main(["--data-dir", str(empty), "--prune", "7"]), 0)


if __name__ == "__main__":
    unittest.main()
