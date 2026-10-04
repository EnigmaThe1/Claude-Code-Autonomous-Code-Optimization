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

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping

from environment_policy import sanitised_subprocess_env
from operator_authority import require_top_level_operator
from repo_identity import repo_state_dir
from runtime_paths import ensure_private_dir, utcnow
from state_store import json_dump, load_json


_MAX_EXCLUDES_BYTES = 2 * 1024 * 1024


def _trust_dir(root: Path) -> Path:
    return ensure_private_dir(repo_state_dir(root) / "git-trust")


def _trust_path(root: Path) -> Path:
    return _trust_dir(root) / "policy.json"


def _package_excludes_path(root: Path) -> Path:
    return _trust_dir(root) / "trusted-excludes"


def _resolve_candidate(root: Path, raw: str | Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve(strict=True)


def validate_trusted_excludes_file(root: Path, raw: str | Path) -> Path:
    """Validate an operator-owned excludes file before Git is allowed to trust it.

    The file must be a regular non-symlink owned by the current user and may not
    be group/world writable.  Repository-owned files are rejected: an autonomous
    product worker must not be able to change what the promotion broker ignores.
    """
    root = root.expanduser().resolve()
    unresolved = Path(raw).expanduser()
    if not unresolved.is_absolute():
        unresolved = root / unresolved
    try:
        lst = unresolved.lstat()
    except OSError as exc:
        raise ValueError(f"trusted excludes file is unavailable: {unresolved}") from exc
    if stat.S_ISLNK(lst.st_mode):
        raise ValueError("trusted excludes file must not be a symlink")
    if not stat.S_ISREG(lst.st_mode):
        raise ValueError("trusted excludes file must be a regular file")
    path = unresolved.resolve(strict=True)
    try:
        path.relative_to(root)
    except ValueError:
        pass
    else:
        raise ValueError("trusted excludes file must live outside the repository")
    st = path.stat()
    if st.st_size > _MAX_EXCLUDES_BYTES:
        raise ValueError("trusted excludes file is unexpectedly large")
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        raise ValueError("trusted excludes file must be owned by the current user")
    if stat.S_IMODE(st.st_mode) & 0o022:
        raise ValueError("trusted excludes file must not be group/world writable")
    return path


def _read_operator_excludes(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("trusted excludes file must remain a regular file")
        if st.st_size > _MAX_EXCLUDES_BYTES:
            raise ValueError("trusted excludes file is unexpectedly large")
        if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
            raise ValueError("trusted excludes file must remain owned by the current user")
        if stat.S_IMODE(st.st_mode) & 0o022:
            raise ValueError("trusted excludes file must not be group/world writable")
        chunks: list[bytes] = []
        remaining = _MAX_EXCLUDES_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > _MAX_EXCLUDES_BYTES:
            raise ValueError("trusted excludes file is unexpectedly large")
        return payload
    finally:
        os.close(fd)


def _write_package_excludes(root: Path, payload: bytes) -> Path:
    target = _package_excludes_path(root)
    tmp = target.with_name(target.name + ".tmp")
    with tmp.open("wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.chmod(0o600)
    os.replace(tmp, target)
    target.chmod(0o600)
    return target


def configure_trusted_excludes(root: Path, raw: str | Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    source = validate_trusted_excludes_file(root, raw)
    payload = _read_operator_excludes(source)
    package_copy = _write_package_excludes(root, payload)
    policy = {
        "schema_version": 2,
        "configured_at": utcnow(),
        "source_excludes_file": str(source),
        "source_sha256": hashlib.sha256(payload).hexdigest(),
        "trusted_excludes_file": str(package_copy),
        "fetch_recurse_submodules": False,
        "submodule_recurse": False,
        "hooks_disabled": True,
        "fsmonitor_disabled": True,
    }
    json_dump(_trust_path(root), policy)
    return policy


def clear_trusted_excludes(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    path = _trust_path(root)
    previous = load_json(path, {})
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    try:
        _package_excludes_path(root).unlink()
    except FileNotFoundError:
        pass
    return {"status": "cleared", "previous": previous if isinstance(previous, dict) else {}}


def load_git_trust_policy(root: Path) -> dict[str, Any]:
    obj = load_json(_trust_path(root.expanduser().resolve()), {})
    return obj if isinstance(obj, dict) else {}


def trusted_git_config(root: Path) -> list[tuple[str, str]]:
    """Return package-owned Git config reconstructed after caller sanitisation.

    Authentication/transport settings remain available from ordinary Git config,
    but WIP visibility, hook execution, submodule recursion and fsmonitor are
    deterministic package-owned policy.
    """
    root = root.expanduser().resolve()
    policy = load_git_trust_policy(root)
    raw = policy.get("trusted_excludes_file")
    if isinstance(raw, str) and raw.strip():
        path = validate_trusted_excludes_file(root, raw)
    else:
        # Deterministic empty excludes without creating repository state merely
        # because a broker read/status/smoke operation was invoked.
        path = Path(os.devnull)
    return [
        ("core.excludesFile", str(path)),
        ("core.hooksPath", os.devnull),
        ("core.fsmonitor", "false"),
        ("fetch.recurseSubmodules", "false"),
        ("submodule.recurse", "false"),
    ]


def trusted_git_env(
    root: Path,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Strip all inherited inline Git config, then reconstruct trusted package config."""
    env = sanitised_subprocess_env(source)
    config = trusted_git_config(root)
    env["GIT_CONFIG_COUNT"] = str(len(config))
    for index, (key, value) in enumerate(config):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def git_trust_status(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    policy = load_git_trust_policy(root)
    try:
        config = trusted_git_config(root)
        valid = True
        error = None
    except (OSError, ValueError) as exc:
        config = []
        valid = False
        error = str(exc)
    return {
        "repository": str(root),
        "configured": bool(policy),
        "valid": valid,
        "error": error,
        "policy": policy,
        "effective_config": [{"key": k, "value": v} for k, v in config],
    }


def git_trust_action(args: Any, *, find_repo_root) -> int:
    try:
        action = getattr(args, "git_trust_command", None)
        root = find_repo_root(getattr(args, "repo", None))
        if action in ["set-excludes","clear-excludes"]:
            require_top_level_operator(root, "Git trust policy change")
        action = getattr(args, "git_trust_command", None)
        if action == "status":
            result = git_trust_status(root)
        elif action == "set-excludes":
            result = configure_trusted_excludes(root, args.path)
        elif action == "clear-excludes":
            result = clear_trusted_excludes(root)
        else:
            raise ValueError("unknown git-trust action")
    except (OSError, ValueError) as exc:
        print(f"REFUSED: {exc}", file=os.sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0
