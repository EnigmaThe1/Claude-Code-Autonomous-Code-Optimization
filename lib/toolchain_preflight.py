from __future__ import annotations

import os
import re
import shlex
import shutil
from pathlib import Path
from typing import Any

from process_runner import run
from repo_profile import load_declared_verification_commands


_KNOWN_TOOLS = {
    "buf",
    "cargo",
    "clippy",
    "initdb",
    "java",
    "javac",
    "just",
    "pg_ctl",
    "postgres",
    "psql",
    "rustc",
    "rustfmt",
    "rustup",
    "uv",
}


def _command_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return []


def _known_tools_in_command(command: str) -> set[str]:
    # Inspect shell words conservatively. This is diagnostic only; the
    # repository-owned verification command remains authoritative.
    words = re.findall(r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+)(?![A-Za-z0-9_.-])", command)
    return {word for word in words if word in _KNOWN_TOOLS}


def _has_marker(root: Path, names: tuple[str, ...]) -> bool:
    return any((root / name).exists() for name in names)


def detect_toolchain_requirements(root: Path, profile: dict[str, Any] | None = None) -> list[dict[str, str]]:
    root = root.resolve()
    profile = profile or {}
    required: dict[str, str] = {}

    if (root / "Cargo.toml").exists() or "rust" in set(profile.get("languages") or []):
        required.update({
            "cargo": "Rust repository",
            "rustc": "Rust repository",
            "rustfmt": "cargo fmt verification",
            "clippy": "cargo clippy verification",
        })
    if _has_marker(root, ("buf.yaml", "buf.gen.yaml", "buf.work.yaml")):
        required["buf"] = "Buf configuration"
    if (root / "uv.lock").exists():
        required["uv"] = "uv.lock"
    if _has_marker(root, ("justfile", "Justfile")):
        required["just"] = "justfile"

    try:
        declared = load_declared_verification_commands(root)
    except ValueError:
        declared = {}
    for rows in declared.values() if isinstance(declared, dict) else []:
        for command in rows:
            for tool in _known_tools_in_command(command):
                required.setdefault(tool, "mandatory verification command")

    return [{"tool": tool, "reason": required[tool]} for tool in sorted(required)]


def _rustup_component_names() -> set[str]:
    if not shutil.which("rustup"):
        return set()
    cp = run(["rustup", "component", "list", "--installed"], timeout=30)
    if cp.returncode != 0:
        return set()
    out: set[str] = set()
    for line in cp.stdout.splitlines():
        token = line.strip().split()[0] if line.strip() else ""
        if not token:
            continue
        out.add(token)
        # rustup commonly reports target-qualified component names such as
        # rustfmt-x86_64-unknown-linux-gnu. Preserve both exact and base names.
        for base in ("rustfmt", "clippy"):
            if token == base or token.startswith(base + "-"):
                out.add(base)
    return out


def _rust_component_available(component: str) -> tuple[bool, str]:
    cargo = shutil.which("cargo")
    if cargo:
        sub = "fmt" if component == "rustfmt" else "clippy"
        cp = run(["cargo", sub, "--version"], timeout=30)
        if cp.returncode == 0:
            return True, (cp.stdout or cp.stderr).strip()[:200]
    installed = _rustup_component_names()
    if component in installed:
        return True, "reported installed by rustup"
    if shutil.which("rustup"):
        return False, f"missing; repair with: rustup component add {component}"
    return False, "missing; cargo subcommand unavailable and rustup is not installed"


def probe_toolchain(root: Path, profile: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for req in detect_toolchain_requirements(root, profile):
        tool = req["tool"]
        if tool in {"rustfmt", "clippy"}:
            ok, detail = _rust_component_available(tool)
            rows.append({**req, "ok": ok, "detail": detail})
            continue
        exe = shutil.which(tool)
        rows.append({
            **req,
            "ok": bool(exe),
            "detail": exe or "not found on effective PATH",
        })
    return rows


def normalise_local_shell_entrypoint(root: Path, command: str) -> tuple[str, str | None]:
    """Use bash for a non-executable local shell script instead of failing 126.

    Only a simple local first token is rewritten; compound commands, arbitrary
    interpreters and non-shell files retain their exact repository declaration.
    """
    if any(op in command for op in ("&&", "||", ";", "|", "\n", "\r")):
        return command, None
    tokens = _command_tokens(command)
    if not tokens:
        return command, None
    first = tokens[0]
    if not (first.startswith("./") or first.startswith("../")):
        return command, None
    try:
        candidate = (root / first).resolve(strict=True)
        candidate.relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return command, None
    if not candidate.is_file() or os.access(candidate, os.X_OK):
        return command, None
    is_shell = candidate.suffix in {".sh", ".bash"}
    if not is_shell:
        try:
            first_line = candidate.open("r", errors="replace").readline(256)
        except OSError:
            first_line = ""
        is_shell = first_line.startswith("#!") and any(x in first_line for x in ("/sh", "/bash", "/dash", "/zsh"))
    if not is_shell:
        return command, None
    rewritten = shlex.join(["bash", first, *tokens[1:]])
    return rewritten, f"non-executable local shell entrypoint {first!r} invoked through bash"
