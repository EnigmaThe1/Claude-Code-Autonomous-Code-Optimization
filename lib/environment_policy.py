from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping, MutableMapping


_GIT_CONFIG_INLINE_RE = re.compile(r"^GIT_CONFIG_(?:KEY|VALUE)_\d+$")
_RESUME_ENV_KEYS = (
    "PATH",
    "CARGO_HOME",
    "RUSTUP_HOME",
    "JAVA_HOME",
    "GOROOT",
    "GOPATH",
    "PNPM_HOME",
    "BUN_INSTALL",
    "UV_TOOL_BIN_DIR",
    "UV_CACHE_DIR",
    "PIP_CACHE_DIR",
    "CARGO_TARGET_DIR",
    "VIRTUAL_ENV",
)
_SENSITIVE_HOME_PARTS = {
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    ".docker",
    ".config",
}


def _is_inline_git_config_name(name: str) -> bool:
    return (
        name in {"GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"}
        or bool(_GIT_CONFIG_INLINE_RE.fullmatch(name))
    )


def strip_injected_git_config(env: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """Remove process-scoped Git config injection without discarding normal Git auth.

    GIT_CONFIG_COUNT/KEY_n/VALUE_n and GIT_CONFIG_PARAMETERS are convenient for
    one command but are hazardous when inherited by a long-running autonomous
    launcher: malformed or stale values can poison every later Git probe. Keep
    ordinary variables such as GIT_SSH_COMMAND and credential-helper context.
    """
    for key in list(env):
        if _is_inline_git_config_name(key):
            env.pop(key, None)
    return env


def sanitised_subprocess_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if source is None else source)
    strip_injected_git_config(env)
    # Hooks and package modules may live on read-only mounts. Python should never
    # need to create __pycache__ beside package/hook source in autonomous runs.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def capture_resume_environment(
    root: Path,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Capture only non-secret development environment needed for safe resume."""
    env = sanitised_subprocess_env(source)
    root = root.expanduser().resolve()
    out: dict[str, str] = {}
    for key in _RESUME_ENV_KEYS:
        value = env.get(key)
        if not value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            continue
        if key == "VIRTUAL_ENV":
            try:
                Path(value).expanduser().resolve().relative_to(root)
            except (OSError, RuntimeError, ValueError):
                continue
        out[key] = value
    out["PYTHONDONTWRITEBYTECODE"] = "1"
    return out


def apply_resume_environment(
    saved: Mapping[str, str] | None,
    target: MutableMapping[str, str] | None = None,
) -> dict[str, str]:
    """Restore the allowlisted development environment into this process."""
    target = os.environ if target is None else target
    allowed = set(_RESUME_ENV_KEYS) | {"PYTHONDONTWRITEBYTECODE"}
    if isinstance(saved, Mapping):
        for key, value in saved.items():
            if key not in allowed or not isinstance(value, str) or not value:
                continue
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
                continue
            target[key] = value
    strip_injected_git_config(target)
    target["PYTHONDONTWRITEBYTECODE"] = "1"
    return dict(target)


def home_toolchain_path_entries(
    path_value: str | None = None,
    *,
    home: Path | None = None,
) -> list[Path]:
    """Return safe existing PATH directories below HOME for sandbox read access.

    This intentionally discovers custom user toolchain directories (for example
    ~/.local/share/<project>/toolchain/bin) without opening credential/config
    trees. Only directories already present in PATH are considered.
    """
    home = (home or Path.home()).expanduser().resolve()
    raw = os.environ.get("PATH", "") if path_value is None else path_value
    out: list[Path] = []
    for entry in raw.split(os.pathsep):
        if not entry:
            continue
        try:
            p = Path(entry).expanduser().resolve(strict=True)
            p.relative_to(home)
        except (OSError, RuntimeError, ValueError):
            continue
        try:
            rel = p.relative_to(home)
        except ValueError:
            continue
        if any(part in _SENSITIVE_HOME_PARTS for part in rel.parts):
            continue
        if p.is_dir() and p not in out:
            out.append(p)
    return out
