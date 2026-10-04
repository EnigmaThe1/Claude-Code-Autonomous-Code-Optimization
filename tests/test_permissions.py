from __future__ import annotations

import json
import importlib.util
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("claude_auto_permissions", ROOT / "lib" / "claude_auto.py")
ca = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ca)


def _repo() -> Path:
    d = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.name", "Test"], check=True)
    (d / "README.md").write_text("x\n")
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
    return d


def test_unattended_profile_removes_package_restrictions():
    d = _repo()
    sd = Path(tempfile.mkdtemp())
    settings = ca.make_settings(
        sd,
        "external",
        "unattended",
        {"repo_root": str(d), "languages": [], "container_files": []},
    )
    assert settings["sandbox"]["enabled"] is False
    assert settings["permissions"]["deny"] == []
    assert settings["permissions"]["blockReadsOutsideWorkingDirectories"] is False
    rendered = str(settings.get("hooks", {}))
    assert "write_boundary_guard.py" not in rendered
    assert "docker_guard.py" not in rendered
    assert ca.permission_mode_for_profile("auto", "unattended") == "bypassPermissions"


def test_unattended_is_not_silently_inherited():
    assert ca.resolve_autonomy_profile(None, "unattended") == "balanced"
    assert ca.resolve_autonomy_profile("unattended", "balanced") == "unattended"
    assert ca.resolve_autonomy_profile(None, "balanced") == "balanced"
    assert (
        ca.resolve_autonomy_profile(
            None,
            "unattended",
            resume_config=True,
            resumed_profile="unattended",
        )
        == "unattended"
    )


def test_unattended_repository_command_uses_full_host_environment(monkeypatch):
    d = _repo()
    monkeypatch.setenv("CLAUDE_AUTO_UNATTENDED_PROBE", "visible")
    result = ca.run_repository_command(
        d,
        ["bash", "-lc", "printf %s \"$CLAUDE_AUTO_UNATTENDED_PROBE\""],
        timeout=10,
        unrestricted_host=True,
    )
    assert result["returncode"] == 0
    assert result["stdout"] == "visible"
    assert result["execution_boundary"] == "unattended-host"
    assert result["sandboxed"] is False
    assert result["environment_scrubbed"] is False


def test_permission_grants_are_scoped_and_once_is_consumed():
    state = {
        "permission_grants": [
            {"capability": "outside-repository", "scope": "once", "objective_hash": "abc", "constraints": {"file_paths": ["/tmp/out"]}},
            {"capability": "container-host-authority", "scope": "run", "objective_hash": "abc", "constraints": {"command_sha256": ["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]}},
            {"capability": "secret-read", "scope": "run", "objective_hash": "other", "constraints": {"file_paths": ["/tmp/secret"]}},
            {"capability": "native-permissions", "scope": "repository"},
        ]
    }
    active = ca.active_permission_overrides(state, "abc")
    assert active == {
        "outside-repository",
        "container-host-authority",
        "native-permissions",
    }
    assert ca.consume_once_grants(state, "abc") is True
    assert ca.active_permission_overrides(state, "abc") == {
        "container-host-authority",
        "native-permissions",
    }


def test_permission_override_relaxes_only_requested_guard():
    d = _repo()
    sd = Path(tempfile.mkdtemp())
    outside = "/tmp/claude-auto-outside.txt"
    settings = ca.make_settings(
        sd,
        "external",
        "balanced",
        {"repo_root": str(d), "languages": [], "container_files": []},
        {"outside-repository"},
        permission_grants=[{
            "capability": "outside-repository",
            "scope": "run",
            "objective_hash": "obj",
            "constraints": {"file_paths": [outside]},
        }],
    )
    rendered = str(settings.get("hooks", {}))
    assert "write_boundary_guard.py" in rendered
    assert "read_scope_guard.py" in rendered
    assert "docker_guard.py" in rendered
    assert settings["permissions"]["deny"], (
        "outside-repository approval must not also expose protected secrets"
    )
    assert "//tmp/claude-auto-outside.txt" in str(settings["permissions"]["allow"])


def test_cli_exposes_permission_decisions_and_unattended_profile():
    parser = ca.build_parser()
    args = parser.parse_args(["run", "--objective", "x", "--profile", "unattended"])
    assert args.profile == "unattended"
    args = parser.parse_args(["permissions", "approve", "--scope", "once", "--id", "abc"])
    assert args.permissions_command == "approve"
    assert args.scope == "once"


def test_service_does_not_restart_waiting_permission():
    d = _repo()
    unit = ca._service_unit_text(d)
    prevent = next(x for x in unit.splitlines() if x.startswith("RestartPreventExitStatus"))
    assert "8" in prevent.split("=", 1)[1].split()


def test_permission_request_protocol_parses_detailed_fields():
    text = (
        'AUTONOMY_PERMISSION_REQUEST: '
        '{"capability":"outside-repository","operation":"write file",'
        '"resource":"/tmp/output","why_needed":"integration fixture",'
        '"risk":"host file change","safer_alternative":"none"}'
    )
    req = ca.parse_permission_request(text)
    assert req["capability"] == "outside-repository"
    assert req["resource"] == "/tmp/output"
    assert req["why_needed"] == "integration fixture"


def test_permission_helpers_record_approval_and_denial():
    request = {
        "id": "req-1",
        "capability": "outside-repository",
        "objective_hash": "obj",
        "resource": "/tmp/out",
        "operation": "write file",
        "constraints": {"file_paths": ["/tmp/out"]},
    }
    state = {"permission_grants": [], "permission_decisions": []}
    grant = ca.approve_request(state, request, "run")
    assert grant["scope"] == "run"
    assert "outside-repository" in ca.active_permission_overrides(state, "obj")
    assert state["status"] == "READY"

    request2 = {
        "id": "req-2",
        "capability": "secret-read",
        "objective_hash": "obj",
    }
    decision = ca.deny_request(state, request2, "not allowed")
    assert decision["decision"] == "denied"
    assert state["permission_decisions"][-1]["request_id"] == "req-2"
    assert state["permission_decisions"][-1]["decision"] == "denied"
    assert state["status"] == "BLOCKED"


def test_foreground_permission_prompt_returns_scope(monkeypatch):
    import permission_escalation as pe

    class TTY:
        def isatty(self):
            return True

    monkeypatch.setattr(pe.sys, "stdin", TTY())
    monkeypatch.setattr("builtins.input", lambda _prompt: "r")
    assert ca.prompt_permission_scope({
        "id": "req",
        "capability": "native-permissions",
        "operation": "run command",
        "resource": "tool",
        "why_needed": "required",
        "risk": "limited",
        "safer_alternative": "none",
    }) == "run"


def test_permission_cli_approval_persists_grant(monkeypatch):
    d = _repo()
    home = Path(tempfile.mkdtemp())
    monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", str(home))
    sd = ca.activate(d)
    state = ca.load_json(sd / "state.json", {})
    state["pending_permission_request"] = {
        "id": "req-cli",
        "capability": "host-repository-execution",
        "objective_hash": "obj",
        "resource": "pytest",
        "operation": "run tests",
        "constraints": {"verification_commands": ["pytest"]},
    }
    state["status"] = "AWAITING_USER_PERMISSION"
    ca.json_dump(sd / "state.json", state)

    args = ca.build_parser().parse_args([
        "permissions", "approve", "--repo", str(d),
        "--id", "req-cli", "--scope", "repository",
    ])
    assert ca.permission_action(args) == 0
    updated = ca.load_json(sd / "state.json", {})
    assert updated["pending_permission_request"] is None
    assert updated["status"] == "READY"
    assert updated["permission_grants"][-1]["scope"] == "repository"
    assert updated["permission_grants"][-1]["capability"] == "host-repository-execution"


def test_unattended_defaults_to_hermetic_but_can_explicitly_keep_compatibility():
    assert ca.resolve_session_settings(None, "unattended") == "hermetic"
    assert ca.resolve_session_settings("compatibility", "unattended") == "compatibility"
    assert ca.resolve_session_settings(None, "balanced") == "compatibility"
    assert ca.resolve_session_settings("hermetic", "strict") == "hermetic"


def test_secret_read_approval_does_not_remove_write_or_container_guards():
    d = _repo()
    sd = Path(tempfile.mkdtemp())
    secret = str(d / ".env")
    settings = ca.make_settings(
        sd,
        "external",
        "balanced",
        {"repo_root": str(d), "languages": [], "container_files": []},
        {"secret-read"},
        permission_grants=[{
            "capability": "secret-read",
            "scope": "run",
            "objective_hash": "obj",
            "constraints": {"file_paths": [secret]},
        }],
    )
    rendered = str(settings.get("hooks", {}))
    assert "write_boundary_guard.py" in rendered
    assert "read_scope_guard.py" in rendered
    assert "docker_guard.py" in rendered
    assert settings["permissions"]["deny"] == []
    assert settings["permissions"]["blockReadsOutsideWorkingDirectories"] is False
    assert ".env" in str(settings["permissions"]["allow"])


def test_container_authority_approval_does_not_remove_write_or_secret_guards():
    d = _repo()
    sd = Path(tempfile.mkdtemp())
    digest = "b" * 64
    settings = ca.make_settings(
        sd,
        "external",
        "balanced",
        {"repo_root": str(d), "languages": [], "container_files": []},
        {"container-host-authority"},
        permission_grants=[{
            "capability": "container-host-authority",
            "scope": "run",
            "objective_hash": "obj",
            "constraints": {"command_sha256": [digest]},
        }],
    )
    rendered = str(settings.get("hooks", {}))
    assert "write_boundary_guard.py" in rendered
    assert "docker_guard.py" in rendered
    assert settings["permissions"]["deny"]
    assert json.loads(settings["env"]["CLAUDE_AUTO_APPROVED_CONTAINER_COMMAND_HASHES"]) == [digest]
    assert "docker *" not in settings["sandbox"].get("excludedCommands", [])


def test_worker_prompt_receives_active_permission_overrides():
    state = {
        "autonomy_profile": "balanced",
        "plan_version": 1,
        "plan_status": "VALIDATED",
    }
    prompt = ca.build_goal_prompt(
        "finish objective",
        state,
        {"repo_root": "/tmp/repo", "languages": [], "container_files": []},
        12,
        None,
        permission_overrides={"outside-repository", "native-permissions"},
        permission_grants=[{
            "capability": "outside-repository",
            "scope": "run",
            "constraints": {"file_paths": ["/tmp/out"]},
        }],
    )
    assert '"active_permission_overrides":["native-permissions","outside-repository"]' in prompt
    assert '"file_paths":["/tmp/out"]' in prompt
    assert "authoritative user-approved scope" in prompt


def test_consumed_once_grant_is_not_active_for_a_later_cycle():
    state = {
        "permission_grants": [
            {
                "request_id": "once-1",
                "capability": "outside-repository",
                "scope": "once",
                "objective_hash": "obj",
                "constraints": {"file_paths": ["/tmp/out"]},
            }
        ]
    }
    first_cycle = ca.active_permission_overrides(state, "obj")
    assert first_cycle == {"outside-repository"}
    assert ca.consume_once_grants(state, "obj") is True

    # The supervisor recomputes this set at the start of every worker round.
    next_cycle = ca.active_permission_overrides(state, "obj")
    assert next_cycle == set()


def test_permission_grant_can_be_revoked_by_capability():
    state = {
        "permission_grants": [
            {
                "request_id": "req-a",
                "capability": "outside-repository",
                "scope": "repository",
                "constraints": {"file_paths": ["/tmp/out"]},
            },
            {
                "request_id": "req-b",
                "capability": "secret-read",
                "scope": "repository",
                "constraints": {"file_paths": ["/tmp/secret"]},
            },
        ],
        "permission_decisions": [],
    }
    revoked = ca.revoke_grants(state, capability="outside-repository")
    assert len(revoked) == 1
    assert revoked[0]["request_id"] == "req-a"
    assert ca.active_permission_overrides(state, "anything") == {"secret-read"}
    assert state["permission_decisions"][-1]["decision"] == "revoked"


def test_permission_cli_revoke_requires_selector():
    parser = ca.build_parser()
    args = parser.parse_args(["permissions", "revoke"])
    state = {"permission_grants": []}
    try:
        ca.revoke_grants(
            state,
            request_id=getattr(args, "id", None),
            capability=getattr(args, "capability", None),
        )
    except ValueError as exc:
        assert "Provide --id or --capability" in str(exc)
    else:
        raise AssertionError("revoke without selector must fail")


def test_strict_secret_read_does_not_enable_unsandboxed_bash():
    d = _repo()
    sd = Path(tempfile.mkdtemp())
    secret = str(d / ".env")
    settings = ca.make_settings(
        sd,
        "external",
        "strict",
        {"repo_root": str(d), "languages": [], "container_files": []},
        {"secret-read"},
        permission_grants=[{
            "capability": "secret-read",
            "scope": "run",
            "objective_hash": "obj",
            "constraints": {"file_paths": [secret]},
        }],
    )
    assert settings["permissions"]["deny"] == []
    assert settings["permissions"]["blockReadsOutsideWorkingDirectories"] is False
    assert settings["sandbox"]["enabled"] is True
    assert settings["sandbox"]["allowUnsandboxedCommands"] is False
    assert settings["sandbox"]["failIfUnavailable"] is True
    assert "read_scope_guard.py" in str(settings.get("hooks", {}))


def test_unconsumed_once_grant_does_not_cross_objectives():
    state = {
        "permission_grants": [
            {
                "request_id": "once-old",
                "capability": "outside-repository",
                "scope": "once",
                "objective_hash": "old-objective",
                "constraints": {"file_paths": ["/tmp/out"]},
            }
        ]
    }
    assert ca.active_permission_overrides(state, "old-objective") == {"outside-repository"}
    assert ca.active_permission_overrides(state, "new-objective") == set()


def test_rendered_background_permission_commands_are_repo_bound():
    request = ca.build_permission_request(
        {
            "capability": "outside-repository",
            "operation": "write fixture",
            "resource": "/tmp/out",
            "why_needed": "integration",
            "risk": "host write",
            "safer_alternative": "none",
        },
        reason="blocked",
        runtime_events=[],
        profile="balanced",
        objective_hash="obj",
        repo_root="/tmp/repo with space",
    )
    rendered = ca.render_permission_request(request)
    assert "--repo '/tmp/repo with space'" in rendered
    assert f"--id {request['id']}" in rendered
