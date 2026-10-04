#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def _git_safe_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key in {"GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"} or (
            key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_")
        ):
            env.pop(key, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run(*args: str) -> str:
    try:
        p = subprocess.run(args, text=True, capture_output=True, timeout=2, env=_git_safe_env())
    except Exception:
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def root_from_cwd() -> Path | None:
    cwd = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()
    top = run("git", "-C", str(cwd), "rev-parse", "--show-toplevel")
    if top:
        return Path(top).resolve()
    markers = ("pyproject.toml", "package.json", "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "Makefile", "justfile")
    cur = cwd
    while True:
        if any((cur / m).exists() for m in markers):
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent


def stack_hints(root: Path) -> tuple[list[str], list[str]]:
    markers = {
        "pyproject.toml": "python", "requirements.txt": "python",
        "package.json": "js-ts", "pnpm-lock.yaml": "js-ts", "yarn.lock": "js-ts",
        "Cargo.toml": "rust", "go.mod": "go", "pom.xml": "java",
        "build.gradle": "java-kotlin", "build.gradle.kts": "java-kotlin",
        "Gemfile": "ruby", "composer.json": "php", "CMakeLists.txt": "c-cpp",
    }
    stacks: list[str] = []
    manifests: list[str] = []
    for name, stack in markers.items():
        if (root / name).exists():
            manifests.append(name)
            if stack not in stacks:
                stacks.append(stack)
    return stacks[:8], manifests[:12]


def main() -> int:
    root = root_from_cwd()
    if root is None:
        return 0
    branch = run("git", "-C", str(root), "branch", "--show-current")
    head = run("git", "-C", str(root), "rev-parse", "--short=12", "HEAD")
    dirty = bool(run("git", "-C", str(root), "status", "--porcelain"))
    stacks, manifests = stack_hints(root)
    instruction_files = [name for name in ("CLAUDE.md", "AGENTS.md", "CONTRIBUTING.md", "README.md") if (root / name).exists()]
    # Keep the always-on context intentionally tiny. Full profiling is deferred
    # until claude-auto autonomous work or an explicit inspect command.
    payload = {
        "root": str(root),
        "branch": branch or None,
        "head": head or None,
        "dirty": dirty,
        "stack": stacks,
        "root_manifests": manifests,
        "repo_instructions": instruction_files,
        "note": "Global Claude Auto user layer is active; no repo-specific installation is required.",
    }
    print("CLAUDE_AUTO_SESSION_CONTEXT=" + json.dumps(payload, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
