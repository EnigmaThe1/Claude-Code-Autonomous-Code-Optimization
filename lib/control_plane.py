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

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from process_runner import run
from protocols import extract_result_json
from repo_runtime import git_snapshot
from telemetry import (
    classify_claude_outcome,
    read_runtime_events,
    redact_text,
    runtime_event_offset,
    transient_backoff_seconds,
    usage_from_result,
)


class ControlPlaneRetryableError(RuntimeError):
    """A planner/reviewer provider boundary that exhausted local retries.

    This is deliberately distinct from a correctness/security BLOCKED verdict:
    the requested engineering work may still be completable once the provider is
    healthy, so the outer supervisor checkpoints a retryable state instead of
    crashing or manufacturing a terminal blocker.
    """
    def __init__(self, outcome: str, reason: str, meta: dict[str, Any] | None = None):
        super().__init__(f"{outcome}: {reason}")
        self.outcome = outcome
        self.reason = reason
        self.meta = meta or {}


def _merge_usage(*items: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, float] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            if isinstance(value, (int, float)):
                out[key] = out.get(key, 0.0) + float(value)
    return {k: (int(v) if float(v).is_integer() and k != "total_cost_usd" else v) for k, v in out.items()}


def _repo_snapshot_unchanged(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return all(before.get(k) == after.get(k) for k in (
        "head", "worktree_diff_sha256", "index_diff_sha256", "untracked_sha256", "ignored_sha256"
    ))


def _tracked_source_unchanged(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return all(before.get(k) == after.get(k) for k in (
        "head", "worktree_diff_sha256", "index_diff_sha256"
    ))


def _completion_source_unchanged(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Bind completion evidence to one exact source snapshot.

    Ignored build/cache outputs are deliberately excluded, but committed state,
    tracked/index changes and untracked source inputs must remain identical.
    """
    return all(before.get(k) == after.get(k) for k in (
        "head", "worktree_diff_sha256", "index_diff_sha256", "untracked_sha256",
        "verification_contract_sha256",
    ))


def _run_control_model(
    *,
    cmd: list[str],
    root: Path,
    sd: Path,
    env: dict[str, str],
    timeout: int | None,
    max_transient_retries: int = 4,
    runner=run,
) -> tuple[subprocess.CompletedProcess[str], str, str | None, dict[str, Any] | None, str, str, dict[str, float], list[dict[str, Any]], float]:
    """Run a planner/reviewer call with worker-equivalent transient recovery."""
    attempts: list[dict[str, Any]] = []
    aggregate_usage: dict[str, float] = {}
    total_wall = 0.0

    for attempt in range(1, max(1, max_transient_retries + 1) + 1):
        offset = runtime_event_offset(sd)
        started = time.monotonic()
        cp = runner(cmd, cwd=root, timeout=timeout, env=env)
        wall = max(0.0, time.monotonic() - started)
        total_wall += wall
        result_text, session_id, raw = extract_result_json(cp.stdout)
        runtime_events = read_runtime_events(sd, offset)
        outcome, reason = classify_claude_outcome(cp, raw, result_text, runtime_events)
        usage = usage_from_result(raw)
        aggregate_usage = _merge_usage(aggregate_usage, usage)
        attempts.append({
            "attempt": attempt,
            "returncode": cp.returncode,
            "outcome": outcome,
            "reason": redact_text(reason, env, 800),
            "usage": usage,
            "wall_seconds": round(wall, 3),
        })

        if outcome not in {"TRANSIENT_PROVIDER", "TIMEOUT_RETRYABLE"}:
            return cp, result_text, session_id, raw, outcome, reason, aggregate_usage, attempts, total_wall

        if attempt > max_transient_retries:
            return cp, result_text, session_id, raw, outcome, reason, aggregate_usage, attempts, total_wall

        delay = transient_backoff_seconds(attempt, base=2.0, cap=30.0)
        if os.environ.get("CLAUDE_AUTO_TEST_NO_BACKOFF") == "1":
            delay = 0.0
        if delay:
            time.sleep(delay)

    raise RuntimeError("unreachable control-model retry state")


def run_readonly_plan_agent(
    *,
    root: Path,
    sd: Path,
    prompt: str,
    env: dict[str, str],
    provider_detail: dict[str, Any],
    model: str | None,
    timeout: int | None,
    max_turns: int = 35,
    verify_repo: bool = False,
    max_budget_usd: float | None = None,
    settings_path: Path | None = None,
    runner=run,
) -> tuple[str, dict[str, Any]]:
    """Run a fresh hard-read-only planning/control context and prove it did not mutate the repo."""
    before = git_snapshot(root) if verify_repo else None
    cmd = [
        "claude",
        "--restricted",
        "--settings", str(settings_path or (sd / "settings-readonly.json")),
        "--setting-sources", "",
        "--add-dir", str(sd),
        "--tools", "Read,Glob,Grep",
        "--disallowed-tools", "Edit,Write,NotebookEdit,Bash,mcp__*",
        "--permission-mode", "plan",
        "--permission-prompts", "none",
        "--effort", "high",
    ]
    if model:
        cmd += ["--model", model]
    if max_budget_usd is not None:
        cmd += ["--max-budget-usd", str(max_budget_usd)]
    # Use the long spelling so existing main-worker command detection remains distinct.
    cmd += [
        "--print", "--output-format", "json", "--max-turns", str(max_turns),
        "--no-session-persistence",
        "--exclude-dynamic-system-prompt-sections",
        prompt,
    ]
    cp, result_text, session_id, raw, outcome, outcome_reason, aggregate_usage, attempts, wall_seconds = _run_control_model(
        cmd=cmd,
        root=root,
        sd=sd,
        env=env,
        timeout=timeout,
        max_transient_retries=4,
        runner=runner,
    )
    after = git_snapshot(root) if verify_repo else None
    unchanged = _repo_snapshot_unchanged(before, after) if before is not None and after is not None else None
    meta = {
        "provider": provider_detail,
        "model": model,
        "returncode": cp.returncode,
        "session_id": session_id,
        "usage": aggregate_usage,
        "repository_unchanged": unchanged,
        "git_before": before,
        "git_after": after,
        "stderr_tail": redact_text(cp.stderr or "", env, 1600),
        "outcome": outcome,
        "outcome_reason": redact_text(outcome_reason, env, 1000),
        "attempts": attempts,
        "wall_seconds": round(wall_seconds, 3),
    }
    if unchanged is False:
        raise RuntimeError("Read-only plan-control invariant violated: repository state changed.")
    if cp.returncode != 0:
        detail = redact_text(
            outcome_reason or cp.stderr or cp.stdout or f"Claude exited {cp.returncode}",
            env,
            1800,
        )
        if outcome in {"TRANSIENT_PROVIDER", "TIMEOUT_RETRYABLE"}:
            raise ControlPlaneRetryableError(outcome, detail, meta)
        raise RuntimeError(f"{outcome}: " + detail)
    return result_text, meta
