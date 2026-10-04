"""Controlled execution primitives for Claude Auto.

Repository-controlled commands must never inherit the supervisor's full host
authority by accident.  The default path uses Anthropic Sandbox Runtime (srt)
with a scrubbed environment.  Direct host execution is available only when the
operator explicitly attests that repository scripts are trusted.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from environment_policy import home_toolchain_path_entries, sanitised_subprocess_env


_SECRET_NAMES = (
    "ANTHROPIC", "OPENAI", "AWS_", "AZURE_", "GCP_", "GOOGLE_", "GITHUB_",
    "GH_TOKEN", "NPM_TOKEN", "PYPI", "DOCKER_AUTH", "KUBECONFIG", "SSH_",
    "CI_JOB_TOKEN", "CI_TOKEN", "VAULT_", "TF_TOKEN", "CLOUDFLARE_",
)


def _safe_env(root: Path, temp_home: Path) -> dict[str, str]:
    """Return a deliberately small environment for repository-controlled code."""
    src = sanitised_subprocess_env()
    env: dict[str, str] = {}
    for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ"):
        value = src.get(key)
        if value:
            env[key] = value
    for key, value in src.items():
        if key.startswith("LC_") and key not in env:
            env[key] = value

    # Preserve an in-repository virtual environment when present, but never copy
    # arbitrary provider/cloud secrets into the child.
    venv = src.get("VIRTUAL_ENV")
    if venv:
        try:
            vp = Path(venv).resolve()
            vp.relative_to(root.resolve())
            env["VIRTUAL_ENV"] = str(vp)
        except (OSError, ValueError):
            pass

    env["HOME"] = str(temp_home)
    env["TMPDIR"] = str(temp_home / "tmp")
    env["CLAUDE_AUTO_SUPERVISOR_SANDBOX"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _contains_secret_name(env: dict[str, str]) -> bool:
    for key in env:
        upper = key.upper()
        if any(upper == x or upper.startswith(x) for x in _SECRET_NAMES):
            return True
    return False


def _readonly_toolchain_paths(root: Path) -> list[Path]:
    """Return narrow non-secret user toolchain/cache paths needed by verification.

    The real home remains denied as a whole.  Only conventional executable,
    package-cache and language-toolchain directories are carved back in read-only.
    Configuration/credential locations such as ~/.ssh, ~/.config, ~/.aws,
    ~/.kube, ~/.npmrc, ~/.pypirc and Maven settings.xml are never included.
    """
    root = root.resolve()
    home = Path.home().resolve()
    candidates: list[Path] = [
        home / ".local" / "bin",
        home / ".cargo" / "bin",
        home / ".cargo" / "registry",
        home / ".cargo" / "git",
        home / ".rustup",
        home / ".cache" / "pip",
        home / ".cache" / "uv",
        home / ".cache" / "go-build",
        home / ".cache" / "pnpm",
        home / ".cache" / "yarn",
        home / ".cache" / "coursier",
        home / ".m2" / "repository",
        home / ".gradle" / "caches",
        home / ".gradle" / "wrapper",
        home / ".gradle" / "native",
        home / ".nuget" / "packages",
        home / "go" / "pkg" / "mod",
        home / "go" / "bin",
        home / ".local" / "share" / "pnpm",
        home / ".local" / "share" / "pipx",
        home / ".pnpm-store",
        home / ".ivy2" / "cache",
        home / ".sbt" / "boot",
        home / ".sbt" / "preloaded",
    ]
    # Also preserve custom executable/toolchain directories that are already on
    # the effective PATH. This is how a project-specific user toolchain such as
    # ~/.local/share/<project>/toolchain/bin survives a resumed/systemd run.
    candidates.extend(home_toolchain_path_entries())

    # User-site Python packages are code, not credentials, and are often required
    # by ~/.local/bin entrypoints (pytest/ruff/mypy/etc.).
    local_lib = home / ".local" / "lib"
    if local_lib.is_dir():
        try:
            candidates.extend(
                p / "site-packages"
                for p in local_lib.iterdir()
                if p.is_dir() and p.name.startswith("python") and (p / "site-packages").is_dir()
            )
        except OSError:
            pass

    out: list[Path] = []
    for candidate in candidates:
        try:
            p = candidate.resolve()
            if not p.exists() or not p.is_dir():
                continue
            # A repository-local toolchain is already visible through the repo bind.
            try:
                p.relative_to(root)
                continue
            except ValueError:
                pass
            # Never allow a symlinked candidate to escape the real home.
            p.relative_to(home)
            if p not in out:
                out.append(p)
        except (OSError, ValueError):
            continue
    return out


def _sandbox_settings(root: Path, temp_home: Path) -> dict[str, Any]:
    root = root.resolve()
    real_home = Path.home().resolve()
    deny_read = [
        str(real_home),
        str(root / ".git"),
    ]
    allow_read = [str(root), *(str(p) for p in _readonly_toolchain_paths(root))]
    allow_write = [str(root), str(temp_home), str(temp_home / "tmp")]
    deny_write = [
        str(root / ".git"),
    ]
    return {
        "network": {
            "allowedDomains": [],
            "deniedDomains": [],
            "allowLocalBinding": False,
        },
        "filesystem": {
            "denyRead": deny_read,
            "allowRead": allow_read,
            "allowWrite": allow_write,
            "denyWrite": deny_write,
        },
        "enableWeakerNestedSandbox": False,
        "enableWeakerNetworkIsolation": False,
        "allowAppleEvents": False,
    }


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _bwrap_base(root: Path) -> list[str] | None:
    """Build a Linux bubblewrap boundary that hides user homes and network."""
    if sys.platform != "linux":
        return None
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return None

    root = root.resolve()
    real_home = Path.home().resolve()
    args = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--ro-bind", "/", "/",
    ]

    # Replace writable/credential-bearing host namespaces with fresh private
    # mounts.  /tmp is intentionally empty; /run is hidden so host Unix sockets
    # (Docker, SSH agents, desktop/session services) are unreachable.
    mask_points: list[Path] = []
    for candidate in (Path("/tmp"), Path("/run"), Path("/root"), Path("/home")):
        if candidate.exists() and candidate not in mask_points:
            mask_points.append(candidate)
            args += ["--tmpfs", str(candidate)]

    # Homes outside conventional /home are masked explicitly too.
    if real_home.exists() and not any(_path_is_within(real_home, p) for p in mask_points):
        mask_points.append(real_home)
        args += ["--tmpfs", str(real_home)]

    created_dirs: set[Path] = set()

    def recreate_target(path: Path) -> None:
        masked_parent = next((p for p in mask_points if _path_is_within(path, p)), None)
        if masked_parent is None:
            return
        current = masked_parent
        for part in path.relative_to(masked_parent).parts:
            current = current / part
            if current not in created_dirs:
                args.extend(["--dir", str(current)])
                created_dirs.add(current)

    # If the selected repository lives under a masked tree (common for /home and
    # pytest /tmp repos), recreate only its destination path before binding it.
    recreate_target(root)
    args += ["--bind", str(root), str(root)]

    # Restore only narrowly-scoped non-secret dependency/toolchain directories
    # beneath a masked home, and restore them read-only.
    for tool_path in _readonly_toolchain_paths(root):
        recreate_target(tool_path)
        args += ["--ro-bind", str(tool_path), str(tool_path)]

    # Repository code may create normal build/test artefacts, but Git metadata is
    # read-only so a verification script cannot rewrite refs/index/config.
    git_meta = root / ".git"
    if git_meta.exists():
        args += ["--ro-bind", str(git_meta), str(git_meta)]

    # Fresh minimal kernel/device views.  The new network namespace has no host
    # interfaces/routes; no host /run sockets survive the tmpfs mask above.
    args += ["--proc", "/proc", "--dev", "/dev", "--chdir", str(root)]
    return args


def _run_bwrap(
    root: Path,
    inner: list[str],
    *,
    env: dict[str, str],
    timeout: int | None,
) -> dict[str, Any] | None:
    base = _bwrap_base(root)
    if base is None:
        return None

    # Probe the exact mount/namespace recipe with trusted /bin/true first so a
    # kernel/AppArmor/user-namespace failure is reported as boundary-unavailable,
    # never mislabelled as a repository test failure.
    probe = _run_process([*base, "--", "/bin/true"], cwd=root, env=env, timeout=15)
    if int(probe.get("returncode", 1)) != 0:
        return {
            "returncode": 125,
            "stdout": "",
            "stderr": "bubblewrap sandbox unavailable: " + str(probe.get("stderr") or probe.get("stdout") or "probe failed"),
            "timed_out": bool(probe.get("timed_out", False)),
            "wall_seconds": float(probe.get("wall_seconds", 0.0) or 0.0),
            "execution_boundary": "unavailable",
            "sandboxed": False,
            "environment_scrubbed": True,
        }

    sandbox_env = dict(env)
    sandbox_env["HOME"] = "/tmp/claude-auto-home"
    sandbox_env["TMPDIR"] = "/tmp"
    result = _run_process(
        [*base, "--dir", "/tmp/claude-auto-home", "--", *inner],
        cwd=root,
        env=sandbox_env,
        timeout=timeout,
    )
    result.update({
        "execution_boundary": "bubblewrap",
        "sandboxed": True,
        "environment_scrubbed": True,
    })
    return result


def _run_process(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int | None,
) -> dict[str, Any]:
    started = time.monotonic()
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout or None)
        return {
            "returncode": proc.returncode,
            "stdout": stdout or "",
            "stderr": stderr or "",
            "timed_out": False,
            "wall_seconds": round(time.monotonic() - started, 3),
        }
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=3)
        except Exception:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                pass
            stdout, stderr = proc.communicate()
        return {
            "returncode": 124,
            "stdout": stdout or "",
            "stderr": stderr or "",
            "timed_out": True,
            "wall_seconds": round(time.monotonic() - started, 3),
        }


def run_repository_command(
    root: Path,
    command: str | list[str],
    *,
    timeout: int | None = None,
    trust_repo_scripts: bool = False,
    unrestricted_host: bool = False,
) -> dict[str, Any]:
    """Run repository-controlled code under an explicit execution boundary.

    Default: prefer Anthropic Sandbox Runtime (srt), with a native bubblewrap
    fallback on Linux. If no safe boundary is available, refuse execution.

    Both isolated paths hide the real home except for a narrow read-only
    projection of conventional non-secret dependency/toolchain directories.

    Explicit trusted override: run directly on the host with a scrubbed
    environment.  This never restores the supervisor's provider/cloud secrets.
    """
    root = root.resolve()
    with tempfile.TemporaryDirectory(prefix="claude-auto-exec-") as td:
        temp_home = Path(td)
        (temp_home / "tmp").mkdir(parents=True, exist_ok=True)
        env = _safe_env(root, temp_home)
        if _contains_secret_name(env):
            raise RuntimeError("internal error: secret-like variables survived repository execution scrubbing")

        if isinstance(command, list):
            raw_argv = [str(x) for x in command]
            shell_argv = None
        else:
            raw_argv = []
            shell_argv = ["bash", "-lc", command]

        inner = raw_argv if raw_argv else shell_argv
        assert inner is not None

        if unrestricted_host:
            result = _run_process(inner, cwd=root, env=dict(os.environ), timeout=timeout)
            result.update({
                "execution_boundary": "unattended-host",
                "sandboxed": False,
                "environment_scrubbed": False,
            })
            return result

        srt = shutil.which("srt")
        if srt:
            settings = _sandbox_settings(root, temp_home)
            settings_path = temp_home / "srt-settings.json"
            settings_path.write_text(json.dumps(settings, indent=2) + "\n")
            result = _run_process(
                [srt, "--settings", str(settings_path), *inner],
                cwd=root,
                env=env,
                timeout=timeout,
            )
            result.update({
                "execution_boundary": "srt",
                "sandboxed": True,
                "environment_scrubbed": True,
            })
            return result

        # Linux can enforce the same essential boundary directly with bubblewrap,
        # which is also the primitive used by Sandbox Runtime on Linux.  This
        # avoids turning a missing npm-level srt wrapper into vacuous verification.
        bwrap_result = _run_bwrap(root, inner, env=env, timeout=timeout)
        if bwrap_result is not None:
            if bwrap_result.get("execution_boundary") != "unavailable":
                return bwrap_result
            # An installed bubblewrap binary can still be unusable because the
            # host disables the required namespace operations.  The explicit
            # trusted-repository override means "fall back to scrubbed host
            # execution when isolation cannot be established", not merely when
            # the binary is absent.
            if not trust_repo_scripts:
                return bwrap_result

        if not trust_repo_scripts:
            return {
                "returncode": 125,
                "stdout": "",
                "stderr": (
                    "No safe supervisor execution boundary is available. Claude Auto requires "
                    "Anthropic Sandbox Runtime (srt), or bubblewrap on Linux, and refused "
                    "to execute repository-controlled code directly on the host. "
                    "Install @anthropic-ai/sandbox-runtime / bubblewrap, or explicitly "
                    "select the trusted-repository override outside Strict."
                ),
                "timed_out": False,
                "wall_seconds": 0.0,
                "execution_boundary": "unavailable",
                "sandboxed": False,
                "environment_scrubbed": True,
            }

        result = _run_process(inner, cwd=root, env=env, timeout=timeout)
        result.update({
            "execution_boundary": "trusted-host",
            "sandboxed": False,
            "environment_scrubbed": True,
        })
        return result
