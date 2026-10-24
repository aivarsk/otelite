import json
import shutil
import tarfile
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from test_http import HttpTestBase
from test_normalize import logs_payload, traces_payload

from otelite import archiveio, db
from otelite.normalize import log_records_from, spans_from


def make_data_dir(root):
    root = Path(root)
    (root / "logs" / "2026-10-23").mkdir(parents=True)
    (root / "errors" / "2026-10-23").mkdir(parents=True)
    conn = db.connect(root / "otelite.sqlite")
    db.insert_spans(conn, spans_from(traces_payload(), time.time_ns()))
    conn.commit()
    conn.close()
    with open(root / "logs" / "2026-10-23" / "checkout.jsonl", "w", encoding="utf-8") as handle:
        for record in log_records_from(logs_payload()):
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    (root / "errors" / "2026-10-23" / "1-mykey.jsonl").write_text(
        '{"time_unix_nano":1729328732051000000,"kind":"ValueError","group":"abc","event":{}}\n',
        encoding="utf-8",
    )
    return root


def zip_dir(source, target, prefix=""):
    with zipfile.ZipFile(target, "w") as zf:
        for path in sorted(Path(source).rglob("*")):
            if path.is_file():
                zf.write(path, prefix + str(path.relative_to(source)))


def tar_dir(source, target, prefix=""):
    with tarfile.open(target, "w:gz") as tf:
        for path in sorted(Path(source).rglob("*")):
            if path.is_file():
                tf.add(path, prefix + str(path.relative_to(source)))


class TestExtract(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="otelite-archive-test-"))
        self.data = make_data_dir(self.tmp / "data")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_zip_roundtrip(self):
        archive = self.tmp / "run.zip"
        zip_dir(self.data, archive)
        root = archiveio.extract(archive)
        self.assertEqual((root / "otelite.sqlite").name, "otelite.sqlite")
        self.assertTrue((root / "logs" / "2026-10-23" / "checkout.jsonl").exists())
        self.assertTrue((root / "errors" / "2026-10-23" / "1-mykey.jsonl").exists())
        # the extracted copy is readable through the normal db path
        conn = db.connect(root / "otelite.sqlite")
        count = conn.execute("SELECT count(*) FROM spans").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_tar_gz_roundtrip(self):
        archive = self.tmp / "run.tgz"
        tar_dir(self.data, archive)
        root = archiveio.extract(archive)
        self.assertTrue((root / "otelite.sqlite").exists())

    def test_single_top_level_folder_is_tolerated(self):
        # people zip the directory, not its contents
        archive = self.tmp / "run.zip"
        zip_dir(self.data, archive, prefix="otelite-ci-run-42/")
        root = archiveio.extract(archive)
        self.assertTrue((root / "otelite.sqlite").exists())

    def test_no_database_rejected(self):
        archive = self.tmp / "empty.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("README.txt", "not an otelite archive")
        with self.assertRaises(ValueError):
            archiveio.extract(archive)

    def test_junk_rejected(self):
        archive = self.tmp / "junk.tgz"
        archive.write_bytes(b"this is not an archive")
        with self.assertRaises(ValueError):
            archiveio.extract(archive)

    def test_zip_path_traversal_rejected(self):
        archive = self.tmp / "evil.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../escape.txt", "nope")
        with self.assertRaises(ValueError):
            archiveio.extract(archive)


class TestArchiveModeHttp(HttpTestBase, unittest.TestCase):
    def test_ingest_rejected_but_reads_work(self):
        self.cfg.archive = "run.zip"  # the route reads the flag live
        status, body = self.post("/v1/traces", traces_payload())
        self.assertEqual(status, 403)
        self.assertIn("read-only", body["message"])
        status, body = self.post("/api/42/store/", {"event_id": "x" * 32})
        self.assertEqual(status, 403)
        status, _, data = self.get("/strace/traces", {"from": 0, "to": int(time.time() * 1000) + 60000})
        self.assertEqual(status, 200)
        self.assertIn("traces", data)
        status, _, data = self.get("/healthz")
        self.assertEqual(status, 200)

    def test_normal_mode_still_ingests(self):
        self.assertIsNone(self.cfg.archive)
        status, _ = self.post("/v1/traces", traces_payload())
        self.assertEqual(status, 200)
