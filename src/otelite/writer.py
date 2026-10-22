import os
import queue
import sqlite3
import threading
import time

from . import db, logsio

CHECKPOINT_INTERVAL_S = 30


def _queue(size):
    return queue.Queue(size)


def _list():
    return []


class Writer:
    """One thread owns all sqlite and log-file writes; HTTP handlers only enqueue."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.queue = _queue(cfg.queue_size)
        self.logs = logsio.LogSink(cfg.logs_dir)
        self.errors = logsio.LogSink(cfg.errors_dir)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="otelite-writer", daemon=True)
        self._checkpoint = threading.Thread(target=self._checkpoint_loop, name="otelite-checkpoint", daemon=True)
        self._checkpoint_interval_s = int(os.environ.get("OTELITE_CHECKPOINT_SECONDS", CHECKPOINT_INTERVAL_S))

    def start(self):
        self._thread.start()
        self._checkpoint.start()

    def submit(self, job):
        try:
            self.queue.put_nowait(job)
            return True
        except queue.Full:
            return False

    def stop(self):
        self._stop.set()
        self.queue.put(None)
        self._thread.join(timeout=5)

    def _run(self):
        conn = db.connect(self.cfg.db_path)
        stop = False
        while not stop:
            batch = _list()
            deadline = time.monotonic() + self.cfg.flush_ms / 1000
            while len(batch) < self.cfg.batch_size:
                try:
                    item = self.queue.get(timeout=max(deadline - time.monotonic(), 0))
                except queue.Empty:
                    break
                if item is None:
                    stop = True
                    break
                batch.append(item)
            if batch:
                self._flush(conn, batch)
        conn.close()
        self.logs.close()
        self.errors.close()

    def _flush(self, conn, batch):
        span_rows = []
        metric_jobs = []
        log_records = []
        error_records = []
        for kind, payload in batch:
            if kind == "spans":
                span_rows.extend(payload)
            elif kind == "metrics":
                metric_jobs.extend(payload)
            elif kind == "errors":
                error_records.extend(payload)
            else:
                log_records.extend(payload)
        if span_rows or metric_jobs:
            try:
                conn.execute("BEGIN")
                if span_rows:
                    db.insert_spans(conn, span_rows)
                for metric in metric_jobs:
                    db.insert_metric(conn, metric)
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
        if log_records:
            self.logs.write(log_records)
        if error_records:
            self.errors.write(error_records)

    def _checkpoint_loop(self):
        while not self._stop.wait(self._checkpoint_interval_s):
            try:
                conn = db.connect(self.cfg.db_path)
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                conn.close()
            except sqlite3.Error:
                pass
