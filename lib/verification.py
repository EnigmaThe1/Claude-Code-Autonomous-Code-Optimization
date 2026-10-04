from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path
from typing import Any

from control_plane import _tracked_source_unchanged
from execution import run_repository_command
from permission_escalation import verification_command_authorized
from repo_runtime import git_snapshot
from repo_profile import load_declared_verification_commands
from runtime_paths import utcnow
from state_store import json_dump, sha256_text
from telemetry import redact_text
from toolchain_preflight import normalise_local_shell_entrypoint


def _host_execution_authorized(args: argparse.Namespace, command: str) -> bool:
    if bool(getattr(args, "trust_repo_scripts", False)):
        return True
    if getattr(args, "profile", None) == "unattended":
        return True
    return verification_command_authorized(
        list(getattr(args, "_permission_grants", []) or []),
        command,
    )


def _unrestricted_host_authorized(args: argparse.Namespace) -> bool:
    return (
        getattr(args, "profile", None) == "unattended"
        or "unrestricted" in set(getattr(args, "_permission_overrides", []) or [])
    )


def _verification_commands(prof: dict[str, Any]) -> list[tuple[str, str]]:
    hints = prof.get("build_test_hints") if isinstance(prof.get("build_test_hints"), dict) else {}
    mandatory: list[tuple[str, str]] = []
    root_raw = prof.get("repo_root")
    if isinstance(root_raw, str) and root_raw.strip():
        declared = load_declared_verification_commands(Path(root_raw))
        for category in ("test", "lint", "typecheck", "build", "format"):
            for command in declared.get(category, []):
                row = (category, command.strip())
                if row not in mandatory:
                    mandatory.append(row)

    discovered: list[tuple[str, str]] = []
    for category in ("test", "lint", "typecheck", "build", "format"):
        for command in hints.get(category, []) or []:
            if not isinstance(command, str) or not command.strip():
                continue
            row = (category, command.strip())
            if row not in mandatory and row not in discovered:
                discovered.append(row)

    # Mandatory repository-owned checks are never silently truncated. Generic
    # discovery remains bounded to avoid runaway heuristic execution.
    return mandatory + discovered[:48]


def _receipt_signature(stdout: str, stderr: str) -> str:
    def norm(x: str) -> str:
        x = re.sub(r"\x1b\[[0-9;]*m", "", x)
        # Remove common run-to-run noise while retaining test IDs, exception
        # classes and stable message content.
        x = re.sub(r"\b\d+(?:\.\d+)?(?:s|ms|µs|us)\b", "<TIME>", x)
        x = re.sub(r"/tmp/pytest-of-[^/]+/pytest-\d+", "/tmp/pytest-of-<USER>/pytest-<N>", x)
        x = re.sub(r"\b0x[0-9a-fA-F]{6,}\b", "<ADDR>", x)
        x = re.sub(r"\bpid[=: ]+\d+\b", "pid=<PID>", x, flags=re.IGNORECASE)
        x = re.sub(r"\bprocess[=: ]+\d+\b", "process=<PID>", x, flags=re.IGNORECASE)
        x = re.sub(r"\b20\d{2}-\d{2}-\d{2}[T ][0-9:.+-]+Z?\b", "<TIMESTAMP>", x)
        # Hash the complete normalised evidence.  Truncating to the tail can
        # make two long runs look identical when the changed failure appears
        # earlier in the log.
        return "\n".join(line.rstrip() for line in x.splitlines()).strip()
    return sha256_text(norm(stdout) + "\n---STDERR---\n" + norm(stderr))


def _run_verification_command(
    root: Path,
    category: str,
    command: str,
    timeout: int,
    *,
    trust_repo_scripts: bool = False,
    unrestricted_host: bool = False,
) -> dict[str, Any]:
    before = git_snapshot(root)
    effective_command, command_adjustment = normalise_local_shell_entrypoint(root, command)
    result = run_repository_command(
        root,
        effective_command,
        timeout=timeout or None,
        trust_repo_scripts=trust_repo_scripts,
        unrestricted_host=unrestricted_host,
    )
    rc = int(result.get("returncode", 125))
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    timed_out = bool(result.get("timed_out", False))
    after = git_snapshot(root)
    return {
        "category": category,
        "command": command,
        "effective_command": effective_command,
        "command_adjustment": command_adjustment,
        "started_at": utcnow(),
        "wall_seconds": float(result.get("wall_seconds", 0.0) or 0.0),
        "exit_code": rc,
        "timed_out": timed_out,
        "signature": _receipt_signature(stdout, stderr),
        "stdout_tail": redact_text(stdout, None, 4000),
        "stderr_tail": redact_text(stderr, None, 4000),
        "git_before": before,
        "git_after": after,
        "tracked_source_unchanged": _tracked_source_unchanged(before, after),
        "source_fingerprint": sha256_text(json.dumps(after, sort_keys=True, default=str)),
        "execution_boundary": result.get("execution_boundary"),
        "sandboxed": bool(result.get("sandboxed", False)),
        "environment_scrubbed": bool(result.get("environment_scrubbed", False)),
    }

def ensure_verification_baseline(
    root: Path,
    sd: Path,
    prof: dict[str, Any],
    state: dict[str, Any],
    args: argparse.Namespace,
    objective_hash: str,
    *,
    snapshot_hash_func,
    verification_runner=_run_verification_command,
) -> list[dict[str, Any]]:
    existing = state.get("verification_baseline")
    if isinstance(existing, list) and state.get("verification_baseline_objective_hash") == objective_hash:
        return existing
    receipts = []
    timeout = int(getattr(args, "verification_timeout", 900) or 900)
    for category, command in _verification_commands(prof):
        receipts.append(verification_runner(
            root, category, command, timeout,
            trust_repo_scripts=_host_execution_authorized(args, command),
            unrestricted_host=_unrestricted_host_authorized(args),
        ))
    state["verification_baseline"] = receipts
    state["verification_baseline_objective_hash"] = objective_hash
    state["verification_baseline_at"] = utcnow()
    baseline_after = receipts[-1].get("git_after") if receipts else git_snapshot(root)
    if isinstance(baseline_after, dict):
        state["last_git_head"] = baseline_after.get("head")
        state["last_git_snapshot_hash"] = snapshot_hash_func(baseline_after)
    json_dump(sd / "verification-baseline.json", {"objective_hash": objective_hash, "receipts": receipts})
    json_dump(sd / "state.json", state)
    return receipts


def _baseline_integrity_error(receipts: list[dict[str, Any]]) -> str | None:
    changed = [
        f"{rec.get('category')}: {rec.get('command')}"
        for rec in receipts
        if isinstance(rec, dict) and not rec.get("tracked_source_unchanged", True)
    ]
    if not changed:
        return None
    return (
        "Baseline verification mutated tracked/indexed repository state before "
        "implementation began: " + "; ".join(changed)[:1600]
    )


def run_verification_gate(
    root: Path,
    sd: Path,
    prof: dict[str, Any],
    state: dict[str, Any],
    args: argparse.Namespace,
    *,
    verification_runner=_run_verification_command,
) -> tuple[str, str, list[dict[str, Any]]]:
    commands = _verification_commands(prof)
    baseline = {(x.get("category"), x.get("command")): x for x in (state.get("verification_baseline") or []) if isinstance(x, dict)}
    timeout = int(getattr(args, "verification_timeout", 900) or 900)
    receipts: list[dict[str, Any]] = []
    failures: list[str] = []
    for category, command in commands:
        rec = verification_runner(
            root, category, command, timeout,
            trust_repo_scripts=_host_execution_authorized(args, command),
            unrestricted_host=_unrestricted_host_authorized(args),
        )
        base = baseline.get((category, command))
        if rec.get("execution_boundary") == "unavailable":
            rec["verdict"] = "UNVERIFIED"
            diagnostic = str(rec.get("stderr_tail") or "no sandbox diagnostic was returned").strip()
            failures.append(
                f"{category}: {command} was not executed because a safe repository execution boundary is unavailable: "
                + diagnostic[:900]
            )
        elif rec.get("timed_out"):
            rec["verdict"] = "UNVERIFIED"
            failures.append(
                f"{category}: {command} timed out before deterministic verification completed"
            )
        elif int(rec.get("exit_code", 1)) in {126, 127}:
            rec["verdict"] = "UNVERIFIED"
            failures.append(
                f"{category}: {command} could not be executed by the verification shell (exit {rec.get('exit_code')})"
            )
        elif not rec.get("tracked_source_unchanged", True):
            rec["verdict"] = "FAIL"
            failures.append(f"{category}: {command} mutated tracked/indexed repository state during verification")
        elif rec["exit_code"] == 0:
            rec["verdict"] = "PASS"
        elif base and base.get("exit_code") == rec["exit_code"] and base.get("signature") == rec.get("signature"):
            rec["verdict"] = "BASELINE_FAILURE_UNCHANGED"
        else:
            rec["verdict"] = "FAIL"
            failures.append(f"{category}: {command} exited {rec['exit_code']}")
        receipts.append(rec)
    bundle = {
        "finished_at": utcnow(), "final_git": git_snapshot(root), "receipts": receipts,
        "detected_command_count": len(commands), "failures": failures,
    }
    json_dump(sd / "verification-final.json", bundle)
    state["last_verification_receipts"] = receipts
    state["last_verification_at"] = bundle["finished_at"]
    state["last_verification_fingerprint"] = sha256_text(json.dumps(bundle["final_git"], sort_keys=True, default=str))
    json_dump(sd / "state.json", state)
    if failures:
        if any(x.get("verdict") == "UNVERIFIED" for x in receipts):
            return "UNVERIFIED", "; ".join(failures)[:2000], receipts
        return "FAIL", "; ".join(failures)[:2000], receipts
    if not commands:
        strong_markers = (
            "tox.ini", "noxfile.py", "build.gradle", "build.gradle.kts", "pom.xml",
            "CMakeLists.txt", "Rakefile",
        )
        has_dotnet = bool(list(root.glob("*.sln")) or list(root.glob("*.csproj")))
        if any((root / x).exists() for x in strong_markers) or has_dotnet:
            return "UNVERIFIED", (
                "Repository contains a recognised verification/build marker but no deterministic "
                "verification command could be established."
            ), receipts
        return "PASS", "No deterministic repository verification commands were detected; correctness/security review gates remain mandatory.", receipts
    return "PASS", f"{len(commands)} deterministic verification commands passed or matched an unchanged pre-existing baseline failure.", receipts



def _security_scanner_commands(root: Path, prof: dict[str, Any]) -> list[tuple[str, list[str]]]:
    langs=set(prof.get("languages") or [])
    out: list[tuple[str,list[str]]] = []
    if shutil.which("gitleaks"):
        out.append(("gitleaks", ["gitleaks","detect","--source",str(root),"--no-banner","--redact","--exit-code","1"]))
    if "python" in langs and shutil.which("bandit"):
        out.append(("bandit", ["bandit","-q","-r",str(root),"-x",f"{root}/.venv,{root}/venv,{root}/node_modules"]))
    if "python" in langs and shutil.which("pip-audit") and any((root/x).exists() for x in ("requirements.txt","pyproject.toml","poetry.lock")):
        out.append(("pip-audit", ["pip-audit"]))
    if prof.get("container_files") and shutil.which("trivy"):
        out.append(("trivy", ["trivy","fs","--scanners","vuln,secret,misconfig","--severity","HIGH,CRITICAL","--exit-code","1","--no-progress",str(root)]))
    if shutil.which("semgrep"):
        out.append(("semgrep", ["semgrep","scan","--config","auto","--error","--quiet",str(root)]))
    if shutil.which("osv-scanner"):
        out.append(("osv-scanner", ["osv-scanner","scan","source","-r",str(root)]))
    if (root/"package.json").exists() and shutil.which("npm"):
        out.append(("npm-audit", ["npm","audit","--audit-level=high"]))
    if (root/"Cargo.toml").exists() and shutil.which("cargo-audit"):
        out.append(("cargo-audit", ["cargo-audit","audit"]))
    if (root/"go.mod").exists() and shutil.which("govulncheck"):
        out.append(("govulncheck", ["govulncheck","./..."]))
    if any((root/x).exists() for x in ("main.tf","terraform.tf",".terraform.lock.hcl")):
        if shutil.which("checkov"):
            out.append(("checkov", ["checkov","-d",str(root),"--quiet","--compact"]))
        elif shutil.which("tfsec"):
            out.append(("tfsec", ["tfsec",str(root)]))
    return out


def _scanner_verdict(name: str, rc: int) -> str:
    """Interpret each scanner according to its own exit-code contract."""
    if rc == 0:
        return "PASS"
    finding_codes = {
        "gitleaks": {1},
        "bandit": {1},
        "pip-audit": {1},
        "trivy": {1},
        "semgrep": {1},
        "osv-scanner": {1},
        "npm-audit": {1},
        "cargo-audit": {1},
        "govulncheck": {1, 3},
        "checkov": {1},
        "tfsec": {1},
    }
    return "FINDING" if rc in finding_codes.get(name, {1}) else "TOOL_ERROR"


def run_security_scanners(
    root: Path,
    sd: Path,
    prof: dict[str, Any],
    timeout: int = 600,
    *,
    trust_repo_scripts: bool = False,
    unrestricted_host: bool = False,
) -> tuple[str,str,list[dict[str,Any]]]:
    receipts=[]; findings=[]; warnings=[]
    for name, cmd in _security_scanner_commands(root,prof):
        before = git_snapshot(root)
        result = run_repository_command(
            root,
            cmd,
            timeout=timeout or None,
            trust_repo_scripts=trust_repo_scripts,
            unrestricted_host=unrestricted_host,
        )
        rc=int(result.get("returncode",125))
        stdout=str(result.get("stdout") or "")
        stderr=str(result.get("stderr") or "")
        timed_out=bool(result.get("timed_out",False))
        after = git_snapshot(root)
        verdict=_scanner_verdict(name,rc)
        if result.get("execution_boundary") == "unavailable":
            verdict="TOOL_ERROR"
            warnings.append(f"{name} was not run because a safe repository execution boundary is unavailable")
        elif not _tracked_source_unchanged(before, after):
            verdict="FINDING"; findings.append(f"{name} mutated tracked/indexed repository state while scanning")
        elif verdict=="FINDING":
            findings.append(f"{name} reported one or more material findings")
        elif verdict=="TOOL_ERROR":
            warnings.append(f"{name} could not complete (exit {rc})")
        rec={
            "scanner":name,
            "command":cmd,
            "exit_code":rc,
            "timed_out":timed_out,
            "verdict":verdict,
            "wall_seconds":float(result.get("wall_seconds",0.0) or 0.0),
            "stdout_tail":redact_text(stdout,None,5000),
            "stderr_tail":redact_text(stderr,None,3000),
            "tracked_source_unchanged":_tracked_source_unchanged(before,after),
            "execution_boundary":result.get("execution_boundary"),
            "sandboxed":bool(result.get("sandboxed",False)),
            "environment_scrubbed":bool(result.get("environment_scrubbed",False)),
        }
        receipts.append(rec)
    json_dump(sd/"security-scanners.json",{"finished_at":utcnow(),"receipts":receipts,"findings":findings,"warnings":warnings})
    if findings:
        return "FAIL", "; ".join(findings)[:2000], receipts
    if warnings:
        return "DEGRADED", "Installed security scanners with unavailable/tool-error diagnostics: "+"; ".join(warnings)[:1500], receipts
    if not receipts:
        return "PASS", "No supported deterministic security scanner is installed; independent security review remains mandatory.", receipts
    return "PASS", f"{len(receipts)} installed deterministic security scanners completed without material findings.", receipts
