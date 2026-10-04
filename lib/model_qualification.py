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
import json
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import asdict
from pathlib import Path
from typing import Any

from control_plane import ControlPlaneRetryableError, _repo_snapshot_unchanged, _run_control_model
from environment_policy import sanitised_subprocess_env
from protocols import parse_json_protocol, parse_qualification
from provider_config import (
    QUALIFICATION_RANK,
    _claude_version_text,
    _model_registry_name,
    provider_env,
    qualification_route_fingerprint,
    require_supported_claude,
    save_model_qualification,
)
from repo_identity import SupervisorLease, find_repo_root, repo_state_dir
from repo_profile import RepoProfile, profile_repo
from repo_runtime import activate, git_snapshot, make_runtime_agents
from runtime_paths import data_home, ensure_private_dir, utcnow
from settings_policy import make_settings, refuse_nested_claude_launch
from state_store import json_dump
from telemetry import redact_text

def qualification_challenge(root: Path, prof: RepoProfile) -> tuple[str | None, str | None]:
    candidates = [*prof.repo_instruction_files, *prof.manifests]
    for rel in candidates:
        p = root / rel
        try:
            if not p.is_file() or p.stat().st_size > 200_000:
                continue
            text = p.read_text(errors="replace")
            line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
            if line:
                return rel, line[:120]
        except Exception:
            continue
    return None, None


def _qualification_command(
    *,
    settings_path: Path,
    model: str | None,
    effort: str,
    tools: str,
    denied: str,
    prompt: str,
    max_turns: int,
    agents: str | None = None,
    permission_mode: str = "auto",
    max_budget_usd: float | None = None,
    setting_sources: str = "user,project,local",
) -> list[str]:
    cmd = [
        "claude", "--restricted",
        "--settings", str(settings_path),
        "--setting-sources", setting_sources,
        "--tools", tools,
        "--disallowed-tools", denied,
        "--permission-mode", permission_mode,
        "--permission-prompts", "none",
        "--effort", effort,
    ]
    if model:
        cmd += ["--model", model]
    if agents:
        cmd += ["--agents", agents]
    if max_budget_usd is not None:
        cmd += ["--max-budget-usd", str(max(0.0, max_budget_usd))]
    cmd += [
        "-p", "--output-format", "json", "--max-turns", str(max_turns),
        "--no-session-persistence", "--exclude-dynamic-system-prompt-sections", prompt,
    ]
    return cmd


def _qualification_stage(
    *,
    name: str,
    cmd: list[str],
    cwd: Path,
    sd: Path,
    env: dict[str, str],
    timeout: int | None,
    protocol: str,
) -> tuple[bool, dict[str, Any]]:
    run_env = dict(env)
    run_env["CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"] = str(max(8, min(128, int(cmd[cmd.index("--max-turns") + 1]))))
    run_env["CLAUDE_AUTO_HEADLESS"] = "1"
    cp, text_result, session_id, raw, outcome, reason, aggregate_usage, attempts, wall = _run_control_model(
        cmd=cmd,
        root=cwd,
        sd=sd,
        env=run_env,
        timeout=timeout,
        max_transient_retries=4,
    )
    parsed = parse_json_protocol(text_result, protocol)
    meta = {
        "name": name,
        "ok": False,
        "returncode": cp.returncode,
        "outcome": outcome,
        "outcome_reason": redact_text(reason, run_env, 600),
        "session_id": session_id,
        "usage": aggregate_usage,
        "wall_seconds": round(wall, 3),
        "protocol": parsed,
        "stderr_tail": redact_text(cp.stderr or "", run_env, 800),
        "attempts": attempts,
    }
    if cp.returncode != 0 and outcome in {"TRANSIENT_PROVIDER", "TIMEOUT_RETRYABLE"}:
        raise ControlPlaneRetryableError(outcome, meta["outcome_reason"], meta)
    ok = cp.returncode == 0 and outcome == "SUCCESS" and isinstance(parsed, dict) and bool(parsed.get("ok"))
    meta["ok"] = ok
    return ok, meta


def qualify_model(args: argparse.Namespace) -> int:
    refuse_nested_claude_launch()
    require_supported_claude()
    root = find_repo_root(args.repo)
    sd = repo_state_dir(root)
    with SupervisorLease(sd, root):
        try:
            return _qualify_model_unlocked(args, root, sd)
        except ControlPlaneRetryableError as exc:
            print(
                "Model qualification is checkpointed because the provider remained "
                f"temporarily unavailable after bounded retries: {exc.reason}",
                file=sys.stderr,
            )
            return 7


def _qualify_model_unlocked(args: argparse.Namespace, root: Path, sd: Path) -> int:
    activate(root)
    prof = profile_repo(root)
    before = git_snapshot(root)
    env, detail = provider_env(
        args.provider,
        gateway_url=args.gateway_url,
        gateway_token_env=args.gateway_token_env,
        enable_discovery=args.gateway_discovery,
        isolate_provider_profile=args.isolate_provider_profile,
        gateway_hints=args.gateway_hints,
    )
    timeout = args.timeout if args.timeout else None
    requested_level = getattr(args, "level", "full")
    session_settings = getattr(args, "session_settings", None) or "compatibility"
    qualification_setting_sources = "" if session_settings == "hermetic" else "user,project,local"
    stages: list[dict[str, Any]] = []
    qualification_total_budget = getattr(args, "max_total_budget_usd", None)
    qualification_per_call_budget = getattr(args, "max_budget_usd", None)

    def qualification_spent() -> float:
        return sum(float((st.get("usage") or {}).get("total_cost_usd", 0) or 0) for st in stages if isinstance(st, dict))

    def qualification_budget_remaining() -> float | None:
        if qualification_total_budget is None:
            return None
        return max(0.0, float(qualification_total_budget) - qualification_spent())

    def qualification_can_launch() -> bool:
        remaining = qualification_budget_remaining()
        return remaining is None or remaining > 1e-9

    def qualification_call_budget() -> float | None:
        remaining = qualification_budget_remaining()
        if qualification_per_call_budget is None:
            return remaining
        if remaining is None:
            return float(qualification_per_call_budget)
        return min(float(qualification_per_call_budget), remaining)

    # Stage 1: hard read-only protocol/tool compatibility against the real target
    # repository. This preserves the qualification non-mutation proof.
    rel, sample = qualification_challenge(root, prof)
    prompt = textwrap.dedent(f"""
    This is a HARD READ-ONLY Claude Code harness compatibility qualification.
    Static settings and a PreToolUse guard prevent repository writes. Do not attempt to bypass them.
    Use only Read/Glob/Grep. Shell, MCP, and write tools are unavailable.

    Inspect the current repository and return ONE final line containing valid compact JSON:
    MODEL_QUALIFICATION: {{"challenge_file":<string-or-null>,"challenge_sample":<string-or-null>,"instruction_files":[...],"manifests":[...]}}

    For challenge_file, read exactly this repository-relative path: {rel!r}
    challenge_sample must be the first non-empty line from that file, truncated to at most 120 characters.
    List repository instruction files and build/package manifests you actually observed.
    Do not add prose after MODEL_QUALIFICATION.
    """).strip()
    readonly_cmd = [
        "claude", "--restricted",
        "--settings", str(sd / "settings-readonly.json"),
        "--setting-sources", qualification_setting_sources,
        "--tools", "Read,Glob,Grep",
        "--disallowed-tools", "Edit,Write,NotebookEdit,Bash,AskUserQuestion,mcp__*",
        "--permission-mode", "plan", "--permission-prompts", "none", "--effort", args.effort,
    ]
    if args.model:
        readonly_cmd += ["--model", args.model]
    readonly_budget = qualification_call_budget()
    if not qualification_can_launch():
        result = {
            "provider": args.provider,
            "model": _model_registry_name(args.model),
            "qualified_at": utcnow(),
            "claude_code_version": _claude_version_text(),
            "route_fingerprint": qualification_route_fingerprint(args.provider, args.model, {"gateway_url": getattr(args,"gateway_url",None), "gateway_token_env": getattr(args,"gateway_token_env",None), "gateway_discovery": getattr(args,"gateway_discovery",None), "isolate_provider_profile": getattr(args,"isolate_provider_profile",False), "gateway_hints": getattr(args,"gateway_hints",True)}),
            "requested_level": requested_level,
            "qualification_level": "FAILED",
            "highest_achieved_level": "FAILED",
            "compatible": False,
            "score": 0,
            "max_score": 0,
            "stages": [],
            "repository_unchanged": True,
            "provider_detail": detail,
            "repo_profile_id": prof.repo_id,
            "repo_root": str(root),
            "failure_reason": "qualification aggregate budget exhausted before first stage",
        }
        save_model_qualification(args.provider, args.model, result)
        print(json.dumps(result, indent=2))
        return 6
    if readonly_budget is not None:
        readonly_cmd += ["--max-budget-usd", str(readonly_budget)]
    readonly_cmd += [
        "-p", "--output-format", "json", "--max-turns", str(args.max_turns),
        "--no-session-persistence", "--exclude-dynamic-system-prompt-sections", prompt,
    ]
    cp, text_result, session_id, raw, outcome, reason, readonly_usage, readonly_attempts, readonly_wall = _run_control_model(
        cmd=readonly_cmd,
        root=root,
        sd=sd,
        env=env,
        timeout=timeout,
        max_transient_retries=4,
    )
    if cp.returncode != 0 and outcome in {"TRANSIENT_PROVIDER", "TIMEOUT_RETRYABLE"}:
        raise ControlPlaneRetryableError(
            outcome,
            redact_text(reason, env, 1000),
            {
                "name": "readonly",
                "returncode": cp.returncode,
                "outcome": outcome,
                "outcome_reason": redact_text(reason, env, 600),
                "usage": readonly_usage,
                "wall_seconds": round(readonly_wall, 3),
                "attempts": readonly_attempts,
            },
        )
    observed = parse_qualification(text_result)
    after_read = git_snapshot(root)
    unchanged = _repo_snapshot_unchanged(before, after_read)
    expected_instructions = set(prof.repo_instruction_files)
    expected_manifests = set(prof.manifests)
    observed_instructions = set(observed.get("instruction_files", [])) if observed else set()
    observed_manifests = set(observed.get("manifests", [])) if observed else set()
    read_checks = {
        "process_exit": cp.returncode == 0,
        "runtime_outcome_success": outcome == "SUCCESS",
        "parseable_protocol": observed is not None,
        "repository_unchanged": unchanged,
        "challenge_exact": bool(observed is not None and observed.get("challenge_file") == rel and observed.get("challenge_sample") == sample),
        "instruction_recall": not expected_instructions or bool(expected_instructions & observed_instructions),
        "manifest_recall": not expected_manifests or bool(expected_manifests & observed_manifests),
    }
    read_ok = all(read_checks[k] for k in ("process_exit", "runtime_outcome_success", "parseable_protocol", "repository_unchanged", "challenge_exact")) and sum(bool(v) for v in read_checks.values()) >= 6
    stages.append({
        "name": "readonly",
        "ok": read_ok,
        "checks": read_checks,
        "outcome": outcome,
        "outcome_reason": redact_text(reason, env, 600),
        "session_id": session_id,
        "usage": readonly_usage,
        "wall_seconds": round(readonly_wall,3),
        "stderr_tail": redact_text(cp.stderr or "", env, 800),
        "attempts": readonly_attempts,
    })

    achieved = "HARNESS_SMOKE_READONLY" if read_ok else "FAILED"
    if read_ok and qualification_can_launch() and requested_level in {"auto", "full"}:
        qroot_parent = data_home() / "qualification-sandboxes"
        ensure_private_dir(qroot_parent)
        with tempfile.TemporaryDirectory(prefix="claude-auto-model-", dir=qroot_parent) as qd:
            scratch = Path(qd).resolve()
            git_env = sanitised_subprocess_env()
            subprocess.run(["git", "init", "-q", str(scratch)], check=True, env=git_env)
            subprocess.run(["git", "-C", str(scratch), "config", "user.email", "qualification@example.invalid"], check=True, env=git_env)
            subprocess.run(["git", "-C", str(scratch), "config", "user.name", "Claude Auto Qualification"], check=True, env=git_env)
            (scratch / "seed.txt").write_text("CLAUDE_AUTO_SEED\n")
            subprocess.run(["git", "-C", str(scratch), "add", "seed.txt"], check=True, env=git_env)
            subprocess.run(["git", "-C", str(scratch), "commit", "-qm", "qualification baseline"], check=True, env=git_env)
            origin_dir = Path(qd) / "origin.git"
            subprocess.run(["git", "init", "--bare", "-q", str(origin_dir)], check=True, env=git_env)
            subprocess.run(["git", "-C", str(scratch), "remote", "add", "origin", str(origin_dir)], check=True, env=git_env)
            subprocess.run(["git", "-C", str(scratch), "push", "-q", "-u", "origin", "HEAD"], check=True, env=git_env)
            qprof = asdict(profile_repo(scratch))
            qsettings = scratch / "qualification-settings.json"
            json_dump(qsettings, make_settings(sd, "external", "balanced", qprof))
            common_denied = "AskUserQuestion,mcp__*"

            auto_prompt = textwrap.dedent("""
            CLAUDE AUTO EXECUTION QUALIFICATION. This is an isolated disposable Git repository.
            Use Bash to create auto-shell-proof.txt containing exactly Claude Auto_AUTO_BASH_OK followed by a newline, then read it back with Bash.
            Do not ask a question. Return exactly:
            AUTO_EXECUTION_QUALIFICATION: {"ok":true}
            """).strip()
            auto_cmd = _qualification_command(
                settings_path=qsettings, model=args.model, effort=args.effort,
                tools="Read,Bash", denied=f"Edit,Write,NotebookEdit,{common_denied}", prompt=auto_prompt,
                max_turns=max(6, min(args.max_turns, 20)), permission_mode="auto", max_budget_usd=qualification_call_budget(),
                setting_sources=qualification_setting_sources,
            )
            auto_ok, auto_meta = _qualification_stage(
                name="auto_execution", cmd=auto_cmd, cwd=scratch, sd=sd, env=env, timeout=timeout,
                protocol="AUTO_EXECUTION_QUALIFICATION",
            )
            auto_ok = auto_ok and (scratch / "auto-shell-proof.txt").read_text(errors="replace") == "Claude Auto_AUTO_BASH_OK\n" if (scratch / "auto-shell-proof.txt").exists() else False
            auto_meta["ok"] = bool(auto_ok)
            auto_meta["artifact_verified"] = bool(auto_ok)
            stages.append(auto_meta)
            if auto_ok:
                achieved = "AUTO_EXECUTION"

            if auto_ok and requested_level == "full":
                mut_ok = goal_ok = sub_ok = False
                review_checks: list[bool] = []

                if qualification_can_launch():
                    mutation_prompt = textwrap.dedent("""
                    CLAUDE AUTO SAFE MUTATION QUALIFICATION. This is an isolated disposable Git repository.
                    Use the Write tool to create mutation-proof.txt with Claude Auto_MUTATION_INITIAL and a newline.
                    Then use Edit to change INITIAL to OK. Read the file and verify its exact final content.
                    Do not use AskUserQuestion. Return exactly:
                    MUTATION_QUALIFICATION: {"ok":true}
                    """).strip()
                    mutation_cmd = _qualification_command(
                        settings_path=qsettings, model=args.model, effort=args.effort,
                        tools="Read,Write,Edit", denied=f"Bash,NotebookEdit,{common_denied}", prompt=mutation_prompt,
                        max_turns=max(8, min(args.max_turns, 24)), permission_mode="auto", max_budget_usd=qualification_call_budget(),
                        setting_sources=qualification_setting_sources,
                    )
                    mut_ok, mut_meta = _qualification_stage(
                        name="safe_mutation", cmd=mutation_cmd, cwd=scratch, sd=sd, env=env, timeout=timeout,
                        protocol="MUTATION_QUALIFICATION",
                    )
                    mut_ok = mut_ok and (scratch / "mutation-proof.txt").exists() and (scratch / "mutation-proof.txt").read_text(errors="replace") == "Claude Auto_MUTATION_OK\n"
                    mut_meta["ok"] = bool(mut_ok)
                    mut_meta["artifact_verified"] = bool(mut_ok)
                    stages.append(mut_meta)

                if mut_ok and qualification_can_launch():
                    goal_prompt = textwrap.dedent("""
                    /goal CLAUDE AUTO GOAL CONTINUITY QUALIFICATION. In this isolated disposable repository, create goal-proof.txt containing exactly CLAUDE_AUTO_GOAL_OK and a newline, verify it, and do not stop until it is correct. Your final response must include exactly one protocol line: GOAL_QUALIFICATION: {"ok":true}
                    """).strip()
                    goal_cmd = _qualification_command(
                        settings_path=qsettings, model=args.model, effort=args.effort,
                        tools="Read,Write,Edit,Bash", denied=common_denied, prompt=goal_prompt,
                        max_turns=max(10, min(args.max_turns, 32)), permission_mode="auto", max_budget_usd=qualification_call_budget(),
                        setting_sources=qualification_setting_sources,
                    )
                    goal_ok, goal_meta = _qualification_stage(
                        name="goal_continuity", cmd=goal_cmd, cwd=scratch, sd=sd, env=env, timeout=timeout,
                        protocol="GOAL_QUALIFICATION",
                    )
                    goal_ok = goal_ok and (scratch / "goal-proof.txt").exists() and (scratch / "goal-proof.txt").read_text(errors="replace") == "CLAUDE_AUTO_GOAL_OK\n"
                    goal_meta["ok"] = bool(goal_ok)
                    goal_meta["artifact_verified"] = bool(goal_ok)
                    stages.append(goal_meta)

                if goal_ok and qualification_can_launch():
                    sub_prompt = textwrap.dedent("""
                    CLAUDE AUTO SUBAGENT QUALIFICATION. Use the autonomy-verifier subagent exactly once to inspect seed.txt and confirm it contains CLAUDE_AUTO_SEED. Do not modify files and do not ask a question. After the verifier returns, output exactly:
                    SUBAGENT_QUALIFICATION: {"ok":true}
                    """).strip()
                    sub_cmd = _qualification_command(
                        settings_path=qsettings, model=args.model, effort=args.effort,
                        tools="Read,Glob,Grep,Agent", denied=f"Edit,Write,NotebookEdit,Bash,{common_denied}", prompt=sub_prompt,
                        max_turns=max(8, min(args.max_turns, 24)), agents=make_runtime_agents(sd, args.model, args.model),
                        permission_mode="auto", max_budget_usd=qualification_call_budget(),
                        setting_sources=qualification_setting_sources,
                    )
                    sub_ok, sub_meta = _qualification_stage(
                        name="subagent_evaluator", cmd=sub_cmd, cwd=scratch, sd=sd, env=env, timeout=timeout,
                        protocol="SUBAGENT_QUALIFICATION",
                    )
                    stages.append(sub_meta)

                if sub_ok:
                    for review_name, review_prompt in (("code_review", "/code-review high"), ("security_review", "/security-review")):
                        if not qualification_can_launch():
                            break
                        review_cmd = _qualification_command(
                            settings_path=qsettings, model=args.model, effort=args.effort,
                            tools="Read,Glob,Grep", denied=f"Edit,Write,NotebookEdit,Bash,{common_denied}",
                            prompt=review_prompt, max_turns=max(8, min(args.max_turns, 24)), permission_mode="plan", max_budget_usd=qualification_call_budget(),
                            setting_sources=qualification_setting_sources,
                        )
                        before_review = git_snapshot(scratch)
                        rcp, rtxt, rsid, rraw, routcome, rreason, rusage, rattempts, rwall = _run_control_model(
                            cmd=review_cmd,
                            root=scratch,
                            sd=sd,
                            env=env,
                            timeout=timeout,
                            max_transient_retries=4,
                        )
                        after_review = git_snapshot(scratch)
                        unchanged_review = _repo_snapshot_unchanged(before_review, after_review)
                        rmeta = {
                            "name": review_name,
                            "ok": False,
                            "returncode": rcp.returncode,
                            "session_id": rsid,
                            "usage": rusage,
                            "wall_seconds": round(rwall, 3),
                            "repository_unchanged": unchanged_review,
                            "stderr_tail": redact_text(rcp.stderr or "", env, 800),
                            "outcome": routcome,
                            "outcome_reason": redact_text(rreason, env, 600),
                            "attempts": rattempts,
                        }
                        if rcp.returncode != 0 and routcome in {"TRANSIENT_PROVIDER", "TIMEOUT_RETRYABLE"}:
                            raise ControlPlaneRetryableError(routcome, rmeta["outcome_reason"], rmeta)
                        rok = rcp.returncode == 0 and routcome == "SUCCESS" and unchanged_review
                        rmeta["ok"] = rok
                        stages.append(rmeta)
                        review_checks.append(rok)
                        if not rok:
                            break

                if mut_ok and goal_ok and sub_ok and len(review_checks) == 2 and all(review_checks):
                    achieved = "AUTONOMOUS_FULL"

    final_after = git_snapshot(root)
    repo_unchanged_final = before == final_after
    if not repo_unchanged_final:
        achieved = "FAILED"
    required = {"readonly": "HARNESS_SMOKE_READONLY", "auto": "AUTO_EXECUTION", "full": "AUTONOMOUS_FULL"}[requested_level]
    compatible = QUALIFICATION_RANK.get(achieved, 0) >= QUALIFICATION_RANK[required] and repo_unchanged_final
    all_checks = sum(1 for st in stages if st.get("ok"))
    result = {
        "provider": args.provider,
        "model": _model_registry_name(args.model),
        "qualified_at": utcnow(),
        "claude_code_version": _claude_version_text(),
        "route_fingerprint": qualification_route_fingerprint(args.provider, args.model, {"gateway_url": getattr(args,"gateway_url",None), "gateway_token_env": getattr(args,"gateway_token_env",None), "gateway_discovery": getattr(args,"gateway_discovery",None), "isolate_provider_profile": getattr(args,"isolate_provider_profile",False), "gateway_hints": getattr(args,"gateway_hints",True)}),
        "requested_level": requested_level,
        "qualification_level": achieved if compatible else "FAILED",
        "highest_achieved_level": achieved,
        "compatible": compatible,
        "score": all_checks,
        "max_score": len(stages),
        "stages": stages,
        "repository_unchanged": repo_unchanged_final,
        "provider_detail": detail,
        "repo_profile_id": prof.repo_id,
        "repo_root": str(root),
        "note": "AUTONOMOUS_FULL proves this model/provider lane can execute headless Auto shell, safe Write/Edit mutation, /goal continuity, verifier subagent, and native correctness/security review commands in an isolated disposable repository. It is a harness qualification, not a guarantee of reasoning quality.",
    }
    save_model_qualification(args.provider, args.model, result)
    print(json.dumps(result, indent=2))
    if cp.returncode != 0:
        print(redact_text(cp.stderr or cp.stdout, env, 1200), file=sys.stderr)
    return 0 if compatible else 6
