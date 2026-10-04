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
import json
import textwrap
from pathlib import Path
from typing import Any

from planning_support import _bounded_prompt_text
from repo_runtime import compact_profile, make_runtime_agents
from settings_policy import AUTONOMY_PROFILES, permission_mode_for_profile
from state_store import load_json
from telemetry import progress_evidence

def resolve_session_settings(
    requested: str | None,
    autonomy_profile: str,
    *,
    headless: bool = False,
) -> str:
    if requested in {"compatibility", "hermetic"}:
        return requested
    if autonomy_profile == "unattended":
        return "hermetic"
    # Headless autonomous Balanced/Isolated-Full runs own their execution
    # policy. Inheriting user/project/local settings can merge denyWrite/ask
    # rules and hooks back into the worker and recreate environment blockers
    # after the harness has already authorised routine repository work.
    if headless and autonomy_profile in {"balanced", "isolated-full"}:
        return "hermetic"
    # Interactive start remains compatibility-first; Strict intentionally keeps
    # repository/user restrictions unless the operator explicitly requests
    # hermetic settings.
    return "compatibility"


def build_cycle_prompt(objective: str, state: dict[str, Any], prof: dict[str, Any], cycle: int) -> str:
    compact_state = {
        "cycle": cycle,
        "last_summary": state.get("last_summary"),
        "last_result_status": state.get("last_result_status"),
        "blocker": state.get("blocker"),
        "last_git_head": state.get("last_git_head"),
        "last_challenger_findings": state.get("last_challenger_findings"),
    }
    return textwrap.dedent(f"""
    OVERALL OBJECTIVE:
    {objective}

    This is autonomous supervisor cycle {cycle}. You are NOT starting a new unrelated task. Continue pursuing the overall objective from repository reality and the compact checkpoint below.

    COMPACT CHECKPOINT:
    {json.dumps(compact_state, separators=(',', ':'))}

    REPOSITORY PROFILE:
    {json.dumps(compact_profile(prof), separators=(',', ':'))}

    Work autonomously. First reconcile Git/repository state and relevant project instructions. Resume unfinished work before selecting new work. Do as much coherent, verified work as is sensible in this cycle. Do not ask the human routine questions. If one work item is blocked, continue independent useful work. Use independent verification selectively for material changes or before claiming the overall objective complete.

    Follow the autonomy status protocol from your system instructions exactly at the end.
    """).strip()


def progress_fingerprint(
    status: str,
    summary: str | None,
    snap: dict[str, Any],
    state: dict[str, Any] | None = None,
) -> str:
    # Claude's prose is intentionally excluded: progress is deterministic
    # repository/plan/evidence state, not whether the model rephrased a summary.
    material_obj: dict[str, Any] = {"status": status, **progress_evidence(state or {}, snap)}
    material = json.dumps(material_obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()

def claude_base_args(
    sd: Path,
    model: str | None,
    effort: str,
    permission_mode: str,
    memory_mode: str = "external",
    verifier_model: str | None = None,
    researcher_model: str | None = None,
    autonomy_profile: str = "balanced",
    settings_path: Path | None = None,
    permission_overrides: set[str] | None = None,
) -> list[str]:
    if autonomy_profile not in AUTONOMY_PROFILES:
        raise SystemExit(f"Unknown autonomy profile: {autonomy_profile}")
    settings_name = f"settings-{autonomy_profile}-{memory_mode}.json"
    resolved_permission_mode = permission_mode_for_profile(permission_mode, autonomy_profile, permission_overrides)
    selected_settings = settings_path or (sd / settings_name)
    args = [
        "claude",
        "--settings", str(selected_settings),
        "--agents", make_runtime_agents(sd, verifier_model, researcher_model),
        "--append-system-prompt-file", str(sd / "system-prompt.md"),
        "--permission-mode", resolved_permission_mode,
        "--effort", effort,
    ]
    if model:
        args += ["--model", model]
    if autonomy_profile == "unattended" or "unrestricted" in set(permission_overrides or ()):
        # Current Claude Code exposes this stronger explicit user-authority switch
        # in addition to bypassPermissions. Use both for unattended execution so
        # classifier permission prompts are not reintroduced by normal policy.
        args += ["--dangerously-skip-permissions"]
    return args

def build_goal_prompt(
    objective: str,
    state: dict[str, Any],
    prof: dict[str, Any],
    max_turns: int,
    sd: Path | None = None,
    permission_overrides: set[str] | None = None,
    permission_grants: list[dict[str, Any]] | None = None,
) -> str:
    challenger = state.get("last_challenger_findings")
    plan: dict[str, Any] = {}
    if sd is not None:
        plan = load_json(sd / "plans" / "current-plan.json", {})
    condition = (
        "The OVERALL OBJECTIVE is genuinely satisfied, relevant repository acceptance criteria are met, "
        "locally executable verification has been run and truthfully reported, and an independent verification pass "
        "has found no material unresolved defect; OR no safe useful independent work remains and the final response "
        "explicitly reports AUTONOMY_STATUS: BLOCKED with the external dependency. "
        f"Stop after at most {max_turns} worker turns if the condition cannot be established."
    )
    context = {
        "last_summary": state.get("last_summary"),
        "last_git_head": state.get("last_git_head"),
        "last_challenger_findings": challenger,
        "last_security_review_verdict": state.get("last_security_review_verdict"),
        "last_security_review_findings": state.get("last_security_review_findings"),
        "last_correctness_review_findings": state.get("last_correctness_review_findings"),
        "last_verification_findings": state.get("last_verification_findings"),
        "last_progress_protocol_errors": state.get("last_progress_protocol_errors"),
        "completion_anchor_invalidated_reason": state.get("completion_anchor_invalidated_reason"),
        "autonomy_profile": state.get("autonomy_profile", "balanced"),
        "plan_version": state.get("plan_version"),
        "plan_hash": state.get("plan_hash"),
        "plan_status": state.get("plan_status"),
        "plan_revalidation_findings": state.get("plan_revalidation_findings"),
        "approved_remediation": state.get("approved_remediation"),
        "active_permission_overrides": sorted(set(permission_overrides or ())),
        "active_permission_grants": [
            {
                "capability": grant.get("capability"),
                "scope": grant.get("scope"),
                "constraints": grant.get("constraints") if isinstance(grant.get("constraints"), dict) else {},
            }
            for grant in (permission_grants or [])
            if isinstance(grant, dict)
        ],
        "profile": compact_profile(prof),
    }
    plan_for_prompt = {
        "version": plan.get("version"),
        "plan_hash": plan.get("plan_hash"),
        "complexity": plan.get("complexity"),
        "acceptance_criteria": plan.get("acceptance_criteria", []),
        "tasks": plan.get("tasks", []),
        "assumptions": plan.get("assumptions", []),
        "risks": plan.get("risks", []),
        "plan_markdown": plan.get("plan_markdown", ""),
    }
    return textwrap.dedent(f"""
    /goal {condition}

    OVERALL OBJECTIVE:
    {objective}

    EXTERNAL COMPACT CHECKPOINT:
    {json.dumps(context, separators=(',', ':'))}

    VALIDATED DURABLE IMPLEMENTATION PLAN:
    {_bounded_prompt_text(json.dumps(plan_for_prompt, separators=(',', ':')), 220_000, pointer=str(sd / "plans" / "current-plan.json") if sd is not None else None, label="validated implementation plan")}

    Work autonomously toward the objective and the currently validated plan. Reconcile current Git/repository reality and repository-local instructions first.
    This is a headless run: do not invoke AskUserQuestion or tools that require interactive user input. If an interaction-only tool is denied, use a safe non-interactive alternative when possible; otherwise report the genuine external dependency instead of looping.
    Completing one subtask is not completion. If one item is blocked, continue independent useful work where dependency-safe.
    The plan is a controlled baseline, not a reason to ignore contradictory repository evidence. For trivial contained defects, perform root-cause analysis,
    implement the smallest coherent fix and verify impacted work. For a non-trivial local remediation, assess backward and forward whole-plan impact,
    stop before applying it, and emit AUTONOMY_PLAN_IMPACT: LOCAL plus AUTONOMY_PLAN_CHANGE so the supervisor can stress the proposal first.
    If a discovered issue or proposed fix materially changes
    architecture, schema, API/contracts, security boundaries, requirements, task dependencies, already-completed plan assumptions, or future plan steps,
    DO NOT implement that material remediation yet. Diagnose it, propose the fix and its whole-plan impact, then emit AUTONOMY_PLAN_IMPACT: MATERIAL or REQUIREMENT
    so the supervisor can re-simulate/red-team/version the plan before implementation continues.
    Emit AUTONOMY_PHASE_BOUNDARY: YES when a logical implementation phase has just completed and remaining plan assumptions should be revalidated; otherwise NO.
    If EXTERNAL COMPACT CHECKPOINT contains failed deterministic verification, correctness-review, or security-review findings, remediate every validated finding and add regression/negative-path verification before claiming completion again.
    Before claiming completion, surface the relevant verification evidence in the conversation so the /goal evaluator can judge it.
    Also emit one compact deterministic progress record near the end:
    AUTONOMY_PROGRESS: {{"completed_task_ids":["T001"],"remaining_task_ids":[],"verification":{{"command-or-check":"PASS|FAIL|UNKNOWN"}},"external_checkpoint":null}}
    Use the exact durable plan task IDs. completed_task_ids and remaining_task_ids must be duplicate-free, disjoint and together account for every
    task in the validated plan. Never mark a task complete before all of its dependencies are complete. AUTONOMY_STATUS: COMPLETE is valid only
    when remaining_task_ids is empty and every validated task ID is in completed_task_ids. Do not put free-form reasoning in this record.
    EXTERNAL COMPACT CHECKPOINT.active_permission_grants is the authoritative user-approved scope. A constrained grant authorises only the exact listed path/command/verification resource; the capability name by itself is not blanket authority. Retry the matching approved operation, but request a new grant for any different resource.
    If a genuinely required operation is blocked by a permission/restriction and the current profile is not unattended, do not loop and do not invent success. Explain the exact operation, resource, need, risk/side effects and safer alternative, then emit exactly one compact record:\n    AUTONOMY_PERMISSION_REQUEST: {{"capability":"native-permissions|outside-repository|container-host-authority|secret-read|host-repository-execution|unrestricted","operation":"...","resource":"exact absolute file path or exact command when applicable","why_needed":"...","risk":"...","safer_alternative":"...","constraints":{{"file_paths":["/exact/path"]}}}}\n    End that turn with AUTONOMY_STATUS: BLOCKED so the outer supervisor can checkpoint as AWAITING_USER_PERMISSION. Never emit this record merely for convenience; try safe alternatives first.\n    Follow the AUTONOMY_STATUS/AUTONOMY_SUMMARY protocol from the system instructions in the final response.
    """).strip()


def _same_provider_route(args: argparse.Namespace) -> bool:
    fp = getattr(args, "fallback_provider", None)
    return not fp or fp == args.provider

RUN_RESUME_FIELDS = (
    "model", "effort", "profile", "permission_mode", "memory_mode", "session_settings",
    "model_qualification", "max_cycles", "max_plan_revisions", "max_stagnant_cycles",
    "max_turns", "max_budget_usd", "cycle_timeout", "verification_timeout", "runtime_verify",
    "security_scanners", "security_scanner_timeout", "trust_repo_scripts",
    "max_transient_retries", "retry_backoff_seconds", "retry_backoff_cap_seconds",
    "max_total_turns", "max_wall_seconds",
    "max_total_budget_usd", "pause_seconds", "provider", "gateway_url", "gateway_token_env",
    "gateway_discovery", "isolate_provider_profile", "gateway_hints", "opus_model",
    "sonnet_model", "haiku_model", "subagent_model", "verifier_model", "researcher_model",
    "fallback_provider", "fallback_model", "fallback_gateway_url", "fallback_gateway_token_env",
    "fallback_gateway_discovery", "fallback_isolate_provider_profile", "fallback_gateway_hints",
    "fallback_verifier_model", "fallback_researcher_model", "challenger_policy",
    "challenger_provider", "challenger_model", "challenger_gateway_url",
    "challenger_gateway_token_env", "challenger_gateway_discovery",
    "challenger_isolate_provider_profile", "challenger_gateway_hints",
)


def _apply_resume_config(args: argparse.Namespace, state: dict[str, Any]) -> None:
    if not getattr(args, "resume_config", False):
        return
    cfg = state.get("resume_config")
    if not isinstance(cfg, dict):
        return
    for key in RUN_RESUME_FIELDS:
        if key in cfg and hasattr(args, key):
            setattr(args, key, cfg[key])


def _capture_resume_config(args: argparse.Namespace) -> dict[str, Any]:
    return {key: getattr(args, key) for key in RUN_RESUME_FIELDS if hasattr(args, key)}
