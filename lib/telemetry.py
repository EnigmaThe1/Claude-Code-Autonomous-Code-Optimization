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
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from state_store import sha256_text


def _secret_values(env: dict[str, str] | None = None) -> list[str]:
    source = env or os.environ
    out = []
    for key, value in source.items():
        if not value or len(value) < 8:
            continue
        if re.search(r"(TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|AUTH)", key, re.I):
            out.append(value)
    return sorted(set(out), key=len, reverse=True)


def redact_text(text: str | None, env: dict[str, str] | None = None, limit: int | None = None) -> str:
    out = text or ""
    for value in _secret_values(env):
        out = out.replace(value, "[REDACTED_SECRET]")
    # Common token/key shapes; conservative and intentionally lossy in logs.
    out = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED_KEY]", out)
    out = re.sub(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}\b", "Bearer [REDACTED]", out, flags=re.I)
    if limit is not None and len(out) > limit:
        out = out[-limit:]
    return out


def compact_run_log(
    *,
    cycle: int,
    started_at: str,
    finished_at: str,
    provider: dict[str, Any],
    model: str | None,
    returncode: int,
    result_text: str,
    session_id: str | None,
    raw_obj: dict[str, Any] | None,
    stderr: str,
    git_before: dict[str, Any],
    git_after: dict[str, Any],
    env: dict[str, str],
    retain_transcripts: bool = False,
    stdout: str = "",
) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "cycle": cycle,
        "started_at": started_at,
        "finished_at": finished_at,
        "provider": provider,
        "model": model,
        "returncode": returncode,
        "session_id": session_id,
        "usage": usage_from_result(raw_obj),
        "result_tail": redact_text(result_text, env, 4000),
        "stderr_tail": redact_text(stderr, env, 2000),
        "git_before": git_before,
        "git": git_after,
    }
    if retain_transcripts:
        obj["stdout"] = redact_text(stdout, env)
        obj["parsed_result"] = redact_text(result_text, env)
        obj["stderr"] = redact_text(stderr, env)
    return obj

TRANSIENT_FAILURES = {"rate_limit", "overloaded", "server_error", "unknown"}
BLOCKING_FAILURES = {"authentication_failed", "oauth_org_not_allowed", "account_on_hold", "billing_error", "cloud_credential_error", "invalid_request"}


def runtime_event_path(sd: Path) -> Path:
    return sd / "runtime-events.jsonl"


def runtime_event_offset(sd: Path) -> int:
    try:
        return runtime_event_path(sd).stat().st_size
    except OSError:
        return 0


def read_runtime_events(sd: Path, offset: int) -> list[dict[str, Any]]:
    path = runtime_event_path(sd)
    try:
        with path.open("r", encoding="utf-8") as f:
            f.seek(offset)
            lines = f.readlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-200:]:
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                out.append(obj)
        except json.JSONDecodeError:
            continue
    return out


def classify_claude_outcome(
    cp: subprocess.CompletedProcess[str],
    raw_obj: dict[str, Any] | None,
    result_text: str,
    runtime_events: list[dict[str, Any]],
) -> tuple[str, str]:
    """Map Claude process endings to supervisor semantics.

    The classifier prefers structured Claude result fields and StopFailure events.
    Human-facing stderr is used only as a final compatibility fallback.
    """
    if bool(getattr(cp, "timed_out", False)):
        return "TIMEOUT_RETRYABLE", f"Claude invocation exceeded {getattr(cp, 'timeout_seconds', None)} seconds"

    obj = raw_obj if isinstance(raw_obj, dict) else {}
    subtype = str(obj.get("subtype") or "").lower()
    terminal_reason = str(obj.get("terminal_reason") or obj.get("stop_reason") or "").lower()
    errors = obj.get("errors") if isinstance(obj.get("errors"), list) else []
    structured = " ".join([subtype, terminal_reason, *(str(x) for x in errors)]).lower()
    stderr_text = (cp.stderr or "").lower()
    classifier_signals = (structured + " " + stderr_text).strip()

    if subtype == "error_max_turns" or terminal_reason == "max_turns" or "maximum number of turns" in classifier_signals:
        return "TURN_LIMIT", "Claude reached the per-invocation turn boundary"
    if subtype in {"error_max_budget_usd", "error_max_budget"} or terminal_reason in {"max_budget", "max_budget_usd"} or "maximum budget" in classifier_signals:
        return "BUDGET_LIMIT", "Claude reached the per-invocation budget boundary"

    stop_failures = [x for x in runtime_events if x.get("event") == "StopFailure"]
    if stop_failures:
        latest = stop_failures[-1]
        kind = str(latest.get("error") or "unknown")
        detail = str(latest.get("error_details") or latest.get("last_assistant_message") or kind)[:600]
        if kind in TRANSIENT_FAILURES:
            return "TRANSIENT_PROVIDER", detail
        if kind == "model_not_found":
            return "MODEL_UNAVAILABLE", detail
        if kind == "max_output_tokens":
            return "OUTPUT_LIMIT_RETRYABLE", detail
        if kind in BLOCKING_FAILURES:
            return "EXTERNAL_BLOCKER", detail
        return "PROVIDER_ERROR", detail

    if cp.returncode == 0:
        # A clean process result is usable even when classifier denials occurred;
        # Claude receives those denials and can choose another safe route.
        return "SUCCESS", "Claude process completed"

    # Compatibility fallbacks for versions/providers that did not emit
    # StopFailure.  Classify only from stderr, which is process/provider
    # telemetry; never infer terminal semantics from the worker's own prose
    # summary.  A benign summary mentioning "credentials", "permission", or a
    # "server down" scenario must not change the supervisor state.
    fallback = stderr_text
    if re.search(r"rate.?limit|\b429\b|overload|temporar(?:y|ily) unavailable|server error|\b5\d\d\b|(?:provider|native|service).{0,20}down", fallback):
        return "TRANSIENT_PROVIDER", (cp.stderr or "transient provider failure")[-600:]
    if re.search(r"model.+not found|unknown model|unsupported model", fallback):
        return "MODEL_UNAVAILABLE", (cp.stderr or "model unavailable")[-600:]
    if re.search(r"auth(?:entication)? failed|billing|account on hold|(?:missing|invalid|expired).{0,20}(?:credential|api.?key|token)", fallback):
        return "EXTERNAL_BLOCKER", (cp.stderr or "provider/account blocker")[-600:]
    if re.search(r"permission denied|approval required|askuserquestion|requiresuserinteraction", fallback):
        return "INTERACTION_BLOCKED", (cp.stderr or "headless interaction blocked")[-600:]
    return "REAL_ERROR", (cp.stderr or result_text or f"Claude exited {cp.returncode}")[-600:]


def progress_evidence(state: dict[str, Any], snap: dict[str, Any]) -> dict[str, Any]:
    """Deterministic progress material beyond Git mutations alone."""
    return {
        "head": snap.get("head"),
        "worktree_diff_sha256": snap.get("worktree_diff_sha256"),
        "index_diff_sha256": snap.get("index_diff_sha256"),
        "untracked_sha256": snap.get("untracked_sha256"),
        "verification_contract_sha256": snap.get("verification_contract_sha256"),
        "plan_version": state.get("plan_version"),
        "plan_hash": state.get("plan_hash"),
        "plan_status": state.get("plan_status"),
        "last_plan_impact": state.get("last_plan_impact"),
        "approved_remediation": state.get("approved_remediation"),
        "plan_revalidation_sha256": sha256_text(json.dumps(state.get("plan_revalidation_findings"), sort_keys=True, default=str)) if state.get("plan_revalidation_findings") is not None else None,
        "last_challenger_verdict": state.get("last_challenger_verdict"),
        "last_security_review_verdict": state.get("last_security_review_verdict"),
        "security_review_findings_sha256": sha256_text(json.dumps(state.get("last_security_review_findings"), sort_keys=True, default=str)) if state.get("last_security_review_findings") is not None else None,
        "progress_checkpoint_sha256": sha256_text(json.dumps(state.get("progress_checkpoint"), sort_keys=True, default=str)) if state.get("progress_checkpoint") is not None else None,
    }


def _finite_nonnegative(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0.0 else None


def _mark_invalid_accounting(state: dict[str, Any], field: str, value: Any) -> None:
    state["usage_accounting_invalid"] = True
    details = state.setdefault("usage_accounting_errors", [])
    if isinstance(details, list) and len(details) < 50:
        details.append({"field": field, "value": repr(value)[:200]})


def _state_counter(state: dict[str, Any], field: str) -> float | None:
    value = state.get(field, 0)
    number = _finite_nonnegative(value)
    if number is None:
        _mark_invalid_accounting(state, field, value)
    return number


def circuit_breaker_reason(args: argparse.Namespace, state: dict[str, Any]) -> str | None:
    turns = _state_counter(state, "total_reported_turns")
    wall = _state_counter(state, "total_wall_seconds")
    cost = _state_counter(state, "total_cost_usd")
    if state.get("usage_accounting_invalid") or None in {turns, wall, cost}:
        return "Global usage accounting is invalid; autonomous continuation is blocked until the accounting state is reconciled."

    max_turns = int(getattr(args, "max_total_turns", 0) or 0)
    max_wall = int(getattr(args, "max_wall_seconds", 0) or 0)
    if max_turns > 0 and float(turns) >= max_turns:
        return f"Global reported-turn limit reached ({turns:g}/{max_turns})."
    if max_wall > 0 and float(wall) >= max_wall:
        return f"Global wall-time limit reached ({wall:.1f}s/{max_wall}s)."

    max_total_budget = getattr(args, "max_total_budget_usd", None)
    if max_total_budget is not None:
        budget = _finite_nonnegative(max_total_budget)
        if budget is None:
            return "Global spend-limit configuration is invalid; refusing autonomous continuation."
        if float(cost) >= budget:
            return f"Global spend limit reached (${cost:.4f}/${budget:.4f})."
    return None


def remaining_global_budget(args: argparse.Namespace, state: dict[str, Any]) -> float | None:
    total = getattr(args, "max_total_budget_usd", None)
    if total is None:
        return None
    budget = _finite_nonnegative(total)
    spent = _state_counter(state, "total_cost_usd")
    if budget is None or spent is None or state.get("usage_accounting_invalid"):
        return 0.0
    return max(0.0, budget - spent)


def effective_invocation_budget(args: argparse.Namespace, state: dict[str, Any]) -> float | None:
    per_call_raw = getattr(args, "max_budget_usd", None)
    remaining = remaining_global_budget(args, state)
    if per_call_raw is None:
        return remaining
    per_call = _finite_nonnegative(per_call_raw)
    if per_call is None:
        return 0.0
    if remaining is None:
        return per_call
    return max(0.0, min(per_call, remaining))


def update_global_usage(state: dict[str, Any], log: dict[str, Any]) -> None:
    usage = log.get("usage") if isinstance(log.get("usage"), dict) else {}
    if usage.get("_invalid_accounting"):
        _mark_invalid_accounting(state, "provider_usage", "invalid values were reported")

    fields = (
        ("total_reported_turns", usage.get("num_turns", 0)),
        ("total_cost_usd", usage.get("total_cost_usd", 0)),
        ("total_wall_seconds", log.get("wall_seconds", 0)),
    )
    for state_field, raw_increment in fields:
        current = _state_counter(state, state_field)
        increment = _finite_nonnegative(raw_increment)
        if increment is None:
            _mark_invalid_accounting(state, state_field + "_increment", raw_increment)
            continue
        if current is None:
            continue
        state[state_field] = current + increment


def transient_backoff_seconds(failures: int, base: float = 5.0, cap: float = 300.0) -> float:
    return min(cap, base * (2 ** max(0, failures - 1)))


def usage_from_result(obj: dict[str, Any] | None) -> dict[str, float]:
    if not isinstance(obj, dict):
        return {}
    usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
    out: dict[str, float] = {}
    invalid = False

    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        if key not in usage:
            continue
        value = _finite_nonnegative(usage.get(key))
        if value is None:
            invalid = True
        else:
            out[key] = value

    for source, key in (
        (obj.get("total_cost_usd"), "total_cost_usd"),
        (obj.get("num_turns"), "num_turns"),
        (obj.get("duration_ms"), "duration_ms"),
    ):
        if source is None:
            continue
        value = _finite_nonnegative(source)
        if value is None:
            invalid = True
        else:
            out[key] = value

    if invalid:
        # Numeric sentinel survives usage merging so the supervisor can fail
        # closed instead of silently undercounting malformed provider accounting.
        out["_invalid_accounting"] = 1.0
    return out
