import argparse
import asyncio
import logging
import os
import shutil
import threading
from pathlib import Path

from . import archiveio, demo
from .config import APP_VERSION, Config
from .server import serve
from .writer import Writer


def main(argv=None):
    # writer and checkpoint threads run on 1 MiB stacks (pairs with LimitSTACK=1M)
    threading.stack_size(1024 * 1024)

    parser = argparse.ArgumentParser(prog="otelite", description="Small OTEL backend for test and dev.")
    parser.add_argument("--port", type=int, default=None, help="listen port (default 4318)")
    parser.add_argument("--data-dir", type=Path, default=None, help="instance data directory")
    parser.add_argument("--daemon", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    parser.add_argument("--version", action="store_true", help="show version and data locations")
    parser.add_argument("--demo", action="store_true", help="generate demo telemetry into this instance")
    parser.add_argument(
        "--archive",
        type=Path,
        default=None,
        help="serve a zipped or tarred otelite data directory read-only; ingestion is disabled",
    )
    args = parser.parse_args(argv)

    if args.version:
        cfg = Config.from_env()
        print("otelite", APP_VERSION)
        print("  database:  ", cfg.db_path)
        print("  log files: ", cfg.logs_dir)
        print("  error files:", cfg.errors_dir)
        return 0

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )

    cfg = Config.from_env(port=args.port, data_dir=args.data_dir)
    extracted = None
    if args.archive:
        if args.demo:
            parser.error("--demo cannot be combined with --archive")
        if args.data_dir:
            parser.error("--data-dir cannot be combined with --archive")
        if not args.archive.is_file():
            print(f"no such archive: {args.archive}")
            return 2
        try:
            extracted = archiveio.extract(args.archive)
        except ValueError as exc:
            print(exc)
            return 2
        cfg.root = extracted
        cfg.archive = str(args.archive)
        print(f"serving {args.archive} read-only from {extracted}; ingestion is disabled")
    cfg.root.mkdir(parents=True, exist_ok=True)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    cfg.errors_dir.mkdir(parents=True, exist_ok=True)

    writer = Writer(cfg)
    writer.start()
    if args.demo:
        demo_host = os.environ.get("OTELITE_DEMO_HOST", "127.0.0.1")
        threading.Thread(target=demo.serve, args=(cfg.port, demo_host), daemon=True).start()
    try:
        asyncio.run(serve(cfg, writer))
    except KeyboardInterrupt:
        pass
    finally:
        writer.stop()
        if extracted is not None:
            shutil.rmtree(extracted, ignore_errors=True)
    return 0
