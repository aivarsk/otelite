import os
from pathlib import Path

APP_NAME = "otelite"
try:
    from importlib.metadata import version

    APP_VERSION = version(APP_NAME)
except Exception:
    APP_VERSION = "dev"


def _app_dir(system_dir, env_var, user_default):
    if os.geteuid() == 0:
        return Path(system_dir) / APP_NAME
    return Path(os.environ.get(env_var, os.path.expanduser(user_default))) / APP_NAME


def default_data_dir():
    override = os.environ.get("OTELITE_DATA_DIR")
    if override:
        return Path(override)
    return _app_dir("/var/lib", "XDG_STATE_HOME", "~/.local/state")


class Config:
    def __init__(
        self, root, port=4318, max_body_bytes=32 * 1024 * 1024, queue_size=10000, batch_size=1000, flush_ms=1000
    ):
        self.root = root
        self.port = port
        self.max_body_bytes = max_body_bytes
        self.queue_size = queue_size
        self.batch_size = batch_size
        self.flush_ms = flush_ms

    @property
    def db_path(self):
        return self.root / "otelite.sqlite"

    @property
    def logs_dir(self):
        return self.root / "logs"

    @classmethod
    def from_env(cls, port=None, data_dir=None):
        cfg = cls(root=data_dir or default_data_dir())
        if port is not None:
            cfg.port = port
        cfg.max_body_bytes = int(os.environ.get("OTELITE_MAX_BODY_BYTES", cfg.max_body_bytes))
        cfg.queue_size = int(os.environ.get("OTELITE_QUEUE_SIZE", cfg.queue_size))
        return cfg
