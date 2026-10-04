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

import argparse
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIRS = ("bin", "docs", "hooks", "lib", "templates", "tests")
PACKAGE_FILES = (
    ".gitignore",
    "CHANGELOG.md",
    "COMMANDS.md",
    "LICENSE",
    "NOTICE",
    "QUICKSTART.md",
    "README.md",
    "SOURCES.md",
    "VERSION",
    "install.sh",
    "uninstall.sh",
)
IGNORED_PARTS = {"__pycache__", ".pytest_cache"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def package_paths() -> list[str]:
    paths = list(PACKAGE_FILES)
    for rel in PACKAGE_FILES:
        path = ROOT / rel
        if not path.is_file():
            raise SystemExit(f"missing package file: {rel}")
    for dirname in PACKAGE_DIRS:
        base = ROOT / dirname
        if not base.is_dir():
            raise SystemExit(f"missing package directory: {dirname}")
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT)
            if any(part in IGNORED_PARTS for part in rel.parts):
                continue
            if path.suffix in IGNORED_SUFFIXES:
                continue
            paths.append(rel.as_posix())
    return sorted(set(paths))


def render_manifest() -> str:
    lines = []
    for rel in package_paths():
        digest = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
        lines.append(f"{digest}  {rel}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build or verify the package SHA-256 manifest.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="write MANIFEST.sha256")
    mode.add_argument("--check", action="store_true", help="verify MANIFEST.sha256 is current")
    args = parser.parse_args()

    expected = render_manifest()
    manifest = ROOT / "MANIFEST.sha256"
    if args.write:
        manifest.write_text(expected)
        print(f"wrote {len(expected.splitlines())} manifest entries")
        return 0

    current = manifest.read_text() if manifest.is_file() else ""
    if current != expected:
        current_lines = set(current.splitlines())
        expected_lines = set(expected.splitlines())
        current_paths = {line.split(None, 1)[1] for line in current_lines if line.strip() and len(line.split(None, 1)) == 2}
        expected_paths = {line.split(None, 1)[1] for line in expected_lines if line.strip() and len(line.split(None, 1)) == 2}
        missing = sorted(expected_paths - current_paths)
        stale = sorted(current_paths - expected_paths)
        details = []
        if missing:
            details.append("missing entries: " + ", ".join(missing))
        if stale:
            details.append("stale entries: " + ", ".join(stale))
        changed = sorted(
            path for path in expected_paths & current_paths
            if next(line for line in expected_lines if line.endswith("  " + path))
            != next(line for line in current_lines if line.endswith("  " + path))
        )
        if changed:
            details.append("changed digests: " + ", ".join(changed))
        raise SystemExit("MANIFEST.sha256 is not current" + (" (" + "; ".join(details) + ")" if details else ""))
    print(f"MANIFEST.sha256 is current ({len(expected.splitlines())} entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
