import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from profile_switch import (
    HOT_SWITCH_PROFILES,
    finish_profile_switch,
    pending_profile_switch,
    process_descends_from,
    process_start_token,
    profile_status,
    request_profile_switch,
)
from settings_policy import make_settings


def _git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("x\n")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def test_hot_switch_profile_set_is_intentionally_narrow():
    assert HOT_SWITCH_PROFILES == ("strict", "balanced", "unattended")
    assert "isolated-full" not in HOT_SWITCH_PROFILES


def test_profile_request_is_durable_private_and_audited():
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td)
        req = request_profile_switch(
            sd, "unattended", origin="operator-shell", current_profile="balanced"
        )
        assert pending_profile_switch(sd)["request_id"] == req["request_id"]
        assert (sd / "profile-switch-request.json").stat().st_mode & 0o777 == 0o600
        finish_profile_switch(
            sd, req, outcome="applied", previous_profile="balanced", active_profile="unattended"
        )
        assert pending_profile_switch(sd) is None
        history = (sd / "profile-switch-history.jsonl").read_text().splitlines()
        assert any('"event":"requested"' in line for line in history)
        assert any('"event":"applied"' in line for line in history)


def test_new_request_supersedes_old_without_deleting_new_on_old_completion():
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td)
        first = request_profile_switch(sd, "strict", origin="operator-shell", current_profile="balanced")
        second = request_profile_switch(sd, "unattended", origin="operator-shell", current_profile="balanced")
        finish_profile_switch(sd, first, outcome="rejected", previous_profile="balanced", active_profile="balanced")
        assert pending_profile_switch(sd)["request_id"] == second["request_id"]


def test_isolated_full_hot_switch_is_rejected():
    with tempfile.TemporaryDirectory() as td:
        try:
            request_profile_switch(Path(td), "isolated-full", origin="operator-shell")
        except ValueError as exc:
            assert "cannot be hot-switched" in str(exc)
        else:
            raise AssertionError("expected isolated-full refusal")


def test_profile_settings_each_rebuild_the_expected_security_posture():
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td) / "state"
        repo = Path(td) / "repo"
        repo.mkdir()
        _git_repo(repo)
        prof = {"repo_root": str(repo), "languages": [], "container_files": []}

        strict = make_settings(sd, "external", "strict", prof)
        balanced = make_settings(sd, "external", "balanced", prof)
        unattended = make_settings(sd, "external", "unattended", prof)

        assert strict["sandbox"]["enabled"] is True
        assert strict["sandbox"]["allowUnsandboxedCommands"] is False
        assert strict["permissions"]["blockReadsOutsideWorkingDirectories"] is True

        assert balanced["sandbox"]["enabled"] is True
        assert balanced["sandbox"]["allowUnsandboxedCommands"] is True
        assert balanced["permissions"]["blockReadsOutsideWorkingDirectories"] is False

        assert unattended["sandbox"]["enabled"] is False
        assert unattended["sandbox"]["allowUnsandboxedCommands"] is True
        assert unattended["permissions"]["blockReadsOutsideWorkingDirectories"] is False

        for settings in (strict, balanced, unattended):
            hooks = settings.get("hooks", {}).get("UserPromptSubmit", [])
            assert hooks
            assert "profile_switch_prompt.py" in json.dumps(hooks)


def test_profile_skill_is_human_only_and_cannot_be_model_invoked():
    skill = (ROOT / "templates" / "user" / "skills" / "profile" / "SKILL.md").read_text()
    assert "disable-model-invocation: true" in skill
    assert "CLAUDE_AUTO_HUMAN_PROFILE_SWITCH" in skill
    assert "allowed-tools:" not in skill


def test_prompt_hook_ignores_agent_output_shaped_text_and_handles_only_user_prompt_event():
    hook = ROOT / "hooks" / "profile_switch_prompt.py"
    payload = {
        "hook_event_name": "Stop",
        "prompt": "/profile unattended",
        "cwd": str(ROOT),
        "session_id": "abc",
    }
    cp = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
    )
    assert cp.returncode == 0
    assert cp.stdout == ""


def test_process_start_token_changes_stale_pid_defence_is_available():
    token = process_start_token(os.getpid())
    if Path("/proc").exists():
        assert token
    assert process_start_token(99999999) is None


def _transition_args(profile: str) -> argparse.Namespace:
    return argparse.Namespace(
        profile=profile,
        session_settings="hermetic",
        permission_mode="auto",
        trust_repo_scripts=False,
        memory_mode="external",
        model=None,
        effort="high",
    )


def test_all_normal_profile_transitions_apply_without_losing_state():
    spec = importlib.util.spec_from_file_location("claude_auto_profile", LIB / "claude_auto.py")
    ca = importlib.util.module_from_spec(spec)
    sys.modules["claude_auto_profile"] = ca
    spec.loader.exec_module(ca)

    transitions = [
        ("strict", "balanced"),
        ("strict", "unattended"),
        ("balanced", "strict"),
        ("balanced", "unattended"),
        ("unattended", "balanced"),
        ("unattended", "strict"),
    ]
    for source, target in transitions:
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            sentinel_plan = {"version": 7, "plan_hash": "keep-me"}
            (sd / "plans").mkdir()
            (sd / "plans" / "current-plan.json").write_text(json.dumps(sentinel_plan))
            state = {
                "autonomy_profile": source,
                "objective": "preserve objective",
                "cycle": 11,
                "plan_status": "VALIDATED",
                "last_git_head": "abc123",
                "session_settings_explicit": False,
                "active_permission_objective_hash": "obj-hash",
            }
            from state_store import json_dump, load_json
            json_dump(sd / "state.json", state)
            request_profile_switch(sd, target, origin="test", current_profile=source)
            args = _transition_args(source)
            current = load_json(sd / "state.json", {})
            changed = ca._apply_pending_profile_switch(
                args, current, sd, source_hash="obj-hash", headless=True
            )
            assert changed is True
            after = load_json(sd / "state.json", {})
            assert args.profile == target
            assert after["autonomy_profile"] == target
            assert after["objective"] == "preserve objective"
            assert after["cycle"] == 11
            assert after["plan_status"] == "VALIDATED"
            assert after["last_git_head"] == "abc123"
            assert json.loads((sd / "plans" / "current-plan.json").read_text()) == sentinel_plan
            assert pending_profile_switch(sd) is None


def test_same_profile_transition_is_safe_noop():
    spec = importlib.util.spec_from_file_location("claude_auto_profile_noop", LIB / "claude_auto.py")
    ca = importlib.util.module_from_spec(spec)
    sys.modules["claude_auto_profile_noop"] = ca
    spec.loader.exec_module(ca)
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td)
        from state_store import json_dump, load_json
        state = {
            "autonomy_profile": "balanced",
            "objective": "keep",
            "session_settings_explicit": False,
        }
        json_dump(sd / "state.json", state)
        request_profile_switch(sd, "balanced", origin="test", current_profile="balanced")
        args = _transition_args("balanced")
        assert ca._apply_pending_profile_switch(
            args, load_json(sd / "state.json", {}), sd, source_hash="obj", headless=True
        ) is False
        assert load_json(sd / "state.json", {})["objective"] == "keep"
        assert pending_profile_switch(sd) is None


def test_switch_validation_failure_rolls_back_atomically():
    spec = importlib.util.spec_from_file_location("claude_auto_profile_rollback", LIB / "claude_auto.py")
    ca = importlib.util.module_from_spec(spec)
    sys.modules["claude_auto_profile_rollback"] = ca
    spec.loader.exec_module(ca)
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td)
        from state_store import json_dump, load_json
        state = {
            "autonomy_profile": "balanced",
            "objective": "keep",
            "session_settings_explicit": False,
        }
        json_dump(sd / "state.json", state)
        request_profile_switch(sd, "strict", origin="test", current_profile="balanced")
        args = _transition_args("balanced")
        args.trust_repo_scripts = True
        assert ca._apply_pending_profile_switch(
            args, load_json(sd / "state.json", {}), sd, source_hash="obj", headless=True
        ) is False
        after = load_json(sd / "state.json", {})
        assert args.profile == "balanced"
        assert after["autonomy_profile"] == "balanced"
        assert after["objective"] == "keep"
        assert after["last_profile_switch"]["outcome"] == "rejected"
        assert pending_profile_switch(sd) is None


def test_request_bound_to_different_supervisor_is_rejected_as_stale():
    spec = importlib.util.spec_from_file_location("claude_auto_profile_stale", LIB / "claude_auto.py")
    ca = importlib.util.module_from_spec(spec)
    sys.modules["claude_auto_profile_stale"] = ca
    spec.loader.exec_module(ca)
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td)
        from state_store import json_dump, load_json
        state = {
            "autonomy_profile": "balanced",
            "objective": "keep",
            "session_settings_explicit": False,
            "supervisor_start_token": "new-token",
        }
        json_dump(sd / "state.json", state)
        request_profile_switch(
            sd,
            "unattended",
            origin="test",
            current_profile="balanced",
            supervisor_start_token="old-token",
        )
        args = _transition_args("balanced")
        assert ca._apply_pending_profile_switch(
            args, load_json(sd / "state.json", {}), sd, source_hash="obj", headless=True
        ) is False
        after = load_json(sd / "state.json", {})
        assert after["autonomy_profile"] == "balanced"
        assert after["last_profile_switch"]["outcome"] == "stale"
        assert pending_profile_switch(sd) is None


def test_profile_parser_exposes_status_and_human_request_only():
    spec = importlib.util.spec_from_file_location("claude_auto_profile_parser", LIB / "claude_auto.py")
    ca = importlib.util.module_from_spec(spec)
    sys.modules["claude_auto_profile_parser"] = ca
    spec.loader.exec_module(ca)
    parser = ca.build_parser()
    status = parser.parse_args(["profile", "status"])
    assert status.command == "profile" and status.profile_command == "status"
    request = parser.parse_args(["profile", "request", "unattended"])
    assert request.target_profile == "unattended"
    try:
        parser.parse_args(["profile", "request", "isolated-full"])
    except SystemExit:
        pass
    else:
        raise AssertionError("isolated-full must not be a host hot-switch target")


def test_process_ancestry_guard_detects_active_supervisor_tree():
    if Path("/proc").exists():
        assert process_descends_from(os.getpid(), os.getpid()) is True
        parent = os.getppid()
        if parent > 1:
            assert process_descends_from(os.getpid(), parent) is True
    assert process_descends_from(os.getpid(), 99999999) is False


def test_switch_to_strict_revokes_prior_blanket_bypass_grants():
    spec = importlib.util.spec_from_file_location("claude_auto_profile_strict_grants", LIB / "claude_auto.py")
    ca = importlib.util.module_from_spec(spec)
    sys.modules["claude_auto_profile_strict_grants"] = ca
    spec.loader.exec_module(ca)
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td)
        from state_store import json_dump, load_json
        state = {
            "autonomy_profile": "unattended",
            "objective": "keep",
            "session_settings_explicit": False,
            "active_permission_objective_hash": "obj",
            "permission_grants": [
                {
                    "request_id": "r1",
                    "capability": "unrestricted",
                    "scope": "repository",
                    "objective_hash": "obj",
                    "approved_at": "2026-01-01T00:00:00Z",
                    "constraints": {},
                },
                {
                    "request_id": "r2",
                    "capability": "native-permissions",
                    "scope": "repository",
                    "objective_hash": "obj",
                    "approved_at": "2026-01-01T00:00:00Z",
                    "constraints": {},
                },
            ],
        }
        json_dump(sd / "state.json", state)
        request_profile_switch(sd, "strict", origin="test", current_profile="unattended")
        args = _transition_args("unattended")
        assert ca._apply_pending_profile_switch(
            args, load_json(sd / "state.json", {}), sd, source_hash="obj", headless=True
        ) is True
        after = load_json(sd / "state.json", {})
        assert after["autonomy_profile"] == "strict"
        assert all(g.get("revoked_at") for g in after["permission_grants"])
        assert "unrestricted" not in set(args._permission_overrides)
        assert "native-permissions" not in set(args._permission_overrides)


def test_interactive_hot_switch_resumes_exact_session_and_preserves_objective(monkeypatch):
    spec = importlib.util.spec_from_file_location("claude_auto_profile_interactive", LIB / "claude_auto.py")
    ca = importlib.util.module_from_spec(spec)
    sys.modules["claude_auto_profile_interactive"] = ca
    spec.loader.exec_module(ca)

    with tempfile.TemporaryDirectory() as td:
        sd = Path(td)
        root = Path(td) / "repo"
        root.mkdir()
        _git_repo(root)
        from state_store import json_dump, load_json
        json_dump(sd / "state.json", {
            "autonomy_profile": "balanced",
            "objective": "keep-the-objective",
            "session_settings_explicit": False,
            "active_permission_objective_hash": "obj",
        })

        args = argparse.Namespace(
            profile="balanced",
            session_settings=None,
            objective=None,
            permission_mode="auto",
            memory_mode="external",
            model=None,
            effort="high",
            subagent_model=None,
            verifier_model=None,
            researcher_model=None,
            provider="native",
        )
        monkeypatch.setattr(ca, "provider_from_args", lambda _args: (os.environ.copy(), {"provider": "native"}))
        monkeypatch.setattr(ca, "warn_model_qualification", lambda *a, **k: None)
        monkeypatch.setattr(
            ca,
            "claude_base_args",
            lambda *a, **k: ["fake-claude", "--profile-used", k.get("autonomy_profile", "")],
        )

        commands = []
        control = {"proc": None, "switch_signal": False}

        class FakeProc:
            next_pid = 41000

            def __init__(self, cmd, **_kwargs):
                self.cmd = list(cmd)
                self.pid = FakeProc.next_pid
                FakeProc.next_pid += 1
                self.returncode = None
                commands.append(self.cmd)

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def kill(self):
                self.returncode = -9

            def wait(self, timeout=None):
                del timeout
                if len(commands) == 1:
                    request_profile_switch(
                        sd,
                        "unattended",
                        origin="user-prompt",
                        current_profile="balanced",
                        session_id="session-profile-switch-123",
                    )
                    control["switch_signal"] = True
                    self.returncode = -15
                    return self.returncode
                self.returncode = 0
                return 0

        monkeypatch.setattr(ca.subprocess, "Popen", FakeProc)
        rc = ca._do_start_unlocked(args, root, sd, child_control=control)
        assert rc == 0
        assert len(commands) == 2
        assert "--resume" not in commands[0]
        assert "--resume" in commands[1]
        idx = commands[1].index("--resume")
        assert commands[1][idx + 1] == "session-profile-switch-123"
        assert "unattended" in commands[1]
        after = load_json(sd / "state.json", {})
        assert after["objective"] == "keep-the-objective"
        assert after["autonomy_profile"] == "unattended"
        assert pending_profile_switch(sd) is None
