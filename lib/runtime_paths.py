# Copyright 2026 Bogdan Carp (@EnigmaThe1)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

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
