from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import subprocess
import sys
from typing import Any
from pathlib import Path

from repo_identity import SupervisorLease, find_repo_root, repo_state_dir
from runtime_paths import utcnow
from state_store import json_dump, load_json
from telemetry import redact_text

CAPABILITIES = {
    "native-permissions",
    "outside-repository",
    "container-host-authority",
    "secret-read",
    "host-repository-execution",
    "unrestricted",
}
APPROVAL_SCOPES = {"once", "run", "repository"}
WORKER_PERMISSION_CAPABILITIES = {
    "native-permissions",
    "outside-repository",
    "container-host-authority",
    "secret-read",
    "unrestricted",
}
CONSTRAINED_CAPABILITIES = {
    "outside-repository",
    "container-host-authority",
    "secret-read",
    "host-repository-execution",
}


def _redact_structure(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _redact_structure(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_structure(v) for v in value[:100]]
    if isinstance(value, str):
        return redact_text(value, None, 4000)
    return value


def _constraints_from_runtime(latest: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(latest, dict):
        return {}
    tool = str(latest.get("tool_name") or "")
    ti = latest.get("tool_input")
    if not isinstance(ti, dict):
        return {}
    out: dict[str, Any] = {}
    file_paths: list[str] = []
    for key in ("file_path", "path", "notebook_path"):
        value = ti.get(key)
        if isinstance(value, str) and value.strip():
            file_paths.append(value.strip()[:2000])
    if file_paths:
        out["file_paths"] = sorted(set(file_paths))
    command = ti.get("command")
    if isinstance(command, dict):
        digest = str(command.get("sha256") or "").lower()
        if len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest):
            out["command_sha256"] = [digest]
        executable = str(command.get("executable") or "").strip()
        if executable:
            out["executables"] = [executable[:300]]
    if tool.startswith("mcp__"):
        out["tool_names"] = [tool]
    server = str(latest.get("mcp_server") or "").strip()
    if server:
        out["mcp_servers"] = [server[:300]]
    return out


def _grant_has_enforceable_constraints(grant: dict[str, Any]) -> bool:
    capability = str(grant.get("capability") or "")
    if capability not in CONSTRAINED_CAPABILITIES:
        return True
    constraints = grant.get("constraints")
    if not isinstance(constraints, dict):
        return False
    if capability == "outside-repository":
        return bool(constraints.get("file_paths") or constraints.get("command_sha256"))
    if capability == "secret-read":
        return bool(constraints.get("file_paths"))
    if capability == "container-host-authority":
        return bool(constraints.get("command_sha256"))
    if capability == "host-repository-execution":
        return bool(constraints.get("verification_commands"))
    return False


def worker_permission_overrides(capabilities: set[str] | list[str] | tuple[str, ...] | None) -> set[str]:
    return set(capabilities or ()) & WORKER_PERMISSION_CAPABILITIES


def worker_permission_grants(grants: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        grant for grant in (grants or [])
        if isinstance(grant, dict)
        and str(grant.get("capability") or "") in WORKER_PERMISSION_CAPABILITIES
    ]


def verification_command_authorized(
    grants: list[dict[str, Any]] | None,
    command: str,
) -> bool:
    wanted = command.strip()
    for grant in grants or []:
        if not isinstance(grant, dict) or grant.get("capability") != "host-repository-execution":
            continue
        constraints = grant.get("constraints")
        if not isinstance(constraints, dict):
            continue
        commands = constraints.get("verification_commands")
        if isinstance(commands, list) and wanted in {
            str(item).strip() for item in commands if isinstance(item, str)
        }:
            return True
    return False


def _constraint_values(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {str(item).strip() for item in value if isinstance(item, str) and str(item).strip()}


def grant_covers_request(grant: dict[str, Any], request: dict[str, Any]) -> bool:
    grant_cap = str(grant.get("capability") or "")
    req_cap = str(request.get("capability") or "")
    if grant_cap == "unrestricted":
        return True
    if grant_cap != req_cap:
        return False
    if req_cap not in CONSTRAINED_CAPABILITIES:
        return True
    gcons = grant.get("constraints")
    rcons = request.get("constraints")
    if not isinstance(gcons, dict) or not isinstance(rcons, dict):
        return False
    keys = {
        "outside-repository": ("file_paths", "command_sha256"),
        "secret-read": ("file_paths",),
        "container-host-authority": ("command_sha256",),
        "host-repository-execution": ("verification_commands",),
    }.get(req_cap, ())
    saw = False
    for key in keys:
        requested = _constraint_values(rcons.get(key))
        if not requested:
            continue
        saw = True
        if not requested.issubset(_constraint_values(gcons.get(key))):
            return False
    return saw


def request_already_authorized(
    grants: list[dict[str, Any]] | None,
    request: dict[str, Any],
) -> bool:
    return any(
        grant_covers_request(grant, request)
        for grant in (grants or [])
        if isinstance(grant, dict)
    )


def _expire_unenforceable_legacy_grants(state: dict[str, Any], reason: str) -> bool:
    grants = state.get("permission_grants")
    if not isinstance(grants, list):
        return False
    now = utcnow()
    changed = False
    for grant in grants:
        if not isinstance(grant, dict):
            continue
        if grant.get("revoked_at") or grant.get("expired_at"):
            continue
        capability = str(grant.get("capability") or "")
        if capability in CONSTRAINED_CAPABILITIES and not _grant_has_enforceable_constraints(grant):
            grant["expired_at"] = now
            grant["expired_reason"] = (
                "Claude Auto requires an exact enforceable resource constraint; "
                "this legacy grant must be re-approved from a fresh request. "
                + reason
            )
            changed = True
    return changed


def _request_snapshot(request: dict[str, Any]) -> dict[str, Any]:
    """Return the durable, redacted permission-request evidence."""
    fields = (
        "id", "capability", "operation", "resource", "why_needed", "risk",
        "safer_alternative", "observed_tool_name", "observed_tool_input",
        "constraints", "profile", "objective_hash", "repo_root", "requested_at",
    )
    out: dict[str, Any] = {}
    for key in fields:
        value = request.get(key)
        if isinstance(value, str):
            out[key] = redact_text(value, None, 4000)
        else:
            out[key] = _redact_structure(value)
    return out


def record_permission_request(state: dict[str, Any], request: dict[str, Any]) -> bool:
    """Persist a complete request once so later decisions retain their rationale."""
    requests = state.setdefault("permission_requests", [])
    if not isinstance(requests, list):
        requests = []
        state["permission_requests"] = requests
    rid = request.get("id")
    objective_hash = request.get("objective_hash")
    if any(
        isinstance(item, dict)
        and item.get("id") == rid
        and item.get("objective_hash") == objective_hash
        for item in requests
    ):
        return False
    requests.append(_request_snapshot(request))
    return True


def _supersede_pending(
    state: dict[str, Any],
    *,
    reason: str,
) -> bool:
    pending = state.get("pending_permission_request")
    if not isinstance(pending, dict):
        return False
    record_permission_request(state, pending)
    state.setdefault("permission_decisions", []).append({
        "request_id": pending.get("id"),
        "capability": pending.get("capability"),
        "decided_at": utcnow(),
        "decision": "superseded",
        "reason": reason,
        "request": _request_snapshot(pending),
    })
    state["pending_permission_request"] = None
    return True


def reset_permission_epoch(
    state: dict[str, Any],
    *,
    new_objective_hash: str | None,
    reason: str,
    unattended: bool = False,
) -> bool:
    """Move permission state to a new objective/source epoch.

    Repository grants intentionally persist. Once/run grants are objective-bound,
    and a pending request must never survive into a different source epoch or an
    explicitly unattended run.
    """
    changed = _expire_unenforceable_legacy_grants(
        state,
        "permission state was reconciled",
    )
    previous = state.get("active_permission_objective_hash")
    epoch_changed = bool(previous and new_objective_hash and previous != new_objective_hash)
    epoch_initialized = previous is None and new_objective_hash is not None
    pending = state.get("pending_permission_request")
    pending_hash = pending.get("objective_hash") if isinstance(pending, dict) else None
    pending_stale = bool(
        isinstance(pending, dict)
        and new_objective_hash
        and pending_hash
        and pending_hash != new_objective_hash
    )
    reset_to_unknown = new_objective_hash is None

    if epoch_changed or pending_stale or reset_to_unknown or unattended:
        if _supersede_pending(
            state,
            reason=(
                "superseded by explicit unattended authority"
                if unattended and not (epoch_changed or pending_stale)
                else reason
            ),
        ):
            changed = True

    if epoch_changed or epoch_initialized or new_objective_hash is None:
        now = utcnow()
        grants = state.get("permission_grants")
        if isinstance(grants, list):
            for grant in grants:
                if not isinstance(grant, dict):
                    continue
                if grant.get("scope") not in {"once", "run"}:
                    continue
                if grant.get("revoked_at") or grant.get("expired_at"):
                    continue
                if new_objective_hash is not None and grant.get("objective_hash") == new_objective_hash:
                    continue
                grant["expired_at"] = now
                grant["expired_reason"] = reason
                changed = True

    if state.get("active_permission_objective_hash") != new_objective_hash:
        state["active_permission_objective_hash"] = new_objective_hash
        changed = True
    if changed:
        state["updated_at"] = utcnow()
    return changed


def active_permission_grants(
    state: dict[str, Any],
    objective_hash: str | None,
) -> list[dict[str, Any]]:
    grants = state.get("permission_grants")
    if not isinstance(grants, list):
        return []
    out: list[dict[str, Any]] = []
    for grant in grants:
        if not isinstance(grant, dict):
            continue
        if grant.get("revoked_at") or grant.get("expired_at"):
            continue
        scope = grant.get("scope")
        if scope == "once" and grant.get("consumed_at"):
            continue
        if scope in {"once", "run"}:
            if not objective_hash:
                continue
            if grant.get("objective_hash") != objective_hash:
                continue
        if str(grant.get("capability") or "") not in CAPABILITIES:
            continue
        if not _grant_has_enforceable_constraints(grant):
            continue
        out.append(grant)
    return out


def _request_id(payload: dict[str, Any]) -> str:
    material = {
        key: payload.get(key)
        for key in (
            "capability", "operation", "resource", "why_needed", "risk",
            "safer_alternative", "observed_tool_name", "observed_tool_input",
            "constraints", "objective_hash",
        )
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:20]


def _infer_capability(reason: str, runtime_events: list[dict[str, Any]]) -> str:
    text = reason.lower()
    latest = next(
        (x for x in reversed(runtime_events) if x.get("event") == "PermissionDenied"),
        None,
    )
    if latest:
        text += " " + str(latest.get("reason") or "").lower()
        text += " " + str(latest.get("tool_name") or "").lower()
        text += " " + json.dumps(latest.get("tool_input") or {}, sort_keys=True, default=str).lower()
    if "safe repository execution boundary" in text or "sandbox unavailable" in text:
        return "host-repository-execution"
    if "docker" in text or "container" in text or "privileged" in text:
        return "container-host-authority"
    if "outside" in text and ("repository" in text or "workspace" in text):
        return "outside-repository"
    if any(x in text for x in ("secret", "credential", ".env", ".ssh", ".aws", ".kube")):
        return "secret-read"
    return "native-permissions"


def build_permission_request(
    explicit: dict[str, Any] | None,
    *,
    reason: str,
    runtime_events: list[dict[str, Any]],
    profile: str,
    objective_hash: str,
    repo_root: str | None = None,
) -> dict[str, Any]:
    explicit = explicit or {}
    explicit_capability = str(explicit.get("capability") or "").strip().lower()
    latest = next(
        (x for x in reversed(runtime_events) if x.get("event") == "PermissionDenied"),
        None,
    )
    runtime_capability = _infer_capability(reason, runtime_events) if latest else None
    if runtime_capability and runtime_capability != "native-permissions":
        capability = runtime_capability
    elif explicit_capability in CAPABILITIES:
        capability = explicit_capability
    else:
        capability = runtime_capability or _infer_capability(reason, runtime_events)

    tool = str((latest or {}).get("tool_name") or "")
    tool_input = (latest or {}).get("tool_input")
    observed_constraints = _constraints_from_runtime(latest)
    explicit_constraints = explicit.get("constraints")
    constraints = (
        observed_constraints
        if observed_constraints
        else _redact_structure(explicit_constraints) if isinstance(explicit_constraints, dict) else {}
    )
    if not constraints:
        resource_hint = str(explicit.get("resource") or "").strip()
        if capability in {"outside-repository", "secret-read"} and resource_hint:
            try:
                candidate = Path(resource_hint).expanduser()
                if candidate.is_absolute():
                    constraints = {"file_paths": [str(candidate.resolve(strict=False))]}
            except (OSError, RuntimeError, ValueError):
                pass
        elif capability == "container-host-authority" and resource_hint:
            constraints = {
                "command_sha256": [hashlib.sha256(resource_hint.encode()).hexdigest()]
            }
    operation = str(explicit.get("operation") or tool or "restricted operation").strip()[:2000]
    resource = str(
        explicit.get("resource")
        or (json.dumps(tool_input, sort_keys=True) if tool_input else "")
    ).strip()[:2000]
    why = str(
        explicit.get("why_needed")
        or reason
        or "The current autonomy policy blocked an operation required to continue."
    ).strip()[:3000]
    risk = str(
        explicit.get("risk")
        or "Approving this exception relaxes the named capability for the selected approval scope."
    ).strip()[:3000]
    safer = str(
        explicit.get("safer_alternative")
        or "No verified alternative was supplied by the worker."
    ).strip()[:3000]
    request = {
        "capability": capability,
        "operation": redact_text(operation, None, 2000),
        "resource": redact_text(resource, None, 2000),
        "why_needed": redact_text(why, None, 3000),
        "risk": redact_text(risk, None, 3000),
        "safer_alternative": redact_text(safer, None, 3000),
        "observed_tool_name": redact_text(tool, None, 500) if tool else None,
        "observed_tool_input": (
            redact_text(json.dumps(tool_input, sort_keys=True, default=str), None, 3000)
            if tool_input else None
        ),
        "constraints": constraints,
        "profile": profile,
        "objective_hash": objective_hash,
        "repo_root": repo_root,
        "requested_at": utcnow(),
    }
    request["id"] = _request_id(request)
    return request


def active_permission_overrides(
    state: dict[str, Any],
    objective_hash: str | None,
) -> set[str]:
    return {
        str(grant.get("capability"))
        for grant in active_permission_grants(state, objective_hash)
    }


def consume_once_grants(state: dict[str, Any], objective_hash: str | None) -> bool:
    grants = state.get("permission_grants")
    if not isinstance(grants, list):
        return False
    changed = False
    for grant in grants:
        if not isinstance(grant, dict):
            continue
        if grant.get("revoked_at") or grant.get("expired_at"):
            continue
        if grant.get("scope") != "once" or grant.get("consumed_at"):
            continue
        if objective_hash and grant.get("objective_hash") not in {None, objective_hash}:
            continue
        grant["consumed_at"] = utcnow()
        changed = True
    return changed


def render_permission_request(request: dict[str, Any]) -> str:
    rid = str(request.get("id") or "")
    repo_arg = ""
    if request.get("repo_root"):
        repo_arg = " --repo " + shlex.quote(str(request.get("repo_root")))
    return (
        "\nPERMISSION REQUIRED\n"
        f"Request ID: {rid}\n"
        f"Capability: {request.get('capability')}\n"
        f"Operation: {request.get('operation') or 'unspecified'}\n"
        f"Resource: {request.get('resource') or 'unspecified'}\n"
        f"Why needed: {request.get('why_needed')}\n"
        f"Risk/side effects: {request.get('risk')}\n"
        f"Safer alternative: {request.get('safer_alternative')}\n"
        f"Observed denied tool: {request.get('observed_tool_name') or 'not captured'}\n"
        f"Observed denied input: {request.get('observed_tool_input') or 'not captured'}\n"
        f"Enforced grant constraints: {json.dumps(request.get('constraints') or {}, sort_keys=True)}\n\n"
        f"Approve once:      claude-auto permissions approve{repo_arg} --scope once --id {rid}\n"
        f"Approve this run:  claude-auto permissions approve{repo_arg} --scope run --id {rid}\n"
        f"Approve for repo:  claude-auto permissions approve{repo_arg} --scope repository --id {rid}\n"
        f"Deny:              claude-auto permissions deny{repo_arg} --id {rid}"
    )


def approve_request(
    state: dict[str, Any],
    request: dict[str, Any],
    scope: str,
) -> dict[str, Any]:
    if scope not in APPROVAL_SCOPES:
        raise ValueError(f"Unsupported permission scope: {scope}")
    probe = {
        "capability": request.get("capability"),
        "constraints": request.get("constraints") or {},
    }
    if not _grant_has_enforceable_constraints(probe):
        raise ValueError(
            "This constrained permission request does not contain an enforceable exact resource. "
            "Rerun the blocked operation so Claude Auto can capture the denied path/command, "
            "or explicitly use native-permissions/unrestricted authority when broader access is intended."
        )
    grants = state.setdefault("permission_grants", [])
    decisions = state.setdefault("permission_decisions", [])
    decided_at = utcnow()
    record_permission_request(state, request)
    snapshot = _request_snapshot(request)
    grant = {
        "request_id": request.get("id"),
        "capability": request.get("capability"),
        "scope": scope,
        "objective_hash": request.get("objective_hash"),
        "resource": request.get("resource"),
        "operation": request.get("operation"),
        "constraints": snapshot.get("constraints") or {},
        "approved_at": decided_at,
        "request": snapshot,
    }
    grants.append(grant)
    decisions.append({
        "request_id": request.get("id"),
        "capability": request.get("capability"),
        "decided_at": decided_at,
        "decision": "approved",
        "scope": scope,
        "request": snapshot,
    })
    state["pending_permission_request"] = None
    state["status"] = "READY"
    state["last_result_status"] = "CONTINUE"
    state["blocker"] = None
    state["updated_at"] = decided_at
    return grant


def deny_request(
    state: dict[str, Any],
    request: dict[str, Any],
    reason: str = "",
) -> dict[str, Any]:
    decided_at = utcnow()
    record_permission_request(state, request)
    decision = {
        "request_id": request.get("id"),
        "capability": request.get("capability"),
        "decided_at": decided_at,
        "decision": "denied",
        "reason": redact_text(reason, None, 2000),
        "request": _request_snapshot(request),
    }
    state.setdefault("permission_decisions", []).append(decision)
    state["pending_permission_request"] = None
    state["status"] = "BLOCKED"
    state["last_result_status"] = "BLOCKED"
    state["blocker"] = (
        f"User denied permission request {request.get('id')} "
        f"({request.get('capability')})."
    )
    state["updated_at"] = decided_at
    return decision


def revoke_grants(
    state: dict[str, Any],
    *,
    request_id: str | None = None,
    capability: str | None = None,
) -> list[dict[str, Any]]:
    if not request_id and not capability:
        raise ValueError("Provide --id or --capability to revoke a grant.")
    grants = state.get("permission_grants")
    if not isinstance(grants, list):
        return []
    revoked: list[dict[str, Any]] = []
    now = utcnow()
    for grant in grants:
        if not isinstance(grant, dict) or grant.get("revoked_at"):
            continue
        if request_id and grant.get("request_id") != request_id:
            continue
        if capability and grant.get("capability") != capability:
            continue
        grant["revoked_at"] = now
        revoked.append(grant)
    if revoked:
        state.setdefault("permission_decisions", []).append({
            "decision": "revoked",
            "decided_at": now,
            "request_id": request_id,
            "capability": capability,
            "revoked_count": len(revoked),
        })
        state["updated_at"] = now
    return revoked


def prompt_permission_scope(request: dict[str, Any]) -> str | None:
    """Return an approval scope, 'deny', or None when no interactive TTY exists."""
    if not sys.stdin.isatty():
        return None
    print(render_permission_request(request))
    while True:
        try:
            answer = input(
                "\nChoose: [o]nce, current [r]un, [p]ersist for repository, "
                "[d]eny: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        mapping = {
            "o": "once", "once": "once",
            "r": "run", "run": "run",
            "p": "repository", "repo": "repository", "repository": "repository",
            "d": "deny", "deny": "deny", "n": "deny", "no": "deny",
        }
        if answer in mapping:
            return mapping[answer]
        print("Please choose once, run, repository, or deny.")


def permission_action(args) -> int:
    root = find_repo_root(getattr(args, "repo", None))
    sd = repo_state_dir(root)
    approved_grant: dict[str, Any] | None = None

    with SupervisorLease(sd, root):
        state = load_json(sd / "state.json", {})
        pending = (
            state.get("pending_permission_request")
            if isinstance(state.get("pending_permission_request"), dict)
            else None
        )
        history = (
            state.get("permission_decisions")
            if isinstance(state.get("permission_decisions"), list)
            else []
        )
        grants = (
            state.get("permission_grants")
            if isinstance(state.get("permission_grants"), list)
            else []
        )
        if args.permissions_command == "status":
            objective_hash = state.get("active_permission_objective_hash")
            current = active_permission_grants(state, objective_hash)
            print(json.dumps({
                "pending": pending,
                "active_objective_hash": objective_hash,
                "active_grants": current,
                "historical_grant_count": len(grants) - len(current),
            }, indent=2))
            return 0
        if args.permissions_command == "history":
            print(json.dumps({"decisions": history, "grants": grants}, indent=2))
            return 0
        if args.permissions_command == "revoke":
            try:
                revoked = revoke_grants(
                    state,
                    request_id=getattr(args, "id", None),
                    capability=getattr(args, "capability", None),
                )
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc
            json_dump(sd / "state.json", state)
            print(json.dumps({
                "revoked_count": len(revoked),
                "revoked": revoked,
            }, indent=2))
            return 0
        if not pending:
            raise SystemExit("No permission request is currently pending for this repository.")
        expected = getattr(args, "id", None)
        if expected and expected != pending.get("id"):
            raise SystemExit(
                f"Pending permission request is {pending.get('id')}, not {expected}."
            )
        active_hash = state.get("active_permission_objective_hash")
        pending_hash = pending.get("objective_hash")
        if active_hash and pending_hash and active_hash != pending_hash:
            raise SystemExit(
                "Pending permission request belongs to an older objective/source epoch. "
                "Run the objective again to generate a current request."
            )

        if args.permissions_command == "approve":
            try:
                approved_grant = approve_request(state, pending, str(args.scope))
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc
            json_dump(sd / "state.json", state)
        else:
            decision = deny_request(
                state,
                pending,
                str(getattr(args, "reason", None) or ""),
            )
            json_dump(sd / "state.json", state)
            print(json.dumps({"denied": decision}, indent=2))
            return 0

    # Start a configured resume service only after releasing the repository writer
    # lease, otherwise the just-started service could immediately contend with us.
    resumed_service = False
    resume_error: str | None = None
    from service_manager import _service_unit_name, _service_unit_path
    service_path = _service_unit_path(root)
    systemctl = shutil.which("systemctl")
    if approved_grant is not None and service_path.exists() and systemctl:
        cp = subprocess.run(
            [systemctl, "--user", "start", _service_unit_name(root)],
            text=True,
            capture_output=True,
        )
        resumed_service = cp.returncode == 0
        if cp.returncode != 0:
            resume_error = (cp.stderr or cp.stdout or "systemctl start failed").strip()[:1000]

    print(json.dumps({
        "approved": approved_grant,
        "resume_service_started": resumed_service,
        "resume_service_error": resume_error,
        "next": (
            "The configured resume service was started."
            if resumed_service
            else "Resume with claude-auto run --resume-config if no resume service is configured."
        ),
    }, indent=2))
    return 0
