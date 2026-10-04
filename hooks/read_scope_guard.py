#!/usr/bin/env python3
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

import json
import os
from pathlib import Path
import sys


DOTENV_SAMPLE_NAMES = {".env.example", ".env.sample", ".env.template", ".env.dist"}


def decision(value: str, reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": value,
            "permissionDecisionReason": reason,
        }
    }, separators=(",", ":")))


def _path_set(env_name: str) -> set[Path]:
    raw = os.environ.get(env_name, "[]")
    try:
        values = json.loads(raw)
    except Exception:
        return set()
    out: set[Path] = set()
    if isinstance(values, list):
        for value in values:
            if not isinstance(value, str):
                continue
            try:
                out.add(Path(value).expanduser().resolve(strict=False))
            except (OSError, RuntimeError, ValueError):
                continue
    return out


def _data_home() -> Path:
    explicit = os.environ.get("CLAUDE_AUTONOMY_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve(strict=False)
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return (Path(xdg).expanduser() / "claude-autonomy").resolve(strict=False)
    return (Path.home() / ".local" / "share" / "claude-autonomy").resolve(strict=False)


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _real_secret_dotenv(path: Path) -> bool:
    name = path.name
    if name in DOTENV_SAMPLE_NAMES:
        return False
    return name == ".env" or name.startswith(".env.")


def _protected_secret(path: Path, profile: str) -> bool:
    home = Path.home().resolve(strict=False)
    protected_dirs = [
        home / ".ssh",
        home / ".aws",
        home / ".config" / "gcloud",
        home / ".kube",
        _data_home() / "provider-configs",
    ]
    custom_config = os.environ.get("CLAUDE_CONFIG_DIR")
    if custom_config:
        protected_dirs.append(Path(custom_config).expanduser().resolve(strict=False))

    if any(_within(path, p.resolve(strict=False)) for p in protected_dirs):
        return True
    if path in {
        (home / ".claude" / ".credentials.json").resolve(strict=False),
        (home / ".claude.json").resolve(strict=False),
    }:
        return True
    if _real_secret_dotenv(path):
        return True
    if profile == "strict" and path.suffix.lower() in {".pem", ".p12", ".pfx"}:
        return True
    return False


def _tree_contains_protected_secret(
    target: Path,
    profile: str,
    *,
    max_entries: int = 50_000,
) -> bool:
    if not target.is_dir():
        return _protected_secret(target, profile)
    prune = {
        ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env",
        "dist", "build", "target", ".tox", ".nox", ".gradle", ".idea",
        ".pytest_cache", "__pycache__",
    }
    seen = 0
    try:
        for base, dirs, files in os.walk(target, topdown=True, followlinks=False):
            dirs[:] = [d for d in dirs if d not in prune]
            for name in files:
                seen += 1
                if seen > max_entries:
                    # If the tree is too large to prove safe, fail closed.
                    return True
                p = (Path(base) / name).resolve(strict=False)
                if _protected_secret(p, profile):
                    return True
    except OSError:
        return True
    return False


def main() -> int:
    try:
        event = json.load(sys.stdin)
        tool = str(event.get("tool_name") or "")
        if tool not in {"Read", "Grep", "Glob"}:
            decision("allow", "Tool is not a guarded file-read/search operation.")
            return 0
        ti = event.get("tool_input") or {}
        root_raw = os.environ.get("CLAUDE_AUTO_REPO_ROOT")
        if not root_raw:
            decision("deny", "Claude Auto read-scope guard has no repository root.")
            return 0

        root = Path(root_raw).expanduser().resolve()
        raw = ti.get("file_path") or ti.get("path")
        if not raw:
            if tool == "Read":
                decision("deny", "Claude Auto read-scope guard cannot identify the read path.")
                return 0
            target = root
        else:
            expanded = os.path.expandvars(os.path.expanduser(str(raw)))
            target = Path(expanded)
            target = (root / target).resolve(strict=False) if not target.is_absolute() else target.resolve(strict=False)

        outside_paths = _path_set("CLAUDE_AUTO_APPROVED_OUTSIDE_PATHS")
        secret_paths = _path_set("CLAUDE_AUTO_APPROVED_SECRET_PATHS")
        outside = not _within(target, root)
        if outside and target not in outside_paths and target not in secret_paths:
            decision("deny", f"{tool} outside selected repository is not explicitly approved: {target}")
            return 0

        profile = os.environ.get("CLAUDE_AUTONOMY_PROFILE", "balanced")
        if tool == "Glob" and secret_paths and (target.is_dir() or target == root):
            decision(
                "deny",
                "Broad Glob is disabled while an exact secret-read grant is active; "
                "use a non-secret exact path/pattern or Read the approved secret file directly.",
            )
            return 0
        if tool == "Grep" and (target.is_dir() or target == root):
            # Grep returns file contents, so a broad search must not become a
            # side channel around exact Read denies. Exact-file Grep remains
            # available, and Glob is path-only so it does not expose contents.
            if _tree_contains_protected_secret(target, profile):
                decision(
                    "deny",
                    "Broad Grep search intersects protected secret files; "
                    "search an exact non-secret file/path or request explicit broader authority.",
                )
                return 0

        if _protected_secret(target, profile) and target not in secret_paths:
            decision("deny", f"Protected secret read is not explicitly approved: {target}")
            return 0

        if target in secret_paths:
            decision("allow", "Exact user-approved secret file read.")
        elif target in outside_paths:
            decision("allow", "Exact user-approved outside-repository read.")
        else:
            decision("allow", "Repository-local non-protected read.")
        return 0
    except Exception as exc:
        decision("deny", f"Claude Auto read-scope guard failed closed: {type(exc).__name__}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
