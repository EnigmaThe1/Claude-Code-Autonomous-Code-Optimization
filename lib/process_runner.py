from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Any

from environment_policy import sanitised_subprocess_env


class SupervisorInterrupted(BaseException):
    def __init__(self, signum: int):
        super().__init__(f"supervisor interrupted by signal {signum}")
        self.signum = signum


def run(
    cmd: list[str],
    cwd: Path | None = None,
    check: bool = False,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess with timeout-safe whole-process-tree termination.

    Claude Auto never lets ``subprocess.TimeoutExpired`` escape into the supervisor.  A
    timeout becomes a completed-process shaped result with return code 124 and
    ``timed_out`` metadata so callers can checkpoint and classify it as a
    recoverable boundary instead of crashing.  On POSIX a dedicated process
    group prevents child tools (test runners, compilers, etc.) being orphaned.
    """
    popen_kwargs: dict[str, Any] = {
        "cwd": str(cwd) if cwd else None,
        "text": True,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": sanitised_subprocess_env(env),
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    elif os.name == "nt":  # pragma: no cover - Linux is the supported pack target
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)
    except FileNotFoundError:
        cp = subprocess.CompletedProcess(cmd, 127, "", f"command not found: {cmd[0]}")
        setattr(cp, "timed_out", False)
        return cp

    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except (KeyboardInterrupt, SupervisorInterrupted):
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGTERM)
            else:  # pragma: no cover
                proc.terminate()
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix": os.killpg(proc.pid, signal.SIGKILL)
                else: proc.kill()  # pragma: no cover
            except (ProcessLookupError, OSError):
                pass
            proc.communicate()
        raise
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        partial_out = exc.stdout or ""
        partial_err = exc.stderr or ""
        if isinstance(partial_out, bytes):
            partial_out = partial_out.decode(errors="replace")
        if isinstance(partial_err, bytes):
            partial_err = partial_err.decode(errors="replace")
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGTERM)
            else:  # pragma: no cover
                proc.terminate()
        except (ProcessLookupError, OSError):
            pass
        try:
            tail_out, tail_err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:  # pragma: no cover
                    proc.kill()
            except (ProcessLookupError, OSError):
                pass
            tail_out, tail_err = proc.communicate()
        stdout = str(partial_out) + (tail_out or "")
        stderr = str(partial_err) + (tail_err or "")
        marker = f"[CLAUDE_AUTO_TIMEOUT seconds={timeout}]"
        stderr = (stderr.rstrip() + "\n" + marker + "\n").lstrip("\n")

    rc = 124 if timed_out else int(proc.returncode or 0)
    cp = subprocess.CompletedProcess(cmd, rc, stdout or "", stderr or "")
    setattr(cp, "timed_out", timed_out)
    setattr(cp, "timeout_seconds", timeout if timed_out else None)
    if check and cp.returncode != 0:
        raise subprocess.CalledProcessError(cp.returncode, cmd, output=cp.stdout, stderr=cp.stderr)
    return cp
