"""Read-only viewing of sealed runs: extract an archived data directory.

CI recipe: point OTELITE_DATA_DIR at a job directory, archive the
directory into the build artifact (zip or tar.gz), and anyone holding
the artifact can run `otelite --archive run.tgz` to browse the run
exactly as it was collected. Extraction goes to a scratch directory
(the archive file itself is never touched), the page and the read APIs
work as usual, and every ingestion endpoint answers 403 - see the
archive check in server.py.
"""

import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path

DB_NAME = "otelite.sqlite"


def extract(archive_path):
    """Unpack a zip/tar.gz data-directory archive into a scratch dir.

    Returns the directory that holds otelite.sqlite (the data root),
    tolerating archives that wrap everything in a single top-level
    folder. Raises ValueError on junk input; the scratch dir is removed
    again by the caller (cli) when the viewer shuts down.
    """
    dest = Path(tempfile.mkdtemp(prefix="otelite-archive-"))
    try:
        if zipfile.is_zipfile(archive_path):
            _extract_zip(archive_path, dest)
        elif tarfile.is_tarfile(archive_path):
            _extract_tar(archive_path, dest)
        else:
            raise ValueError(f"{archive_path}: not a zip or tar.gz archive")
        root = _find_data_root(dest)
        if root is None:
            raise ValueError(f"{archive_path}: no {DB_NAME} in archive, not an otelite data directory")
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return root


def _extract_zip(archive_path, dest):
    # zipfile has no extraction filter, so keep every member inside dest
    base = dest.resolve()
    with zipfile.ZipFile(archive_path) as zf:
        for name in zf.namelist():
            target = (dest / name).resolve()
            if target != base and base not in target.parents:
                raise ValueError(f"unsafe path in archive: {name}")
        zf.extractall(dest)


def _extract_tar(archive_path, dest):
    # filter="data" refuses absolute paths, "..", and device files
    with tarfile.open(archive_path) as tf:
        tf.extractall(dest, filter="data")


def _find_data_root(dest):
    best = None
    for path in dest.rglob(DB_NAME):
        if best is None or len(path.parts) < len(best.parts):
            best = path
    return best.parent if best else None
