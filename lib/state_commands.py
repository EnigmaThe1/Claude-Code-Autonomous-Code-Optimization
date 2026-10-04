from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import asdict
from pathlib import Path

from operator_tools import install_plugins
from protocols import extract_result_json
from provider_config import load_model_registry
from repo_identity import SupervisorLease, find_repo_root, repo_state_dir
from repo_profile import BASE_PLUGINS, profile_repo
from repo_runtime import activate
from state_store import load_json
from telemetry import usage_from_result

def show_model_registry() -> int:
    print(json.dumps(load_model_registry(), indent=2))
    return 0


def show_metrics(root: Path) -> int:
    sd = repo_state_dir(root)
    log_dir = sd / "logs"
    if not log_dir.exists():
        print(f"No supervisor logs exist for {root}. Run `claude-auto run` first.")
        return 1
    totals: dict[str, float] = {}
    by_route: dict[str, dict[str, float]] = {}
    logs = 0
    measured = 0
    plan_dir = sd / "plans"
    plan_paths: set[Path] = set()
    if plan_dir.exists():
        for pattern in (
            "planner-v*.json", "simulation-v*.json", "redteam-v*.json",
            "phase-*-cycle-*.json", "remediation-*-cycle-*.json", "final-*-cycle-*.json",
        ):
            plan_paths.update(plan_dir.glob(pattern))
    paths = sorted(set(log_dir.glob("goal-*.json")) | set(log_dir.glob("cycle-*.json")) | plan_paths)
    for path in paths:
        # Challenger logs are counted separately by route but still contribute to
        # overall usage; their compact JSON already exposes `usage`.
        log = load_json(path, {})
        if not isinstance(log, dict):
            continue
        logs += 1
        u: dict[str, float] = {}
        if isinstance(log.get("usage"), dict):
            for key, value in log["usage"].items():
                if isinstance(value, (int, float)):
                    u[key] = float(value)
        if not u and isinstance(log.get("meta"), dict) and isinstance(log["meta"].get("usage"), dict):
            for key, value in log["meta"]["usage"].items():
                if isinstance(value, (int, float)):
                    u[key] = float(value)
        if not u:
            obj = None
            stdout = log.get("stdout", "")
            if isinstance(stdout, str) and stdout:
                _, _, obj = extract_result_json(stdout)
            if obj is None and isinstance(log.get("meta"), dict) and isinstance(log["meta"].get("raw"), dict):
                obj = log["meta"]["raw"]
            u = usage_from_result(obj)
        if u:
            measured += 1
        provider_obj = log.get("provider") if isinstance(log.get("provider"), dict) else {}
        if not provider_obj and isinstance(log.get("meta"), dict) and isinstance(log["meta"].get("provider"), dict):
            provider_obj = log["meta"]["provider"]
        provider_name = provider_obj.get("provider") or "unknown"
        model_name = log.get("model") or (log.get("meta", {}).get("model") if isinstance(log.get("meta"), dict) else None) or "default"
        if "security-review" in path.name:
            suffix = ":security-review"
        elif "challenger" in path.name:
            suffix = ":challenger"
        elif path.parent == plan_dir:
            kind = "planner" if path.name.startswith("planner-") else "simulation" if "simulation" in path.name else "redteam"
            suffix = f":plan-{kind}"
        else:
            suffix = ""
        route = f"{provider_name}:{model_name}{suffix}"
        bucket = by_route.setdefault(route, {})
        for key, value in u.items():
            totals[key] = totals.get(key, 0.0) + value
            bucket[key] = bucket.get(key, 0.0) + value
    token_keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
    token_total = sum(totals.get(k, 0.0) for k in token_keys)
    cache_read = totals.get("cache_read_input_tokens", 0.0)
    cache_denom = totals.get("input_tokens", 0.0) + totals.get("cache_creation_input_tokens", 0.0) + cache_read
    route_report = {}
    for route, vals in sorted(by_route.items()):
        route_report[route] = {
            "tokens": {k: int(vals.get(k, 0.0)) for k in token_keys},
            "estimated_cost_usd": round(vals.get("total_cost_usd", 0.0), 6) if "total_cost_usd" in vals else None,
            "turns": int(vals.get("num_turns", 0.0)) if "num_turns" in vals else None,
        }
    report = {
        "repo": str(root),
        "state_dir": str(sd),
        "logs": logs,
        "logs_with_usage": measured,
        "tokens": {k: int(totals.get(k, 0.0)) for k in token_keys},
        "token_events_total": int(token_total),
        "cache_read_share": round(cache_read / cache_denom, 4) if cache_denom else None,
        "estimated_cost_usd": round(totals.get("total_cost_usd", 0.0), 6) if "total_cost_usd" in totals else None,
        "turns": int(totals.get("num_turns", 0.0)) if "num_turns" in totals else None,
        "duration_seconds": round(totals.get("duration_ms", 0.0) / 1000.0, 3) if "duration_ms" in totals else None,
        "by_provider_model": route_report,
        "note": "Fields are reported only when Claude Code JSON results expose them. Full transcripts are not retained unless explicitly requested.",
    }
    print(json.dumps(report, indent=2))
    return 0

def show_status(root: Path) -> int:
    sd = repo_state_dir(root)
    if not (sd / "state.json").exists():
        print(f"No autonomy state exists for {root}. Run `claude-auto activate` first.")
        return 1
    print(json.dumps(load_json(sd / "state.json", {}), indent=2))
    return 0


def reset_state(root: Path, yes: bool) -> int:
    sd = repo_state_dir(root)
    if not sd.exists():
        print("No state to reset.")
        return 0
    if not yes:
        print(f"Would remove external autonomy state only: {sd}")
        print("Re-run with --yes. The repository itself will not be modified.")
        return 0
    # State deletion is itself a durable-state mutation.  The outer lease guard
    # lives outside sd, so the directory can be removed atomically with respect to
    # every other Claude Auto writer without deleting the fence that protects the reset.
    with SupervisorLease(sd, root):
        shutil.rmtree(sd)
    print(f"Removed {sd}")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    root = find_repo_root(args.repo)
    p = profile_repo(root)
    print(json.dumps(asdict(p), indent=2))
    return 0


def cmd_activate(args: argparse.Namespace) -> int:
    root = find_repo_root(args.repo)
    if args.dry_run:
        activate(root, True)
        return 0
    sd = repo_state_dir(root)
    with SupervisorLease(sd, root):
        activate(root)
    print(f"Activated repository without modifying it:\n  repo:  {root}\n  state: {sd}")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    root = find_repo_root(args.repo)
    if args.dry_run:
        prof = profile_repo(root)
        print(json.dumps({
            "repo": str(root),
            "would_activate_external_state": str(repo_state_dir(root)),
            "languages": prof.languages,
            "recommended_plugins": prof.recommended_plugins,
            "lsp_candidates": prof.lsp_candidates,
            "repository_writes": [],
        }, indent=2))
        return 0

    sd = repo_state_dir(root)
    with SupervisorLease(sd, root):
        activate(root)
    prof = load_json(sd / "profile.json", {})
    print(f"Repository discovered: {root}")
    print(f"External state:       {sd}")
    print("Repository writes:    NONE")
    langs = prof.get("languages", [])
    print("Detected stack:       " + (", ".join(langs) if langs else "no language signal detected"))
    plugins = prof.get("recommended_plugins", BASE_PLUGINS)
    print("Core plugins:         " + ", ".join(plugins))
    lsp = prof.get("lsp_candidates", [])
    if lsp:
        print("LSP candidates:       " + ", ".join(lsp) + " (explicit opt-in; upstream issue)")
    if args.install_plugins:
        return install_plugins(root, True, args.include_lsp, args.include_deferred)
    print("\nPre-profile complete. Global plugins were not changed. Use `claude-auto plugins --install` if wanted.")
    return 0
