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

from provider_config import PROVIDER_PROFILES
from settings_policy import AUTONOMY_PROFILES

def add_provider_args(q: argparse.ArgumentParser, *, prefix: str = "", include_roles: bool = False, default_provider: str | None = "native") -> None:
    opt = (lambda name: "--" + prefix.replace("_", "-") + name.replace("_", "-"))
    dest = (lambda name: prefix + name)
    q.add_argument(opt("provider"), dest=dest("provider"), default=default_provider, choices=sorted(PROVIDER_PROFILES), help="Model provider/gateway profile")
    q.add_argument(opt("gateway_url"), dest=dest("gateway_url"), help="Override gateway base URL for the selected provider")
    q.add_argument(opt("gateway_token_env"), dest=dest("gateway_token_env"), help="Environment variable containing the gateway client/API token; secret value is never stored")
    q.add_argument(opt("gateway_discovery"), dest=dest("gateway_discovery"), action=argparse.BooleanOptionalAction, default=None, help="Enable/disable Claude Code gateway model discovery")
    q.add_argument(opt("isolate_provider_profile"), dest=dest("isolate_provider_profile"), action="store_true", help="Use a separate CLAUDE_CONFIG_DIR for this provider; off by default so user plugins/skills remain available")
    q.add_argument(opt("gateway_hints"), dest=dest("gateway_hints"), action=argparse.BooleanOptionalAction, default=True, help="Send Claude Code request-class/agent/compaction/prompt-id hint headers to gateways")
    if include_roles:
        q.add_argument(opt("opus_model"), dest=dest("opus_model"), help="Override the provider's Opus-class model slot")
        q.add_argument(opt("sonnet_model"), dest=dest("sonnet_model"), help="Override the provider's Sonnet-class model slot")
        q.add_argument(opt("haiku_model"), dest=dest("haiku_model"), help="Override the provider's Haiku-class model slot")
        q.add_argument(opt("subagent_model"), dest=dest("subagent_model"), help="Override all subagents with one model")
        q.add_argument(opt("verifier_model"), dest=dest("verifier_model"), help="Model ID/alias for the pack's independent verifier")
        q.add_argument(opt("researcher_model"), dest=dest("researcher_model"), help="Model ID/alias for the pack's focused researcher")


def show_profiles() -> int:
    rows = []
    for name, spec in AUTONOMY_PROFILES.items():
        rows.append({
            "name": name,
            "description": spec["description"],
            "default": name == "balanced",
            "sandbox": (
                "off (explicit unattended host authority)" if spec.get("unrestricted")
                else "off (outer isolation required)" if spec["isolated_full"]
                else "strict" if not spec["allow_unsandboxed"]
                else "sandbox-first with classifier-reviewed unsandboxed retry"
            ),
        })
    print(json.dumps({"profiles": rows}, indent=2))
    return 0

def build_parser(version: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="claude-auto", description="Universal autonomous Claude Code optimisation harness")
    p.add_argument("--version", action="version", version=f"%(prog)s {version}")
    sp = p.add_subparsers(dest="command", required=True)

    q = sp.add_parser("doctor", help="Check Claude Code, sandbox prerequisites and effective headless-session blockers")
    q.add_argument("--repo", help="Optional repository to audit user/project/local settings against")
    sp.add_parser("profiles", help="Show available autonomy profiles and their safety posture")

    q = sp.add_parser("global", help="Manage the always-on user-level Claude Code optimisation layer")
    gsp = q.add_subparsers(dest="global_command", required=True)
    gsp.add_parser("status", help="Show whether global user rules/agents/skill/hook are installed")
    gsp.add_parser("install", help="Install/update the global user layer without touching repositories")
    gsp.add_parser("remove", help="Remove only the package-owned global user layer")

    q = sp.add_parser("inspect", help="Profile a repository without modifying it")
    q.add_argument("--repo", help="Repository path; defaults to current repo")

    q = sp.add_parser("activate", help="Create/update external per-repo autonomy state")
    q.add_argument("--repo")
    q.add_argument("--dry-run", action="store_true")

    q = sp.add_parser("setup", help="Optionally pre-profile a repo; not required when the global user layer is installed")
    q.add_argument("--repo", help="Repository path; defaults to current repo")
    q.add_argument("--install-plugins", action="store_true", help="Also install stable core plugins at user scope")
    q.add_argument("--include-lsp", action="store_true", help="Also install detected official LSP candidates despite current upstream compatibility caveat")
    q.add_argument("--include-deferred", action="store_true", help="Also install deferred plugin candidates with known upstream issues")
    q.add_argument("--dry-run", action="store_true", help="Show what would be prepared without writing external state")

    q = sp.add_parser("plugins", help="Recommend or install user-scope plugins; works globally and can add repo-specific LSP candidates")
    q.add_argument("--repo")
    q.add_argument("--recommend", action="store_true", help="Recommendation only (default)")
    q.add_argument("--install", action="store_true", help="Install stable core plugins at user scope")
    q.add_argument("--include-lsp", action="store_true", help="Also install detected official LSP candidates despite current upstream compatibility caveat")
    q.add_argument("--include-deferred", action="store_true", help="Also install deferred plugin candidates with known upstream issues")

    q = sp.add_parser("start", help="Start an interactive Claude Code session with the autonomy profile")
    q.add_argument("--repo")
    q.add_argument("--objective")
    q.add_argument("--model")
    q.add_argument("--effort", default="medium", choices=["low", "medium", "high", "max"])
    q.add_argument("--profile", default=None, choices=sorted(AUTONOMY_PROFILES), help="Autonomy posture; unattended must be explicitly selected (or restored via --resume-config); other profiles may reuse the repository's remembered selection")
    q.add_argument("--permission-mode", default="auto", choices=["default", "acceptEdits", "plan", "auto", "dontAsk"])
    q.add_argument("--memory-mode", default="external", choices=["external", "hybrid"], help="external uses only compact harness checkpoints; hybrid also enables Claude auto memory")
    q.add_argument("--session-settings", choices=["compatibility", "hermetic"], default=None, help="Settings-source policy. Interactive start defaults to compatibility except unattended, which is hermetic. Select explicitly to override.")
    add_provider_args(q, include_roles=True)

    q = sp.add_parser("run", help="Run autonomous work until complete/blocked/limited")
    q.add_argument("--repo")
    q.add_argument("--objective")
    q.add_argument("--objective-file", help="Read the original objective/specification from a file")
    q.add_argument("--plan", dest="plan_file", help="Candidate implementation plan. It is analysed, simulated and red-teamed before implementation; it is never trusted blindly.")
    q.add_argument("--model")
    q.add_argument("--effort", default="medium", choices=["low", "medium", "high", "max"])
    q.add_argument("--profile", default=None, choices=sorted(AUTONOMY_PROFILES), help="Autonomy posture; unattended must be explicitly selected (or restored via --resume-config); other profiles may reuse the repository's remembered selection")
    q.add_argument("--permission-mode", default="auto", choices=["default", "acceptEdits", "auto", "dontAsk"])
    q.add_argument("--memory-mode", default="external", choices=["external", "hybrid"], help="external uses compact deterministic checkpoints; hybrid also enables Claude auto memory")
    q.add_argument("--session-settings", choices=["compatibility", "hermetic"], default=None, help="Settings-source policy. Headless Balanced/Isolated Full/Unattended default to hermetic so inherited user/project/local hooks and sandbox rules cannot deadlock autonomous execution; select compatibility explicitly to retain them.")
    q.add_argument("--resume-config", action="store_true", help="Resume using the last sanitised durable run configuration for this repository (used by the optional systemd service)")
    q.add_argument("--model-qualification", choices=["auto", "required", "off"], default="auto", help="Explicit model-route preflight: auto qualifies unseen routes (default), required refuses until prequalified, off is an explicit expert override")
    q.add_argument("--max-cycles", type=int, default=0, help="Maximum native /goal repair rounds; 0 (default) continues until COMPLETE/BLOCKED/circuit-breaker")
    q.add_argument("--max-plan-revisions", type=int, default=3, help="Maximum plan revise + simulate + red-team attempts before failing closed")
    q.add_argument("--max-stagnant-cycles", type=int, default=3, help="Stop after this many identical repository/plan/evidence repair rounds; 0 disables")
    q.add_argument("--max-turns", type=int, default=60, help="Hard Claude worker-turn cap per invocation")
    q.add_argument("--max-budget-usd", type=float, default=None, help="Hard Claude Code spend cap per invocation (print mode)")
    q.add_argument("--cycle-timeout", type=int, default=0, help="Wall-clock seconds per Claude invocation; timeout becomes a recoverable checkpoint; 0 means no wrapper timeout")
    q.add_argument("--verification-timeout", type=int, default=900, help="Seconds allowed for each supervisor-owned deterministic verification command")
    q.add_argument("--trust-repo-scripts", action="store_true", help="Explicitly permit supervisor verification/scanners to run repository-controlled code on the host when neither srt nor the Linux bubblewrap fallback is available; environment is still scrubbed. Incompatible with Strict.")
    q.add_argument("--runtime-verify", choices=["auto", "off"], default="auto", help="Attempt native /verify for repositories with runnable-application signals; unavailable/insufficient evidence is non-blocking")
    q.add_argument("--security-scanners", choices=["auto", "off"], default="auto", help="Run supported installed deterministic security scanners before final security review")
    q.add_argument("--security-scanner-timeout", type=int, default=600, help="Seconds allowed for each installed deterministic security scanner")
    q.add_argument("--max-total-turns", type=int, default=0, help="Global reported-turn circuit breaker across rounds; 0 disables")
    q.add_argument("--max-wall-seconds", type=int, default=0, help="Global accumulated worker wall-time circuit breaker; 0 disables")
    q.add_argument("--max-total-budget-usd", type=float, default=None, help="Global accumulated Claude cost circuit breaker across rounds")
    q.add_argument("--max-transient-retries", type=int, default=12, help="Automatic provider-error retries before returning WAITING_RETRYABLE_LIMIT; 0 means unlimited")
    q.add_argument("--retry-backoff-seconds", type=float, default=5.0, help="Initial transient-provider retry delay")
    q.add_argument("--retry-backoff-cap-seconds", type=float, default=300.0, help="Maximum transient-provider retry delay")
    q.add_argument("--pause-seconds", type=float, default=0.0)
    q.add_argument("--retain-transcripts", action="store_true", help="Persist full redacted Claude stdout/stderr; default stores compact telemetry/tails only")
    add_provider_args(q, include_roles=True)
    q.add_argument("--fallback-model", help="Same-provider native fallback model/chain, or model used by an explicitly different fallback provider")
    add_provider_args(q, prefix="fallback_", include_roles=True, default_provider=None)
    q.add_argument("--challenger-policy", choices=["off", "final", "each-cycle"], default="off", help="Independent hard-read-only challenger; final is recommended")
    q.add_argument("--challenger-model", help="Model ID/alias for the external challenger")
    add_provider_args(q, prefix="challenger_", include_roles=False, default_provider="native")

    q = sp.add_parser("gateway", help="List, diagnose, install, or manage optional Claude Code gateways")
    gsp = q.add_subparsers(dest="gateway_command", required=True)
    gsp.add_parser("list", help="List built-in provider/gateway profiles")
    g = gsp.add_parser("doctor", help="Check one gateway profile; /v1/models is informational and --probe-model verifies actual Claude Code request compatibility")
    add_provider_args(g)
    g.add_argument("--no-network", action="store_true", help="Skip live discovery probe")
    g.add_argument("--probe-model", help="Send a one-turn no-tools Claude Code request through the gateway using this model ID")
    g.add_argument("--timeout", type=int, default=30, help="Seconds for the actual harness probe")
    g = gsp.add_parser("install", help="Install optional local gateway software")
    g.add_argument("provider", choices=["ccr", "litellm", "native", "openrouter", "custom"])
    g.add_argument("--yes", action="store_true", help="Actually install; otherwise show the command only")
    for action in ("start", "stop", "ui"):
        g = gsp.add_parser(action, help=f"{action.title()} a managed local gateway service")
        g.add_argument("provider", choices=["ccr", "litellm"])
        g.set_defaults(action=action)

    q = sp.add_parser("models", help="Qualify alternate models or inspect the model registry")
    msp = q.add_subparsers(dest="models_command", required=True)
    msp.add_parser("registry", help="Show recorded model compatibility qualifications")
    m = msp.add_parser("qualify", help="Qualify a model/provider lane for read-only, Auto execution, or full autonomous operation")
    m.add_argument("--repo")
    m.add_argument("--model", help="Model ID/alias; omit only for native provider to qualify Claude Code default routing")
    m.add_argument("--effort", default="medium", choices=["low", "medium", "high", "max"])
    m.add_argument("--max-turns", type=int, default=24)
    m.add_argument("--level", choices=["readonly", "auto", "full"], default="full", help="Qualification depth; full is required for autonomous main routes")
    m.add_argument("--timeout", type=int, default=0, help="Seconds; 0 means no subprocess timeout")
    m.add_argument("--max-budget-usd", type=float, help="Maximum spend per qualification Claude call")
    m.add_argument("--max-total-budget-usd", type=float, help="Maximum aggregate spend for this qualification run")
    add_provider_args(m)

    q = sp.add_parser("service", help="Manage an optional user-level systemd resume service for one repository")
    ssp = q.add_subparsers(dest="service_action", required=True)
    for action in ("install", "status", "remove"):
        sq = ssp.add_parser(action)
        sq.add_argument("--repo")
        if action == "install":
            sq.add_argument("--objective", help="Seed/update the durable objective before enabling the service")
            sq.add_argument("--start", action="store_true", help="Enable and start the user service immediately")

    q = sp.add_parser("cleanup-untracked", help="Delete one proven-recoverable untracked file without broad rm authority")
    q.add_argument("--repo", help="Repository path; defaults to current repo")
    q.add_argument("--path", required=True, help="Repository-relative untracked regular file")
    q.add_argument("--match-commit", required=True, help="Descendant commit containing the byte-identical file at the same path")

    q = sp.add_parser("promote-ff", help="Exact local/optional-remote fast-forward promotion that preserves existing WIP")
    q.add_argument("--repo", help="Repository path; defaults to current repo")
    q.add_argument("--sha", required=True, help="Exact descendant commit SHA to promote")
    q.add_argument("--attestation-contract", help="Require an exact-SHA VERIFIED attestation under this contract")
    q.add_argument("--remote", help="Optional remote name/URL for remote-aware promotion")
    q.add_argument("--remote-branch", help="Remote branch name; defaults to the current local branch")
    q.add_argument("--expected-remote-sha", help="Required exact remote base SHA when --remote is used")

    q = sp.add_parser("git-trust", help="Manage package-owned trusted Git compatibility configuration")
    gsp = q.add_subparsers(dest="git_trust_command", required=True)
    gq = gsp.add_parser("status", help="Show the effective trusted Git configuration")
    gq.add_argument("--repo")
    gq = gsp.add_parser("set-excludes", help="Register an operator-owned excludes file outside the repository")
    gq.add_argument("--repo")
    gq.add_argument("--path", required=True)
    gq = gsp.add_parser("clear-excludes", help="Remove the registered trusted excludes file")
    gq.add_argument("--repo")

    q = sp.add_parser("promotion", help="Manage exact-SHA promotion-attestation policy")
    pspromo = q.add_subparsers(dest="promotion_command", required=True)
    pq = pspromo.add_parser("status", help="Show promotion policy and optional exact-SHA attestation")
    pq.add_argument("--repo")
    pq.add_argument("--sha")
    pq = pspromo.add_parser("require-contract", help="Require VERIFIED exact-SHA attestations for protected promotion")
    pq.add_argument("--repo")
    pq.add_argument("--contract", required=True)
    pq = pspromo.add_parser("clear-contract", help="Disable repository promotion-attestation requirement")
    pq.add_argument("--repo")

    q = sp.add_parser("planning-repair", help="Manage repository-owned canonical planning repair")
    prsp = q.add_subparsers(dest="planning_repair_command", required=True)
    pr = prsp.add_parser("status", help="Show planning-repair policy and active worktree state")
    pr.add_argument("--repo")
    pr = prsp.add_parser("configure", help="Bind a tracked canonical plan to the protected repair workflow")
    pr.add_argument("--repo")
    pr.add_argument("--plan", required=True, help="Repository-relative tracked canonical plan path")
    pr.add_argument("--product-branch", help="Canonical product branch; defaults to current branch")
    pr.add_argument("--remote", help="Optional durable remote used for protected promotion")
    pr.add_argument("--remote-branch", help="Remote canonical branch; defaults to product branch")
    pr = prsp.add_parser("begin", help="Create or recover the dedicated planning repair worktree")
    pr.add_argument("--repo")
    pr.add_argument("--reason", default="", help="Concrete planning defect/reconciliation reason")
    pr = prsp.add_parser("architect", help="Run the dedicated one-plan Planning Repair Architect")
    pr.add_argument("--repo")
    pr.add_argument("--reason", default="", help="Concrete planning defect/reconciliation reason")
    pr.add_argument("--model")
    pr.add_argument("--timeout", type=int, default=0)
    pr.add_argument("--max-turns", type=int, default=40)
    pr.add_argument("--max-budget-usd", type=float)
    add_provider_args(pr)
    pr = prsp.add_parser("verify", help="Independently verify and attest the exact repair candidate SHA")
    pr.add_argument("--repo")
    pr.add_argument("--sha", help="Exact candidate SHA; defaults to active candidate")
    pr.add_argument("--model")
    pr.add_argument("--timeout", type=int, default=0)
    pr.add_argument("--max-turns", type=int, default=35)
    pr.add_argument("--max-budget-usd", type=float)
    add_provider_args(pr)
    pr = prsp.add_parser("refresh-base", help="Reconcile/rebase an active planning repair onto the latest product base")
    pr.add_argument("--repo")
    pr = prsp.add_parser("promote", help="Promote only the exact independently VERIFIED active candidate")
    pr.add_argument("--repo")
    pr = prsp.add_parser("abort", help="Remove the dedicated repair worktree/branch without touching product work")
    pr.add_argument("--repo")

    q = sp.add_parser("profile", help="Inspect or request a human-controlled hot profile switch")
    psp = q.add_subparsers(dest="profile_command", required=True)
    pq = psp.add_parser("status", help="Show the active profile and any pending switch")
    pq.add_argument("--repo")
    pq = psp.add_parser("request", help="Queue a profile switch for the live autonomous supervisor")
    pq.add_argument("target_profile", choices=["strict", "balanced", "unattended"])
    pq.add_argument("--repo")

    q = sp.add_parser("permissions", help="Inspect or decide a durable permission-escalation request")
    psp = q.add_subparsers(dest="permissions_command", required=True)
    for action in ("status", "history"):
        pq = psp.add_parser(action)
        pq.add_argument("--repo")
    pq = psp.add_parser("approve", help="Approve the currently pending request")
    pq.add_argument("--repo")
    pq.add_argument("--id", help="Expected request ID; refuses if it does not match the pending request")
    pq.add_argument("--scope", choices=["once", "run", "repository"], default="once")
    pq = psp.add_parser("deny", help="Deny the currently pending request")
    pq.add_argument("--repo")
    pq.add_argument("--id", help="Expected request ID; refuses if it does not match the pending request")
    pq.add_argument("--reason", help="Optional user reason recorded in the audit ledger")
    pq = psp.add_parser("revoke", help="Revoke active grants by request ID or capability")
    pq.add_argument("--repo")
    pq.add_argument("--id", help="Revoke active grants created from this request ID")
    pq.add_argument("--capability", choices=[
        "native-permissions", "outside-repository", "container-host-authority",
        "secret-read", "host-repository-execution", "unrestricted",
    ])

    q = sp.add_parser("status", help="Show compact durable state for a repository")
    q.add_argument("--repo")

    q = sp.add_parser("metrics", help="Aggregate available token/cache/cost telemetry from supervisor JSON logs")
    q.add_argument("--repo")

    q = sp.add_parser("reset", help="Remove only the pack's external state for a repository")
    q.add_argument("--repo")
    q.add_argument("--yes", action="store_true")
    return p
