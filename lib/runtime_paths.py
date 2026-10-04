from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

APP = "claude-autonomy"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def data_home() -> Path:
    if os.environ.get("CLAUDE_AUTONOMY_HOME"):
        return Path(os.environ["CLAUDE_AUTONOMY_HOME"]).expanduser().resolve()
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return (base / APP).resolve()


def package_root() -> Path:
    return Path(__file__).resolve().parent.parent


def model_registry_path() -> Path:
    return data_home() / "model-registry.json"



def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path
