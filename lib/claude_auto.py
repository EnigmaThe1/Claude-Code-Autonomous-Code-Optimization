#!/usr/bin/env python3
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
import os
import socket
import fcntl
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Keep sibling modules importable both when claude_auto.py is executed by the
# launcher and when tests/tools load it directly via importlib from another cwd.
_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from execution import run_repository_command
from state_store import StateCorruptionError, json_dump, load_json, sha256_text
from process_runner import SupervisorInterrupted, run
from provider_config import (
    MIN_CLAUDE_VERSION,
    PROVIDER_PROFILES,
    QUALIFICATION_RANK,
    RECOMMENDED_CLAUDE_VERSION,
    _claude_version_text,
    _model_registry_name,
    _qualification_provider_values,
    claude_version_tuple,
    load_model_registry,
    provider_env,
    provider_from_args,
    qualification_is_fresh,
    qualification_record,
    qualification_route_fingerprint,
    require_supported_claude,
    safe_gateway_probe,
    save_model_qualification,
    warn_model_qualification,
)
from runtime_paths import APP, data_home, ensure_private_dir, model_registry_path, package_root, utcnow
from repo_identity import (
    SupervisorLease,
    _filesystem_birth_marker,
    _legacy_repo_identity,
    _legacy_supervisor_is_active,
    _migrate_legacy_state_if_needed,
    _unchecked_state_json,
    find_repo_root,
    repo_id,
    repo_state_dir,
    repository_identity,
)
from repo_profile import (
    BASE_PLUGINS,
    DEFERRED_PLUGINS,
    LANG_BY_SUFFIX,
    LSP_MAP,
    MANIFESTS,
    RepoProfile,
    detect_commands,
    iter_files_limited,
    iter_repo_files,
    profile_repo,
)
from repo_runtime import (
    _command_sha256,
    _ignored_digest,
    _untracked_digest,
    activate,
    compact_profile,
    git_snapshot,
    make_runtime_agents,
    plan_state_dir,
    prune_runtime_history,
)
from settings_policy import (
    AUTONOMY_PROFILES,
    DOTENV_SAMPLE_NAMES,
    _append_command_hook,
    _compat_excluded_commands,
    _discovered_dotenv_read_denies,
    _inside_container,
    isolated_full_authorized,
    make_readonly_settings,
    make_settings,
    permission_mode_for_profile,
    refuse_nested_claude_launch,
    running_inside_claude,
    validate_repository_execution_policy,
    resolve_autonomy_profile,
)
from planning_completeness import (
    validate_plan_against_scope,
    validate_plan_completeness,
    validate_scope_baseline,
)
from planning_support import (
    _assessment_prompt,
    _bounded_prompt_text,
    _git_snapshot_context_hash,
    _hash_file_streaming,
    _normalise_plan_control,
    _persist_plan_version,
    _planner_prompt,
    _scope_prompt,
    _planning_context_fingerprint,
    _validation_anchor_compatible,
    build_readonly_evidence,
    extract_source_plan_task_ids,
    validate_plan_graph,
    validate_progress_checkpoint,
    validate_source_plan_coverage,
)
from protocols import (
    extract_result_json,
    has_explicit_status,
    parse_challenger,
    parse_code_review_gate,
    parse_json_protocol,
    parse_permission_request,
    parse_plan_impact,
    parse_progress_checkpoint,
    parse_qualification,
    parse_runtime_verify_gate,
    parse_security_review_gate,
    parse_status,
    parse_task_result,
)
from control_plane import (
    ControlPlaneRetryableError,
    _merge_usage,
    _repo_snapshot_unchanged,
    _completion_source_unchanged,
    _run_control_model as _control_run_model,
    _tracked_source_unchanged,
    run_readonly_plan_agent as _control_readonly_plan_agent,
)
from review_gates import (
    _runnable_application_signal,
    run_challenger,
    run_correctness_review_gate,
    run_runtime_verify_gate,
    run_security_review_gate,
)
from verification import (
    _baseline_integrity_error,
    _receipt_signature,
    _run_verification_command as _run_verification_command_impl,
    _scanner_verdict,
    _security_scanner_commands,
    _verification_commands,
    ensure_verification_baseline as _ensure_verification_baseline_impl,
    run_security_scanners,
    run_verification_gate as _run_verification_gate_impl,
)
from service_manager import (
    _service_unit_name as _service_unit_name_impl,
    _service_unit_path as _service_unit_path_impl,
    _service_unit_text as _service_unit_text_impl,
    _systemd_escape_arg as _systemd_escape_arg_impl,
    service_action as _service_action_impl,
)
from supervisor_support import (
    RUN_RESUME_FIELDS,
    _apply_resume_config,
    _capture_resume_config,
    _same_provider_route,
    build_cycle_prompt,
    build_goal_prompt,
    claude_base_args,
    progress_fingerprint,
    resolve_session_settings,
)
from permission_escalation import (
    active_permission_grants,
    active_permission_overrides,
    approve_request,
    build_permission_request,
    consume_once_grants,
    deny_request,
    permission_action,
    prompt_permission_scope,
    record_permission_request,
    render_permission_request,
    request_already_authorized,
    reset_permission_epoch,
    revoke_grants,
    verification_command_authorized,
    worker_permission_grants,
    worker_permission_overrides,
)
from state_commands import (
    cmd_activate,
    cmd_inspect,
    cmd_setup,
    reset_state,
    show_metrics,
    show_model_registry,
    show_status,
)
from operator_tools import (
    _settings_audit,
    doctor,
    gateway_doctor,
    gateway_install,
    gateway_list,
    gateway_service,
    install_plugins,
    plugin_recommendations,
    user_layer_action,
    user_layer_status,
)
from model_qualification import (
    _qualification_command,
    _qualification_stage,
    _qualify_model_unlocked,
    qualification_challenge,
    qualify_model,
)
from cli_schema import (
    add_provider_args,
    build_parser as _build_parser,
    show_profiles,
)
from authority_set import build_authority_snapshot, governance_action
from task_authority import (
    TaskAuthorityError,
    active_task_prompt_context,
    ensure_supervisor_task_activation,
    task_action,
)
from task_workspace import (
    TaskWorkspaceError,
    begin_task_workspace,
    ensure_supervisor_task_workspace,
    load_active_task_workspace,
    task_workspace_status,
)
from task_acceptance import (
    TaskAcceptanceError,
    abort_task_workspace,
    accept_verified_task,
    cleanup_accepted_task_workspace,
    reopen_task_candidate_for_repair,
    seal_task_candidate,
    verify_task_candidate_deterministic,
    verify_task_candidate_independent,
)
from shadow_validation import shadow_action
from migration import MigrationError, migrate_legacy_planning_repair
from state_adoption import (
    StateAdoptionError,
    adopt_active_task_primary_wip,
    begin_adopted_active_task,
    begin_adopted_task_reattestation,
    import_legacy_state_claims,
    load_current_adoption,
)
from state_migration import (
    StateMigrationError,
    migrate_state_on_disk,
    preflight_state_schema,
)
from operator_authority import require_top_level_operator
from environment_policy import apply_resume_environment, capture_resume_environment
from git_trust import git_trust_action
from promotion_policy import promotion_policy_action
from planning_repair import (
    abort_planning_repair,
    begin_planning_repair,
    load_active_repair,
    load_planning_repair_policy,
    planning_repair_action,
    planning_repair_session_context,
    promote_planning_repair,
    refresh_planning_repair_base,
    run_planning_repair_architect,
    verify_planning_repair,
)
from toolchain_preflight import probe_toolchain
from workspace_recovery import cleanup_untracked_action, promote_ff_action
from profile_switch import (
    finish_profile_switch,
    pending_profile_switch,
    process_start_token,
    profile_action,
)
from session_adoption import (
    SessionAdoptionError,
    block_session_adoption,
    capture_adopted_session_id,
    finish_session_adoption,
    load_session_adoption,
    mark_session_adoption_launched,
    prepare_session_adoption,
)
from telemetry import (
    circuit_breaker_reason,
    classify_claude_outcome,
    compact_run_log,
    effective_invocation_budget,
    progress_evidence,
    read_runtime_events,
    redact_text,
    remaining_global_budget,
    runtime_event_offset,
    runtime_event_path,
    transient_backoff_seconds,
    update_global_usage,
    usage_from_result,
)

VERSION = (package_root() / "VERSION").read_text(encoding="utf-8").strip()

def _run_control_model(
    *,
    cmd: list[str],
    root: Path,
    sd: Path,
    env: dict[str, str],
    timeout: int | None,
    max_transient_retries: int = 4,
) -> tuple[subprocess.CompletedProcess[str], str, str | None, dict[str, Any] | None, str, str, dict[str, float], list[dict[str, Any]], float]:
    """Compatibility wrapper preserving claude_auto.run injection semantics."""
    return _control_run_model(
        cmd=cmd,
        root=root,
        sd=sd,
        env=env,
        timeout=timeout,
        max_transient_retries=max_transient_retries,
        runner=run,
    )


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
) -> tuple[str, dict[str, Any]]:
    """Compatibility wrapper preserving claude_auto.run injection semantics."""
    return _control_readonly_plan_agent(
        root=root,
        sd=sd,
        prompt=prompt,
        env=env,
        provider_detail=provider_detail,
        model=model,
        timeout=timeout,
        max_turns=max_turns,
        verify_repo=verify_repo,
        max_budget_usd=max_budget_usd,
        runner=run,
    )



def ensure_model_qualification(
    args: argparse.Namespace,
    root: Path,
    model: str | None,
    *,
    role: str,
    required_level: str,
    provider_prefix: str = "",
    caller_holds_lease: bool = False,
) -> dict[str, Any] | None:
    provider, values = _qualification_provider_values(args, provider_prefix)
    if not model and provider != "native":
        raise SystemExit(f"Autonomous provider {provider!r} requires an explicit --model so Claude Auto can qualify the route.")
    route_name=_model_registry_name(model)
    expected_fp=qualification_route_fingerprint(provider, model, values)
    rec=qualification_record(provider, model)
    current=str(rec.get("qualification_level") or "FAILED") if rec else "FAILED"
    required_rank=QUALIFICATION_RANK[required_level]
    if qualification_is_fresh(rec, expected_fp) and rec.get("compatible") and QUALIFICATION_RANK.get(current,0)>=required_rank:
        return None
    policy=str(getattr(args,"model_qualification","auto") or "auto")
    level_arg={"HARNESS_SMOKE_READONLY":"readonly","AUTO_EXECUTION":"auto","AUTONOMOUS_FULL":"full"}[required_level]
    if policy=="off":
        print(f"WARNING: {role} route {provider}:{route_name} is unqualified/stale ({current}); continuing only because --model-qualification off was explicitly selected.",file=sys.stderr); return None
    if policy=="required":
        raise SystemExit(f"{role} route {provider}:{route_name} requires fresh {required_level} qualification.")
    print(f"Preflight: automatically qualifying {role} route {provider}:{route_name} to {required_level}...")
    qargs=argparse.Namespace(repo=str(root),model=model,effort=getattr(args,"effort","medium"),max_turns=max(24,min(int(getattr(args,"max_turns",24) or 24),32)),level=level_arg,timeout=int(getattr(args,"cycle_timeout",0) or 0),provider=provider,gateway_url=values["gateway_url"],gateway_token_env=values["gateway_token_env"],gateway_discovery=values["gateway_discovery"],isolate_provider_profile=values["isolate_provider_profile"],gateway_hints=values["gateway_hints"],max_budget_usd=getattr(args,"max_budget_usd",None),max_total_budget_usd=getattr(args,"_qualification_budget_remaining",None),session_settings=getattr(args,"session_settings","compatibility"))
    # Autonomous run already owns the repository writer lease.  Re-entering
    # qualify_model() there would self-contend on the same durable fence, so the
    # run path uses the unlocked implementation explicitly.  Standalone callers
    # keep the public locked entrypoint.
    rc = (
        _qualify_model_unlocked(qargs, root, repo_state_dir(root))
        if caller_holds_lease
        else qualify_model(qargs)
    )
    rec=qualification_record(provider, model); achieved=str(rec.get("qualification_level") or "FAILED") if rec else "FAILED"
    if rec and getattr(args,"_qualification_budget_remaining",None) is not None:
        spent=sum(float((st.get("usage") or {}).get("total_cost_usd",0) or 0) for st in rec.get("stages",[]) if isinstance(st,dict))
        args._qualification_budget_remaining=max(0.0,float(args._qualification_budget_remaining)-spent)
    if rc!=0 or not qualification_is_fresh(rec,expected_fp) or not rec.get("compatible") or QUALIFICATION_RANK.get(achieved,0)<required_rank:
        raise SystemExit(f"{role} route {provider}:{route_name} did not achieve fresh required {required_level} qualification (achieved {achieved}).")
    return rec


def _run_plan_assessment(
    *,
    kind: str,
    stage: str,
    root: Path,
    sd: Path,
    objective: str,
    plan: dict[str, Any],
    state: dict[str, Any],
    env: dict[str, str],
    provider_detail: dict[str, Any],
    model: str | None,
    timeout: int | None,
    main_summary: str | None = None,
    evidence_path: Path | None = None,
    max_budget_usd: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    text, meta = run_readonly_plan_agent(
        root=root, sd=sd,
        prompt=_assessment_prompt(
            kind=kind,
            stage=stage,
            objective=objective,
            plan=plan,
            state=state,
            main_summary=main_summary,
            evidence_path=evidence_path,
            plan_pointer=sd / "plans" / "current-plan.json",
        ),
        env=env, provider_detail=provider_detail, model=model, timeout=timeout, max_budget_usd=max_budget_usd,
    )
    prefix = (
        "PLAN_SIMULATION"
        if kind == "simulation"
        else ("PLAN_REDTEAM" if kind == "redteam" else "PLAN_VERIFIER")
    )
    obj = parse_json_protocol(text, prefix)
    if not obj:
        return {"verdict": "BLOCKED", "scope": "REQUIREMENT", "summary": f"{prefix} protocol was missing or invalid.", "findings": []}, meta
    verdict = str(obj.get("verdict", "BLOCKED")).upper()
    scope = str(obj.get("scope", "PLAN")).upper()
    if verdict not in {"PASS", "REVISE", "BLOCKED"}:
        verdict = "BLOCKED"
    if scope not in {"PLAN", "IMPLEMENTATION", "REQUIREMENT"}:
        scope = "PLAN"
    return {
        "verdict": verdict,
        "scope": scope,
        "summary": str(obj.get("summary", ""))[:2000],
        "findings": obj.get("findings") if isinstance(obj.get("findings"), list) else [],
        "scenarios": obj.get("scenarios") if isinstance(obj.get("scenarios"), list) else [],
    }, meta


def _account_control_meta(args: argparse.Namespace, state: dict[str, Any], sd: Path, meta: dict[str, Any]) -> str | None:
    update_global_usage(state, {"usage": meta.get("usage") or {}, "wall_seconds": float(meta.get("wall_seconds", 0) or 0)})
    json_dump(sd / "state.json", state)
    return circuit_breaker_reason(args, state)



def _plan_validation_context_hash(
    *,
    root: Path,
    prof: dict[str, Any],
    objective: str,
    source_hash: str,
) -> str:
    identity = repository_identity(root)
    material = {
        "schema": 1,
        "repo_id": identity.get("key"),
        "worktree_key": (identity.get("material") or {}).get("worktree_key"),
        "objective_hash": sha256_text(objective),
        "source_hash": source_hash,
        "profile": compact_profile(prof),
        "planning_context_fingerprint": _planning_context_fingerprint(root, prof),
        "supervisor_version": VERSION,
    }
    return sha256_text(json.dumps(material, sort_keys=True, separators=(",", ":"), default=str))


def ensure_plan_validated(
    *,
    args: argparse.Namespace,
    root: Path,
    sd: Path,
    prof: dict[str, Any],
    state: dict[str, Any],
    objective: str,
    source_kind: str,
    source_ref: str | None,
    source_text: str | None,
    source_hash: str,
    env: dict[str, str],
    provider_detail: dict[str, Any],
    force_reason: str | None = None,
) -> tuple[dict[str, Any] | None, int]:
    gate_before = git_snapshot(root)
    validation_context_hash = _plan_validation_context_hash(
        root=root,
        prof=prof,
        objective=objective,
        source_hash=source_hash,
    )
    existing = load_json(plan_state_dir(sd) / "current-plan.json", {})
    if existing and existing.get("objective") != objective and not force_reason:
        # A previous objective's plan is history, not a candidate for an unrelated
        # objective. Keep version numbering monotonic but do not seed new planning
        # with stale execution structure.
        existing = {}
    if (
        not force_reason
        and state.get("plan_status") == "VALIDATED"
        and state.get("plan_source_hash") == source_hash
        and existing.get("objective") == objective
        and state.get("plan_validation_context_hash") == validation_context_hash
        and _validation_anchor_compatible(root, state)
    ):
        return existing, 0

    review_findings: list[dict[str, Any]] = []
    current_plan = existing if existing else None
    candidate_for_prompt = source_text
    if source_text is not None:
        source_copy = plan_state_dir(sd) / f"input-source-{source_hash[:16]}.txt"
        if not source_copy.exists() or sha256_text(source_copy.read_text(errors="replace")) != sha256_text(source_text):
            source_copy.write_text(source_text)
            try: source_copy.chmod(0o600)
            except OSError: pass
        candidate_for_prompt = _bounded_prompt_text(source_text, 180_000, pointer=str(source_copy), label="candidate source")
    revision_reason = force_reason or "initial plan construction/validation"
    max_revisions = max(
        1,
        int(getattr(args, "max_plan_revisions", 5) or 5),
    )
    timeout = args.cycle_timeout if getattr(args, "cycle_timeout", 0) else None

    scope_text, scope_meta = run_readonly_plan_agent(
        root=root,
        sd=sd,
        prompt=_scope_prompt(
            objective=objective,
            prof=prof,
            source_kind=source_kind,
            source_ref=source_ref,
            candidate_text=candidate_for_prompt,
        ),
        env=env,
        provider_detail=provider_detail,
        model=getattr(args, "verifier_model", None) or args.model,
        timeout=timeout,
        max_budget_usd=effective_invocation_budget(args, state),
    )
    limit_reason = _account_control_meta(args, state, sd, scope_meta)
    if limit_reason:
        state.update({"status": "LIMIT_REACHED", "blocker": limit_reason})
        json_dump(sd / "state.json", state)
        return None, 4
    raw_scope = parse_json_protocol(scope_text, "PLAN_SCOPE")
    if not raw_scope:
        state.update({
            "plan_status": "BLOCKED",
            "status": "BLOCKED",
            "blocker": (
                "Independent scope/complexity protocol was missing or invalid."
            ),
        })
        json_dump(sd / "state.json", state)
        return None, 6
    scope_baseline = {
        "verdict": str(raw_scope.get("verdict", "BLOCKED")).upper(),
        "complexity": str(raw_scope.get("complexity", "")).lower(),
        "complexity_evidence": (
            raw_scope.get("complexity_evidence")
            if isinstance(raw_scope.get("complexity_evidence"), dict)
            else {}
        ),
        "requirements": (
            raw_scope.get("requirements")
            if isinstance(raw_scope.get("requirements"), list)
            else []
        ),
        "mandatory_concerns": (
            raw_scope.get("mandatory_concerns")
            if isinstance(raw_scope.get("mandatory_concerns"), list)
            else []
        ),
        "research_questions": (
            raw_scope.get("research_questions")
            if isinstance(raw_scope.get("research_questions"), list)
            else []
        ),
        "summary": str(raw_scope.get("summary", ""))[:2000],
        "blockers": (
            raw_scope.get("blockers")
            if isinstance(raw_scope.get("blockers"), list)
            else []
        ),
    }
    scope_errors = validate_scope_baseline(scope_baseline)
    if scope_baseline["verdict"] == "BLOCKED" or scope_errors:
        blocker = (
            scope_baseline.get("summary")
            or "; ".join(scope_errors)
            or "Independent scope analysis blocked."
        )
        state.update({
            "plan_status": "BLOCKED",
            "status": "BLOCKED",
            "blocker": blocker[:4000],
            "plan_scope_errors": scope_errors or None,
        })
        json_dump(sd / "state.json", state)
        return None, 3
    pdir = plan_state_dir(sd)
    scope_semantic = json.dumps(
        scope_baseline,
        sort_keys=True,
        separators=(",", ":"),
    )
    scope_sha256 = sha256_text(scope_semantic)
    json_dump(
        pdir / "scope-baseline.json",
        {
            "scope": scope_baseline,
            "scope_sha256": scope_sha256,
            "meta": scope_meta,
        },
    )
    state["plan_scope_sha256"] = scope_sha256
    json_dump(sd / "state.json", state)

    for attempt in range(1, max_revisions + 1):
        planner_text, planner_meta = run_readonly_plan_agent(
            root=root, sd=sd,
            prompt=_planner_prompt(
                objective=objective, prof=prof, source_kind=source_kind, source_ref=source_ref,
                candidate_text=candidate_for_prompt, current_plan=current_plan, revision_reason=revision_reason,
                review_findings=review_findings,
                scope_baseline=scope_baseline,
                state_dir=sd,
            ),
            env=env, provider_detail=provider_detail, model=args.model, timeout=timeout,
            max_budget_usd=effective_invocation_budget(args, state),
        )
        limit_reason = _account_control_meta(args, state, sd, planner_meta)
        if limit_reason:
            state.update({"status": "LIMIT_REACHED", "blocker": limit_reason})
            json_dump(sd / "state.json", state)
            return None, 4
        raw_plan = parse_json_protocol(planner_text, "PLAN_CONTROL")
        if not raw_plan:
            state.update({"plan_status": "BLOCKED", "status": "BLOCKED", "blocker": "Planner protocol was missing or invalid."})
            json_dump(sd / "state.json", state)
            return None, 6
        candidate = _normalise_plan_control(raw_plan, objective)
        candidate["scope_baseline"] = scope_baseline
        candidate["scope_baseline_sha256"] = scope_sha256
        plan_errors = validate_plan_graph(candidate)
        plan_errors.extend(validate_plan_completeness(candidate))
        plan_errors.extend(
            validate_plan_against_scope(candidate, scope_baseline)
        )
        if source_text:
            plan_errors.extend(
                validate_source_plan_coverage(source_text, candidate)
            )
        if candidate["verdict"] == "BLOCKED":
            blocker = (
                candidate.get("summary")
                or "; ".join(
                    str(x) for x in candidate.get("blockers", [])
                )
                or "Plan could not be made implementation-ready."
            )
            state.update({
                "plan_status": "BLOCKED",
                "status": "BLOCKED",
                "blocker": blocker,
            })
            json_dump(sd / "state.json", state)
            return None, 3
        if plan_errors:
            if attempt < max_revisions:
                review_findings = [{
                    "verdict": "REVISE",
                    "scope": "PLAN",
                    "summary": (
                        "Deterministic RC5 planning-completeness gate failed."
                    ),
                    "findings": plan_errors,
                }]
                revision_reason = (
                    "Deterministic planning completeness/traceability "
                    "validation found gaps; revise the whole plan before any "
                    "implementation authority."
                )
                current_plan = candidate
                continue
            blocker = (
                "Deterministic RC5 planning completeness validation failed: "
                + "; ".join(plan_errors)[:4000]
            )
            state.update({
                "plan_status": "BLOCKED",
                "status": "BLOCKED",
                "blocker": blocker,
                "plan_validation_errors": plan_errors,
            })
            json_dump(sd / "state.json", state)
            return None, 3

        candidate = _persist_plan_version(
            sd, state, candidate, source_kind=source_kind, source_ref=source_ref,
            source_hash=source_hash, reason=revision_reason,
        )
        version = candidate["version"]
        pdir = plan_state_dir(sd)
        json_dump(pdir / f"planner-v{version:04d}.json", {"meta": planner_meta, "attempt": attempt})

        simulation, sim_meta = _run_plan_assessment(
            kind="simulation", stage="preflight", root=root, sd=sd, objective=objective, plan=candidate,
            state=state, env=env, provider_detail=provider_detail, model=args.model, timeout=timeout,
            max_budget_usd=effective_invocation_budget(args, state),
        )
        limit_reason = _account_control_meta(args, state, sd, sim_meta)
        if limit_reason:
            state.update({"status": "LIMIT_REACHED", "blocker": limit_reason})
            json_dump(sd / "state.json", state)
            return None, 4
        redteam, red_meta = _run_plan_assessment(
            kind="redteam", stage="preflight", root=root, sd=sd,
            objective=objective, plan=candidate, state=state, env=env,
            provider_detail=provider_detail, model=args.model,
            timeout=timeout,
            max_budget_usd=effective_invocation_budget(args, state),
        )
        limit_reason = _account_control_meta(args, state, sd, red_meta)
        if limit_reason:
            state.update({
                "status": "LIMIT_REACHED",
                "blocker": limit_reason,
            })
            json_dump(sd / "state.json", state)
            return None, 4
        verifier, verifier_meta = _run_plan_assessment(
            kind="verifier", stage="preflight", root=root, sd=sd,
            objective=objective, plan=candidate, state=state, env=env,
            provider_detail=provider_detail,
            model=args.verifier_model or args.model,
            timeout=timeout,
            max_budget_usd=effective_invocation_budget(args, state),
        )
        limit_reason = _account_control_meta(
            args, state, sd, verifier_meta
        )
        if limit_reason:
            state.update({
                "status": "LIMIT_REACHED",
                "blocker": limit_reason,
            })
            json_dump(sd / "state.json", state)
            return None, 4
        json_dump(
            pdir / f"simulation-v{version:04d}.json",
            {"assessment": simulation, "meta": sim_meta},
        )
        json_dump(
            pdir / f"redteam-v{version:04d}.json",
            {"assessment": redteam, "meta": red_meta},
        )
        json_dump(
            pdir / f"verifier-v{version:04d}.json",
            {"assessment": verifier, "meta": verifier_meta},
        )

        if all(
            item["verdict"] == "PASS"
            for item in (simulation, redteam, verifier)
        ):
            gate_after = git_snapshot(root)
            if not _repo_snapshot_unchanged(gate_before, gate_after):
                state.update({"plan_status": "BLOCKED", "status": "BLOCKED", "blocker": "Read-only plan-validation gate changed repository state."})
                json_dump(sd / "state.json", state)
                return None, 6
            state.update({
                "plan_status": "VALIDATED",
                "plan_validated_at": utcnow(),
                "plan_validation_summary": {
                    "simulation": simulation.get("summary"),
                    "redteam": redteam.get("summary"),
                    "verifier": verifier.get("summary"),
                },
                "plan_validation_context_hash": validation_context_hash,
                "plan_validation_git_head": gate_after.get("head"),
                "plan_validation_git_snapshot_hash": _git_snapshot_context_hash(gate_after),
                "last_git_snapshot_hash": _git_snapshot_context_hash(gate_after),
            })
            json_dump(sd / "state.json", state)
            return candidate, 0

        blocked = next(
            (
                x
                for x in (simulation, redteam, verifier)
                if x["verdict"] == "BLOCKED"
            ),
            None,
        )
        if blocked:
            state.update({"plan_status": "BLOCKED", "status": "BLOCKED", "blocker": blocked.get("summary") or "Plan validation blocked."})
            json_dump(sd / "state.json", state)
            return None, 3

        review_findings = [simulation, redteam, verifier]
        revision_reason = (
            "Independent simulation/red-team/Plan Verifier found material "
            "issues; revise the whole plan and revalidate."
        )
        current_plan = candidate

    state.update({
        "plan_status": "VALIDATION_FAILED", "status": "BLOCKED",
        "blocker": (
            "Plan did not pass deterministic completeness plus "
            "simulation/red-team/Plan Verifier after "
            f"{max_revisions} revision attempts."
        ),
    })
    json_dump(sd / "state.json", state)
    return None, 6


def run_plan_checkpoint_review(
    *,
    args: argparse.Namespace,
    root: Path,
    sd: Path,
    state: dict[str, Any],
    objective: str,
    plan: dict[str, Any],
    env: dict[str, str],
    provider_detail: dict[str, Any],
    stage: str,
    main_summary: str | None,
) -> tuple[str, list[dict[str, Any]]]:
    timeout = args.cycle_timeout if args.cycle_timeout else None
    before = git_snapshot(root)
    evidence_path = build_readonly_evidence(root, sd)
    assessments: list[dict[str, Any]] = []
    for kind in ("simulation", "redteam"):
        result, meta = _run_plan_assessment(
            kind=kind, stage=stage, root=root, sd=sd, objective=objective, plan=plan, state=state,
            env=env, provider_detail=provider_detail, model=args.model, timeout=timeout, main_summary=main_summary,
            evidence_path=evidence_path, max_budget_usd=effective_invocation_budget(args, state),
        )
        assessments.append(result)
        limit_reason = _account_control_meta(args, state, sd, meta)
        json_dump(plan_state_dir(sd) / f"{stage}-{kind}-cycle-{int(state.get('cycle', 0)):04d}.json", {"assessment": result, "meta": meta})
        if limit_reason:
            state.update({"status": "LIMIT_REACHED", "blocker": limit_reason})
            json_dump(sd / "state.json", state)
            return "LIMIT_REACHED", assessments
    after = git_snapshot(root)
    if not _repo_snapshot_unchanged(before, after):
        return "BLOCKED", [{"verdict": "BLOCKED", "scope": "IMPLEMENTATION", "summary": "Read-only checkpoint review changed repository state.", "findings": []}]
    if any(x["verdict"] == "BLOCKED" for x in assessments):
        return "BLOCKED", assessments
    if all(x["verdict"] == "PASS" for x in assessments):
        return "PASS", assessments
    if any(x["scope"] == "PLAN" for x in assessments if x["verdict"] == "REVISE"):
        return "REVISE_PLAN", assessments
    return "FIX_IMPLEMENTATION", assessments

def _run_one_goal(
    *,
    root: Path,
    sd: Path,
    args: argparse.Namespace,
    prompt: str,
    env: dict[str, str],
    provider_detail: dict[str, Any],
    model: str | None,
    verifier_model: str | None,
    researcher_model: str | None,
    cycle: int,
    settings_path_override: Path | None = None,
    force_hermetic_settings: bool = False,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any], dict[str, Any], dict[str, Any], str, str | None, dict[str, Any] | None]:
    overrides = set(getattr(args, "_permission_overrides", []) or [])
    worker_overrides = worker_permission_overrides(overrides)
    worker_grants = worker_permission_grants(
        list(getattr(args, "_permission_grants", []) or [])
    )
    effective_settings_path = settings_path_override
    if worker_overrides and effective_settings_path is None:
        prof = load_json(sd / "profile.json", {})
        effective_settings_path = sd / f"settings-effective-{args.profile}-{args.memory_mode}.json"
        json_dump(
            effective_settings_path,
            make_settings(
                sd, args.memory_mode, args.profile, prof, worker_overrides,
                permission_grants=worker_grants,
            ),
        )
    cmd = claude_base_args(
        sd, model, args.effort, args.permission_mode, args.memory_mode,
        verifier_model=verifier_model, researcher_model=researcher_model,
        autonomy_profile=args.profile,
        settings_path=effective_settings_path,
        permission_overrides=worker_overrides,
    )
    setting_sources = "" if (
        force_hermetic_settings
        or getattr(args, "session_settings", "compatibility") == "hermetic"
        or bool(worker_overrides)
    ) else "user,project,local"
    cmd += [
        "--setting-sources", setting_sources,
        "-p", "--output-format", "json", "--max-turns", str(args.max_turns),
        "--permission-prompts", "none",
        "--disallowed-tools", "AskUserQuestion",
        "--exclude-dynamic-system-prompt-sections",
    ]
    invocation_budget = getattr(args, "_effective_max_budget_usd", None)
    if invocation_budget is None:
        invocation_budget = args.max_budget_usd
    if invocation_budget is not None:
        cmd += ["--max-budget-usd", str(invocation_budget)]
    # Native Claude fallback chains preserve the same session. Use them whenever
    # the fallback route stays on the same provider/gateway.
    if args.fallback_model and _same_provider_route(args):
        cmd += ["--fallback-model", args.fallback_model]
    cmd.append(prompt)

    before = git_snapshot(root)
    started = utcnow()
    started_monotonic = time.monotonic()
    event_offset = runtime_event_offset(sd)
    run_env = env.copy()
    # /goal is a prompt-based Stop hook. Align its continuation cap with the
    # worker-turn policy so Claude does not hit the upstream default cap of 8
    # while the outer supervisor still expects useful work to continue.
    run_env["CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"] = str(max(8, min(int(args.max_turns), 128)))
    run_env["CLAUDE_AUTO_HEADLESS"] = "1"
    cp = run(cmd, cwd=root, timeout=args.cycle_timeout if args.cycle_timeout else None, env=run_env)
    wall_seconds = max(0.0, time.monotonic() - started_monotonic)
    after = git_snapshot(root)
    result_text, session_id, raw_obj = extract_result_json(cp.stdout)
    runtime_events = read_runtime_events(sd, event_offset)
    if not session_id:
        session_id = next((str(x.get("session_id")) for x in reversed(runtime_events) if x.get("session_id")), None)
    outcome, outcome_reason = classify_claude_outcome(cp, raw_obj, result_text, runtime_events)
    log = compact_run_log(
        cycle=cycle,
        started_at=started,
        finished_at=utcnow(),
        provider=provider_detail,
        model=model,
        returncode=cp.returncode,
        result_text=result_text,
        session_id=session_id,
        raw_obj=raw_obj,
        stderr=cp.stderr,
        git_before=before,
        git_after=after,
        env=run_env,
        retain_transcripts=args.retain_transcripts,
        stdout=cp.stdout,
    )
    log["runtime_events"] = runtime_events
    log["outcome"] = outcome
    log["outcome_reason"] = redact_text(outcome_reason, run_env, 1200)
    log["wall_seconds"] = wall_seconds
    log["stop_hook_block_cap"] = int(run_env["CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"])
    return cp, before, after, log, result_text, session_id, raw_obj



def _run_verification_command(
    root: Path,
    category: str,
    command: str,
    timeout: int,
    *,
    trust_repo_scripts: bool = False,
    unrestricted_host: bool = False,
) -> dict[str, Any]:
    return _run_verification_command_impl(
        root,
        category,
        command,
        timeout,
        trust_repo_scripts=trust_repo_scripts,
        unrestricted_host=unrestricted_host,
    )


def ensure_verification_baseline(
    root: Path,
    sd: Path,
    prof: dict[str, Any],
    state: dict[str, Any],
    args: argparse.Namespace,
    objective_hash: str,
) -> list[dict[str, Any]]:
    return _ensure_verification_baseline_impl(
        root,
        sd,
        prof,
        state,
        args,
        objective_hash,
        snapshot_hash_func=_git_snapshot_context_hash,
        verification_runner=_run_verification_command,
    )


def run_verification_gate(
    root: Path,
    sd: Path,
    prof: dict[str, Any],
    state: dict[str, Any],
    args: argparse.Namespace,
) -> tuple[str, str, list[dict[str, Any]]]:
    return _run_verification_gate_impl(
        root,
        sd,
        prof,
        state,
        args,
        verification_runner=_run_verification_command,
    )



_COMPLETION_EVIDENCE_FIELDS = (
    "last_verification_receipts",
    "last_verification_findings",
    "last_runtime_verify_verdict",
    "last_runtime_verify_summary",
    "last_runtime_verify_findings",
    "last_correctness_review_verdict",
    "last_correctness_review_findings",
    "last_security_scanner_receipts",
    "last_security_scanner_summary",
    "last_security_review_verdict",
    "last_security_review_summary",
    "last_security_review_findings",
    "last_challenger_verdict",
    "last_challenger_findings",
)


def _completion_anchor_hash(snapshot: dict[str, Any]) -> str:
    material = {
        key: snapshot.get(key)
        for key in (
            "head", "worktree_diff_sha256", "index_diff_sha256", "untracked_sha256",
            "verification_contract_sha256",
        )
    }
    return sha256_text(json.dumps(material, sort_keys=True, separators=(",", ":"), default=str))


def _invalidate_completion_evidence(state: dict[str, Any], reason: str) -> None:
    for field in _COMPLETION_EVIDENCE_FIELDS:
        state[field] = None
    state["completion_anchor"] = None
    state["completion_anchor_hash"] = None
    state["completion_anchor_verified_at"] = None
    state["completion_anchor_invalidated_reason"] = reason


def _apply_pending_profile_switch(
    args: argparse.Namespace,
    state: dict[str, Any],
    sd: Path,
    *,
    source_hash: str | None,
    headless: bool,
) -> bool:
    """Apply one human-requested profile transition at a safe supervisor boundary.

    The request lives outside state.json so a second operator process can queue it
    without contending on the active supervisor lease.  Validation is performed
    before mutating the active profile; failure leaves the previous profile intact.
    """
    request = pending_profile_switch(sd)
    if not request:
        return False
    bound_token = str(request.get("supervisor_start_token") or "")
    current_token = str(state.get("supervisor_start_token") or "")
    if bound_token and bound_token != current_token:
        previous = str(getattr(args, "profile", None) or state.get("autonomy_profile") or "balanced")
        finish_profile_switch(
            sd, request, outcome="stale", previous_profile=previous,
            active_profile=previous, reason="request belongs to a different supervisor instance",
        )
        state["last_profile_switch"] = {
            "request_id": request.get("request_id"),
            "outcome": "stale",
            "previous_profile": previous,
            "active_profile": previous,
            "reason": "request belongs to a different supervisor instance",
            "at": utcnow(),
        }
        json_dump(sd / "state.json", state)
        print(f"Stale profile switch request rejected; {previous} remains active.")
        return False

    target = str(request.get("target_profile") or "").strip().lower()
    previous = str(getattr(args, "profile", None) or state.get("autonomy_profile") or "balanced")

    if target == previous:
        state["last_profile_switch"] = {
            "request_id": request.get("request_id"),
            "outcome": "no-op",
            "previous_profile": previous,
            "active_profile": previous,
            "at": utcnow(),
        }
        json_dump(sd / "state.json", state)
        finish_profile_switch(
            sd, request, outcome="no-op", previous_profile=previous, active_profile=previous,
            reason="requested profile already active",
        )
        print(f"Profile switch: {previous} already active (no-op).")
        return False

    explicit_session_settings = bool(state.get("session_settings_explicit", False))
    previous_session_settings = getattr(args, "session_settings", None)
    candidate_session_settings = (
        previous_session_settings
        if explicit_session_settings
        else resolve_session_settings(None, target, headless=headless)
    )

    try:
        overrides = set(active_permission_overrides(state, source_hash))
        if target == "strict":
            # A prior blanket bypass grant must not silently defeat an explicit
            # operator request to tighten the active run to Strict.
            overrides.discard("unrestricted")
            overrides.discard("native-permissions")
        permission_mode_for_profile(args.permission_mode, target, overrides)
        validate_repository_execution_policy(
            target,
            bool(getattr(args, "trust_repo_scripts", False)) or target == "unattended",
        )
    except (SystemExit, ValueError) as exc:
        reason = str(exc)
        state["last_profile_switch"] = {
            "request_id": request.get("request_id"),
            "outcome": "rejected",
            "previous_profile": previous,
            "active_profile": previous,
            "reason": reason[:1600],
            "at": utcnow(),
        }
        json_dump(sd / "state.json", state)
        finish_profile_switch(
            sd, request, outcome="rejected", previous_profile=previous,
            active_profile=previous, reason=reason,
        )
        print(f"Profile switch rejected safely; {previous} remains active: {reason}")
        return False

    if target == "strict":
        revoke_grants(state, capability="unrestricted")
        revoke_grants(state, capability="native-permissions")
    if target == "unattended":
        reset_permission_epoch(
            state,
            new_objective_hash=source_hash,
            reason="profile switched to explicit unattended authority",
            unattended=True,
        )

    args.profile = target
    args.session_settings = candidate_session_settings
    args._permission_grants = active_permission_grants(state, source_hash)
    args._permission_overrides = sorted(active_permission_overrides(state, source_hash))
    state["autonomy_profile"] = target
    state["session_settings"] = candidate_session_settings
    state["resume_config"] = _capture_resume_config(args)
    state["last_profile_switch"] = {
        "request_id": request.get("request_id"),
        "outcome": "applied",
        "previous_profile": previous,
        "active_profile": target,
        "origin": request.get("origin"),
        "at": utcnow(),
    }
    json_dump(sd / "state.json", state)
    finish_profile_switch(
        sd, request, outcome="applied", previous_profile=previous, active_profile=target,
    )
    print(f"Profile switched safely: {previous} -> {target}. Objective and durable state preserved.")
    return True


def _source_identity_hash(
    source_kind: str,
    source_ref: str | None,
    objective: str,
    source_text: str | None,
) -> str:
    return sha256_text(json.dumps({
        "kind": source_kind,
        "ref": source_ref,
        "objective": objective,
        "content": source_text,
    }, sort_keys=True, separators=(",", ":")))


def _repository_plan_policy_error(
    root: Path,
    *,
    source_kind: str,
    source_ref: str | None,
) -> str | None:
    # P1 understands AuthoritySets but deliberately does not yet execute
    # multi-file planning sources. Preserve the RC3 single-source path exactly;
    # fail closed for a multi-file authority until P2/P5 provide the source/task
    # normalisation and multi-file repair machinery.
    snapshot = build_authority_snapshot(root)
    if not snapshot:
        return None
    source_members = sorted({
        member["path"]
        for authority_set in snapshot.get("sets", [])
        for member in authority_set.get("members", [])
        if member.get("role") == "source"
    })
    all_members = [
        member
        for authority_set in snapshot.get("sets", [])
        for member in authority_set.get("members", [])
    ]
    if len(source_members) == 1 and len(all_members) == 1:
        canonical = source_members[0]
        if source_kind != "supplied-plan":
            return (
                "Repository-owned planning is configured, so autonomous execution must "
                f"use the canonical plan via --plan {canonical!r}; refusing a second planning authority."
            )
        supplied = Path(str(source_ref or "")).as_posix()
        if supplied != Path(canonical).as_posix():
            return (
                "Repository-owned planning is configured for "
                f"{canonical!r}, but this run supplied {supplied!r}; refusing divergent planning authorities."
            )
        return None
    return (
        "Repository governance resolves a multi-file/multi-domain AuthoritySet. "
        "RC4-P1 can inspect and protect this authority read-only, but autonomous "
        "execution adoption remains blocked until the TaskSource/multi-file planning phases are installed."
    )


def _repair_args(
    args: argparse.Namespace,
    *,
    reason: str,
    verifier: bool = False,
) -> argparse.Namespace:
    values = dict(vars(args))
    values["reason"] = reason
    values["timeout"] = int(getattr(args, "cycle_timeout", 0) or 0)
    values["max_turns"] = min(
        40 if not verifier else 35,
        max(1, int(getattr(args, "max_turns", 40) or 40)),
    )
    if verifier:
        values["model"] = getattr(args, "verifier_model", None) or getattr(args, "model", None)
    return argparse.Namespace(**values)


def _revalidate_authoritative_plan(
    *,
    args: argparse.Namespace,
    root: Path,
    sd: Path,
    prof: dict[str, Any],
    state: dict[str, Any],
    objective: str,
    source_kind: str,
    source_ref: str | None,
    source_text: str | None,
    source_hash: str,
    env: dict[str, str],
    provider_detail: dict[str, Any],
    reason: str,
) -> tuple[dict[str, Any] | None, int, str | None, str, dict[str, Any]]:
    """Repair the configured planning authority, then rebuild the executable plan.

    With no repository-owned planning policy, this is the existing external-plan
    path.  With a configured canonical plan, the repository file is repaired in
    a dedicated worktree, independently verified at the exact candidate SHA and
    promoted through the protected broker before Claude Auto rebuilds its
    external executable task graph from the newly-promoted canonical source.
    """
    policy = load_planning_repair_policy(root)
    if not policy:
        plan, rc = ensure_plan_validated(
            args=args, root=root, sd=sd, prof=prof, state=state, objective=objective,
            source_kind=source_kind, source_ref=source_ref, source_text=source_text,
            source_hash=source_hash, env=env, provider_detail=provider_detail,
            force_reason=reason,
        )
        return plan, rc, source_text, source_hash, load_json(sd / "state.json", state)

    policy_error = _repository_plan_policy_error(
        root, source_kind=source_kind, source_ref=source_ref,
    )
    if policy_error:
        state.update({"status": "BLOCKED", "plan_status": "BLOCKED", "blocker": policy_error})
        json_dump(sd / "state.json", state)
        return None, 3, source_text, source_hash, state

    max_attempts = max(1, int(getattr(args, "max_plan_revisions", 3) or 3))
    repair_reason = reason
    last_finding = ""

    for attempt in range(1, max_attempts + 1):
        active = load_active_repair(root)
        if not active:
            active = begin_planning_repair(root, reason=repair_reason)

        # Product work may have advanced while the repair was paused.  Refresh is
        # idempotent, recognises already-absorbed bases and invalidates a verifier
        # result whenever rebasing changes the candidate SHA.
        refresh = refresh_planning_repair_base(root)
        active = load_active_repair(root)

        candidate = str(active.get("candidate_sha") or "")
        verified = str(active.get("verified_sha") or "")

        if not candidate:
            architect_result = run_planning_repair_architect(
                root,
                _repair_args(args, reason=repair_reason, verifier=False),
            )
            active = load_active_repair(root)
            candidate = str(active.get("candidate_sha") or "")
            if architect_result.get("status") != "candidate" or not candidate:
                classification = str(architect_result.get("classification") or "SEMANTIC_DECISION")
                summary = str(architect_result.get("summary") or "Planning repair architect blocked.")
                state.update({
                    "status": "BLOCKED",
                    "plan_status": "BLOCKED",
                    "blocker": (
                        "Repository-owned planning repair requires an unresolved semantic/user decision: "
                        if classification == "SEMANTIC_DECISION"
                        else "Repository-owned planning repair was blocked: "
                    ) + summary,
                    "repository_planning_repair": architect_result,
                })
                json_dump(sd / "state.json", state)
                return None, 3, source_text, source_hash, state
            verified = ""

        if verified != candidate:
            verify_result = verify_planning_repair(
                root,
                _repair_args(args, reason=repair_reason, verifier=True),
            )
            if verify_result.get("status") != "verified":
                findings = verify_result.get("findings")
                rendered = json.dumps(findings or [], separators=(",", ":"))[:3000]
                last_finding = str(verify_result.get("summary") or "candidate rejected")
                abort_planning_repair(root)
                repair_reason = (
                    f"{reason}\nIndependent Planning Verifier rejected attempt {attempt}: "
                    f"{last_finding}. Findings: {rendered}"
                )
                continue

        # Reconcile once more after verification.  If the product base advanced,
        # candidate identity changes and exact-SHA verification must be repeated.
        before_promote = load_active_repair(root)
        verified_before = str(before_promote.get("verified_sha") or "")
        refresh = refresh_planning_repair_base(root)
        active = load_active_repair(root)
        candidate = str(active.get("candidate_sha") or "")
        verified = str(active.get("verified_sha") or "")
        if not candidate or verified != candidate or candidate != verified_before:
            repair_reason = (
                f"{reason}\nProduct base advanced during planning verification; "
                "the refreshed exact candidate must be independently verified again."
            )
            continue

        try:
            promotion = promote_planning_repair(root)
        except ValueError as exc:
            # A concurrent product/remote advance is recoverable by refreshing the
            # dedicated repair branch and re-verifying the resulting exact SHA.
            message = str(exc)
            if any(token in message for token in (
                "remote branch moved unexpectedly",
                "local HEAD must equal the expected remote base",
                "target commit is not a descendant",
            )):
                repair_reason = f"{reason}\nPromotion race detected and reconciled: {message}"
                try:
                    refresh_planning_repair_base(root)
                except ValueError:
                    pass
                continue
            raise

        canonical = str(policy["canonical_plan"])
        canonical_path = (root / canonical).resolve()
        source_text = canonical_path.read_text()
        source_hash = _source_identity_hash(
            source_kind, source_ref, objective, source_text,
        )
        state = load_json(sd / "state.json", state)

        # Plan-content identity changed.  Permission and verification evidence
        # tied to the prior source generation must not silently survive.
        if reset_permission_epoch(
            state,
            new_objective_hash=source_hash,
            reason="repository-owned canonical plan was independently repaired and promoted",
            unattended=getattr(args, "profile", None) == "unattended",
        ):
            args._permission_grants = active_permission_grants(state, source_hash)
            args._permission_overrides = sorted(active_permission_overrides(state, source_hash))
        state["verification_baseline"] = None
        state["verification_baseline_objective_hash"] = None
        state["repository_planning_repair"] = {
            "attempt": attempt,
            "promotion": promotion,
            "source_hash": source_hash,
            "completed_at": utcnow(),
        }
        json_dump(sd / "state.json", state)

        plan, rc = ensure_plan_validated(
            args=args, root=root, sd=sd, prof=prof, state=state, objective=objective,
            source_kind=source_kind, source_ref=source_ref, source_text=source_text,
            source_hash=source_hash, env=env, provider_detail=provider_detail,
            force_reason=reason,
        )
        state = load_json(sd / "state.json", state)
        if rc != 0 or not plan:
            return plan, rc, source_text, source_hash, state

        # The canonical-plan commit changed repository identity/evidence.  Rebuild
        # the deterministic baseline before normal implementation resumes.
        baseline_receipts = ensure_verification_baseline(
            root, sd, prof, state, args, source_hash,
        )
        state = load_json(sd / "state.json", state)
        baseline_integrity = _baseline_integrity_error(baseline_receipts)
        if baseline_integrity:
            state.update({
                "status": "BLOCKED",
                "last_result_status": "BLOCKED",
                "blocker": baseline_integrity,
                "last_verification_findings": [
                    rec for rec in baseline_receipts
                    if isinstance(rec, dict) and not rec.get("tracked_source_unchanged", True)
                ],
            })
            json_dump(sd / "state.json", state)
            return None, 3, source_text, source_hash, state
        return plan, 0, source_text, source_hash, state

    state = load_json(sd / "state.json", state)
    state.update({
        "status": "BLOCKED",
        "plan_status": "BLOCKED",
        "blocker": (
            f"Repository-owned canonical plan did not obtain a promotable independently VERIFIED "
            f"candidate after {max_attempts} attempts."
            + (f" Last verifier finding: {last_finding}" if last_finding else "")
        ),
    })
    json_dump(sd / "state.json", state)
    return None, 6, source_text, source_hash, state



def _p4_supervisor_completion_result(plan: dict[str, Any]) -> str:
    task_rows = plan.get("tasks") if isinstance(plan.get("tasks"), list) else []
    completed = sorted(
        {
            str(row.get("id")).strip()
            for row in task_rows
            if isinstance(row, dict)
            and isinstance(row.get("id"), str)
            and str(row.get("id")).strip()
        }
    )
    progress = {
        "completed_task_ids": completed,
        "remaining_task_ids": [],
        "verification": {"repository_tasks": "PASS"},
        "external_checkpoint": None,
    }
    return (
        "AUTONOMY_STATUS: COMPLETE\n"
        "AUTONOMY_SUMMARY: All repository-owned TaskSpecs are durably accepted; "
        "begin supervisor-owned final whole-system qualification.\n"
        "AUTONOMY_PLAN_IMPACT: NONE\n"
        "AUTONOMY_PHASE_BOUNDARY: NO\n"
        "AUTONOMY_PROGRESS: "
        + json.dumps(progress, sort_keys=True, separators=(",", ":"))
    )


def _p4_prepare_supervisor_task(
    coordinator_root: Path,
    sd: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Reconcile interruption checkpoints, then return the current P4 worker state."""
    for _ in range(8):
        activation = ensure_supervisor_task_workspace(
            coordinator_root,
            state_dir=sd,
        )
        if activation.get("status") != "CHECKPOINT":
            return activation
        checkpoint = _p4_advance_acceptance_checkpoint(
            coordinator_root,
            sd,
            args,
        )
        status = str(checkpoint.get("status") or "")
        if status == "BLOCKED":
            return {
                "status": "BLOCKED",
                "reason": checkpoint.get("reason")
                or "P4 acceptance checkpoint is blocked",
                "checkpoint": checkpoint,
            }
        if status in {
            "ACTIVE",
            "REPAIR",
            "NEXT_READY",
            "TASKS_COMPLETE",
            "CLEAN",
        }:
            continue
        return {
            "status": "BLOCKED",
            "reason": (
                "P4 acceptance checkpoint returned an unsupported state: "
                + status
            ),
            "checkpoint": checkpoint,
        }
    return {
        "status": "BLOCKED",
        "reason": "P4 supervisor task preparation exceeded bounded reconciliation steps",
    }


def _p4_worker_execution_context(
    coordinator_root: Path,
    sd: Path,
    args: argparse.Namespace,
    activation: dict[str, Any],
) -> tuple[Path, dict[str, Any] | None, Path | None]:
    if activation.get("status") != "ACTIVE":
        return coordinator_root, None, None

    workspace = load_active_task_workspace(
        coordinator_root,
        state_dir=sd,
    )
    if workspace is None:
        raise TaskWorkspaceError(
            "P4 supervisor reported ACTIVE without a TaskWorkspaceRecord"
        )
    if workspace["lifecycle_state"] != "ACTIVE":
        raise TaskWorkspaceError(
            "P4 worker execution requires ACTIVE lifecycle state, found "
            f"{workspace['lifecycle_state']!r}"
        )
    if activation.get("task_id") != workspace["task_id"]:
        raise TaskWorkspaceError(
            "P4 supervisor activation and TaskWorkspaceRecord task IDs disagree"
        )

    task_root = Path(workspace["task_worktree"]).expanduser().resolve()
    try:
        task_context = active_task_prompt_context(
            task_root,
            state_dir=sd,
            authority_root=coordinator_root,
        )
    except TaskAuthorityError as exc:
        raise TaskWorkspaceError(str(exc)) from exc
    if not isinstance(task_context, dict):
        raise TaskWorkspaceError(
            "P4 active task has no current worker prompt context"
        )
    task_context = {
        **task_context,
        "task_worktree": str(task_root),
        "task_workspace_sha256": workspace["task_workspace_sha256"],
        "prior_verifier_findings": list(
            workspace.get("repair_findings") or []
        )[:100],
    }

    task_profile = asdict(profile_repo(task_root))
    task_profile.update({
        "repo_root": str(task_root),
        "task_workspace": True,
        "authority_root": str(coordinator_root),
        "semantic_protected_paths": [
            str(coordinator_root.resolve()),
            str(sd.resolve()),
        ],
    })
    settings_dir = ensure_private_dir(sd / "tasks" / "worker-settings")
    settings_path = (
        settings_dir
        / (
            f"{workspace['task_workspace_sha256'][:20]}-"
            f"{args.profile}-{args.memory_mode}.json"
        )
    )
    json_dump(
        settings_path,
        make_settings(
            sd,
            args.memory_mode,
            args.profile,
            task_profile,
            worker_permission_overrides(
                set(getattr(args, "_permission_overrides", []) or [])
            ),
            permission_grants=worker_permission_grants(
                list(getattr(args, "_permission_grants", []) or [])
            ),
        ),
    )
    return task_root, task_context, settings_path


def _p4_advance_acceptance_checkpoint(
    coordinator_root: Path,
    sd: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Advance package-owned P4 states while caller holds SupervisorLease."""
    for _ in range(12):
        workspace = load_active_task_workspace(
            coordinator_root,
            state_dir=sd,
        )
        if workspace is None:
            return {"status": "CLEAN"}

        lifecycle = str(workspace["lifecycle_state"])
        if lifecycle == "ACTIVE":
            return {
                "status": "ACTIVE",
                "task_id": workspace["task_id"],
                "task_worktree": workspace["task_worktree"],
            }

        if lifecycle == "CANDIDATE":
            deterministic = verify_task_candidate_deterministic(
                coordinator_root,
                timeout=int(
                    getattr(args, "verification_timeout", 900) or 900
                ),
                trust_repo_scripts=(
                    bool(getattr(args, "trust_repo_scripts", False))
                    or args.profile == "unattended"
                ),
                unrestricted_host=(
                    args.profile == "unattended"
                    or "unrestricted"
                    in set(
                        getattr(args, "_permission_overrides", []) or []
                    )
                ),
                state_dir=sd,
                acquire_lease=False,
            )
            verdict = str(deterministic.get("status") or "").upper()
            if verdict == "PASS":
                continue
            if verdict == "FAIL":
                reopened = reopen_task_candidate_for_repair(
                    coordinator_root,
                    findings=list(deterministic.get("findings") or []),
                    state_dir=sd,
                    acquire_lease=False,
                )
                return {
                    "status": "REPAIR",
                    "task_id": reopened["task_id"],
                    "findings": deterministic.get("findings") or [],
                }
            return {
                "status": "BLOCKED",
                "task_id": workspace["task_id"],
                "reason": (
                    "deterministic task verification could not establish a "
                    "safe exact-SHA result"
                ),
                "findings": deterministic.get("findings") or [],
            }

        if lifecycle == "VERIFYING":
            deterministic_verdict = str(
                workspace.get("deterministic_verification_verdict") or ""
            ).upper()
            if deterministic_verdict == "FAIL":
                reopened = reopen_task_candidate_for_repair(
                    coordinator_root,
                    findings=list(
                        workspace.get(
                            "deterministic_verification_findings"
                        )
                        or []
                    ),
                    state_dir=sd,
                    acquire_lease=False,
                )
                return {
                    "status": "REPAIR",
                    "task_id": reopened["task_id"],
                    "findings": reopened.get("repair_findings") or [],
                }
            if deterministic_verdict and deterministic_verdict != "PASS":
                return {
                    "status": "BLOCKED",
                    "task_id": workspace["task_id"],
                    "reason": (
                        "deterministic task verification is "
                        + deterministic_verdict
                    ),
                    "findings": workspace.get(
                        "deterministic_verification_findings"
                    )
                    or [],
                }
            if deterministic_verdict != "PASS":
                deterministic = verify_task_candidate_deterministic(
                    coordinator_root,
                    timeout=int(
                        getattr(args, "verification_timeout", 900) or 900
                    ),
                    trust_repo_scripts=(
                        bool(getattr(args, "trust_repo_scripts", False))
                        or args.profile == "unattended"
                    ),
                    unrestricted_host=(
                        args.profile == "unattended"
                        or "unrestricted"
                        in set(
                            getattr(args, "_permission_overrides", []) or []
                        )
                    ),
                    state_dir=sd,
                    acquire_lease=False,
                )
                if deterministic.get("status") != "PASS":
                    continue

            independent = verify_task_candidate_independent(
                coordinator_root,
                args,
                state_dir=sd,
                acquire_lease=False,
            )
            verdict = str(independent.get("status") or "").upper()
            if verdict == "VERIFIED":
                continue
            if verdict == "REJECTED":
                return {
                    "status": "REPAIR",
                    "task_id": independent.get("task_id"),
                    "findings": independent.get("findings") or [],
                }
            return {
                "status": "BLOCKED",
                "task_id": independent.get("task_id"),
                "reason": (
                    independent.get("summary")
                    or "independent Task Verifier was blocked"
                ),
                "findings": independent.get("findings") or [],
            }

        if lifecycle in {"VERIFIED_PENDING_PROMOTION", "PROMOTING"}:
            accepted = accept_verified_task(
                coordinator_root,
                state_dir=sd,
                acquire_lease=False,
            )
            status = str(accepted.get("status") or "")
            if status == "ACCEPTED_PENDING_CLEANUP":
                continue
            if status == "STALE_BASE":
                return {
                    "status": "BLOCKED",
                    "task_id": accepted.get("task_id"),
                    "reason": "verified task candidate has a stale product base",
                    "details": accepted,
                }
            return {
                "status": "BLOCKED",
                "task_id": accepted.get("task_id"),
                "reason": (
                    "verified task acceptance did not reach a cleanup checkpoint"
                ),
                "details": accepted,
            }

        if lifecycle == "ACCEPTED_PENDING_CLEANUP":
            cleaned = cleanup_accepted_task_workspace(
                coordinator_root,
                state_dir=sd,
                acquire_lease=False,
            )
            scheduler = str(cleaned.get("scheduler_status") or "")
            if scheduler == "NEXT_READY":
                return {
                    "status": "NEXT_READY",
                    **cleaned,
                }
            if scheduler == "COMPLETE":
                return {
                    "status": "TASKS_COMPLETE",
                    **cleaned,
                }
            return {
                "status": "BLOCKED",
                "reason": (
                    "post-acceptance task scheduler has unresolved blockers"
                ),
                **cleaned,
            }

        if lifecycle == "BLOCKED":
            return {
                "status": "BLOCKED",
                "task_id": workspace["task_id"],
                "reason": (
                    "active P4 task workspace is durably blocked and requires "
                    "reconciliation"
                ),
                "findings": (
                    workspace.get("independent_verification_findings")
                    or workspace.get("deterministic_verification_findings")
                    or workspace.get("guard_violations")
                    or []
                ),
            }

        return {
            "status": "BLOCKED",
            "task_id": workspace["task_id"],
            "reason": (
                "P4 task workspace requires explicit reconciliation from "
                f"lifecycle state {lifecycle}"
            ),
        }

    return {
        "status": "BLOCKED",
        "reason": "P4 acceptance checkpoint exceeded bounded reconciliation steps",
    }


def _do_run_goal_unlocked(args: argparse.Namespace) -> int:
    refuse_nested_claude_launch()
    root = find_repo_root(args.repo)
    sd = repo_state_dir(root)
    prof = load_json(sd / "profile.json", {})
    state = load_json(sd / "state.json", {})
    requested_session_settings = getattr(args, "session_settings", None)
    session_settings_explicit = (
        bool(state.get("session_settings_explicit", False))
        if getattr(args, "resume_config", False)
        else requested_session_settings is not None
    )
    if getattr(args, "resume_config", False):
        apply_resume_environment(state.get("resume_environment"))
    explicit_profile = args.profile
    _apply_resume_config(args, state)
    resumed_profile = args.profile if getattr(args, "resume_config", False) else None
    args.profile = resolve_autonomy_profile(
        explicit_profile,
        state.get("autonomy_profile"),
        resume_config=bool(getattr(args, "resume_config", False)),
        resumed_profile=resumed_profile,
    )
    args.session_settings = resolve_session_settings(
        getattr(args, "session_settings", None),
        args.profile,
        headless=True,
    )

    # Re-evaluate the environment after resume restoration so the planner/worker
    # sees missing prerequisites as bootstrap work rather than mysterious command
    # failures. This is diagnostic; it never installs or mutates the repository.
    environment_toolchain = probe_toolchain(root, prof)
    prof = dict(prof)
    prof["toolchain_status"] = environment_toolchain
    state["environment_toolchain"] = environment_toolchain

    def resolve_input_path(raw: str) -> tuple[Path, str]:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            candidate = (root / p).resolve()
            p = candidate if candidate.exists() else p.resolve()
        else:
            p = p.resolve()
        if not p.is_file():
            raise SystemExit(f"Input file not found: {p}")
        try:
            ref = p.relative_to(root).as_posix()
        except ValueError:
            ref = str(p)
        return p, ref

    objective = args.objective
    source_kind = "generated"
    source_ref: str | None = None
    source_text: str | None = None

    if args.objective_file:
        objective_path, objective_ref = resolve_input_path(args.objective_file)
        objective_text = objective_path.read_text()
        objective = objective or objective_text
        source_kind = "objective-file"
        source_ref = objective_ref
        source_text = objective_text

    if getattr(args, "plan_file", None):
        plan_path, plan_ref = resolve_input_path(args.plan_file)
        plan_text = plan_path.read_text()
        source_kind = "supplied-plan"
        source_ref = plan_ref
        source_text = plan_text
        if not objective:
            objective = f"Execute the supplied implementation plan {plan_ref} completely while satisfying its stated objective and acceptance criteria."

    if not objective:
        objective = state.get("objective")
        source_kind = state.get("plan_source_kind") or "generated"
        source_ref = state.get("plan_source_ref")
        # On resume, re-read a supplied source when it still exists so edits force revalidation.
        if source_ref and source_kind in {"supplied-plan", "objective-file"}:
            p = Path(source_ref)
            if not p.is_absolute():
                p = root / p
            if p.is_file():
                source_text = p.read_text()
    if not objective:
        raise SystemExit("An objective is required on the first run: --objective TEXT, --objective-file FILE, or --plan FILE")

    source_hash = _source_identity_hash(source_kind, source_ref, objective, source_text)

    planning_policy_error = _repository_plan_policy_error(
        root, source_kind=source_kind, source_ref=source_ref,
    )
    if planning_policy_error:
        state.update({
            "status": "BLOCKED",
            "plan_status": "BLOCKED",
            "blocker": planning_policy_error,
            "objective": objective,
        })
        json_dump(sd / "state.json", state)
        print(planning_policy_error, file=sys.stderr)
        return 3

    if reset_permission_epoch(
        state,
        new_objective_hash=source_hash,
        reason="objective or supplied source changed",
        unattended=args.profile == "unattended",
    ):
        json_dump(sd / "state.json", state)
    args._permission_grants = active_permission_grants(state, source_hash)
    args._permission_overrides = sorted(active_permission_overrides(state, source_hash))

    def resolve_permission_request(request: dict[str, Any]) -> str:
        """Resolve immediately on a foreground TTY or checkpoint for later.

        A denial terminates the current run, but a later explicit rerun may ask
        again so the operator can deliberately change a previous decision.
        """
        if record_permission_request(state, request):
            json_dump(sd / "state.json", state)
        choice = prompt_permission_scope(request)
        if choice in {"once", "run", "repository"}:
            grant = approve_request(state, request, choice)
            args._permission_grants = active_permission_grants(state, source_hash)
            args._permission_overrides = sorted(active_permission_overrides(state, source_hash))
            json_dump(sd / "state.json", state)
            print(
                f"Permission approved ({choice}) for {grant.get('capability')}; "
                "resuming autonomous work."
            )
            return "approved"
        if choice == "deny":
            deny_request(state, request, "Denied in foreground permission prompt.")
            json_dump(sd / "state.json", state)
            return "denied"

        state.update({
            "status": "AWAITING_USER_PERMISSION",
            "last_result_status": "AWAITING_USER_PERMISSION",
            "blocker": (
                f"User permission is required for {request.get('capability')} "
                f"(request {request.get('id')})."
            ),
            "pending_permission_request": request,
            "updated_at": utcnow(),
        })
        json_dump(sd / "state.json", state)
        if not sys.stdin.isatty():
            print(render_permission_request(request))
        return "pending"

    # Validate the selected autonomy posture before changing durable state or starting a provider.
    permission_mode_for_profile(args.permission_mode, args.profile, set(args._permission_overrides))
    validate_repository_execution_policy(
        args.profile,
        bool(getattr(args, "trust_repo_scripts", False)) or args.profile == "unattended",
    )
    if args.subagent_model and (args.verifier_model or args.researcher_model):
        raise SystemExit("--subagent-model globally overrides subagents; do not combine it with --verifier-model/--researcher-model.")
    if args.challenger_policy != "off" and not args.challenger_model:
        raise SystemExit("--challenger-policy requires --challenger-model.")

    # Bound automatic qualification by the same global objective spend envelope.
    # A new objective starts from the full global allowance; a resume gets only the
    # unspent remainder already recorded in durable state.
    max_total_for_qualification = getattr(args, "max_total_budget_usd", None)
    if max_total_for_qualification is not None:
        already = 0.0 if state.get("objective") not in (None, objective) else float(state.get("total_cost_usd",0) or 0)
        args._qualification_budget_remaining = max(0.0, float(max_total_for_qualification) - already)
    else:
        args._qualification_budget_remaining = None

    # Prove explicit autonomous model lanes before any mutating worker starts.
    # Qualification is automatic by default and runs read-only against the target
    # repo plus mutation/goal/subagent checks only in a disposable scratch repo.
    qualification_runs: list[dict[str, Any]] = []
    qrec = ensure_model_qualification(args, root, args.model, role="main", required_level="AUTONOMOUS_FULL", caller_holds_lease=True)
    if qrec: qualification_runs.append(qrec)
    if args.fallback_model:
        qrec = ensure_model_qualification(args, root, args.fallback_model, role="fallback", required_level="AUTONOMOUS_FULL", provider_prefix="fallback_", caller_holds_lease=True)
        if qrec: qualification_runs.append(qrec)
    if args.verifier_model:
        qrec = ensure_model_qualification(args, root, args.verifier_model, role="verifier", required_level="HARNESS_SMOKE_READONLY", caller_holds_lease=True)
        if qrec: qualification_runs.append(qrec)
    if args.researcher_model:
        qrec = ensure_model_qualification(args, root, args.researcher_model, role="researcher", required_level="HARNESS_SMOKE_READONLY", caller_holds_lease=True)
        if qrec: qualification_runs.append(qrec)
    if args.challenger_model and args.challenger_policy != "off":
        qrec = ensure_model_qualification(args, root, args.challenger_model, role="challenger", required_level="HARNESS_SMOKE_READONLY", provider_prefix="challenger_", caller_holds_lease=True)
        if qrec: qualification_runs.append(qrec)

    main_env, main_provider = provider_from_args(args)
    challenger_enabled = bool(args.challenger_model and args.challenger_policy != "off")

    # A new objective starts a new progress/stagnation epoch. Do not let stale
    # challenger findings or a previous objective's fingerprint stop unrelated work.
    if state.get("objective") not in (None, objective):
        state["last_progress_fingerprint"] = None
        state["stagnant_cycles"] = 0
        state["last_challenger_verdict"] = None
        state["last_challenger_findings"] = None
        state["last_security_review_verdict"] = None
        state["last_security_review_findings"] = None
        state["last_security_review_summary"] = None
        state["last_correctness_review_verdict"] = None
        state["last_correctness_review_findings"] = None
        state["verification_baseline"] = None
        state["verification_baseline_objective_hash"] = None
        state["last_verification_receipts"] = None
        state["last_result_status"] = None
        state["blocker"] = None
        state["plan_status"] = None
        state["plan_revalidation_findings"] = None
        state.pop("plan_validation_context_hash", None)
        state.pop("plan_validation_git_head", None)
        state.pop("plan_validation_git_snapshot_hash", None)
        state["total_reported_turns"] = 0.0
        state["total_cost_usd"] = 0.0
        state["total_wall_seconds"] = 0.0
        state["transient_failures"] = 0
        state["objective_started_at"] = utcnow()

    for qrec in qualification_runs:
        for stage in qrec.get("stages", []) if isinstance(qrec, dict) else []:
            if isinstance(stage, dict):
                update_global_usage(state, {"usage": stage.get("usage") or {}, "wall_seconds": float(stage.get("wall_seconds", 0) or 0)})
    state["qualification_runs_this_objective"] = len(qualification_runs)

    limit_reason = circuit_breaker_reason(args, state)
    if limit_reason:
        state.update({"status":"LIMIT_REACHED","blocker":limit_reason,"objective":objective})
        json_dump(sd / "state.json", state)
        return 4

    state.update({
        "objective": objective,
        "status": "PLANNING",
        "blocker": None,
        "engine": "goal",
        "autonomy_profile": args.profile,
        "session_settings_explicit": session_settings_explicit,
        "session_settings": args.session_settings,
        "main_provider": main_provider,
        "main_model": args.model,
        "resume_config": _capture_resume_config(args),
        "resume_environment": (
            state.get("resume_environment")
            if getattr(args, "resume_config", False) and isinstance(state.get("resume_environment"), dict)
            else capture_resume_environment(root)
        ),
        "environment_toolchain": environment_toolchain,
    })
    json_dump(sd / "state.json", state)

    plan, plan_rc = ensure_plan_validated(
        args=args, root=root, sd=sd, prof=prof, state=state, objective=objective,
        source_kind=source_kind, source_ref=source_ref, source_text=source_text, source_hash=source_hash,
        env=main_env, provider_detail=main_provider,
    )
    if plan_rc != 0 or not plan:
        return plan_rc or 6
    state = load_json(sd / "state.json", state)
    state.update({"status": "RUNNING", "blocker": None, "plan_revalidation_findings": None})
    json_dump(sd / "state.json", state)
    baseline_receipts = ensure_verification_baseline(root, sd, prof, state, args, source_hash)
    state = load_json(sd / "state.json", state)
    baseline_integrity = _baseline_integrity_error(baseline_receipts)
    if baseline_integrity:
        state.update({
            "status": "BLOCKED",
            "last_result_status": "BLOCKED",
            "blocker": baseline_integrity,
            "last_verification_findings": [
                rec for rec in baseline_receipts
                if isinstance(rec, dict) and not rec.get("tracked_source_unchanged", True)
            ],
            "updated_at": utcnow(),
        })
        json_dump(sd / "state.json", state)
        return 3

    # P4 repository-task authority is selected by the outer supervisor and
    # executed only in a package-owned task worktree. The caller already holds
    # SupervisorLease, so reconciliation must not acquire a nested lease.
    try:
        task_activation = _p4_prepare_supervisor_task(root, sd, args)
    except (TaskWorkspaceError, TaskAcceptanceError) as exc:
        state = load_json(sd / "state.json", state)
        state.update({
            "status": "BLOCKED",
            "last_result_status": "BLOCKED",
            "blocker": f"P4 task workspace activation blocked: {exc}",
            "updated_at": utcnow(),
        })
        json_dump(sd / "state.json", state)
        print(state["blocker"], file=sys.stderr)
        return 3
    if task_activation.get("status") == "BLOCKED":
        state = load_json(sd / "state.json", state)
        state.update({
            "status": "BLOCKED",
            "last_result_status": "BLOCKED",
            "blocker": str(
                task_activation.get("reason")
                or "P4 task workspace preparation is blocked"
            )[:2000],
            "updated_at": utcnow(),
        })
        json_dump(sd / "state.json", state)
        print(state["blocker"], file=sys.stderr)
        return 3

    print(f"Repository: {root}")
    print(f"State:      {sd}")
    print("Engine:     native Claude /goal (headless)")
    print(f"Profile:    {args.profile}")
    print(f"Plan:       v{int(plan.get('version', 0)):04d} VALIDATED ({plan.get('complexity', 'standard')})")
    print(f"Provider:   {main_provider['provider']}" + (f" @ {main_provider['base_url']}" if main_provider.get("base_url") else ""))
    if task_activation.get("status") == "ACTIVE":
        reuse = "resumed" if task_activation.get("reused") else "selected"
        print(
            f"Task:       {task_activation.get('task_id')} "
            f"({reuse}; envelope {str(task_activation.get('execution_envelope_sha256'))[:12]})"
        )
    rounds_text = "unlimited (until COMPLETE/BLOCKED/circuit-breaker)" if args.max_cycles <= 0 else str(args.max_cycles)
    print(f"Max goal rounds: {rounds_text}; max worker turns/round: {args.max_turns}")
    if args.max_budget_usd is not None:
        print(f"Budget/round: ${args.max_budget_usd:.2f}")
    if args.max_total_budget_usd is not None:
        print(f"Global spend circuit breaker: ${args.max_total_budget_usd:.2f}")
    if args.max_total_turns > 0:
        print(f"Global reported-turn circuit breaker: {args.max_total_turns}")
    if args.max_wall_seconds > 0:
        print(f"Global wall-time circuit breaker: {args.max_wall_seconds}s")

    previous_fp = state.get("last_progress_fingerprint")
    stagnant = int(state.get("stagnant_cycles", 0) or 0)
    rounds_this_invocation = 0

    while args.max_cycles <= 0 or rounds_this_invocation < args.max_cycles:
        rounds_this_invocation += 1
        state = load_json(sd / "state.json", state)
        _apply_pending_profile_switch(
            args, state, sd, source_hash=source_hash, headless=True,
        )
        state = load_json(sd / "state.json", state)
        args._permission_grants = active_permission_grants(state, source_hash)
        args._permission_overrides = sorted(active_permission_overrides(state, source_hash))
        limit_reason = circuit_breaker_reason(args, state)
        if limit_reason:
            state.update({"status": "LIMIT_REACHED", "blocker": limit_reason, "updated_at": utcnow()})
            json_dump(sd / "state.json", state)
            print(limit_reason)
            return 4
        cycle = int(state.get("cycle", 0)) + 1
        try:
            cycle_task_activation = _p4_prepare_supervisor_task(
                root,
                sd,
                args,
            )
            if cycle_task_activation.get("status") == "BLOCKED":
                raise TaskWorkspaceError(
                    str(
                        cycle_task_activation.get("reason")
                        or "P4 task workspace preparation is blocked"
                    )
                )
            worker_root, task_context, task_settings = (
                _p4_worker_execution_context(
                    root,
                    sd,
                    args,
                    cycle_task_activation,
                )
            )
        except (TaskWorkspaceError, TaskAcceptanceError) as exc:
            state.update({
                "status": "BLOCKED",
                "last_result_status": "BLOCKED",
                "blocker": f"P4 task workspace revalidation blocked: {exc}",
                "updated_at": utcnow(),
            })
            json_dump(sd / "state.json", state)
            print(state["blocker"], file=sys.stderr)
            return 3

        prompt = build_goal_prompt(
            objective,
            state,
            prof,
            args.max_turns,
            sd,
            permission_overrides=worker_permission_overrides(set(args._permission_overrides)),
            permission_grants=worker_permission_grants(args._permission_grants),
            task_context=task_context,
        )
        args._effective_max_budget_usd = effective_invocation_budget(args, state)
        if cycle_task_activation.get("status") == "COMPLETE":
            before = git_snapshot(root)
            after = git_snapshot(root)
            result_text = _p4_supervisor_completion_result(plan)
            raw_obj = {
                "result": result_text,
                "session_id": None,
                "usage": {},
                "num_turns": 0,
                "total_cost_usd": 0.0,
            }
            cp = subprocess.CompletedProcess(
                ["claude-auto", "p4-final-qualification"],
                0,
                json.dumps(raw_obj),
                "",
            )
            session_id = None
            now = utcnow()
            log = compact_run_log(
                cycle=cycle,
                started_at=now,
                finished_at=now,
                provider=main_provider,
                model=args.model,
                returncode=0,
                result_text=result_text,
                session_id=None,
                raw_obj=raw_obj,
                stderr="",
                git_before=before,
                git_after=after,
                env=main_env,
                retain_transcripts=args.retain_transcripts,
                stdout=cp.stdout,
            )
            log["runtime_events"] = []
            log["outcome"] = "SUCCESS"
            log["outcome_reason"] = (
                "all repository-owned TaskSpecs are durably accepted"
            )
            log["wall_seconds"] = 0.0
            log["supervisor_generated_completion_candidate"] = True
        else:
            cp, before, after, log, result_text, session_id, raw_obj = _run_one_goal(
                root=worker_root,
                sd=sd,
                args=args,
                prompt=prompt,
                env=main_env,
                provider_detail=main_provider,
                model=args.model,
                verifier_model=args.verifier_model,
                researcher_model=args.researcher_model,
                cycle=cycle,
                settings_path_override=task_settings,
                force_hermetic_settings=task_settings is not None,
            )
        effective_env = main_env
        effective_provider_detail = main_provider
        effective_model = args.model
        route_attempts = [{
            "provider": main_provider,
            "model": args.model,
            "returncode": cp.returncode,
            "outcome": log.get("outcome"),
            "outcome_reason": log.get("outcome_reason"),
            "stderr_tail": redact_text(cp.stderr or "", main_env, 1200),
            "git_before": before,
            "git_after": after,
        }]

        # Cross-provider fallback is reserved for route/provider failures. Turn,
        # budget and wrapper-time boundaries are checkpoints, not a reason to swap
        # models/providers. Same-provider fallback is already handled inside the
        # Claude session through --fallback-model.
        first_route_log = log
        route_failure = log.get("outcome") in {"TRANSIENT_PROVIDER", "PROVIDER_ERROR", "MODEL_UNAVAILABLE", "EXTERNAL_BLOCKER"}
        if (
            cycle_task_activation.get("status") != "COMPLETE"
            and route_failure
            and args.fallback_model
            and not _same_provider_route(args)
        ):
            fb_env, fb_detail = provider_from_args(args, prefix="fallback_")
            fb_verifier = getattr(args, "fallback_verifier_model", None)
            fb_researcher = getattr(args, "fallback_researcher_model", None)
            print(f"Goal round {cycle}: {log.get('outcome')} on main route; retrying through {fb_detail['provider']}:{args.fallback_model}")
            cp, before, after, log, result_text, session_id, raw_obj = _run_one_goal(
                root=worker_root,
                sd=sd,
                args=args,
                prompt=prompt,
                env=fb_env,
                provider_detail=fb_detail,
                model=args.fallback_model,
                verifier_model=fb_verifier,
                researcher_model=fb_researcher,
                cycle=cycle,
                settings_path_override=task_settings,
                force_hermetic_settings=task_settings is not None,
            )
            effective_env = fb_env
            effective_provider_detail = fb_detail
            effective_model = args.fallback_model
            route_attempts.append({
                "provider": fb_detail,
                "model": args.fallback_model,
                "returncode": cp.returncode,
                "outcome": log.get("outcome"),
                "outcome_reason": log.get("outcome_reason"),
                "stderr_tail": redact_text(cp.stderr or "", fb_env, 1200),
                "git_before": before,
                "git_after": after,
            })
            log["provider_fallback_used"] = True
            log["usage"] = _merge_usage(first_route_log.get("usage"), log.get("usage"))
            log["wall_seconds"] = float(first_route_log.get("wall_seconds", 0) or 0) + float(log.get("wall_seconds", 0) or 0)

        log["route_attempts"] = route_attempts
        json_dump(sd / "logs" / f"goal-{cycle:04d}.json", log)
        status, summary = parse_status(result_text)
        task_result = (
            parse_task_result(result_text)
            if task_context is not None
            else None
        )
        plan_impact, plan_change, phase_boundary = parse_plan_impact(result_text)
        progress_checkpoint = parse_progress_checkpoint(result_text)
        progress_errors: list[str] = []
        if progress_checkpoint is not None or status == "COMPLETE":
            progress_errors = validate_progress_checkpoint(
                plan,
                progress_checkpoint,
                require_partition=status == "COMPLETE",
            )
            if progress_errors:
                state["progress_checkpoint"] = None
                state["last_progress_protocol_errors"] = progress_errors
                progress_checkpoint = None
                if status == "COMPLETE":
                    status = "CONTINUE"
                    summary = (
                        "Structured completion evidence was rejected: "
                        + "; ".join(progress_errors)[:1600]
                    )
            else:
                state["last_progress_protocol_errors"] = None
        explicit_permission_request = parse_permission_request(result_text)
        outcome = str(log.get("outcome") or "REAL_ERROR")
        outcome_reason = str(log.get("outcome_reason") or outcome)

        update_global_usage(state, log)
        state["last_runtime_outcome"] = outcome
        state["last_runtime_reason"] = redact_text(outcome_reason, effective_env, 1200)

        # "once" means one complete supervisor cycle, not merely one tool call.
        # Mark it consumed durably now while keeping the in-memory override active
        # through verification/review gates in this same cycle.
        if consume_once_grants(state, source_hash):
            json_dump(sd / "state.json", state)

        runtime_permission_events = [
            x for x in log.get("runtime_events", [])
            if isinstance(x, dict) and x.get("event") == "PermissionDenied"
        ]
        needs_permission = (
            bool(explicit_permission_request)
            or outcome == "INTERACTION_BLOCKED"
            or (status == "BLOCKED" and bool(runtime_permission_events))
        )
        if args.profile != "unattended" and needs_permission:
            request = build_permission_request(
                explicit_permission_request,
                reason=redact_text(outcome_reason, effective_env, 1600),
                runtime_events=runtime_permission_events,
                profile=args.profile,
                objective_hash=source_hash,
                repo_root=str(root),
            )
            active_grants = list(getattr(args, "_permission_grants", []) or [])
            if request_already_authorized(active_grants, request):
                state.update({
                    "updated_at": utcnow(),
                    "cycle": cycle,
                    "status": "BLOCKED",
                    "last_result_status": "BLOCKED",
                    "blocker": (
                        f"Capability {request.get('capability')} was already explicitly approved, "
                        "but an external or managed boundary still prevented the required operation."
                    ),
                    "pending_permission_request": None,
                    "last_session_id": session_id,
                    "last_git_head": after.get("head"),
                    "last_git_snapshot_hash": _git_snapshot_context_hash(after),
                })
                json_dump(sd / "state.json", state)
                return 3
            state.update({
                "cycle": cycle,
                "last_session_id": session_id,
                "last_git_head": after.get("head"),
                "last_git_snapshot_hash": _git_snapshot_context_hash(after),
            })
            disposition = resolve_permission_request(request)
            if disposition == "approved":
                continue
            if disposition == "denied":
                return 3
            return 8

        if outcome in {"TRANSIENT_PROVIDER", "PROVIDER_ERROR"}:
            failures = int(state.get("transient_failures", 0) or 0) + 1
            state.update({
                "updated_at": utcnow(), "cycle": cycle, "status": "WAITING_RETRYABLE",
                "transient_failures": failures,
                "last_session_id": session_id, "last_git_head": after.get("head"), "last_git_snapshot_hash": _git_snapshot_context_hash(after),
                "last_summary": redact_text(outcome_reason, effective_env, 1000),
                "last_result_status": "WAITING_RETRYABLE",
                "blocker": None,
            })
            limit_reason = circuit_breaker_reason(args, state)
            if limit_reason:
                state.update({"status": "LIMIT_REACHED", "blocker": limit_reason})
                json_dump(sd / "state.json", state)
                return 4
            if args.max_transient_retries > 0 and failures > args.max_transient_retries:
                state.update({
                    "status": "WAITING_RETRYABLE_LIMIT",
                    "blocker": f"Provider remained unavailable after {failures - 1} automatic retries. Resume when the provider is healthy or configure a fallback route.",
                })
                json_dump(sd / "state.json", state)
                return 7
            delay = transient_backoff_seconds(failures, args.retry_backoff_seconds, args.retry_backoff_cap_seconds)
            state["next_retry_delay_seconds"] = delay
            json_dump(sd / "state.json", state)
            print(f"Goal round {cycle}: {outcome}; retrying after {delay:.1f}s")
            if delay:
                time.sleep(delay)
            continue

        if outcome == "MODEL_UNAVAILABLE":
            state.update({
                "updated_at": utcnow(), "cycle": cycle, "status": "BLOCKED",
                "last_session_id": session_id, "last_git_head": after.get("head"),
                "last_summary": redact_text(outcome_reason, effective_env, 1000),
                "last_result_status": "BLOCKED",
                "blocker": "Selected model is unavailable and no working equivalent fallback route remains.",
            })
            json_dump(sd / "state.json", state)
            return 3

        if outcome in {"EXTERNAL_BLOCKER", "INTERACTION_BLOCKED"}:
            blocker = outcome_reason
            if outcome == "INTERACTION_BLOCKED":
                blocker = "An external or managed interaction boundary remained even after the selected profile. " + blocker
            state.update({
                "updated_at": utcnow(), "cycle": cycle, "status": "BLOCKED",
                "last_session_id": session_id, "last_git_head": after.get("head"),
                "last_summary": redact_text(outcome_reason, effective_env, 1000),
                "last_result_status": "BLOCKED",
                "blocker": redact_text(blocker, effective_env, 2000),
            })
            json_dump(sd / "state.json", state)
            return 3

        if outcome == "REAL_ERROR":
            state.update({
                "updated_at": utcnow(), "cycle": cycle, "status": "ERROR",
                "last_session_id": session_id, "last_git_head": after.get("head"),
                "last_summary": redact_text(cp.stderr or result_text, effective_env, 1000),
                "last_result_status": "ERROR",
                "blocker": redact_text(cp.stderr or cp.stdout or f"Claude exited {cp.returncode}", effective_env, 2000),
            })
            json_dump(sd / "state.json", state)
            print(f"Goal round {cycle}: ERROR")
            return cp.returncode or 1

        # Recoverable limits are checkpoints. They can return a non-zero process
        # exit and still be healthy progress boundaries.
        if outcome in {"TURN_LIMIT", "BUDGET_LIMIT", "TIMEOUT_RETRYABLE", "OUTPUT_LIMIT_RETRYABLE"}:
            status = "CONTINUE"
            if not summary:
                summary = outcome_reason
        elif not has_explicit_status(result_text):
            # Fail safe on protocol ambiguity: only an explicit COMPLETE marker is
            # a completion candidate.
            status = "CONTINUE"

        if task_context is not None and outcome == "SUCCESS":
            if task_result is None:
                state.update({
                    "updated_at": utcnow(),
                    "cycle": cycle,
                    "status": "BLOCKED",
                    "last_result_status": "BLOCKED",
                    "last_session_id": session_id,
                    "last_task_result": None,
                    "blocker": (
                        "Task-governed headless worker completed without the "
                        "required AUTONOMY_TASK_RESULT protocol record."
                    ),
                })
                json_dump(sd / "state.json", state)
                return 3
            if task_result["task_id"] != task_context["id"]:
                state.update({
                    "updated_at": utcnow(),
                    "cycle": cycle,
                    "status": "BLOCKED",
                    "last_result_status": "BLOCKED",
                    "last_session_id": session_id,
                    "last_task_result": task_result,
                    "blocker": (
                        "AUTONOMY_TASK_RESULT task_id does not match the "
                        "package-selected active TaskSpec."
                    ),
                })
                json_dump(sd / "state.json", state)
                return 3
            if task_result["status"] == "BLOCKED":
                status = "BLOCKED"
                summary = (
                    task_result.get("summary")
                    or "active repository task is blocked"
                )
            elif task_result["status"] in {
                "CONTINUE",
                "READY_FOR_ACCEPTANCE",
            }:
                # Task-level readiness/continuation can never make the overall
                # objective COMPLETE before package acceptance and scheduling.
                status = "CONTINUE"

        state["transient_failures"] = 0
        state.pop("next_retry_delay_seconds", None)
        state.update({
            "updated_at": utcnow(),
            "cycle": cycle,
            "last_session_id": session_id,
            "last_git_head": after.get("head"),
            "last_git_snapshot_hash": _git_snapshot_context_hash(after),
            "last_summary": summary or redact_text(result_text, effective_env, 1000) or outcome_reason,
            "last_result_status": status,
            "last_plan_impact": plan_impact,
            "last_plan_change": plan_change,
            "last_task_result": task_result,
            "progress_checkpoint": progress_checkpoint or state.get("progress_checkpoint"),
            # A previously approved remediation was available to this worker turn;
            # consume it now unless a new gate below approves another one.
            "approved_remediation": None,
        })
        limit_reason = circuit_breaker_reason(args, state)
        if limit_reason:
            state.update({"status": "LIMIT_REACHED", "blocker": limit_reason})
            json_dump(sd / "state.json", state)
            return 4

        if (
            task_context is not None
            and isinstance(task_result, dict)
            and task_result.get("status") == "READY_FOR_ACCEPTANCE"
            and plan_impact not in {"MATERIAL", "REQUIREMENT"}
        ):
            try:
                sealed = seal_task_candidate(
                    root,
                    state_dir=sd,
                    acquire_lease=False,
                )
                checkpoint = _p4_advance_acceptance_checkpoint(
                    root,
                    sd,
                    args,
                )
            except (TaskAcceptanceError, TaskWorkspaceError) as exc:
                state = load_json(sd / "state.json", state)
                state.update({
                    "status": "BLOCKED",
                    "last_result_status": "BLOCKED",
                    "blocker": (
                        "P4 task acceptance checkpoint failed closed: "
                        + str(exc)
                    )[:2000],
                    "updated_at": utcnow(),
                })
                json_dump(sd / "state.json", state)
                return 3

            state = load_json(sd / "state.json", state)
            checkpoint_status = str(checkpoint.get("status") or "")
            state["last_task_candidate_sha"] = sealed.get("candidate_sha")
            state["last_task_acceptance_checkpoint"] = checkpoint
            if checkpoint_status == "BLOCKED":
                state.update({
                    "status": "BLOCKED",
                    "last_result_status": "BLOCKED",
                    "blocker": str(
                        checkpoint.get("reason")
                        or "P4 task acceptance is blocked"
                    )[:2000],
                    "updated_at": utcnow(),
                })
                json_dump(sd / "state.json", state)
                return 3
            if checkpoint_status == "REPAIR":
                status = "CONTINUE"
                phase_boundary = False
                findings = [
                    str(item)
                    for item in (checkpoint.get("findings") or [])
                ]
                summary = (
                    "Task candidate was rejected by package acceptance gates; "
                    "the same task was reopened for repair."
                    + (
                        " Findings: " + "; ".join(findings[:8])[:1200]
                        if findings
                        else ""
                    )
                )
                state["last_result_status"] = "CONTINUE"
                state["last_summary"] = summary
                state["blocker"] = None
                json_dump(sd / "state.json", state)
            elif checkpoint_status in {
                "NEXT_READY",
                "TASKS_COMPLETE",
                "CLEAN",
            }:
                status = "CONTINUE"
                summary = (
                    "Repository task accepted and cleaned up. "
                    + (
                        f"Next READY task: {checkpoint.get('next_task_id')}."
                        if checkpoint_status == "NEXT_READY"
                        else "All current repository TaskSpecs are accepted."
                    )
                )
                state["last_result_status"] = "CONTINUE"
                state["last_summary"] = summary
                state["blocker"] = None
                json_dump(sd / "state.json", state)
            else:
                state.update({
                    "status": "BLOCKED",
                    "last_result_status": "BLOCKED",
                    "blocker": (
                        "P4 task acceptance reached an unsupported checkpoint "
                        f"state: {checkpoint_status}"
                    ),
                    "updated_at": utcnow(),
                })
                json_dump(sd / "state.json", state)
                return 3

        # Material remediation is deliberately two-stage: diagnose/propose first, then
        # re-plan + simulate + red-team before the worker may implement the remediation.
        if plan_impact in {"MATERIAL", "REQUIREMENT"}:
            reason = (
                f"Worker discovered {plan_impact.lower()} plan impact during cycle {cycle}. "
                f"Root-cause/remediation proposal: {plan_change or state.get('last_summary') or 'not supplied'}"
            )
            state["status"] = "REVALIDATING_PLAN"
            json_dump(sd / "state.json", state)
            plan, plan_rc, source_text, source_hash, state = _revalidate_authoritative_plan(
                args=args, root=root, sd=sd, prof=prof, state=state, objective=objective,
                source_kind=source_kind, source_ref=source_ref, source_text=source_text,
                source_hash=source_hash, env=main_env, provider_detail=main_provider,
                reason=reason,
            )
            if plan_rc != 0 or not plan:
                return plan_rc or 6
            state = load_json(sd / "state.json", state)
            state.update({"status": "READY", "last_result_status": "CONTINUE", "plan_revalidation_findings": None})
            json_dump(sd / "state.json", state)
            status = "CONTINUE"

        # A non-trivial local remediation still receives whole-plan stress review after
        # the worker's root-cause/impact analysis. This catches a locally-correct patch
        # that quietly breaks completed or future plan steps without forcing full
        # re-planning when the validated plan itself remains sound.
        elif plan_impact == "LOCAL" and status != "BLOCKED":
            plan = load_json(plan_state_dir(sd) / "current-plan.json", plan or {})
            review_status, assessments = run_plan_checkpoint_review(
                args=args, root=root, sd=sd, state=state, objective=objective, plan=plan,
                env=main_env, provider_detail=main_provider, stage="remediation", main_summary=state.get("last_summary"),
            )
            if review_status == "LIMIT_REACHED":
                return 4
            if review_status == "BLOCKED":
                state.update({"status": "BLOCKED", "blocker": assessments[0].get("summary") or "Remediation validation blocked."})
                json_dump(sd / "state.json", state)
                return 3
            if review_status == "REVISE_PLAN":
                reason = "Local remediation simulation/red-team exposed plan-level impact: " + json.dumps(assessments, separators=(",", ":"))[:5000]
                plan, plan_rc, source_text, source_hash, state = _revalidate_authoritative_plan(
                    args=args, root=root, sd=sd, prof=prof, state=state, objective=objective,
                    source_kind=source_kind, source_ref=source_ref, source_text=source_text,
                    source_hash=source_hash, env=main_env, provider_detail=main_provider,
                    reason=reason,
                )
                if plan_rc != 0 or not plan:
                    return plan_rc or 6
                status = "CONTINUE"
            elif review_status == "FIX_IMPLEMENTATION":
                state["plan_revalidation_findings"] = assessments
                state["last_result_status"] = "CONTINUE"
                json_dump(sd / "state.json", state)
                status = "CONTINUE"
            else:
                state["approved_remediation"] = plan_change or state.get("last_summary")
                state["plan_revalidation_findings"] = None
                state["last_result_status"] = "CONTINUE"
                json_dump(sd / "state.json", state)
                status = "CONTINUE"

        # At a logical phase boundary, stress the remaining validated plan against the
        # actual partial implementation before another phase is allowed to proceed.
        elif phase_boundary and status != "BLOCKED":
            plan = load_json(plan_state_dir(sd) / "current-plan.json", plan or {})
            review_status, assessments = run_plan_checkpoint_review(
                args=args, root=root, sd=sd, state=state, objective=objective, plan=plan,
                env=main_env, provider_detail=main_provider, stage="phase", main_summary=state.get("last_summary"),
            )
            if review_status == "LIMIT_REACHED":
                return 4
            if review_status == "BLOCKED":
                state.update({"status": "BLOCKED", "blocker": assessments[0].get("summary") or "Phase-boundary validation blocked."})
                json_dump(sd / "state.json", state)
                return 3
            if review_status == "REVISE_PLAN":
                reason = "Phase-boundary simulation/red-team found plan-level issues: " + json.dumps(assessments, separators=(",", ":"))[:5000]
                plan, plan_rc, source_text, source_hash, state = _revalidate_authoritative_plan(
                    args=args, root=root, sd=sd, prof=prof, state=state, objective=objective,
                    source_kind=source_kind, source_ref=source_ref, source_text=source_text,
                    source_hash=source_hash, env=main_env, provider_detail=main_provider,
                    reason=reason,
                )
                if plan_rc != 0 or not plan:
                    return plan_rc or 6
                status = "CONTINUE"
            elif review_status == "FIX_IMPLEMENTATION":
                state["plan_revalidation_findings"] = assessments
                state["last_result_status"] = "CONTINUE"
                json_dump(sd / "state.json", state)
                status = "CONTINUE"

        # COMPLETE is only a candidate. Bind every final completion gate to one
        # exact source snapshot before re-running whole-system review. If HEAD,
        # tracked/indexed changes or untracked source inputs move before the final
        # return, every completion receipt is stale and the chain must restart.
        completion_anchor: dict[str, Any] | None = None
        if status == "COMPLETE":
            completion_anchor = git_snapshot(root)
            state["completion_anchor"] = {
                key: completion_anchor.get(key)
                for key in (
                    "branch", "head", "worktree_diff_sha256",
                    "index_diff_sha256", "untracked_sha256",
                    "verification_contract_sha256",
                )
            }
            state["completion_anchor_hash"] = _completion_anchor_hash(completion_anchor)
            state["completion_anchor_started_at"] = utcnow()
            state["completion_anchor_invalidated_reason"] = None
            json_dump(sd / "state.json", state)

        # Re-run whole-system simulation and red-team against the anchored
        # repository reality before any final challenger or success return.
        if status == "COMPLETE":
            plan = load_json(plan_state_dir(sd) / "current-plan.json", plan or {})
            final_review, assessments = run_plan_checkpoint_review(
                args=args, root=root, sd=sd, state=state, objective=objective, plan=plan,
                env=main_env, provider_detail=main_provider, stage="final", main_summary=state.get("last_summary"),
            )
            if final_review == "LIMIT_REACHED":
                return 4
            if final_review == "BLOCKED":
                state.update({"status": "BLOCKED", "blocker": next((x.get("summary") for x in assessments if x["verdict"] == "BLOCKED"), "Final validation blocked.")})
                json_dump(sd / "state.json", state)
                return 3
            if final_review == "REVISE_PLAN":
                reason = "Final whole-system simulation/red-team found plan-level defects: " + json.dumps(assessments, separators=(",", ":"))[:5000]
                plan, plan_rc, source_text, source_hash, state = _revalidate_authoritative_plan(
                    args=args, root=root, sd=sd, prof=prof, state=state, objective=objective,
                    source_kind=source_kind, source_ref=source_ref, source_text=source_text,
                    source_hash=source_hash, env=main_env, provider_detail=main_provider,
                    reason=reason,
                )
                if plan_rc != 0 or not plan:
                    return plan_rc or 6
                state = load_json(sd / "state.json", state)
                state["plan_revalidation_findings"] = assessments
                state["last_result_status"] = "CONTINUE"
                json_dump(sd / "state.json", state)
                status = "CONTINUE"
            elif final_review == "FIX_IMPLEMENTATION":
                state["plan_revalidation_findings"] = assessments
                state["last_result_status"] = "CONTINUE"
                json_dump(sd / "state.json", state)
                status = "CONTINUE"
            else:
                state["plan_revalidation_findings"] = None

        # Claude Auto deterministic completion proof: the supervisor, not the model, owns
        # fresh test/lint/type/build receipts tied to the final repository state.
        if status == "COMPLETE":
            ver_verdict, ver_summary, ver_receipts = run_verification_gate(root, sd, prof, state, args)
            print(f"Deterministic verification: {ver_verdict} - {ver_summary}")
            if ver_verdict == "UNVERIFIED":
                findings = [x for x in ver_receipts if x.get("verdict") == "UNVERIFIED"]
                boundary_unavailable = any(x.get("execution_boundary") == "unavailable" for x in findings)
                if (
                    args.profile != "unattended"
                    and boundary_unavailable
                    and "host-repository-execution" not in set(getattr(args, "_permission_overrides", []) or [])
                ):
                    request = build_permission_request(
                        {
                            "capability": "host-repository-execution",
                            "operation": "Run deterministic repository verification on the host",
                            "resource": "; ".join(str(x.get("command") or "") for x in findings)[:2000],
                            "why_needed": ver_summary,
                            "risk": (
                                "Repository-controlled test/build commands would run directly on the host. "
                                "This scoped approval keeps the supervisor environment scrubbed unless "
                                "unrestricted is separately approved."
                            ),
                            "safer_alternative": (
                                "Install or repair Anthropic Sandbox Runtime or Linux bubblewrap so "
                                "verification can remain isolated."
                            ),
                            "constraints": {
                                "verification_commands": [
                                    str(x.get("command") or "").strip()
                                    for x in findings
                                    if str(x.get("command") or "").strip()
                                ]
                            },
                        },
                        reason=ver_summary,
                        runtime_events=[],
                        profile=args.profile,
                        objective_hash=source_hash,
                        repo_root=str(root),
                    )
                    state["last_verification_findings"] = findings
                    disposition = resolve_permission_request(request)
                    if disposition == "approved":
                        continue
                    if disposition == "denied":
                        return 3
                    return 8
                state.update({
                    "status": "BLOCKED",
                    "last_result_status": "BLOCKED",
                    "blocker": ver_summary,
                    "last_verification_findings": findings,
                })
                json_dump(sd / "state.json", state)
                status = "BLOCKED"
            elif ver_verdict != "PASS":
                state.update({
                    "status": "READY", "last_result_status": "CONTINUE", "blocker": None,
                    "last_verification_findings": [x for x in ver_receipts if x.get("verdict") == "FAIL"],
                })
                json_dump(sd / "state.json", state)
                status = "CONTINUE"
            else:
                state["last_verification_findings"] = None
                json_dump(sd / "state.json", state)

        if status == "COMPLETE" and getattr(args, "runtime_verify", "auto") != "off":
            rv_verdict, rv_summary, rv_findings, rv_meta = run_runtime_verify_gate(
                root=root, sd=sd, prof=prof, objective=objective, env=effective_env,
                provider_detail=effective_provider_detail, model=effective_model,
                timeout=args.cycle_timeout if args.cycle_timeout else None,
                max_budget_usd=effective_invocation_budget(args, state),
                autonomy_profile=args.profile,
            )
            state["last_runtime_verify_verdict"] = rv_verdict
            state["last_runtime_verify_summary"] = rv_summary
            state["last_runtime_verify_findings"] = rv_findings
            update_global_usage(state,{"usage":_merge_usage(rv_meta.get("native_usage"),rv_meta.get("usage")),"wall_seconds":float(rv_meta.get("wall_seconds",0) or 0)})
            json_dump(sd/"logs"/f"goal-{cycle:04d}-runtime-verify.json",{"cycle":cycle,"finished_at":utcnow(),"verdict":rv_verdict,"summary":rv_summary,"findings":rv_findings,"usage":rv_meta.get("usage"),"native_usage":rv_meta.get("native_usage"),"repository_unchanged":rv_meta.get("repository_unchanged")})
            limit_reason=circuit_breaker_reason(args,state)
            if limit_reason:
                state.update({"status":"LIMIT_REACHED","blocker":limit_reason}); json_dump(sd/"state.json",state); return 4
            print(f"Runtime verification: {rv_verdict} - {rv_summary}")
            if rv_verdict == "FAIL":
                state.update({"status":"READY","last_result_status":"CONTINUE","blocker":None})
                json_dump(sd/"state.json",state)
                status="CONTINUE"

        # Claude Auto mandatory final correctness review is independent from security review.
        if status == "COMPLETE":
            cr_verdict, cr_summary, cr_findings, cr_meta = run_correctness_review_gate(
                root=root, sd=sd, objective=objective, main_summary=state.get("last_summary"),
                env=effective_env, provider_detail=effective_provider_detail, model=args.model or effective_model,
                timeout=args.cycle_timeout if args.cycle_timeout else None,
                max_budget_usd=effective_invocation_budget(args, state),
            )
            update_global_usage(state, {"usage": _merge_usage(cr_meta.get("native_usage"), cr_meta.get("usage")), "wall_seconds": float(cr_meta.get("wall_seconds", 0) or 0)})
            state["last_correctness_review_verdict"] = cr_verdict
            state["last_correctness_review_findings"] = cr_findings
            json_dump(sd / "logs" / f"goal-{cycle:04d}-correctness-review.json", {
                "cycle": cycle, "finished_at": utcnow(), "verdict": cr_verdict,
                "summary": cr_summary, "findings": cr_findings, "meta": cr_meta,
            })
            json_dump(sd / "state.json", state)
            limit_reason = circuit_breaker_reason(args, state)
            if limit_reason:
                state.update({"status":"LIMIT_REACHED","blocker":limit_reason}); json_dump(sd / "state.json", state); return 4
            print(f"Correctness review: {cr_verdict} - {cr_summary or 'no summary'}")
            if cr_verdict == "BLOCKED":
                state.update({"status": "BLOCKED", "last_result_status": "BLOCKED", "blocker": cr_summary or "Correctness review blocked."})
                json_dump(sd / "state.json", state)
                return 3
            if cr_verdict == "FAIL":
                state.update({"status": "READY", "last_result_status": "CONTINUE", "blocker": None})
                json_dump(sd / "state.json", state)
                status = "CONTINUE"
            else:
                state["last_correctness_review_findings"] = None
                json_dump(sd / "state.json", state)

        if status == "COMPLETE" and getattr(args, "security_scanners", "auto") != "off":
            scan_verdict, scan_summary, scan_receipts = run_security_scanners(
                root, sd, prof,
                timeout=int(getattr(args, "security_scanner_timeout", 600) or 600),
                trust_repo_scripts=(
                    bool(getattr(args, "trust_repo_scripts", False))
                    or args.profile == "unattended"
                ),
                unrestricted_host=(
                    args.profile == "unattended"
                    or "unrestricted" in set(getattr(args, "_permission_overrides", []) or [])
                ),
            )
            state["last_security_scanner_receipts"] = scan_receipts
            state["last_security_scanner_summary"] = scan_summary
            print(f"Security scanners: {scan_verdict} - {scan_summary}")
            if scan_verdict == "FAIL":
                state.update({
                    "status": "READY", "last_result_status": "CONTINUE", "blocker": None,
                    "last_security_review_findings": [scan_summary],
                })
                json_dump(sd / "state.json", state)
                status = "CONTINUE"
            else:
                json_dump(sd / "state.json", state)

        # Claude Auto mandatory completion gate: prefer native /security-review with portable fallback
        # in a fresh hard-read-only session, then independently adjudicate its
        # free-form output into a deterministic PASS/FAIL/BLOCKED decision. A FAIL
        # reopens implementation and is fed back into the next worker turn; a
        # BLOCKED audit fails closed rather than silently accepting completion.
        if status == "COMPLETE":
            sec_verdict, sec_summary, sec_findings, sec_meta = run_security_review_gate(
                root=root, sd=sd, objective=objective, main_summary=state.get("last_summary"),
                env=effective_env, provider_detail=effective_provider_detail, model=effective_model,
                timeout=args.cycle_timeout if args.cycle_timeout else None,
                max_budget_usd=effective_invocation_budget(args, state),
            )
            state["last_security_review_verdict"] = sec_verdict
            state["last_security_review_summary"] = sec_summary
            state["last_security_review_findings"] = sec_findings
            update_global_usage(state, {"usage": _merge_usage(sec_meta.get("native_usage"), sec_meta.get("usage")), "wall_seconds": float(sec_meta.get("wall_seconds", 0) or 0)})
            json_dump(sd / "logs" / f"goal-{cycle:04d}-security-review.json", {
                "cycle": cycle, "finished_at": utcnow(), "provider": sec_meta.get("provider"),
                "model": effective_model, "verdict": sec_verdict, "summary": sec_summary,
                "findings": sec_findings, "usage": sec_meta.get("usage"),
                "native_usage": sec_meta.get("native_usage"),
                "native_returncode": sec_meta.get("native_returncode"),
                "repository_unchanged": sec_meta.get("repository_unchanged"),
                "git_before": sec_meta.get("git_before"), "git_after": sec_meta.get("git_after"),
            })
            limit_reason = circuit_breaker_reason(args, state)
            if limit_reason:
                state.update({"status":"LIMIT_REACHED","blocker":limit_reason}); json_dump(sd / "state.json", state); return 4
            print(f"Security review: {sec_verdict} - {sec_summary or 'no summary'}")
            if sec_verdict == "BLOCKED":
                state.update({
                    "status": "BLOCKED",
                    "last_result_status": "BLOCKED",
                    "blocker": sec_summary or "Mandatory Claude Code /security-review could not establish a reliable result.",
                })
                json_dump(sd / "state.json", state)
                return 3
            if sec_verdict == "FAIL":
                state.update({
                    "status": "READY",
                    "last_result_status": "CONTINUE",
                    "blocker": None,
                })
                json_dump(sd / "state.json", state)
                status = "CONTINUE"
            else:
                state["last_security_review_findings"] = None
                state["last_security_review_summary"] = sec_summary
                json_dump(sd / "state.json", state)

        should_challenge = challenger_enabled and (
            args.challenger_policy in {"final", "each-cycle"} and status == "COMPLETE"
        )
        if should_challenge:
            verdict, ch_summary, ch_meta = run_challenger(
                root=root, sd=sd, objective=objective, main_summary=state.get("last_summary"),
                provider=args.challenger_provider, model=args.challenger_model,
                gateway_url=args.challenger_gateway_url,
                gateway_token_env=args.challenger_gateway_token_env,
                gateway_discovery=args.challenger_gateway_discovery,
                isolate_provider_profile=args.challenger_isolate_provider_profile,
                gateway_hints=args.challenger_gateway_hints,
                timeout=args.cycle_timeout if args.cycle_timeout else None,
                max_budget_usd=effective_invocation_budget(args, state),
            )
            state["last_challenger_verdict"] = verdict
            state["last_challenger_findings"] = ch_summary
            update_global_usage(state, {"usage": ch_meta.get("usage") or {}, "wall_seconds": float(ch_meta.get("wall_seconds", 0) or 0)})
            limit_reason = circuit_breaker_reason(args, state)
            if limit_reason:
                state.update({"status":"LIMIT_REACHED","blocker":limit_reason}); json_dump(sd / "state.json", state); return 4
            json_dump(sd / "logs" / f"goal-{cycle:04d}-challenger.json", {
                "cycle": cycle, "finished_at": utcnow(), "provider": ch_meta.get("provider"),
                "model": args.challenger_model, "verdict": verdict, "summary": ch_summary,
                "usage": ch_meta.get("usage"), "repository_unchanged": ch_meta.get("repository_unchanged"),
                "git_before": ch_meta.get("git_before"), "git_after": ch_meta.get("git_after"),
            })
            print(f"Challenger: {verdict} - {ch_summary or 'no summary'}")
            if verdict == "PASS":
                final_snapshot = git_snapshot(root)
                if completion_anchor is None or not _completion_source_unchanged(completion_anchor, final_snapshot):
                    reason = (
                        "Repository source state changed after completion qualification began; "
                        "all completion evidence was invalidated and must be regenerated."
                    )
                    _invalidate_completion_evidence(state, reason)
                    state.update({
                        "status": "READY",
                        "last_result_status": "CONTINUE",
                        "blocker": None,
                        "last_summary": reason,
                    })
                    json_dump(sd / "state.json", state)
                    print(f"Completion anchor invalidated: {reason}")
                else:
                    state["completion_anchor_verified_at"] = utcnow()
                    state["completion_anchor_final_hash"] = _completion_anchor_hash(final_snapshot)
                    state["status"] = "COMPLETE"
                    state["blocker"] = None
                    json_dump(sd / "state.json", state)
                    return 0
            status = "CONTINUE"
            state["last_result_status"] = "CONTINUE"
        elif status == "COMPLETE":
            final_snapshot = git_snapshot(root)
            if completion_anchor is None or not _completion_source_unchanged(completion_anchor, final_snapshot):
                reason = (
                    "Repository source state changed after completion qualification began; "
                    "all completion evidence was invalidated and must be regenerated."
                )
                _invalidate_completion_evidence(state, reason)
                state.update({
                    "status": "READY",
                    "last_result_status": "CONTINUE",
                    "blocker": None,
                    "last_summary": reason,
                })
                json_dump(sd / "state.json", state)
                print(f"Completion anchor invalidated: {reason}")
                status = "CONTINUE"
            else:
                state["completion_anchor_verified_at"] = utcnow()
                state["completion_anchor_final_hash"] = _completion_anchor_hash(final_snapshot)
                state["status"] = "COMPLETE"
                state["blocker"] = None
                json_dump(sd / "state.json", state)
                return 0

        if status == "BLOCKED":
            state["status"] = "BLOCKED"
            state["blocker"] = state.get("last_summary")
            json_dump(sd / "state.json", state)
            return 3

        # Recompute the fingerprint after challenger disposition, not before.
        final_snap = git_snapshot(root)
        fp = progress_fingerprint(status, state.get("last_summary"), final_snap, state)
        stagnant = stagnant + 1 if status == "CONTINUE" and fp == previous_fp else 0
        previous_fp = fp
        state["last_progress_fingerprint"] = fp
        state["stagnant_cycles"] = stagnant
        state["status"] = "READY"
        json_dump(sd / "state.json", state)
        if args.max_stagnant_cycles > 0 and stagnant >= args.max_stagnant_cycles:
            state["status"] = "STAGNATED"
            state["blocker"] = f"No Git/plan/structured-evidence progress for {stagnant} consecutive goal repair rounds."
            json_dump(sd / "state.json", state)
            return 5
        if args.pause_seconds:
            time.sleep(args.pause_seconds)

    state["status"] = "LIMIT_REACHED"
    state["updated_at"] = utcnow()
    json_dump(sd / "state.json", state)
    return 4


def do_run_goal(args: argparse.Namespace) -> int:
    refuse_nested_claude_launch()
    require_supported_claude()
    root = find_repo_root(args.repo)
    sd = repo_state_dir(root)
    old_handlers: dict[int, Any] = {}
    supervisor_pid = os.getpid()
    supervisor_start = process_start_token(supervisor_pid) or ""

    def _interrupt(signum: int, _frame: Any) -> None:
        raise SupervisorInterrupted(signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        old_handlers[int(sig)] = signal.getsignal(sig)
        signal.signal(sig, _interrupt)
    try:
        # Acquire ownership before any activate/state write so two supervisors cannot
        # race during repository-profile/state initialization.
        with SupervisorLease(sd, root) as lease:
            activate(root)
            state = load_json(sd / "state.json", {})
            state.update({
                "supervisor_fence_token": lease.token,
                "supervisor_epoch": int(state.get("supervisor_epoch", 0) or 0) + 1,
                "supervisor_pid": supervisor_pid,
                "supervisor_start_token": supervisor_start,
                "supervisor_mode": "run",
                "supervisor_started_at": utcnow(),
            })
            json_dump(sd / "state.json", state)
            try:
                try:
                    return _do_run_goal_unlocked(args)
                except ControlPlaneRetryableError as exc:
                    state = load_json(sd / "state.json", {})
                    if isinstance(exc.meta, dict):
                        update_global_usage(state, {
                            "usage": exc.meta.get("usage") or {},
                            "wall_seconds": float(exc.meta.get("wall_seconds", 0) or 0),
                        })
                    attempts = exc.meta.get("attempts") if isinstance(exc.meta, dict) else None
                    state.update({
                        "status": "WAITING_RETRYABLE_LIMIT",
                        "last_result_status": "WAITING_RETRYABLE",
                        "last_runtime_outcome": exc.outcome,
                        "last_runtime_reason": str(exc.reason)[:1200],
                        "blocker": (
                            "A planner/reviewer provider boundary remained unavailable after bounded automatic retries. "
                            "The run is checkpointed and can be resumed when the provider is healthy."
                        ),
                        "control_plane_retry_attempts": attempts if isinstance(attempts, list) else [],
                        "updated_at": utcnow(),
                    })
                    json_dump(sd / "state.json", state)
                    return 7
                except SupervisorInterrupted as exc:
                    state = load_json(sd / "state.json", {})
                    state.update({
                        "status": "INTERRUPTED",
                        "last_result_status": "CONTINUE",
                        "blocker": None,
                        "last_interrupt_signal": int(exc.signum),
                        "updated_at": utcnow(),
                    })
                    json_dump(sd / "state.json", state)
                    return 128 + int(exc.signum)
            finally:
                final_state = load_json(sd / "state.json", {})
                if (
                    int(final_state.get("supervisor_pid") or 0) == supervisor_pid
                    and str(final_state.get("supervisor_start_token") or "") == supervisor_start
                ):
                    final_state["supervisor_pid"] = None
                    final_state["supervisor_start_token"] = None
                    final_state["supervisor_mode"] = None
                    final_state["supervisor_stopped_at"] = utcnow()
                    json_dump(sd / "state.json", final_state)
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)

def do_run(args: argparse.Namespace) -> int:
    return do_run_goal(args)

def _interactive_start_context(
    root: Path,
    sd: Path,
    args: argparse.Namespace,
) -> tuple[Path, dict[str, Any] | None, Path | None, dict[str, Any]]:
    """Resolve the exact current interactive worker boundary under SupervisorLease."""
    state = load_json(sd / "state.json", {})
    requested_session_settings = getattr(args, "session_settings", None)
    args._session_settings_was_explicit = (
        requested_session_settings is not None
    )
    args.profile = resolve_autonomy_profile(
        getattr(args, "profile", None),
        state.get("autonomy_profile"),
    )
    args.session_settings = resolve_session_settings(
        requested_session_settings,
        args.profile,
    )

    role = str(getattr(args, "resume_role", "product") or "product")
    selector = str(getattr(args, "resume_session", "") or "").strip()
    if role == "planning-architect":
        if not selector:
            raise SessionAdoptionError(
                "--resume-role planning-architect requires --resume-session"
            )
        try:
            repair_root, repair_settings, active = (
                planning_repair_session_context(root)
            )
        except ValueError as exc:
            raise SessionAdoptionError(str(exc)) from exc
        return (
            repair_root,
            None,
            repair_settings,
            {
                "status": "PLANNING_REPAIR",
                "repair_envelope_sha256": active.get(
                    "repair_envelope_sha256"
                ),
                "worktree": str(repair_root),
            },
        )

    active_repair = load_active_repair(root)
    if active_repair:
        raise SessionAdoptionError(
            "product interactive session is blocked while planning repair is active"
        )

    try:
        activation = ensure_supervisor_task_workspace(
            root,
            state_dir=sd,
        )
    except TaskWorkspaceError:
        raise
    status = str(activation.get("status") or "")
    if status == "BLOCKED":
        raise TaskWorkspaceError(
            str(
                activation.get("reason")
                or "P4 task workspace preparation is blocked"
            )
        )
    if status == "CHECKPOINT":
        raise TaskWorkspaceError(
            "P4 task workspace is at an acceptance checkpoint; "
            "reconcile/verify/accept it before interactive worker execution"
        )
    if status == "ACTIVE":
        worker_root, task_context, settings_path = (
            _p4_worker_execution_context(
                root,
                sd,
                args,
                activation,
            )
        )
        return worker_root, task_context, settings_path, activation
    if status in {"UNCONFIGURED", "COMPLETE"}:
        return root, None, None, activation
    raise TaskWorkspaceError(
        "unsupported P4 interactive task state: " + status
    )


def do_start(args: argparse.Namespace) -> int:
    refuse_nested_claude_launch()
    require_supported_claude()
    root = find_repo_root(args.repo)
    sd = repo_state_dir(root)
    supervisor_pid = os.getpid()
    supervisor_start = process_start_token(supervisor_pid) or ""
    old_usr1 = signal.getsignal(signal.SIGUSR1)
    child_control: dict[str, Any] = {"proc": None, "switch_signal": False}

    def _profile_switch_interrupt(_signum: int, _frame: Any) -> None:
        # Never raise asynchronously into state writes. Mark the request and stop
        # only the interactive Claude child; the normal loop will consume the
        # already-durable request and resume the exact session.
        child_control["switch_signal"] = True
        proc = child_control.get("proc")
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    signal.signal(signal.SIGUSR1, _profile_switch_interrupt)
    try:
        # Interactive start is another active repository owner. Hold the same lease
        # for the entire session so it cannot race an autonomous run.
        with SupervisorLease(sd, root) as lease:
            activate(root)
            state = load_json(sd / "state.json", {})
            state.update({
                "supervisor_fence_token": lease.token,
                "supervisor_epoch": int(state.get("supervisor_epoch", 0) or 0) + 1,
                "supervisor_pid": supervisor_pid,
                "supervisor_start_token": supervisor_start,
                "supervisor_mode": "start",
                "supervisor_started_at": utcnow(),
            })
            json_dump(sd / "state.json", state)
            try:
                try:
                    worker_root, task_context, settings_override, start_state = (
                        _interactive_start_context(root, sd, args)
                    )
                except (
                    TaskWorkspaceError,
                    TaskAcceptanceError,
                    SessionAdoptionError,
                ) as exc:
                    state = load_json(sd / "state.json", state)
                    state.update({
                        "status": "BLOCKED",
                        "last_result_status": "BLOCKED",
                        "blocker": f"Interactive session boundary blocked: {exc}",
                        "updated_at": utcnow(),
                    })
                    json_dump(sd / "state.json", state)
                    print(state["blocker"], file=sys.stderr)
                    return 3

                adoption_selector = str(
                    getattr(args, "resume_session", "") or ""
                ).strip()
                if adoption_selector:
                    settings_for_adoption = settings_override or (
                        sd
                        / (
                            f"settings-{args.profile}-"
                            f"{args.memory_mode}.json"
                        )
                    )
                    try:
                        prepare_session_adoption(
                            root,
                            adoption_selector,
                            working_directory=worker_root,
                            settings_path=settings_for_adoption,
                            autonomy_profile=args.profile,
                            role=str(
                                getattr(args, "resume_role", "product")
                                or "product"
                            ),
                            state_dir=sd,
                        )
                    except SessionAdoptionError as exc:
                        state = load_json(sd / "state.json", state)
                        state.update({
                            "status": "BLOCKED",
                            "last_result_status": "BLOCKED",
                            "blocker": f"Session adoption blocked: {exc}",
                            "updated_at": utcnow(),
                        })
                        json_dump(sd / "state.json", state)
                        print(state["blocker"], file=sys.stderr)
                        return 3

                return _do_start_unlocked(
                    args,
                    worker_root,
                    sd,
                    child_control=child_control,
                    task_activation=start_state,
                    task_context=task_context,
                    settings_path_override=settings_override,
                    adoption_coordinator_root=(
                        root if adoption_selector else None
                    ),
                    initial_resume_selector=(
                        adoption_selector or None
                    ),
                )
            finally:
                final_state = load_json(sd / "state.json", {})
                if (
                    int(final_state.get("supervisor_pid") or 0) == supervisor_pid
                    and str(final_state.get("supervisor_start_token") or "") == supervisor_start
                ):
                    final_state["supervisor_pid"] = None
                    final_state["supervisor_start_token"] = None
                    final_state["supervisor_mode"] = None
                    final_state.pop("interactive_child_pid", None)
                    final_state["supervisor_stopped_at"] = utcnow()
                    json_dump(sd / "state.json", final_state)
    finally:
        signal.signal(signal.SIGUSR1, old_usr1)


def _do_start_unlocked(
    args: argparse.Namespace,
    root: Path,
    sd: Path,
    *,
    child_control: dict[str, Any] | None = None,
    task_activation: dict[str, Any] | None = None,
    task_context: dict[str, Any] | None = None,
    settings_path_override: Path | None = None,
    adoption_coordinator_root: Path | None = None,
    initial_resume_selector: str | None = None,
) -> int:
    state = load_json(sd / "state.json", {})
    requested_session_settings = getattr(args, "session_settings", None)
    session_settings_explicit = bool(
        getattr(
            args,
            "_session_settings_was_explicit",
            requested_session_settings is not None,
        )
    )
    args.profile = resolve_autonomy_profile(args.profile, state.get("autonomy_profile"))
    args.session_settings = resolve_session_settings(
        requested_session_settings,
        args.profile,
    )
    objective = args.objective or state.get("objective")
    state["autonomy_profile"] = args.profile
    state["session_settings_explicit"] = session_settings_explicit
    state["session_settings"] = args.session_settings
    if objective:
        state["objective"] = objective
    state["resume_config"] = _capture_resume_config(args)
    json_dump(sd / "state.json", state)
    permission_mode_for_profile(args.permission_mode, args.profile, set())
    if args.subagent_model and (args.verifier_model or args.researcher_model):
        raise SystemExit("--subagent-model globally overrides subagents; do not combine it with --verifier-model/--researcher-model.")
    env, provider_detail = provider_from_args(args)
    warn_model_qualification(args.provider, args.model, "main")
    warn_model_qualification(args.provider, args.verifier_model, "verifier")
    warn_model_qualification(args.provider, args.researcher_model, "researcher")
    initial = None
    if objective:
        initial = (
            f"OVERALL OBJECTIVE: {objective}\n\n"
            "Operate autonomously across repository tasks. Reconcile current repo state first. "
            "Do not stop merely because one subtask is complete; continue useful independent work toward the overall objective."
        )
        if isinstance(task_context, dict):
            initial += (
                "\n\nCURRENT PACKAGE TASK AUTHORITY (read-only context; package "
                "state/hooks remain authoritative):\n"
                + json.dumps(
                    task_context,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )

    resume_session_id: str | None = (
        str(initial_resume_selector).strip()
        if initial_resume_selector
        else None
    )
    fork_resume_once = bool(resume_session_id and adoption_coordinator_root)
    while True:
        state = load_json(sd / "state.json", state)
        pending = pending_profile_switch(sd)
        if pending:
            pending_session = str(pending.get("session_id") or "").strip() or None
            _apply_pending_profile_switch(
                args,
                state,
                sd,
                source_hash=state.get("active_permission_objective_hash"),
                headless=False,
            )
            state = load_json(sd / "state.json", state)
            if pending_session:
                resume_session_id = pending_session

        cmd = claude_base_args(
            sd, args.model, args.effort, args.permission_mode, args.memory_mode,
            verifier_model=args.verifier_model, researcher_model=args.researcher_model,
            autonomy_profile=args.profile,
            settings_path=settings_path_override,
        )
        cmd += [
            "--setting-sources",
            "" if (
                settings_path_override is not None
                or getattr(args, "session_settings", "compatibility") == "hermetic"
            ) else "user,project,local",
        ]
        if resume_session_id:
            cmd += ["--resume", resume_session_id]
            if fork_resume_once:
                cmd += ["--fork-session"]
        elif initial:
            cmd.append(initial)

        print(f"Starting Claude in {root}")
        print(f"Autonomy profile: {args.profile}")
        print(f"Autonomy state: {sd}")
        if isinstance(task_activation, dict) and task_activation.get("status") == "ACTIVE":
            reuse = "resumed" if task_activation.get("reused") else "selected"
            print(
                f"Task: {task_activation.get('task_id')} "
                f"({reuse}; envelope {str(task_activation.get('execution_envelope_sha256'))[:12]})"
            )
        if resume_session_id:
            if fork_resume_once:
                print(
                    "Adopting Claude session under a fresh RC4-owned fork: "
                    + resume_session_id
                )
            else:
                print(f"Resuming Claude session: {resume_session_id}")
        print(f"Provider: {provider_detail['provider']}" + (f" @ {provider_detail['base_url']}" if provider_detail.get('base_url') else ""))

        adoption_event_offset = (
            runtime_event_offset(sd) if fork_resume_once else None
        )
        try:
            proc = subprocess.Popen(cmd, cwd=str(root), env=env)
        except OSError as exc:
            if adoption_coordinator_root is not None and fork_resume_once:
                try:
                    block_session_adoption(
                        adoption_coordinator_root,
                        f"forked Claude launch failed: {exc}",
                        state_dir=sd,
                    )
                except SessionAdoptionError:
                    pass
            raise
        if adoption_coordinator_root is not None and fork_resume_once:
            try:
                mark_session_adoption_launched(
                    adoption_coordinator_root,
                    event_offset=int(adoption_event_offset or 0),
                    pid=int(proc.pid),
                    state_dir=sd,
                )
                deadline = time.monotonic() + 30.0
                adopted: dict[str, Any] | None = None
                while time.monotonic() < deadline:
                    adopted = capture_adopted_session_id(
                        adoption_coordinator_root,
                        state_dir=sd,
                        block_if_missing=False,
                    )
                    if adopted.get("lifecycle_state") == "ADOPTED":
                        break
                    if proc.poll() is not None:
                        break
                    time.sleep(0.05)
                if (
                    adopted is None
                    or adopted.get("lifecycle_state") != "ADOPTED"
                ):
                    adopted = capture_adopted_session_id(
                        adoption_coordinator_root,
                        state_dir=sd,
                        block_if_missing=True,
                    )
                if adopted.get("lifecycle_state") != "ADOPTED":
                    try:
                        if proc.poll() is None:
                            proc.terminate()
                    except OSError:
                        pass
                    print(
                        str(
                            adopted.get("blocker")
                            or "forked session adoption could not be proven"
                        ),
                        file=sys.stderr,
                    )
                    return 9
                resume_session_id = str(
                    adopted["adopted_session_id"]
                ).strip()
                fork_resume_once = False
            except SessionAdoptionError as exc:
                try:
                    block_session_adoption(
                        adoption_coordinator_root,
                        str(exc),
                        state_dir=sd,
                    )
                except SessionAdoptionError:
                    pass
                try:
                    if proc.poll() is None:
                        proc.terminate()
                except OSError:
                    pass
                print(f"Session adoption blocked: {exc}", file=sys.stderr)
                return 9
        if child_control is not None:
            child_control["proc"] = proc
            # Covers the tiny race where SIGUSR1 arrived after Popen returned but
            # before the parent had published the Popen object to the handler.
            if child_control.get("switch_signal") and proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
        state = load_json(sd / "state.json", state)
        state["interactive_child_pid"] = proc.pid
        json_dump(sd / "state.json", state)
        try:
            return_code = proc.wait()
            request = pending_profile_switch(sd)
            switch_signalled = bool(child_control and child_control.get("switch_signal"))
            if request and switch_signalled:
                # UserPromptSubmit is a safe prompt boundary: the /profile control
                # prompt itself is blocked, so no profile-dependent tool call can
                # be in flight when we stop this child.
                session_id = str(request.get("session_id") or "").strip()
                if not session_id:
                    state = load_json(sd / "state.json", state)
                    previous = str(state.get("autonomy_profile") or args.profile)
                    finish_profile_switch(
                        sd,
                        request,
                        outcome="rejected",
                        previous_profile=previous,
                        active_profile=previous,
                        reason="interactive switch lacked Claude session ID",
                    )
                    print("Profile switch rejected safely because Claude supplied no resumable session ID.")
                    return 9
                state = load_json(sd / "state.json", state)
                _apply_pending_profile_switch(
                    args,
                    state,
                    sd,
                    source_hash=state.get("active_permission_objective_hash"),
                    headless=False,
                )
                resume_session_id = session_id
                if child_control is not None:
                    child_control["switch_signal"] = False
                continue
            if switch_signalled and not request:
                print("Profile switch signal arrived without a durable request; current profile is unchanged.")
                if child_control is not None:
                    child_control["switch_signal"] = False
                return 9
        finally:
            if child_control is not None and child_control.get("proc") is proc:
                child_control["proc"] = None
            state = load_json(sd / "state.json", {})
            if int(state.get("interactive_child_pid") or 0) == proc.pid:
                state.pop("interactive_child_pid", None)
                json_dump(sd / "state.json", state)

        final_code = int(return_code or 0)
        if adoption_coordinator_root is not None:
            try:
                adoption = finish_session_adoption(
                    adoption_coordinator_root,
                    return_code=final_code,
                    state_dir=sd,
                )
            except SessionAdoptionError as exc:
                print(f"Session adoption finalisation blocked: {exc}", file=sys.stderr)
                return 9
            if (
                isinstance(adoption, dict)
                and adoption.get("lifecycle_state") == "BLOCKED"
            ):
                print(
                    str(
                        adoption.get("blocker")
                        or "session adoption remained blocked"
                    ),
                    file=sys.stderr,
                )
                return 9
        return final_code

def _systemd_escape_arg(value: str) -> str:
    return _systemd_escape_arg_impl(value)


def _service_unit_name(root: Path) -> str:
    return _service_unit_name_impl(root)


def _service_unit_path(root: Path) -> Path:
    return _service_unit_path_impl(root, home=Path.home())


def _service_unit_text(root: Path) -> str:
    return _service_unit_text_impl(root, home=Path.home())


def service_action(args: argparse.Namespace) -> int:
    return _service_action_impl(args, home=Path.home(), which=shutil.which)


def build_parser() -> argparse.ArgumentParser:
    return _build_parser(VERSION)


def _p6_migration_cli_action(args: argparse.Namespace) -> int:
    root = find_repo_root(getattr(args, "repo", None))
    sd = repo_state_dir(root)
    action = getattr(args, "migrate_command", None)

    def _git_head() -> str:
        cp = run([
            "git",
            "-C",
            str(root),
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
        ])
        if cp.returncode != 0 or not cp.stdout.strip():
            raise ValueError("migration requires a valid current Git HEAD")
        return cp.stdout.strip().lower()

    try:
        if action == "status":
            schema = preflight_state_schema(sd)
            state = load_json(sd / "state.json", {})
            if not isinstance(state, dict):
                state = {}
            adoption_path = sd / "adoption" / "current.json"
            adoption = (
                load_current_adoption(root)
                if adoption_path.is_file()
                else None
            )
            session_path = sd / "session-adoption" / "active.json"
            session = (
                load_session_adoption(root, state_dir=sd)
                if session_path.is_file()
                else None
            )
            result = {
                "status": "READY",
                "repository": str(root),
                "state_schema": schema,
                "current_state_schema": state.get("schema_version"),
                "state_migration": load_json(
                    sd / "migrations" / "active.json",
                    {},
                ) or None,
                "planning_repair_migration": load_json(
                    sd / "planning-repair" / "migration-active.json",
                    {},
                ) or None,
                "legacy_adoption": (
                    {
                        "adoption_sha256": adoption.get("adoption_sha256"),
                        "source_system": adoption.get("source_system"),
                        "source_state_id": adoption.get("source_state_id"),
                    }
                    if isinstance(adoption, dict)
                    else None
                ),
                "session_adoption": (
                    {
                        "lifecycle_state": session.get("lifecycle_state"),
                        "selector_display": session.get("selector_display"),
                        "adopted_session_id": session.get("adopted_session_id"),
                        "role": session.get("role"),
                    }
                    if isinstance(session, dict)
                    else None
                ),
                "active_planning_repair": load_active_repair(root),
            }
        elif action == "state":
            require_top_level_operator(root, "P6 state migration")
            with SupervisorLease(sd, root):
                result = migrate_state_on_disk(
                    sd,
                    repository_identity=repository_identity(root),
                    git_head=_git_head(),
                )
        elif action == "planning-repair":
            require_top_level_operator(root, "P6 planning-repair migration")
            with SupervisorLease(sd, root):
                result = migrate_legacy_planning_repair(root)
        elif action == "adopt-state":
            require_top_level_operator(root, "P6 legacy state adoption")
            with SupervisorLease(sd, root):
                # Activation performs the explicit schema migration preflight
                # and establishes current RC4 governance before claims import.
                activate(root)
                result = import_legacy_state_claims(
                    root,
                    Path(args.source),
                )
        elif action == "reattest":
            require_top_level_operator(root, "P6 accepted-task re-attestation")
            result = begin_adopted_task_reattestation(root, args.task_id)
        elif action == "adopt-active":
            require_top_level_operator(root, "P6 active-task adoption")
            result = begin_adopted_active_task(root)
        elif action == "adopt-wip":
            require_top_level_operator(root, "P6 active-task WIP adoption")
            result = adopt_active_task_primary_wip(root)
        else:
            raise ValueError(f"unsupported migrate command: {action}")
    except (
        MigrationError,
        StateAdoptionError,
        StateMigrationError,
        SessionAdoptionError,
        TaskWorkspaceError,
        TaskAcceptanceError,
        OSError,
        ValueError,
    ) as exc:
        print(json.dumps({
            "status": "BLOCKED",
            "repository": str(root),
            "error": str(exc),
        }, indent=2))
        return 2

    print(json.dumps(result, indent=2, sort_keys=True))
    return 2 if result.get("status") in {
        "BLOCKED",
        "FAIL",
        "UNVERIFIED",
        "REJECTED",
        "STALE_BASE",
        "PRIMARY_DRIFT",
    } else 0


def _p4_task_cli_action(args: argparse.Namespace) -> int:
    root = find_repo_root(getattr(args, "repo", None))
    action = getattr(args, "tasks_command", None)
    try:
        if action == "workspace-status":
            result = task_workspace_status(root)
        elif action == "abort":
            require_top_level_operator(root, "P4 task workspace abort")
            result = abort_task_workspace(
                root,
                reason=str(
                    getattr(args, "reason", None)
                    or "task workspace aborted by operator"
                ),
            )
        elif action == "begin":
            require_top_level_operator(root, "P4 task workspace begin")
            result = begin_task_workspace(
                root,
                task_id=getattr(args, "task_id", None),
            )
        elif action == "candidate":
            require_top_level_operator(root, "P4 task candidate sealing")
            result = seal_task_candidate(root)
        elif action == "verify":
            require_top_level_operator(root, "P4 task candidate verification")
            deterministic = verify_task_candidate_deterministic(
                root,
                timeout=int(getattr(args, "timeout", 900) or 900),
                trust_repo_scripts=bool(getattr(args, "trust_repo_scripts", False)),
                unrestricted_host=bool(getattr(args, "unrestricted_host", False)),
            )
            if deterministic.get("status") != "PASS":
                result = {
                    "status": deterministic.get("status") or "BLOCKED",
                    "stage": "deterministic",
                    "deterministic": deterministic,
                }
            else:
                independent = verify_task_candidate_independent(root, args)
                result = {
                    "status": independent.get("status") or "BLOCKED",
                    "stage": "independent",
                    "deterministic": deterministic,
                    "independent": independent,
                }
        elif action == "accept":
            require_top_level_operator(root, "P4 task candidate acceptance")
            result = accept_verified_task(
                root,
                remote=getattr(args, "remote", None),
                remote_branch=getattr(args, "remote_branch", None),
                expected_remote=getattr(args, "expected_remote", None),
            )
        elif action == "cleanup":
            require_top_level_operator(root, "P4 accepted task cleanup")
            result = cleanup_accepted_task_workspace(root)
        else:
            return task_action(args, find_repo_root=find_repo_root)
    except (TaskWorkspaceError, TaskAcceptanceError, OSError, ValueError) as exc:
        print(json.dumps({
            "status": "BLOCKED",
            "repository": str(root),
            "error": str(exc),
        }, indent=2))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2 if result.get("status") in {
        "BLOCKED",
        "PRESERVED_MANUAL",
        "FAIL",
        "UNVERIFIED",
        "REJECTED",
        "STALE_BASE",
        "PRIMARY_DRIFT",
    } else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor": return doctor(args)
    if args.command == "profiles": return show_profiles()
    if args.command == "global":
        if args.global_command == "status":
            print(json.dumps(user_layer_status(), indent=2)); return 0
        return user_layer_action("install" if args.global_command == "install" else "remove")
    if args.command == "inspect": return cmd_inspect(args)
    if args.command == "activate": return cmd_activate(args)
    if args.command == "setup": return cmd_setup(args)
    if args.command == "service": return service_action(args)
    if args.command == "plugins":
        root = None
        try:
            root = find_repo_root(args.repo)
        except SystemExit:
            if args.repo:
                raise
        return install_plugins(root, args.install, args.include_lsp, args.include_deferred)
    if args.command == "gateway":
        if args.gateway_command == "list": return gateway_list()
        if args.gateway_command == "doctor": return gateway_doctor(args)
        if args.gateway_command == "install": return gateway_install(args)
        if args.gateway_command in {"start", "stop", "ui"}: return gateway_service(args)
    if args.command == "permissions": return permission_action(args)
    if args.command == "profile": return profile_action(args)
    if args.command == "git-trust": return git_trust_action(args, find_repo_root=find_repo_root)
    if args.command == "promotion": return promotion_policy_action(args, find_repo_root=find_repo_root)
    if args.command == "planning-repair": return planning_repair_action(args, find_repo_root=find_repo_root)
    if args.command == "governance": return governance_action(args, find_repo_root=find_repo_root)
    if args.command == "shadow": return shadow_action(args, find_repo_root=find_repo_root)
    if args.command == "migrate": return _p6_migration_cli_action(args)
    if args.command == "tasks": return _p4_task_cli_action(args)
    if args.command == "promote-ff": return promote_ff_action(args, find_repo_root=find_repo_root)
    if args.command == "cleanup-untracked": return cleanup_untracked_action(args, find_repo_root=find_repo_root)
    if args.command == "models":
        if args.models_command == "registry": return show_model_registry()
        if args.models_command == "qualify": return qualify_model(args)
    if args.command == "start": return do_start(args)
    if args.command == "run": return do_run(args)
    if args.command == "status": return show_status(find_repo_root(args.repo))
    if args.command == "metrics": return show_metrics(find_repo_root(args.repo))
    if args.command == "reset": return reset_state(find_repo_root(args.repo), args.yes)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
