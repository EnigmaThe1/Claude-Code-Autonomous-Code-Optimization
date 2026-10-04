from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("claude_auto_audit", ROOT / "lib" / "claude_auto.py")
ca = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ca)

from model_qualification import _qualification_command


def _repo() -> Path:
    d = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.name", "Test"], check=True)
    (d / "README.md").write_text("x\n")
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
    return d


def _request(rid: str = "req-old", objective_hash: str = "old") -> dict:
    return {
        "id": rid,
        "capability": "outside-repository",
        "operation": "write fixture",
        "resource": "/tmp/output",
        "why_needed": "integration fixture is required",
        "risk": "writes one host file",
        "safer_alternative": "none available",
        "constraints": {"file_paths": ["/tmp/output"]},
        "profile": "balanced",
        "objective_hash": objective_hash,
        "repo_root": "/tmp/repo",
        "requested_at": "2026-10-03T00:00:00+00:00",
    }


def test_new_permission_epoch_supersedes_pending_and_expires_objective_grants():
    pending = _request()
    state = {
        "active_permission_objective_hash": "old",
        "pending_permission_request": pending,
        "permission_requests": [],
        "permission_decisions": [],
        "permission_grants": [
            {"request_id": "once", "capability": "outside-repository", "scope": "once", "objective_hash": "old", "constraints": {"file_paths": ["/tmp/output"]}},
            {"request_id": "run", "capability": "secret-read", "scope": "run", "objective_hash": "old", "constraints": {"file_paths": ["/tmp/secret"]}},
            {"request_id": "repo", "capability": "native-permissions", "scope": "repository", "objective_hash": "old"},
        ],
    }
    assert ca.reset_permission_epoch(
        state,
        new_objective_hash="new",
        reason="objective or supplied source changed",
    ) is True
    assert state["pending_permission_request"] is None
    assert state["active_permission_objective_hash"] == "new"
    assert state["permission_grants"][0]["expired_reason"] == "objective or supplied source changed"
    assert state["permission_grants"][1]["expired_reason"] == "objective or supplied source changed"
    assert "expired_at" not in state["permission_grants"][2]
    decision = state["permission_decisions"][-1]
    assert decision["decision"] == "superseded"
    assert decision["request"]["why_needed"] == pending["why_needed"]


def test_upgrade_state_with_no_epoch_still_drops_mismatched_pending():
    state = {
        "pending_permission_request": _request(objective_hash="old"),
        "permission_requests": [],
        "permission_decisions": [],
        "permission_grants": [],
    }
    assert ca.reset_permission_epoch(
        state,
        new_objective_hash="new",
        reason="source changed",
    ) is True
    assert state["pending_permission_request"] is None
    assert state["active_permission_objective_hash"] == "new"


def test_explicit_unattended_supersedes_pending_request_for_same_epoch():
    state = {
        "active_permission_objective_hash": "same",
        "pending_permission_request": _request(objective_hash="same"),
        "permission_requests": [],
        "permission_decisions": [],
        "permission_grants": [],
    }
    assert ca.reset_permission_epoch(
        state,
        new_objective_hash="same",
        reason="same source",
        unattended=True,
    ) is True
    assert state["pending_permission_request"] is None
    assert state["permission_decisions"][-1]["decision"] == "superseded"
    assert "unattended" in state["permission_decisions"][-1]["reason"]


def test_permission_approval_retains_full_request_evidence():
    req = _request()
    state = {"permission_requests": [], "permission_grants": [], "permission_decisions": []}
    grant = ca.approve_request(state, req, "run")
    assert grant["request"]["why_needed"] == req["why_needed"]
    assert grant["request"]["risk"] == req["risk"]
    assert grant["request"]["safer_alternative"] == req["safer_alternative"]
    assert state["permission_decisions"][-1]["request"]["resource"] == req["resource"]
    assert state["permission_requests"][0]["id"] == req["id"]


def test_permission_denial_retains_full_request_evidence():
    req = _request()
    state = {"permission_requests": [], "permission_decisions": []}
    decision = ca.deny_request(state, req, "not approved")
    assert decision["request"]["why_needed"] == req["why_needed"]
    assert decision["request"]["risk"] == req["risk"]
    assert decision["reason"] == "not approved"
    assert state["permission_requests"][0]["id"] == req["id"]


def test_permission_request_recording_is_idempotent():
    req = _request()
    state = {"permission_requests": []}
    assert ca.record_permission_request(state, req) is True
    assert ca.record_permission_request(state, req) is False
    assert len(state["permission_requests"]) == 1


def test_permission_request_fields_are_redacted(monkeypatch):
    secret = "super-secret-api-key-value-123456"
    monkeypatch.setenv("CLAUDE_AUTO_TEST_API_KEY", secret)
    req = ca.build_permission_request(
        {
            "capability": "native-permissions",
            "operation": f"run command with {secret}",
            "resource": f"Bearer {secret}",
            "why_needed": f"needs {secret}",
            "risk": f"could expose {secret}",
            "safer_alternative": "none",
        },
        reason="blocked",
        runtime_events=[],
        profile="balanced",
        objective_hash="obj",
        repo_root="/tmp/repo",
    )
    rendered = json.dumps(req)
    assert secret not in rendered
    assert "REDACTED" in rendered


def test_active_grants_exclude_expired_consumed_revoked_and_wrong_objective():
    state = {
        "permission_grants": [
            {"request_id": "a", "capability": "outside-repository", "scope": "run", "objective_hash": "obj", "constraints": {"file_paths": ["/tmp/a"]}},
            {"request_id": "b", "capability": "secret-read", "scope": "run", "objective_hash": "other", "constraints": {"file_paths": ["/tmp/b"]}},
            {"request_id": "c", "capability": "native-permissions", "scope": "once", "objective_hash": "obj", "consumed_at": "x"},
            {"request_id": "d", "capability": "container-host-authority", "scope": "repository", "revoked_at": "x"},
            {"request_id": "e", "capability": "native-permissions", "scope": "repository"},
            {"request_id": "f", "capability": "unrestricted", "scope": "run", "objective_hash": "obj", "expired_at": "x"},
        ]
    }
    active = ca.active_permission_grants(state, "obj")
    assert {x["request_id"] for x in active} == {"a", "e"}
    assert ca.active_permission_overrides(state, "obj") == {"outside-repository", "native-permissions"}


def test_supervisor_only_host_execution_grant_is_not_a_worker_permission():
    assert ca.worker_permission_overrides({"host-repository-execution"}) == set()
    assert ca.worker_permission_overrides(
        {"host-repository-execution", "outside-repository"}
    ) == {"outside-repository"}


def test_unattended_uses_bypass_and_strong_skip_switch():
    args = ca.claude_base_args(
        Path("/tmp/state"),
        None,
        "medium",
        "auto",
        autonomy_profile="unattended",
    )
    assert args[args.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--dangerously-skip-permissions" in args


def test_unrestricted_grant_uses_strong_skip_switch():
    args = ca.claude_base_args(
        Path("/tmp/state"),
        None,
        "medium",
        "auto",
        autonomy_profile="balanced",
        permission_overrides={"unrestricted"},
    )
    assert args[args.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--dangerously-skip-permissions" in args


def test_qualification_command_can_be_hermetic():
    cmd = _qualification_command(
        settings_path=Path("/tmp/settings.json"),
        model=None,
        effort="medium",
        tools="Read",
        denied="Write",
        prompt="x",
        max_turns=2,
        setting_sources="",
    )
    assert cmd[cmd.index("--setting-sources") + 1] == ""


def test_stale_cli_permission_approval_is_rejected(monkeypatch):
    d = _repo()
    home = Path(tempfile.mkdtemp())
    monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", str(home))
    sd = ca.activate(d)
    state = ca.load_json(sd / "state.json", {})
    state["active_permission_objective_hash"] = "new"
    state["pending_permission_request"] = _request(objective_hash="old")
    ca.json_dump(sd / "state.json", state)

    args = ca.build_parser().parse_args([
        "permissions", "approve", "--repo", str(d), "--id", "req-old", "--scope", "repository",
    ])
    with pytest.raises(SystemExit, match="older objective/source epoch"):
        ca.permission_action(args)


def test_service_new_objective_clears_waiting_permission_and_expires_run_grant(monkeypatch):
    d = _repo()
    data_home = Path(tempfile.mkdtemp())
    home = Path(tempfile.mkdtemp())
    monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", str(data_home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(ca.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(ca.shutil, "which", lambda _name: None)

    sd = ca.activate(d)
    state = ca.load_json(sd / "state.json", {})
    state.update({
        "objective": "old objective",
        "status": "AWAITING_USER_PERMISSION",
        "blocker": "old blocker",
        "last_result_status": "AWAITING_USER_PERMISSION",
        "active_permission_objective_hash": "old-hash",
        "pending_permission_request": _request(objective_hash="old-hash"),
        "permission_grants": [
            {"request_id": "run", "capability": "outside-repository", "scope": "run", "objective_hash": "old-hash"},
            {"request_id": "repo", "capability": "native-permissions", "scope": "repository", "objective_hash": "old-hash"},
        ],
    })
    ca.json_dump(sd / "state.json", state)

    args = ca.build_parser().parse_args([
        "service", "install", "--repo", str(d), "--objective", "new objective",
    ])
    assert ca.service_action(args) == 0
    updated = ca.load_json(sd / "state.json", {})
    assert updated["objective"] == "new objective"
    assert updated["status"] == "READY"
    assert updated["blocker"] is None
    assert updated["pending_permission_request"] is None
    assert updated["active_permission_objective_hash"] is None
    assert updated["permission_grants"][0].get("expired_at")
    assert not updated["permission_grants"][1].get("expired_at")


def test_activation_upgrades_permission_schema():
    d = _repo()
    data_home = Path(tempfile.mkdtemp())
    import os
    old = os.environ.get("CLAUDE_AUTONOMY_HOME")
    os.environ["CLAUDE_AUTONOMY_HOME"] = str(data_home)
    try:
        sd = ca.activate(d)
        state = ca.load_json(sd / "state.json", {})
        assert state["schema_version"] >= 8
        assert state["permission_requests"] == []
        assert state["active_permission_objective_hash"] is None
    finally:
        if old is None:
            os.environ.pop("CLAUDE_AUTONOMY_HOME", None)
        else:
            os.environ["CLAUDE_AUTONOMY_HOME"] = old


def test_scoped_capabilities_do_not_switch_entire_worker_to_bypass():
    for capability in ("secret-read", "outside-repository", "container-host-authority"):
        assert ca.permission_mode_for_profile("auto", "balanced", {capability}) == "auto"
    assert ca.permission_mode_for_profile("auto", "balanced", {"native-permissions"}) == "bypassPermissions"
    assert ca.permission_mode_for_profile("auto", "balanced", {"unrestricted"}) == "bypassPermissions"


def test_scoped_capabilities_add_only_category_permission_rules():
    d = _repo()
    sd = Path(tempfile.mkdtemp())
    secret_path = str(d / ".env")
    outside_path = "/tmp/claude-auto-outside.txt"
    digest = "c" * 64

    secret = ca.make_settings(
        sd, "external", "balanced",
        {"repo_root": str(d), "languages": [], "container_files": []},
        {"secret-read"},
        permission_grants=[{
            "capability": "secret-read",
            "constraints": {"file_paths": [secret_path]},
        }],
    )
    assert ".env" in str(secret["permissions"]["allow"])
    assert "Write" not in secret["permissions"]["allow"]
    assert "read_scope_guard.py" in str(secret.get("hooks", {}))

    outside = ca.make_settings(
        sd, "external", "balanced",
        {"repo_root": str(d), "languages": [], "container_files": []},
        {"outside-repository"},
        permission_grants=[{
            "capability": "outside-repository",
            "constraints": {"file_paths": [outside_path]},
        }],
    )
    allow_text = str(outside["permissions"]["allow"])
    assert "claude-auto-outside.txt" in allow_text
    assert "Read(//" in allow_text
    assert "Edit(//" in allow_text
    assert "Write(//" in allow_text
    assert "NotebookEdit(//" in allow_text
    assert "read_scope_guard.py" in str(outside.get("hooks", {}))
    assert "write_boundary_guard.py" in str(outside.get("hooks", {}))

    container = ca.make_settings(
        sd, "external", "balanced",
        {"repo_root": str(d), "languages": [], "container_files": []},
        {"container-host-authority"},
        permission_grants=[{
            "capability": "container-host-authority",
            "constraints": {"command_sha256": [digest]},
        }],
    )
    assert container["permissions"]["deny"], "container authority must not expose secret reads"
    assert json.loads(container["env"]["CLAUDE_AUTO_APPROVED_CONTAINER_COMMAND_HASHES"]) == [digest]
    assert "docker_guard.py" in str(container.get("hooks", {}))


def test_legacy_objective_scoped_grants_are_inactive_until_epoch_is_known():
    state = {
        "permission_grants": [
            {"request_id": "old-run", "capability": "outside-repository", "scope": "run", "objective_hash": "old"},
            {"request_id": "repo", "capability": "native-permissions", "scope": "repository"},
        ]
    }
    assert ca.active_permission_overrides(state, None) == {"native-permissions"}
    assert ca.reset_permission_epoch(
        state,
        new_objective_hash="new",
        reason="first audit epoch established",
    ) is True
    assert state["permission_grants"][0].get("expired_at")
    assert ca.active_permission_overrides(state, "new") == {"native-permissions"}


def test_runtime_denial_evidence_overrides_misdeclared_capability():
    runtime_events = [{
        "event": "PermissionDenied",
        "tool_name": "Bash",
        "reason": "container command denied",
        "tool_input": {
            "command": {
                "executable": "docker",
                "sha256": "abc",
                "length": 42,
            }
        },
    }]
    req = ca.build_permission_request(
        {
            "capability": "secret-read",
            "operation": "worker claimed secret read",
            "resource": "worker claimed resource",
            "why_needed": "required for integration",
            "risk": "container host authority",
            "safer_alternative": "none",
        },
        reason="blocked",
        runtime_events=runtime_events,
        profile="balanced",
        objective_hash="obj",
        repo_root="/tmp/repo",
    )
    assert req["capability"] == "container-host-authority"
    assert req["observed_tool_name"] == "Bash"
    assert "docker" in req["observed_tool_input"]


def test_request_id_changes_when_observed_denied_action_changes():
    base = {
        "capability": "native-permissions",
        "operation": "run command",
        "resource": "command",
        "why_needed": "needed",
        "risk": "limited",
        "safer_alternative": "none",
    }
    req1 = ca.build_permission_request(
        base,
        reason="blocked",
        runtime_events=[{
            "event": "PermissionDenied",
            "tool_name": "Bash",
            "reason": "denied",
            "tool_input": {"command": {"executable": "docker", "sha256": "one", "length": 10}},
        }],
        profile="balanced",
        objective_hash="obj",
        repo_root="/tmp/repo",
    )
    req2 = ca.build_permission_request(
        base,
        reason="blocked",
        runtime_events=[{
            "event": "PermissionDenied",
            "tool_name": "Bash",
            "reason": "denied",
            "tool_input": {"command": {"executable": "docker", "sha256": "two", "length": 10}},
        }],
        profile="balanced",
        objective_hash="obj",
        repo_root="/tmp/repo",
    )
    assert req1["id"] != req2["id"]


def test_outside_and_unattended_profiles_extend_filesystem_access():
    d = _repo()
    sd = Path(tempfile.mkdtemp())
    outside_path = "/tmp/permission-scope/file.txt"

    outside = ca.make_settings(
        sd, "external", "balanced",
        {"repo_root": str(d), "languages": [], "container_files": []},
        {"outside-repository"},
        permission_grants=[{
            "capability": "outside-repository",
            "constraints": {"file_paths": [outside_path]},
        }],
    )
    assert "/tmp/permission-scope" in outside["permissions"]["additionalDirectories"]
    assert "/" not in outside["permissions"]["additionalDirectories"]

    unattended = ca.make_settings(
        sd, "external", "unattended",
        {"repo_root": str(d), "languages": [], "container_files": []},
    )
    assert "/" in unattended["permissions"]["additionalDirectories"]


def test_checksum_bad_valid_json_does_not_poison_previous_generation():
    d = Path(tempfile.mkdtemp())
    state_path = d / "state.json"
    ca.json_dump(state_path, {"objective": "good-v1", "schema_version": 8})
    ca.json_dump(state_path, {"objective": "good-v2", "schema_version": 8})
    previous_before = json.loads((d / "state.prev.json").read_text())
    assert previous_before["objective"] == "good-v1"

    # Valid JSON with a stale checksum must be rejected and quarantined.
    state_path.write_text(json.dumps({"objective": "tampered", "schema_version": 8}) + "\n")
    recovered = ca.load_json(state_path, {})
    assert recovered["objective"] == "good-v1"
    assert json.loads((d / "state.corrupt.last.json").read_text())["objective"] == "tampered"
    assert json.loads((d / "state.prev.json").read_text())["objective"] == "good-v1"

    # A second failure must still recover the last known-good previous generation,
    # not the formerly checksum-bad/tampered state.
    state_path.write_text("{broken")
    recovered_again = ca.load_json(state_path, {})
    assert recovered_again["objective"] == "good-v1"


def test_runtime_verify_uses_active_autonomy_profile_settings(monkeypatch):
    import review_gates as rg

    d = _repo()
    captured = {}

    def fake_run(cmd, **_kwargs):
        captured["cmd"] = list(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="native verify unavailable")

    monkeypatch.setattr(rg, "run", fake_run)
    verdict, _summary, _findings, _meta = rg.run_runtime_verify_gate(
        root=d,
        sd=Path("/tmp/permission-state"),
        prof={"container_files": ["Dockerfile"]},
        objective="verify runtime",
        env={},
        provider_detail={"provider": "native"},
        model=None,
        timeout=5,
        autonomy_profile="strict",
    )
    assert verdict == "SKIP"
    cmd = captured["cmd"]
    assert str(Path("/tmp/permission-state") / "settings-strict-external.json") in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == ""


def test_native_security_review_is_hermetic(monkeypatch):
    import review_gates as rg

    d = _repo()
    captured = []

    def fake_run(cmd, **_kwargs):
        captured.append(list(cmd))
        if cmd[:4] == ["git", "-C", str(d), "remote"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="https://example.invalid/repo.git\n", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="native review unavailable")

    monkeypatch.setattr(rg, "run", fake_run)
    monkeypatch.setattr(
        rg,
        "run_readonly_plan_agent",
        lambda **_kwargs: (
            'SECURITY_REVIEW_GATE: {"verdict":"PASS","summary":"ok","findings":[]}',
            {"usage": {}, "repository_unchanged": True},
        ),
    )
    verdict, _summary, _findings, _meta = rg.run_security_review_gate(
        root=d,
        sd=Path("/tmp/permission-state"),
        objective="audit",
        main_summary=None,
        env={},
        provider_detail={"provider": "native"},
        model=None,
        timeout=5,
    )
    assert verdict == "PASS"
    native = next(cmd for cmd in captured if cmd and cmd[0] == "claude")
    assert native[native.index("--setting-sources") + 1] == ""


def test_gateway_probe_ignores_user_project_and_local_settings(monkeypatch):
    import operator_tools as ot
    from types import SimpleNamespace

    captured = {}

    def fake_run(cmd, **_kwargs):
        captured["cmd"] = list(cmd)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps({"result": "GATEWAY_OK", "session_id": "probe"}),
            stderr="",
        )

    monkeypatch.setattr(ot, "run", fake_run)
    args = SimpleNamespace(
        provider="custom",
        gateway_url="http://127.0.0.1:9999",
        gateway_token_env=None,
        gateway_discovery=False,
        isolate_provider_profile=False,
        gateway_hints=False,
        no_network=True,
        probe_model="probe-model",
        timeout=5,
    )
    assert ot.gateway_doctor(args) == 0
    cmd = captured["cmd"]
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert "--disallowed-tools" in cmd
    assert cmd[cmd.index("--disallowed-tools") + 1] == "*"


def _hook_decision(script: str, event: dict, env: dict[str, str]) -> str:
    cp = subprocess.run(
        ["python3", str(ROOT / "hooks" / script)],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        env={**os.environ, **env},
        check=True,
    )
    return json.loads(cp.stdout)["hookSpecificOutput"]["permissionDecision"]


def test_exact_outside_read_and_write_grants_do_not_cover_neighbour_paths():
    repo = _repo()
    outside_dir = Path(tempfile.mkdtemp())
    approved = outside_dir / "approved.txt"
    neighbour = outside_dir / "neighbour.txt"
    approved.write_text("approved\n")
    neighbour.write_text("neighbour\n")
    env = {
        "CLAUDE_AUTO_REPO_ROOT": str(repo),
        "CLAUDE_AUTO_APPROVED_OUTSIDE_PATHS": json.dumps([str(approved)]),
        "CLAUDE_AUTO_APPROVED_SECRET_PATHS": "[]",
        "CLAUDE_AUTONOMY_PROFILE": "balanced",
    }
    assert _hook_decision(
        "read_scope_guard.py",
        {"tool_name": "Read", "tool_input": {"file_path": str(approved)}},
        env,
    ) == "allow"
    assert _hook_decision(
        "read_scope_guard.py",
        {"tool_name": "Read", "tool_input": {"file_path": str(neighbour)}},
        env,
    ) == "deny"
    assert _hook_decision(
        "write_boundary_guard.py",
        {"tool_name": "Write", "tool_input": {"file_path": str(approved)}},
        env,
    ) == "allow"
    assert _hook_decision(
        "write_boundary_guard.py",
        {"tool_name": "Write", "tool_input": {"file_path": str(neighbour)}},
        env,
    ) == "deny"


def test_exact_secret_read_grant_does_not_cover_another_secret():
    repo = _repo()
    approved = repo / ".env"
    other = repo / ".env.production"
    approved.write_text("A=1\n")
    other.write_text("B=2\n")
    env = {
        "CLAUDE_AUTO_REPO_ROOT": str(repo),
        "CLAUDE_AUTO_APPROVED_OUTSIDE_PATHS": "[]",
        "CLAUDE_AUTO_APPROVED_SECRET_PATHS": json.dumps([str(approved)]),
        "CLAUDE_AUTONOMY_PROFILE": "balanced",
    }
    assert _hook_decision(
        "read_scope_guard.py",
        {"tool_name": "Read", "tool_input": {"file_path": str(approved)}},
        env,
    ) == "allow"
    assert _hook_decision(
        "read_scope_guard.py",
        {"tool_name": "Read", "tool_input": {"file_path": str(other)}},
        env,
    ) == "deny"
    assert _hook_decision(
        "read_scope_guard.py",
        {"tool_name": "Grep", "tool_input": {"pattern": "=", "path": str(repo)}},
        env,
    ) == "deny"
    assert _hook_decision(
        "read_scope_guard.py",
        {"tool_name": "Glob", "tool_input": {"pattern": "**/*", "path": str(repo)}},
        env,
    ) == "deny"


def test_exact_container_command_hash_does_not_cover_different_command():
    approved = "docker run --privileged alpine"
    other = "docker run --privileged ubuntu"
    env = {
        "CLAUDE_AUTO_REPO_ROOT": str(_repo()),
        "CLAUDE_AUTO_APPROVED_CONTAINER_COMMAND_HASHES": json.dumps([
            hashlib.sha256(approved.encode()).hexdigest()
        ]),
    }
    assert _hook_decision(
        "docker_guard.py",
        {"tool_name": "Bash", "tool_input": {"command": approved}},
        env,
    ) == "allow"
    assert _hook_decision(
        "docker_guard.py",
        {"tool_name": "Bash", "tool_input": {"command": other}},
        env,
    ) == "deny"


def test_exact_host_verification_grant_does_not_cover_other_command():
    grants = [{
        "capability": "host-repository-execution",
        "scope": "run",
        "constraints": {"verification_commands": ["pytest -q"]},
    }]
    assert ca.verification_command_authorized(grants, "pytest -q") is True
    assert ca.verification_command_authorized(grants, "ruff check .") is False


def test_existing_scoped_grant_does_not_suppress_new_resource_request():
    grant = {
        "capability": "outside-repository",
        "constraints": {"file_paths": ["/tmp/a"]},
    }
    same = {
        "capability": "outside-repository",
        "constraints": {"file_paths": ["/tmp/a"]},
    }
    different = {
        "capability": "outside-repository",
        "constraints": {"file_paths": ["/tmp/b"]},
    }
    assert ca.request_already_authorized([grant], same) is True
    assert ca.request_already_authorized([grant], different) is False


def test_secret_read_grant_does_not_open_bash_secret_reads():
    d = _repo()
    secret = d / ".env"
    sample = d / ".env.example"
    secret.write_text("TOKEN=secret\n")
    sample.write_text("TOKEN=example\n")
    sd = Path(tempfile.mkdtemp())
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
            "constraints": {"file_paths": [str(secret)]},
        }],
    )
    deny_read = set(settings["sandbox"]["filesystem"]["denyRead"])
    assert str(secret.resolve()) in deny_read
    assert str(sample.resolve()) not in deny_read
    assert ".env" in str(settings["permissions"]["allow"])


def test_broad_grep_cannot_bypass_unapproved_repository_secret():
    repo = _repo()
    (repo / ".env").write_text("SECRET=hidden\n")
    safe = repo / "app.py"
    safe.write_text("print('hello')\n")
    env = {
        "CLAUDE_AUTO_REPO_ROOT": str(repo),
        "CLAUDE_AUTO_APPROVED_OUTSIDE_PATHS": "[]",
        "CLAUDE_AUTO_APPROVED_SECRET_PATHS": "[]",
        "CLAUDE_AUTONOMY_PROFILE": "balanced",
    }
    assert _hook_decision(
        "read_scope_guard.py",
        {"tool_name": "Grep", "tool_input": {"pattern": "SECRET", "path": str(repo)}},
        env,
    ) == "deny"
    assert _hook_decision(
        "read_scope_guard.py",
        {"tool_name": "Grep", "tool_input": {"pattern": "hello", "path": str(safe)}},
        env,
    ) == "allow"
    assert _hook_decision(
        "read_scope_guard.py",
        {"tool_name": "Glob", "tool_input": {"pattern": "**/*", "path": str(repo)}},
        env,
    ) == "allow"


def test_read_scope_guard_is_installed_before_any_secret_exception():
    repo = _repo()
    sd = Path(tempfile.mkdtemp())
    settings = ca.make_settings(
        sd,
        "external",
        "balanced",
        {"repo_root": str(repo), "languages": [], "container_files": []},
    )
    assert "read_scope_guard.py" in str(settings.get("hooks", {}))

# ---- planning Environment Blocker cross-audit hardening

def test_plan_graph_rejects_duplicate_dangling_and_cyclic_tasks():
    plan = {
        "acceptance_criteria": ["all work verified"],
        "tasks": [
            {"id": "T001", "title": "one", "depends_on": ["T003"], "verification": ["check"], "risk": "low"},
            {"id": "T002", "title": "two", "depends_on": ["T001"], "verification": ["check"], "risk": "medium"},
            {"id": "T001", "title": "duplicate", "depends_on": [], "verification": ["check"], "risk": "low"},
        ],
    }
    errors = ca.validate_plan_graph(plan)
    assert any("duplicate task id: T001" in x for x in errors)
    assert any("depends on unknown task T003" in x for x in errors)

    cyclic = {
        "acceptance_criteria": ["x"],
        "tasks": [
            {"id": "A", "title": "a", "depends_on": ["B"], "verification": ["x"], "risk": "low"},
            {"id": "B", "title": "b", "depends_on": ["A"], "verification": ["x"], "risk": "low"},
        ],
    }
    assert any("dependency cycle:" in x for x in ca.validate_plan_graph(cyclic))



def test_source_plan_yaml_ids_are_preserved_exactly():
    source = """
steps:
  - id: PV2-CORE-I01.01
    title: First
  - id: "PV2-CORE-I01.02"
    title: Second
  - id: 'PV2-CORE-I01.03'
    title: Third
"""
    assert ca.extract_source_plan_task_ids(source) == [
        "PV2-CORE-I01.01",
        "PV2-CORE-I01.02",
        "PV2-CORE-I01.03",
    ]
    plan = {
        "acceptance_criteria": ["x"],
        "tasks": [
            {"id": "PV2-CORE-I01.01", "title": "one", "depends_on": [], "verification": ["x"], "risk": "low"},
            {"id": "PV2-CORE-I01.03", "title": "three", "depends_on": [], "verification": ["x"], "risk": "low"},
        ],
    }
    errors = ca.validate_source_plan_coverage(source, plan)
    assert any("PV2-CORE-I01.02" in x for x in errors)


def test_source_plan_json_task_ids_are_preserved_exactly():
    source = json.dumps({
        "tasks": [
            {"id": "T001", "title": "one"},
            {"id": "T002", "title": "two"},
        ]
    })
    assert ca.extract_source_plan_task_ids(source) == ["T001", "T002"]
    plan = {
        "acceptance_criteria": ["x"],
        "tasks": [
            {"id": "T001", "title": "one", "depends_on": [], "verification": ["x"], "risk": "low"},
            {"id": "T002", "title": "two", "depends_on": ["T001"], "verification": ["x"], "risk": "low"},
        ],
    }
    assert ca.validate_source_plan_coverage(source, plan) == []


def test_plan_graph_rejects_malformed_task_fields():
    plan = {
        "acceptance_criteria": [""],
        "tasks": [
            {"id": "", "title": "", "depends_on": "T000", "verification": [], "risk": "extreme"},
        ],
    }
    errors = ca.validate_plan_graph(plan)
    assert any(".id must be a non-empty string" in x for x in errors)
    assert any(".depends_on must be a list" in x for x in errors)
    assert any(".verification must be a non-empty list" in x for x in errors)
    assert any(".risk must be low, medium or high" in x for x in errors)
    assert any("acceptance_criteria entries" in x for x in errors)


def test_complete_progress_must_exactly_partition_validated_plan():
    plan = {
        "acceptance_criteria": ["x"],
        "tasks": [
            {"id": "T001", "title": "one", "depends_on": [], "verification": ["x"], "risk": "low"},
            {"id": "T002", "title": "two", "depends_on": ["T001"], "verification": ["x"], "risk": "low"},
        ],
    }
    good = {
        "completed_task_ids": ["T001", "T002"],
        "remaining_task_ids": [],
        "verification": {"tests": "PASS"},
        "external_checkpoint": None,
    }
    assert ca.validate_progress_checkpoint(plan, good, require_partition=True) == []

    incomplete = {
        "completed_task_ids": ["T001"],
        "remaining_task_ids": ["T002"],
        "verification": {"tests": "PASS"},
    }
    errors = ca.validate_progress_checkpoint(plan, incomplete, require_partition=True)
    assert any("still has remaining task ids: T002" in x for x in errors)
    assert any("has not completed task ids: T002" in x for x in errors)

    impossible = {
        "completed_task_ids": ["T002", "UNKNOWN"],
        "remaining_task_ids": [],
        "verification": {"tests": "PASS"},
    }
    errors = ca.validate_progress_checkpoint(plan, impossible, require_partition=True)
    assert any("unknown task UNKNOWN" in x for x in errors)
    assert any("T002 has incomplete dependencies: T001" in x for x in errors)


def test_repository_verification_contract_is_mandatory_and_live():
    import repo_profile as rp
    import verification as vf

    d = _repo()
    contract = d / ".claude-auto" / "verification.json"
    contract.parent.mkdir(parents=True)
    contract.write_text(json.dumps({
        "schema_version": 1,
        "commands": {
            "test": ["python3 scripts/validate-ledgers.py"],
            "lint": ["python3 scripts/validate-control-plane.py"],
        },
    }))
    declared = rp.load_declared_verification_commands(d)
    assert declared["test"] == ["python3 scripts/validate-ledgers.py"]

    prof = {"repo_root": str(d), "build_test_hints": {"test": ["pytest"]}}
    commands = vf._verification_commands(prof)
    assert ("test", "python3 scripts/validate-ledgers.py") in commands
    assert ("lint", "python3 scripts/validate-control-plane.py") in commands
    assert ("test", "pytest") in commands

    # Change the contract after profiling; verification must read the live file.
    contract.write_text(json.dumps({
        "schema_version": 1,
        "commands": {"test": ["python3 scripts/new-validator.py"]},
    }))
    commands = vf._verification_commands(prof)
    assert ("test", "python3 scripts/new-validator.py") in commands


def test_malformed_verification_contract_fails_closed():
    import repo_profile as rp
    import verification as vf

    d = _repo()
    contract = d / ".claude-auto" / "verification.json"
    contract.parent.mkdir(parents=True)
    contract.write_text('{"schema_version":1,"commands":{"test":"not-a-list"}}')
    with pytest.raises(ValueError, match="commands.test must be a list"):
        rp.load_declared_verification_commands(d)
    with pytest.raises(ValueError, match="commands.test must be a list"):
        vf._verification_commands({"repo_root": str(d), "build_test_hints": {}})


def test_completion_anchor_detects_exact_source_movement_but_ignores_cache_noise():
    base = {
        "head": "a",
        "worktree_diff_sha256": "b",
        "index_diff_sha256": "c",
        "untracked_sha256": "d",
        "ignored_sha256": "cache-one",
    }
    assert ca._completion_source_unchanged(base, {**base, "ignored_sha256": "cache-two"})
    for key in ("head", "worktree_diff_sha256", "index_diff_sha256", "untracked_sha256"):
        moved = dict(base)
        moved[key] = "changed"
        assert not ca._completion_source_unchanged(base, moved), key


def test_invalidate_completion_evidence_clears_prior_gate_receipts():
    state = {
        "last_verification_receipts": [{"verdict": "PASS"}],
        "last_runtime_verify_verdict": "PASS",
        "last_correctness_review_verdict": "PASS",
        "last_security_review_verdict": "PASS",
        "last_challenger_verdict": "PASS",
        "completion_anchor": {"head": "old"},
        "completion_anchor_hash": "old-hash",
        "completion_anchor_verified_at": "old-time",
    }
    ca._invalidate_completion_evidence(state, "source moved")
    assert state["last_verification_receipts"] is None
    assert state["last_runtime_verify_verdict"] is None
    assert state["last_correctness_review_verdict"] is None
    assert state["last_security_review_verdict"] is None
    assert state["last_challenger_verdict"] is None
    assert state["completion_anchor"] is None
    assert state["completion_anchor_invalidated_reason"] == "source moved"

