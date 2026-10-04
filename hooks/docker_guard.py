#!/usr/bin/env python3
"""Token-aware container-runtime guard for Claude Auto.

This is defence in depth. Claude Auto does not place broad `docker *` patterns in
sandbox.excludedCommands, so container commands remain subject to Claude Code's
sandbox/Auto policy as well as this hook.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
from pathlib import Path

RUNTIMES = {"docker", "podman", "nerdctl"}
COMPOSE_RUNTIMES = {"docker-compose", "podman-compose"}
SHELLS = {"sh", "bash", "dash", "zsh", "ksh"}
CODE_INTERPRETERS = {"python", "python3", "perl", "ruby", "node"}
HOST_NS_FLAGS = {
    "--network", "--net", "--pid", "--ipc", "--uts", "--userns", "--cgroupns",
}
FORBIDDEN_SUBCOMMANDS = {
    "exec", "attach", "start", "restart", "unpause", "checkpoint", "plugin",
    "cp", "export", "save", "load", "import", "commit",
}
COMPOSE_MUTATING = {"up", "run", "create", "build"}


def _approved_command_hashes() -> set[str]:
    raw = os.environ.get("CLAUDE_AUTO_APPROVED_CONTAINER_COMMAND_HASHES", "[]")
    try:
        values = json.loads(raw)
    except Exception:
        return set()
    return {
        str(value).lower()
        for value in values
        if isinstance(value, str)
        and len(value) == 64
        and all(ch in "0123456789abcdefABCDEF" for ch in value)
    }


def _tokens(command: str) -> list[str]:
    lex = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def _runtime_indices(tokens: list[str]) -> list[int]:
    out = []
    for i, token in enumerate(tokens):
        base = Path(token).name.lower()
        if base in RUNTIMES or base in COMPOSE_RUNTIMES:
            out.append(i)
    return out


def _runtime_hint(text: str) -> bool:
    return bool(re.search(r"(?<![A-Za-z0-9_.-])(docker-compose|podman-compose|docker|podman|nerdctl)(?![A-Za-z0-9_.-])", text, re.I))


def _nested_shell_payloads(tokens: list[str]) -> list[list[str]]:
    payloads: list[list[str]] = []
    for i, token in enumerate(tokens[:-1]):
        if Path(token).name.lower() not in SHELLS:
            continue
        j = i + 1
        while j < len(tokens):
            arg = tokens[j]
            if arg in {"-c", "-lc", "-cl"} and j + 1 < len(tokens):
                try:
                    payloads.append(_tokens(tokens[j + 1]))
                except ValueError:
                    payloads.append([])
                break
            if not arg.startswith("-"):
                break
            j += 1
    return payloads


def _dynamic_runtime_dispatch(tokens: list[str], raw_command: str) -> bool:
    """Detect indirect execution without treating harmless prose as a runtime call."""
    if not _runtime_hint(raw_command):
        return False
    if any("$" in token or chr(96) in token for token in tokens):
        return True
    if any(Path(token).name.lower() in {"eval", "xargs"} for token in tokens):
        return True
    if any(Path(token).name.lower() in SHELLS for token in tokens):
        return True
    for i, token in enumerate(tokens[:-1]):
        base = Path(token).name.lower()
        if base in CODE_INTERPRETERS or re.fullmatch(r"python\d+(?:\.\d+)?", base):
            if tokens[i + 1] in {"-c", "-e"}:
                return True
    return False


def _outside_repo(source: str, repo_root: Path | None) -> bool:
    source = source.strip().strip("\"'")
    if not source:
        return False
    if repo_root is not None:
        for prefix in ("${PWD}", "$PWD"):
            if source == prefix:
                source = str(repo_root)
                break
            if source.startswith(prefix + "/"):
                source = str(repo_root / source[len(prefix) + 1 :])
                break
    if source.startswith("~") or "$" in source or "://" in source or source.startswith("git@"):
        return True
    if "/" not in source and not source.startswith("."):
        return False
    try:
        p = Path(os.path.expandvars(source)).expanduser()
        if not p.is_absolute():
            if repo_root is None:
                return True
            p = (repo_root / p).resolve()
        else:
            p = p.resolve()
        if repo_root is None:
            return True
        p.relative_to(repo_root)
        return False
    except (OSError, ValueError):
        return True


def _volume_source(value: str) -> str:
    return value.split(":", 1)[0].strip()


def _csv_fields(value: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in value.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            fields[k.strip().lower()] = v.strip()
    return fields


def _mount_source(value: str) -> tuple[str | None, str | None]:
    fields = _csv_fields(value)
    return fields.get("type"), fields.get("src") or fields.get("source")


def _strip_yaml_scalar(raw: str) -> str:
    value = raw.strip()
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value.strip().strip("\"'")


def _compose_text_dangerous(text: str, repo_root: Path) -> tuple[bool, str]:
    lower = text.lower()
    if "docker.sock" in lower or "/var/run/docker.sock" in lower:
        return True, "Compose configuration exposes the container-engine socket"
    if re.search(r"(?mi)^\s*volumes\s*:\s*\[", text):
        return True, "Inline Compose volume lists are not safely bounded by the lightweight guard"
    if re.search(r"(?mi)^\s*(build|env_file)\s*:\s*[\[{]", text):
        return True, "Inline Compose build/env-file structures are not safely bounded by the lightweight guard"
    if re.search(r"(?m)^\s*(include|extends|volumes_from|devices|device_cgroup_rules|cap_add|security_opt)\s*:", text, re.I):
        return True, "Compose configuration requests an unbounded host/container capability"
    if re.search(r"(?m)^\s*use_api_socket\s*:\s*(?!false\b|no\b|0\b)", text, re.I):
        return True, "Compose configuration requests the engine API socket"
    if re.search(r"(?m)^\s*privileged\s*:\s*(?!false\b|no\b|0\b|null\b|~\s*$)\S+", text, re.I):
        return True, "Compose privileged service"
    if re.search(r"(?m)^\s*(network_mode|pid|ipc|uts|userns_mode|cgroup)\s*:\s*[\"']?(host|container:|service:)", text, re.I):
        return True, "Compose host/container namespace sharing"

    for match in re.finditer(r"(?mi)^\s*(build|context|dockerfile|env_file|file|source|device)\s*:\s*(.+?)\s*$", text):
        value = _strip_yaml_scalar(match.group(2))
        if value and _outside_repo(value, repo_root):
            return True, f"Compose {match.group(1)} path escapes repository: {value}"

    volume_indent: int | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if re.match(r"^volumes\s*:\s*$", stripped, re.I):
            volume_indent = indent
            continue
        if volume_indent is not None and indent <= volume_indent:
            volume_indent = None
        if volume_indent is None:
            continue
        m = re.match(r"^-\s*(.+)$", stripped)
        if not m:
            continue
        item = _strip_yaml_scalar(m.group(1))
        source = _volume_source(item)
        if _outside_repo(source, repo_root):
            return True, f"Compose bind mount source outside repository: {source}"

    return False, "Compose configuration remains within the repository boundary"


def _compose_files(args: list[str], repo_root: Path, *, standalone: bool = False) -> tuple[list[Path], str | None, str | None]:
    files: list[Path] = []
    if standalone:
        i = 0
    else:
        try:
            i = [x.lower() for x in args].index("compose") + 1
        except ValueError:
            return [], None, None
    value_flags = {"--project-name", "--profile", "--parallel", "--progress", "--ansi"}
    command: str | None = None
    while i < len(args):
        token = args[i]
        low = token.lower()
        if low in {"-f", "--file"}:
            if i + 1 >= len(args):
                return [], None, "Compose file flag has no value"
            raw = args[i + 1]
            if raw == "-" or _outside_repo(raw, repo_root):
                return [], None, f"Compose file outside repository or stdin: {raw}"
            p = Path(raw)
            files.append((repo_root / p).resolve() if not p.is_absolute() else p.resolve())
            i += 2
            continue
        if low.startswith("--file="):
            raw = token.split("=", 1)[1]
            if raw == "-" or _outside_repo(raw, repo_root):
                return [], None, f"Compose file outside repository or stdin: {raw}"
            p = Path(raw)
            files.append((repo_root / p).resolve() if not p.is_absolute() else p.resolve())
            i += 1
            continue
        if low in {"--env-file", "--project-directory"}:
            if i + 1 >= len(args):
                return [], None, f"{token} has no value"
            raw = args[i + 1]
            if _outside_repo(raw, repo_root):
                return [], None, f"Compose option path outside repository: {raw}"
            i += 2
            continue
        if low.startswith(("--env-file=", "--project-directory=")):
            raw = token.split("=", 1)[1]
            if _outside_repo(raw, repo_root):
                return [], None, f"Compose option path outside repository: {raw}"
            i += 1
            continue
        if low in value_flags:
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        command = low
        break

    if not files:
        for name in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"):
            candidate = repo_root / name
            if candidate.is_file():
                files.append(candidate.resolve())
                break
    return files, command, None


def _compose_dangerous(args: list[str], repo_root: Path | None, *, standalone: bool = False) -> tuple[bool, str]:
    if not standalone and "compose" not in [x.lower() for x in args]:
        return False, "not Compose"
    if repo_root is None:
        return True, "Compose command has no selected repository root"
    files, command, error = _compose_files(args, repo_root, standalone=standalone)
    if error:
        return True, error
    if command in {"start", "restart"}:
        return True, "Compose start/restart can activate pre-existing containers with unknown privilege"
    if command not in COMPOSE_MUTATING:
        return False, "non-creating Compose command"
    if not files:
        return True, "mutating Compose command has no inspectable repository Compose file"
    for path in files:
        try:
            path.relative_to(repo_root)
        except ValueError:
            return True, f"Compose file outside repository: {path}"
        try:
            text = path.read_text(errors="replace")
        except OSError:
            return True, f"Compose file cannot be inspected: {path}"
        bad, reason = _compose_text_dangerous(text, repo_root)
        if bad:
            return True, reason
    return False, "inspected Compose configuration remains within repository boundary"


def _dangerous(tokens: list[str], repo_root: Path | None, raw_command: str = "") -> tuple[bool, str]:
    runtime_positions = _runtime_indices(tokens)
    nested_runtime_seen = False

    for nested in _nested_shell_payloads(tokens):
        if not nested:
            if _runtime_hint(raw_command):
                return True, "unparseable indirect shell container-runtime invocation"
            continue
        if _runtime_indices(nested):
            nested_runtime_seen = True
        bad, reason = _dangerous(nested, repo_root, " ".join(nested))
        if bad:
            return True, reason

    if not runtime_positions:
        if nested_runtime_seen:
            return False, "nested container-runtime command passed guard inspection"
        if _dynamic_runtime_dispatch(tokens, raw_command):
            return True, "indirect/dynamic container-runtime invocation"
        return False, "no container runtime invocation"

    if any("$(" in t or chr(96) in t or "${" in t for t in tokens):
        return True, "dynamic shell expansion in container runtime command"

    for token in tokens:
        upper = token.upper()
        if upper.startswith("DOCKER_HOST=") or upper.startswith("CONTAINER_HOST="):
            return True, "remote/alternate container daemon selected"

    for pos in runtime_positions:
        runtime_name = Path(tokens[pos]).name.lower()
        args = tokens[pos + 1 :]
        lowered_args = [x.lower() for x in args]
        if runtime_name in COMPOSE_RUNTIMES:
            bad, reason = _compose_dangerous(args, repo_root, standalone=True)
            if bad:
                return True, reason
        elif "compose" in lowered_args:
            bad, reason = _compose_dangerous(args, repo_root)
            if bad:
                return True, reason

        subcommand = next(
            (Path(x).name.lower() for x in args if x and not x.startswith("-") and "=" not in x),
            None,
        )
        if subcommand in FORBIDDEN_SUBCOMMANDS:
            return True, f"container subcommand {subcommand!r} operates on an unbounded pre-existing daemon object"

        build_mode = subcommand == "build"
        if subcommand == "buildx":
            buildx_command = next(
                (x.lower() for x in args[1:] if x and not x.startswith("-") and "=" not in x),
                None,
            )
            if buildx_command != "build":
                return True, "non-build docker buildx administration is outside the bounded runtime surface"
            build_mode = True

        i = 0
        while i < len(args):
            t = args[i]
            low = t.lower()

            if low == "--privileged" or low.startswith("--privileged="):
                return True, "--privileged grants host-equivalent container authority"
            if low in {"--volumes-from", "--runtime", "--cgroup-parent"} or low.startswith(("--volumes-from=", "--runtime=", "--cgroup-parent=")):
                return True, "container option crosses an unbounded host/container boundary"
            if low.startswith("--device") or low == "--gpus" or low.startswith("--gpus="):
                return True, "host device/GPU passthrough"

            if t == "-H" or low in {"--host", "--context"}:
                return True, "alternate Docker host/context"
            if t.startswith("-H") and t != "-H":
                return True, "alternate Docker host"
            if low.startswith("--host=") or low.startswith("--context="):
                return True, "alternate Docker host/context"

            matched_ns = False
            for flag in HOST_NS_FLAGS:
                if low == flag:
                    if i + 1 < len(args):
                        value = args[i + 1].lower()
                        if value == "host" or value.startswith(("container:", "service:")):
                            return True, f"{flag}={value}"
                    matched_ns = True
                    break
                if low.startswith(flag + "="):
                    value = low.split("=", 1)[1]
                    if value == "host" or value.startswith(("container:", "service:")):
                        return True, f"{flag}={value}"
            if matched_ns:
                i += 2
                continue

            if low == "--cap-add" or low.startswith("--cap-add="):
                return True, "additional Linux capabilities are not allowed"

            if low == "--security-opt" and i + 1 < len(args):
                value = args[i + 1].lower()
                if value not in {"no-new-privileges", "no-new-privileges=true"}:
                    return True, f"container security option is not allowlisted: {value}"
                i += 2
                continue
            if low.startswith("--security-opt="):
                value = low.split("=", 1)[1]
                if value not in {"no-new-privileges", "no-new-privileges=true"}:
                    return True, f"container security option is not allowlisted: {value}"

            if low in {"-v", "--volume"} and i + 1 < len(args):
                source = _volume_source(args[i + 1])
                if _outside_repo(source, repo_root):
                    return True, f"bind mount source outside repository: {source}"
                i += 2
                continue
            if low.startswith("--volume="):
                source = _volume_source(t.split("=", 1)[1])
                if _outside_repo(source, repo_root):
                    return True, f"bind mount source outside repository: {source}"

            if low == "--mount" and i + 1 < len(args):
                kind, source = _mount_source(args[i + 1])
                if kind == "bind" and source and _outside_repo(source, repo_root):
                    return True, f"bind mount source outside repository: {source}"
                i += 2
                continue
            if low.startswith("--mount="):
                kind, source = _mount_source(t.split("=", 1)[1])
                if kind == "bind" and source and _outside_repo(source, repo_root):
                    return True, f"bind mount source outside repository: {source}"

            if low in {"--cidfile"} and i + 1 < len(args):
                if _outside_repo(args[i + 1], repo_root):
                    return True, f"container output path outside repository: {args[i + 1]}"
                i += 2
                continue
            if low.startswith("--cidfile="):
                value = t.split("=", 1)[1]
                if _outside_repo(value, repo_root):
                    return True, f"container output path outside repository: {value}"

            if build_mode and low in {"-f", "--file"} and i + 1 < len(args):
                if _outside_repo(args[i + 1], repo_root):
                    return True, f"Dockerfile path outside repository: {args[i + 1]}"
                i += 2
                continue
            if build_mode and low.startswith("--file="):
                value = t.split("=", 1)[1]
                if _outside_repo(value, repo_root):
                    return True, f"Dockerfile path outside repository: {value}"
            if build_mode and (low == "--ssh" or low.startswith("--ssh=")):
                return True, "BuildKit SSH forwarding is outside the bounded repository authority"
            if build_mode and (low == "--secret" or low.startswith("--secret=")):
                value = args[i + 1] if low == "--secret" and i + 1 < len(args) else t.split("=", 1)[1] if "=" in t else ""
                _kind, source = _mount_source(value)
                if source and _outside_repo(source, repo_root):
                    return True, f"BuildKit secret source outside repository: {source}"
                if low == "--secret":
                    i += 2
                    continue
            if build_mode and low in {"-o", "--output"} and i + 1 < len(args):
                value = args[i + 1]
                fields = _csv_fields(value)
                destination = fields.get("dest") or fields.get("destination")
                if destination and _outside_repo(destination, repo_root):
                    return True, f"Build output destination outside repository: {destination}"
                if fields.get("type", "").lower() == "local" and not destination:
                    return True, "local BuildKit output requires an explicit in-repository destination"
                i += 2
                continue
            if build_mode and (low.startswith("--output=") or low.startswith("-o=")):
                value = t.split("=", 1)[1]
                fields = _csv_fields(value)
                destination = fields.get("dest") or fields.get("destination")
                if destination and _outside_repo(destination, repo_root):
                    return True, f"Build output destination outside repository: {destination}"
                if fields.get("type", "").lower() == "local" and not destination:
                    return True, "local BuildKit output requires an explicit in-repository destination"

            if low == "--env-file" and i + 1 < len(args):
                if _outside_repo(args[i + 1], repo_root):
                    return True, f"environment file outside repository: {args[i + 1]}"
                i += 2
                continue
            if low.startswith("--env-file="):
                value = t.split("=", 1)[1]
                if _outside_repo(value, repo_root):
                    return True, f"environment file outside repository: {value}"

            if "docker.sock" in low or "/var/run/docker.sock" in low:
                return True, "Docker socket exposure"

            i += 1

        if build_mode:
            # Inspect every concrete operand rather than assuming the context is
            # last: Docker accepts forms such as "build /context -t image".
            for candidate in (x for x in args if x and not x.startswith("-")):
                if candidate in {"build", "buildx"}:
                    continue
                if _outside_repo(candidate, repo_root):
                    return True, f"build operand/context outside repository: {candidate}"
    return False, "routine repository container command"


def main() -> int:
    try:
        obj = json.load(sys.stdin)
        cmd = str((obj.get("tool_input") or {}).get("command") or "")
        root_raw = os.environ.get("CLAUDE_AUTO_REPO_ROOT")
        repo_root = Path(root_raw).expanduser().resolve() if root_raw else None
        digest = hashlib.sha256(cmd.encode()).hexdigest()
        if _runtime_hint(cmd) and digest in _approved_command_hashes():
            deny = False
            reason = "exact user-approved container command"
            tokens = None
        else:
            tokens = _tokens(cmd)
        try:
            if tokens is None:
                pass
            else:
                deny, reason = _dangerous(tokens, repo_root, cmd)
        except ValueError:
            deny = _runtime_hint(cmd)
            reason = "unparseable container runtime command" if deny else "not a container runtime command"
    except Exception as exc:
        deny = True
        reason = f"container guard internal error: {type(exc).__name__}"

    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny" if deny else "allow",
            "permissionDecisionReason": (
                f"Claude Auto container guard denied: {reason}."
                if deny
                else f"Claude Auto container guard allowed: {reason}."
            ),
        }
    }
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
