
import importlib.util
import json
import os
import signal
import shutil
import stat
import subprocess
import tempfile
import time
import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOD_PATH = ROOT / "lib" / "claude_auto.py"
spec = importlib.util.spec_from_file_location("claude_auto", MOD_PATH)
ca = importlib.util.module_from_spec(spec)
import sys
sys.modules["claude_auto"] = ca
spec.loader.exec_module(ca)


@pytest.fixture(autouse=True)
def _scrub_ambient_provider_environment(monkeypatch):
    """Keep fake-provider tests independent of the outer Claude/session env."""
    for name in (
        "CLAUDECODE",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "CLAUDE_CONFIG_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


def git_init(r: Path):
    subprocess.run(["git", "init", "-q", str(r)], check=True)


def git_commit_with_origin(r: Path, remote_dir: Path):
    subprocess.run(["git","-C",str(r),"config","user.email","x@example.com"],check=True)
    subprocess.run(["git","-C",str(r),"config","user.name","X"],check=True)
    subprocess.run(["git","-C",str(r),"add","-A"],check=True)
    subprocess.run(["git","-C",str(r),"commit","-qm","baseline"],check=True)
    subprocess.run(["git","init","--bare","-q",str(remote_dir)],check=True)
    subprocess.run(["git","-C",str(r),"remote","add","origin",str(remote_dir)],check=True)
    subprocess.run(["git","-C",str(r),"push","-q","-u","origin","HEAD"],check=True)


def test_status_parser():
    assert ca.parse_status("x\nAUTONOMY_STATUS: COMPLETE\nAUTONOMY_SUMMARY: done") == ("COMPLETE", "done")
    assert ca.parse_status("nothing") == ("CONTINUE", None)


def test_task_result_parser_is_bounded_and_fail_closed():
    parsed = ca.parse_task_result(
        'AUTONOMY_TASK_RESULT: '
        '{"task_id":"T1","status":"READY_FOR_ACCEPTANCE",'
        '"summary":"ready","verification_claims":["unit tests passed"]}'
    )
    assert parsed == {
        "task_id": "T1",
        "status": "READY_FOR_ACCEPTANCE",
        "summary": "ready",
        "verification_claims": ["unit tests passed"],
    }
    assert ca.parse_task_result(
        'AUTONOMY_TASK_RESULT: {"task_id":"T1","status":"COMPLETE"}'
    ) is None
    assert ca.parse_task_result(
        'AUTONOMY_TASK_RESULT: {"status":"CONTINUE"}'
    ) is None


def test_p4_task_operator_cli_routes_workspace_status_and_abort(monkeypatch, capsys):
    root = Path("/tmp/claude-auto-p4-operator-test").resolve()
    monkeypatch.setattr(ca, "find_repo_root", lambda _raw=None: root)

    monkeypatch.setattr(
        ca,
        "task_workspace_status",
        lambda resolved: {
            "repository": str(resolved),
            "state_dir": "/tmp/state",
            "active": {"task_id": "T1", "lifecycle_state": "ACTIVE"},
        },
    )
    assert ca.main([
        "tasks",
        "workspace-status",
        "--repo",
        str(root),
    ]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["active"]["task_id"] == "T1"
    assert status["active"]["lifecycle_state"] == "ACTIVE"

    calls = {}
    monkeypatch.setattr(
        ca,
        "require_top_level_operator",
        lambda resolved, operation: calls.update({
            "operator_root": resolved,
            "operation": operation,
        }),
    )
    monkeypatch.setattr(
        ca,
        "abort_task_workspace",
        lambda resolved, reason: {
            "status": "ABANDONED_PRESERVED",
            "task_id": "T1",
            "preservation_sha": "a" * 40,
            "reason": reason,
        },
    )
    assert ca.main([
        "tasks",
        "abort",
        "--repo",
        str(root),
        "--reason",
        "operator requested stop",
    ]) == 0
    aborted = json.loads(capsys.readouterr().out)
    assert aborted["status"] == "ABANDONED_PRESERVED"
    assert aborted["reason"] == "operator requested stop"
    assert calls == {
        "operator_root": root,
        "operation": "P4 task workspace abort",
    }

    calls.clear()
    monkeypatch.setattr(
        ca,
        "begin_task_workspace",
        lambda resolved, task_id=None: {
            "status": "ACTIVE",
            "task_id": task_id or "T1",
            "task_worktree": "/tmp/task-worktree",
        },
    )
    assert ca.main([
        "tasks",
        "begin",
        "T1",
        "--repo",
        str(root),
    ]) == 0
    begun = json.loads(capsys.readouterr().out)
    assert begun["status"] == "ACTIVE"
    assert begun["task_id"] == "T1"
    assert calls == {
        "operator_root": root,
        "operation": "P4 task workspace begin",
    }

    calls.clear()
    monkeypatch.setattr(
        ca,
        "seal_task_candidate",
        lambda resolved: {
            "status": "CANDIDATE",
            "task_id": "T1",
            "candidate_sha": "b" * 40,
        },
    )
    assert ca.main([
        "tasks",
        "candidate",
        "--repo",
        str(root),
    ]) == 0
    candidate = json.loads(capsys.readouterr().out)
    assert candidate["candidate_sha"] == "b" * 40
    assert calls == {
        "operator_root": root,
        "operation": "P4 task candidate sealing",
    }

    calls.clear()
    verify_calls = []
    monkeypatch.setattr(
        ca,
        "verify_task_candidate_deterministic",
        lambda resolved, **kwargs: (
            verify_calls.append(("deterministic", resolved, kwargs))
            or {
                "status": "PASS",
                "task_id": "T1",
                "candidate_sha": "b" * 40,
            }
        ),
    )
    monkeypatch.setattr(
        ca,
        "verify_task_candidate_independent",
        lambda resolved, args: (
            verify_calls.append(("independent", resolved, args.provider))
            or {
                "status": "VERIFIED",
                "task_id": "T1",
                "candidate_sha": "b" * 40,
            }
        ),
    )
    assert ca.main([
        "tasks",
        "verify",
        "--repo",
        str(root),
        "--timeout",
        "321",
        "--trust-repo-scripts",
    ]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["status"] == "VERIFIED"
    assert verified["stage"] == "independent"
    assert [row[0] for row in verify_calls] == [
        "deterministic",
        "independent",
    ]
    assert verify_calls[0][2]["timeout"] == 321
    assert verify_calls[0][2]["trust_repo_scripts"] is True
    assert verify_calls[1][2] == "native"
    assert calls == {
        "operator_root": root,
        "operation": "P4 task candidate verification",
    }

    calls.clear()
    verify_calls.clear()
    monkeypatch.setattr(
        ca,
        "verify_task_candidate_deterministic",
        lambda resolved, **kwargs: (
            verify_calls.append(("deterministic", resolved, kwargs))
            or {
                "status": "UNVERIFIED",
                "task_id": "T1",
                "candidate_sha": "b" * 40,
                "findings": ["safe execution boundary unavailable"],
            }
        ),
    )
    assert ca.main([
        "tasks",
        "verify",
        "--repo",
        str(root),
    ]) == 2
    unverified = json.loads(capsys.readouterr().out)
    assert unverified["status"] == "UNVERIFIED"
    assert unverified["stage"] == "deterministic"
    assert [row[0] for row in verify_calls] == ["deterministic"]
    assert calls == {
        "operator_root": root,
        "operation": "P4 task candidate verification",
    }

    calls.clear()
    monkeypatch.setattr(
        ca,
        "accept_verified_task",
        lambda resolved, **kwargs: {
            "status": "ACCEPTED_PENDING_CLEANUP",
            "task_id": "T1",
            "candidate_sha": "b" * 40,
            "remote": kwargs.get("remote"),
        },
    )
    assert ca.main([
        "tasks",
        "accept",
        "--repo",
        str(root),
        "--remote",
        "origin",
    ]) == 0
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["status"] == "ACCEPTED_PENDING_CLEANUP"
    assert accepted["remote"] == "origin"
    assert calls == {
        "operator_root": root,
        "operation": "P4 task candidate acceptance",
    }

    calls.clear()
    monkeypatch.setattr(
        ca,
        "cleanup_accepted_task_workspace",
        lambda resolved: {
            "status": "CLEAN",
            "scheduler_status": "COMPLETE",
            "accepted_task_id": "T1",
        },
    )
    assert ca.main([
        "tasks",
        "cleanup",
        "--repo",
        str(root),
    ]) == 0
    cleaned = json.loads(capsys.readouterr().out)
    assert cleaned["scheduler_status"] == "COMPLETE"
    assert calls == {
        "operator_root": root,
        "operation": "P4 accepted task cleanup",
    }


def test_profile_detects_python_and_js():
    with tempfile.TemporaryDirectory() as td:
        r = Path(td)
        git_init(r)
        (r / "pyproject.toml").write_text("[project]\nname='x'\n[tool.pytest.ini_options]\n")
        (r / "package.json").write_text(json.dumps({"scripts": {"test": "vitest", "build": "vite build"}}))
        (r / "a.py").write_text("print('x')\n")
        (r / "a.ts").write_text("export const x=1\n")
        p = ca.profile_repo(r)
        assert "python" in p.languages
        assert "typescript" in p.languages
        assert "pyright-lsp" in p.lsp_candidates
        assert "typescript-lsp" in p.lsp_candidates
        assert "serena" in p.recommended_plugins


def test_git_inventory_finds_deep_manifest():
    with tempfile.TemporaryDirectory() as td:
        r = Path(td)
        git_init(r)
        deep = r / "services" / "a" / "components" / "b" / "internal" / "pkg"
        deep.mkdir(parents=True)
        (deep / "pyproject.toml").write_text("[project]\nname='deep'\n")
        (deep / "main.py").write_text("x=1\n")
        p = ca.profile_repo(r)
        assert "services/a/components/b/internal/pkg/pyproject.toml" in p.manifests
        assert "python" in p.languages


def test_nested_invocation_resolves_full_git_workspace_root():
    with tempfile.TemporaryDirectory() as td:
        r = Path(td)
        git_init(r)
        nested = r / "core" / "control" / "src"
        nested.mkdir(parents=True)
        (r / "README.md").write_text("workspace\n")
        assert ca.find_repo_root(nested) == r.resolve()


def test_activate_writes_outside_repo_and_private_permissions():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh:
        r = Path(td)
        git_init(r)
        (r / "README.md").write_text("hello")
        os.environ["CLAUDE_AUTONOMY_HOME"] = dh
        try:
            before = {str(p.relative_to(r)): p.read_bytes() for p in r.rglob("*") if p.is_file() and ".git" not in p.parts}
            sd = ca.activate(r)
            after = {str(p.relative_to(r)): p.read_bytes() for p in r.rglob("*") if p.is_file() and ".git" not in p.parts}
            assert before == after
            assert stat.S_IMODE(sd.stat().st_mode) == 0o700
            assert stat.S_IMODE((sd / "state.json").stat().st_mode) == 0o600
            assert stat.S_IMODE((sd / "logs").stat().st_mode) == 0o700
        finally:
            del os.environ["CLAUDE_AUTONOMY_HOME"]


def test_readonly_settings_have_hard_boundaries():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh:
        r = Path(td)
        git_init(r)
        (r / "README.md").write_text("x")
        os.environ["CLAUDE_AUTONOMY_HOME"] = dh
        try:
            sd = ca.activate(r)
            st = json.loads((sd / "settings-readonly.json").read_text())
            assert "Edit" in st["permissions"]["deny"]
            assert str(r.resolve()) in st["sandbox"]["filesystem"]["denyWrite"]
            pre_entries = st["hooks"]["PreToolUse"]
            readonly = next(
                x for x in pre_entries
                if x.get("matcher") == "Bash"
                and any("readonly_guard.py" in h.get("command", "") for h in x.get("hooks", []))
            )
            command = readonly["hooks"][0]["command"]
            assert command.startswith("python3 ")
            assert sys.executable not in command or sys.executable == "python3"
        finally:
            del os.environ["CLAUDE_AUTONOMY_HOME"]


def test_readonly_guard_denies_mutation_and_allows_git_status():
    guard = ROOT / "hooks" / "readonly_guard.py"
    def call(command):
        cp = subprocess.run(
            [sys.executable, str(guard)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
            text=True, capture_output=True, check=True,
        )
        return json.loads(cp.stdout)["hookSpecificOutput"]["permissionDecision"]
    assert call("git status --short") == "allow"
    assert call("git diff --stat") == "allow"
    assert call("rm -rf /tmp/x") == "deny"
    assert call("echo x > file") == "deny"
    assert call("git checkout -- file") == "deny"


def test_setup_dry_run_has_no_repo_writes(capsys):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh:
        r = Path(td)
        git_init(r)
        (r / "Cargo.toml").write_text("[package]\nname='x'\nversion='0.1.0'\n")
        before = {str(p.relative_to(r)): p.read_bytes() for p in r.rglob("*") if p.is_file() and ".git" not in p.parts}
        os.environ["CLAUDE_AUTONOMY_HOME"] = dh
        try:
            ns = type("Args", (), {"repo": str(r), "dry_run": True, "install_plugins": False})()
            assert ca.cmd_setup(ns) == 0
            after = {str(p.relative_to(r)): p.read_bytes() for p in r.rglob("*") if p.is_file() and ".git" not in p.parts}
            assert before == after
            assert '"repository_writes": []' in capsys.readouterr().out
        finally:
            del os.environ["CLAUDE_AUTONOMY_HOME"]


def test_progress_fingerprint_tracks_repository_not_summary():
    snap_a = {"head":"a","worktree_diff_sha256":"1","index_diff_sha256":"2","untracked_sha256":"3"}
    snap_b = {"head":"a","worktree_diff_sha256":"9","index_diff_sha256":"2","untracked_sha256":"3"}
    a = ca.progress_fingerprint("CONTINUE", "same", snap_a)
    b = ca.progress_fingerprint("CONTINUE", "different wording", snap_a)
    c = ca.progress_fingerprint("CONTINUE", "same", snap_b)
    assert a == b
    assert a != c


def test_progress_fingerprint_counts_p4_accepted_task_state():
    snap = {
        "head": "a",
        "worktree_diff_sha256": "1",
        "index_diff_sha256": "2",
        "untracked_sha256": "3",
        "verification_contract_sha256": "4",
    }
    before = {
        "accepted_tasks": {},
        "active_task_id": "T1",
        "active_task_workspace_sha256": "w1",
        "task_workspace_lifecycle_state": "ACTIVE",
    }
    after = {
        "accepted_tasks": {
            "T1": {
                "product_sha": "a",
                "task_spec_sha256": "t1",
                "acceptance_sha256": "accepted",
            },
        },
        "active_task_id": None,
        "active_task_workspace_sha256": None,
        "task_workspace_lifecycle_state": None,
    }
    assert ca.progress_fingerprint("CONTINUE", None, snap, before) != (
        ca.progress_fingerprint("CONTINUE", None, snap, after)
    )


def test_git_snapshot_changes_when_content_changes_but_status_name_same():
    with tempfile.TemporaryDirectory() as td:
        r=Path(td)
        git_init(r)
        subprocess.run(["git","-C",str(r),"config","user.email","x@example.com"],check=True)
        subprocess.run(["git","-C",str(r),"config","user.name","X"],check=True)
        f=r/"a.txt"; f.write_text("one\n")
        subprocess.run(["git","-C",str(r),"add","a.txt"],check=True)
        subprocess.run(["git","-C",str(r),"commit","-qm","init"],check=True)
        f.write_text("two\n")
        a=ca.git_snapshot(r)
        f.write_text("three\n")
        b=ca.git_snapshot(r)
        assert a["status_short"] == b["status_short"]
        assert a["worktree_diff_sha256"] != b["worktree_diff_sha256"]


def test_usage_from_result():
    u = ca.usage_from_result({
        "usage": {"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 30},
        "total_cost_usd": 0.0123, "num_turns": 2, "duration_ms": 1500,
    })
    assert u["input_tokens"] == 10
    assert u["cache_read_input_tokens"] == 30
    assert u["total_cost_usd"] == 0.0123


def test_provider_env_openrouter_shared_config_and_no_persisted_secret():
    os.environ["OPENROUTER_API_KEY"] = "test-secret-value"
    os.environ["CLAUDE_CONFIG_DIR"] = "/tmp/native-config"
    try:
        env, detail = ca.provider_env("openrouter")
        assert env["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "test-secret-value"
        assert env["ANTHROPIC_API_KEY"] == ""
        assert env["CLAUDE_CONFIG_DIR"] == "/tmp/native-config"
        assert env["CLAUDE_CODE_GATEWAY_HINT_HEADERS"] == "1"
        assert env["CLAUDE_CODE_ATTRIBUTION_HEADER"] == "0"
        assert "test-secret-value" not in json.dumps(detail)
        assert detail["isolated_profile"] is False
    finally:
        del os.environ["OPENROUTER_API_KEY"]
        del os.environ["CLAUDE_CONFIG_DIR"]


def test_non_native_provider_can_opt_into_isolated_config():
    with tempfile.TemporaryDirectory() as dh:
        os.environ["CLAUDE_AUTONOMY_HOME"] = dh
        os.environ["OPENROUTER_API_KEY"] = "test-secret-value"
        try:
            env, detail = ca.provider_env("openrouter", isolate_provider_profile=True)
            cfg=Path(env["CLAUDE_CONFIG_DIR"])
            assert cfg.exists()
            assert str(cfg).startswith(dh)
            assert stat.S_IMODE(cfg.stat().st_mode) == 0o700
            assert detail["isolated_profile"] is True
        finally:
            del os.environ["CLAUDE_AUTONOMY_HOME"]
            del os.environ["OPENROUTER_API_KEY"]


def test_custom_gateway_clears_inherited_anthropic_credentials():
    os.environ["ANTHROPIC_AUTH_TOKEN"] = "must-not-leak"
    os.environ["ANTHROPIC_API_KEY"] = "must-not-leak-either"
    try:
        env, detail = ca.provider_env("custom", gateway_url="http://127.0.0.1:9999", gateway_token_env="NONEXISTENT_GATEWAY_KEY")
        assert env["ANTHROPIC_AUTH_TOKEN"] == ""
        assert env["ANTHROPIC_API_KEY"] == ""
        assert env["CLAUDE_CODE_ATTRIBUTION_HEADER"] == "0"
        assert detail["base_url"] == "http://127.0.0.1:9999"
    finally:
        del os.environ["ANTHROPIC_AUTH_TOKEN"]
        del os.environ["ANTHROPIC_API_KEY"]


def test_runtime_agents_can_route_roles_independently():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh:
        r = Path(td); git_init(r); (r / "README.md").write_text("hello\n")
        os.environ["CLAUDE_AUTONOMY_HOME"] = dh
        try:
            sd = ca.activate(r)
            agents = json.loads(ca.make_runtime_agents(sd, "review-model", "cheap-model"))
            assert agents["autonomy-verifier"]["model"] == "review-model"
            assert agents["autonomy-researcher"]["model"] == "cheap-model"
        finally:
            del os.environ["CLAUDE_AUTONOMY_HOME"]


def test_parse_challenger_and_qualification_protocols():
    verdict, summary = ca.parse_challenger("CHALLENGER_VERDICT: FAIL\nCHALLENGER_SUMMARY: missing test")
    assert verdict == "FAIL"
    assert summary == "missing test"
    q = ca.parse_qualification('MODEL_QUALIFICATION: {"manifests":[]}')
    assert q == {"manifests": []}


def test_redaction_removes_env_secret():
    env={"OPENROUTER_API_KEY":"abcdefghijklmnop"}
    assert "abcdefghijklmnop" not in ca.redact_text("token abcdefghijklmnop", env)


def test_goal_prompt_starts_native_goal():
    prompt = ca.build_goal_prompt("finish tests", {"last_summary":None}, {"languages":["python"]}, 20)
    assert prompt.startswith("/goal ")
    assert "finish tests" in prompt
    assert "20" in prompt


def test_agents_have_no_shell_or_write_tools():
    agents = json.loads((ROOT / "templates" / "agents.json").read_text())
    for agent in agents.values():
        assert "Bash" not in agent["tools"]
        assert "Edit" not in agent["tools"]
        assert "Write" not in agent["tools"]


def test_status_presence_is_distinct_from_default_continue():
    assert ca.has_explicit_status("AUTONOMY_STATUS: CONTINUE")
    assert not ca.has_explicit_status("finished without protocol")


def test_readonly_evidence_bundle_is_external_private_and_no_repo_write():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh:
        r = Path(td); git_init(r)
        subprocess.run(["git","-C",str(r),"config","user.email","x@example.com"],check=True)
        subprocess.run(["git","-C",str(r),"config","user.name","X"],check=True)
        (r/"a.txt").write_text("one\n")
        subprocess.run(["git","-C",str(r),"add","a.txt"],check=True)
        subprocess.run(["git","-C",str(r),"commit","-qm","init"],check=True)
        (r/"a.txt").write_text("two\n")
        os.environ["CLAUDE_AUTONOMY_HOME"] = dh
        try:
            sd = ca.activate(r)
            before = ca.git_snapshot(r)
            ev = ca.build_readonly_evidence(r, sd)
            after = ca.git_snapshot(r)
            assert sd in ev.parents
            assert ev.parent == sd / "readonly-evidence"
            assert stat.S_IMODE(ev.stat().st_mode) == 0o600
            assert before == after
            obj=json.loads(ev.read_text())
            assert "two" in obj["worktree_diff"]["output"]
        finally:
            del os.environ["CLAUDE_AUTONOMY_HOME"]


def test_compact_log_omits_transcripts_by_default():
    log = ca.compact_run_log(
        cycle=1, started_at="a", finished_at="b", provider={"provider":"native"},
        model=None, returncode=0, result_text="done", session_id="s",
        raw_obj={"usage":{"input_tokens":2}}, stderr="secret tail",
        git_before={}, git_after={}, env={}, retain_transcripts=False, stdout="full stdout"
    )
    assert "stdout" not in log and "parsed_result" not in log and "stderr" not in log
    assert log["usage"]["input_tokens"] == 2


def make_fake_claude(bin_dir: Path):
    script = bin_dir / "claude"
    script.write_text("""#!/usr/bin/env python3
import json, os, sys
args=sys.argv[1:]
capture=os.environ.get("FAKE_CLAUDE_CAPTURE")
if capture:
    with open(capture,"a") as f:
        f.write(json.dumps({"args":args,"base":os.environ.get("ANTHROPIC_BASE_URL"),"stop_cap":os.environ.get("CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"),"headless":os.environ.get("CLAUDE_AUTO_HEADLESS"),"cwd":os.getcwd()})+"\\n")
if "--version" in args:
    print("2.1.283 (Claude Code)")
    raise SystemExit(0)
if len(args)>=2 and args[0]=="auth" and args[1]=="status":
    print("logged in")
    raise SystemExit(0)
prompt=args[-1] if args else ""
if os.environ.get("FAIL_NATIVE")=="1" and not os.environ.get("ANTHROPIC_BASE_URL") and "-p" in args:
    print("native down", file=sys.stderr)
    raise SystemExit(9)
if "PLAN_CONTROL:" in prompt:
    result='PLAN_CONTROL: {"verdict":"READY","complexity":"standard","summary":"validated","plan_markdown":"# Validated plan\\n\\n1. Implement the objective.\\n2. Verify acceptance criteria.","acceptance_criteria":["Objective behaviour is implemented and verified"],"tasks":[{"id":"T001","title":"Implement objective","depends_on":[],"verification":["Run relevant tests"],"risk":"medium"}],"assumptions":[],"risks":[],"blockers":[]}'
elif "PLAN_SIMULATION:" in prompt:
    is_final="REVIEW STAGE: final" in prompt
    verdict=os.environ.get("FAKE_FINAL_SIMULATION_VERDICT" if is_final else "FAKE_SIMULATION_VERDICT","PASS")
    scope=os.environ.get("FAKE_FINAL_SIMULATION_SCOPE" if is_final else "FAKE_SIMULATION_SCOPE","PLAN")
    result='PLAN_SIMULATION: '+json.dumps({"verdict":verdict,"scope":scope,"summary":"simulation checked","findings":[] if verdict=="PASS" else ["material simulation finding"],"scenarios":["happy path","partial failure","rollback"]})
elif "PLAN_REDTEAM:" in prompt:
    is_final="REVIEW STAGE: final" in prompt
    verdict=os.environ.get("FAKE_FINAL_REDTEAM_VERDICT" if is_final else "FAKE_REDTEAM_VERDICT","PASS")
    scope=os.environ.get("FAKE_FINAL_REDTEAM_SCOPE" if is_final else "FAKE_REDTEAM_SCOPE","PLAN")
    result='PLAN_REDTEAM: '+json.dumps({"verdict":verdict,"scope":scope,"summary":"red team checked","findings":[] if verdict=="PASS" else ["material red-team finding"],"scenarios":["dependency outage","stale state"]})
elif "independent, HARD READ-ONLY challenger" in prompt:
    result="CHALLENGER_VERDICT: PASS\\nCHALLENGER_SUMMARY: verified"
elif "MODEL_QUALIFICATION:" in prompt:
    import re
    m=re.search(r"path: (None|'[^']*')", prompt)
    rel=None if not m or m.group(1)=="None" else m.group(1)[1:-1]
    sample=None
    if rel and os.path.isfile(rel):
        sample=next((ln.strip() for ln in open(rel,errors='replace') if ln.strip()),"")[:120]
    result='MODEL_QUALIFICATION: '+json.dumps({"challenge_file":rel,"challenge_sample":sample,"instruction_files":[],"manifests":[]})
elif "CLAUDE AUTO EXECUTION QUALIFICATION" in prompt:
    open("auto-shell-proof.txt","w").write("Claude Auto_AUTO_BASH_OK\\n")
    result='AUTO_EXECUTION_QUALIFICATION: {"ok":true}'
elif "CLAUDE AUTO SAFE MUTATION QUALIFICATION" in prompt:
    open("mutation-proof.txt","w").write("Claude Auto_MUTATION_OK\\n")
    result='MUTATION_QUALIFICATION: {"ok":true}'
elif "/goal CLAUDE AUTO GOAL CONTINUITY QUALIFICATION" in prompt:
    open("goal-proof.txt","w").write("CLAUDE_AUTO_GOAL_OK\\n")
    result='GOAL_QUALIFICATION: {"ok":true}'
elif "CLAUDE AUTO SUBAGENT QUALIFICATION" in prompt:
    result='SUBAGENT_QUALIFICATION: {"ok":true}'
elif "You are the independent Task Verifier" in prompt:
    import re
    task_match=re.search(r"TASK ID:\\s*([^\\n]+)", prompt)
    sha_match=re.search(r"EXACT CANDIDATE SHA:\\s*([0-9a-fA-F]+)", prompt)
    result='TASK_ACCEPT_VERIFY: '+json.dumps({
        "verdict":"VERIFIED",
        "task_id":task_match.group(1).strip() if task_match else "",
        "candidate_sha":sha_match.group(1).lower() if sha_match else "",
        "summary":"exact candidate verified",
        "findings":[],
    })
elif prompt.strip() == "/verify":
    if os.environ.get("FAKE_RUNTIME_VERIFY_NATIVE_FAIL") == "1":
        print("verify unavailable", file=sys.stderr)
        raise SystemExit(14)
    result=os.environ.get("FAKE_RUNTIME_VERIFY_NATIVE_RESULT","Native runtime verification observed the application starting and behaving correctly.")
elif "HARD READ-ONLY runtime acceptance adjudicator" in prompt:
    verdict=os.environ.get("FAKE_RUNTIME_VERIFY_VERDICT","PASS")
    summary=os.environ.get("FAKE_RUNTIME_VERIFY_SUMMARY","runtime behaviour validated")
    findings=[] if verdict!="FAIL" else [os.environ.get("FAKE_RUNTIME_VERIFY_FINDING","runtime acceptance defect") ]
    result='RUNTIME_VERIFY_GATE: '+json.dumps({"verdict":verdict,"summary":summary,"findings":findings})
elif prompt.strip() == "/code-review high":
    if os.environ.get("FAKE_CODE_REVIEW_NATIVE_FAIL") == "1":
        print("code review unavailable", file=sys.stderr)
        raise SystemExit(13)
    result=os.environ.get("FAKE_CODE_REVIEW_NATIVE_RESULT","Native code review completed: no material correctness defects found.")
elif "mandatory HARD READ-ONLY correctness adjudicator" in prompt:
    verdict=os.environ.get("FAKE_CODE_REVIEW_VERDICT","PASS")
    summary=os.environ.get("FAKE_CODE_REVIEW_SUMMARY","correctness review validated")
    findings=[] if verdict=="PASS" else [os.environ.get("FAKE_CODE_REVIEW_FINDING","correctness defect requires remediation")]
    result='CODE_REVIEW_GATE: '+json.dumps({"verdict":verdict,"summary":summary,"findings":findings})
elif prompt.strip() == "/security-review":
    if os.environ.get("FAKE_SECURITY_NATIVE_FAIL") == "1":
        print("security review unavailable", file=sys.stderr)
        raise SystemExit(12)
    result=os.environ.get("FAKE_SECURITY_NATIVE_RESULT","Native security review completed: no material vulnerabilities found.")
elif "mandatory HARD READ-ONLY security completion auditor" in prompt or "deterministic adjudicator for a mandatory Claude Code security completion gate" in prompt:
    verdict=os.environ.get("FAKE_SECURITY_VERDICT","PASS")
    summary=os.environ.get("FAKE_SECURITY_SUMMARY","security review validated")
    findings=[] if verdict=="PASS" else [os.environ.get("FAKE_SECURITY_FINDING","unsafe trust-boundary change requires remediation")]
    result='SECURITY_REVIEW_GATE: '+json.dumps({"verdict":verdict,"summary":summary,"findings":findings})
elif "/goal " in prompt:
    if os.environ.get("FAKE_GOAL_MUTATE_FAILING_PYTEST") == "1":
        os.makedirs("tests", exist_ok=True)
        open("tests/test_injected_failure.py","w").write("def test_injected_failure():\\n    assert 1 == 2\\n")
    result=os.environ.get("FAKE_GOAL_RESULT","AUTONOMY_STATUS: COMPLETE\\nAUTONOMY_SUMMARY: done\\nAUTONOMY_PLAN_IMPACT: NONE\\nAUTONOMY_PHASE_BOUNDARY: NO\\nAUTONOMY_PROGRESS: {\\\"completed_task_ids\\\":[\\\"T001\\\"],\\\"remaining_task_ids\\\":[],\\\"verification\\\":{\\\"fake\\\":\\\"PASS\\\"},\\\"external_checkpoint\\\":null}")
else:
    result="GATEWAY_OK"
print(json.dumps({"result":result,"session_id":"fake-session","usage":{"input_tokens":10,"output_tokens":3,"cache_read_input_tokens":20},"num_turns":2,"total_cost_usd":0.01}))
""")
    script.chmod(0o755)
    return script


def test_p4_headless_noop_task_runs_in_worktree_and_reaches_acceptance(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r = Path(td)
        git_init(r)
        subprocess.run(
            ["git", "-C", str(r), "config", "user.email", "p4@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(r), "config", "user.name", "P4 Test"],
            check=True,
        )
        (r / "PLAN.md").write_text("# Plan\n")
        owned = r / "src" / "task"
        owned.mkdir(parents=True)
        (owned / "existing.txt").write_text("already satisfied\n")
        governance = {
            "schema_version": 1,
            "planning_authority": {
                "sets": [{
                    "id": "default",
                    "members": [{
                        "path": "PLAN.md",
                        "role": "source",
                        "repair": "repairable",
                        "required": True,
                    }],
                    "validators": [],
                    "reconcilers": [],
                }],
            },
            "tasks": {
                "sources": [{
                    "id": "tasks",
                    "kind": "static",
                    "authority_sets": ["default"],
                    "tasks": [{
                        "schema_version": 1,
                        "id": "T1",
                        "authority_sets": ["default"],
                        "depends_on": [],
                        "owned_paths": ["src/task/**"],
                        "evidence_paths": ["evidence/T1/**"],
                        "runtime_scratch_paths": [".scratch/T1/**"],
                        "verification": ["existing implementation is sufficient"],
                        "commit_subject": None,
                        "metadata": {},
                    }],
                }],
                "execution_mode": "single-writer",
                "strict_dependencies": True,
            },
            "control_surfaces": [],
        }
        gov = r / ".claude-auto" / "governance.json"
        gov.parent.mkdir(parents=True)
        gov.write_text(json.dumps(governance, indent=2) + "\n")
        subprocess.run(["git", "-C", str(r), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(r), "commit", "-qm", "governed base"],
            check=True,
        )

        capture = Path(dh) / "capture.jsonl"
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH", bd + os.pathsep + os.environ.get("PATH", ""))
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(capture))
        monkeypatch.setenv(
            "FAKE_GOAL_RESULT",
            'AUTONOMY_TASK_RESULT: {"task_id":"T1","status":"READY_FOR_ACCEPTANCE",'
            '"summary":"task is already satisfied",'
            '"verification_claims":["inspected current implementation"]}'
            "\nAUTONOMY_STATUS: CONTINUE"
            "\nAUTONOMY_SUMMARY: task ready for package acceptance"
            "\nAUTONOMY_PLAN_IMPACT: NONE"
            "\nAUTONOMY_PHASE_BOUNDARY: NO",
        )

        rc = ca.main([
            "run",
            "--model-qualification",
            "off",
            "--repo",
            str(r),
            "--plan",
            "PLAN.md",
            "--max-cycles",
            "2",
            "--max-turns",
            "5",
            "--security-scanners",
            "off",
        ])
        assert rc == 0

        sd = ca.repo_state_dir(r)
        state = json.loads((sd / "state.json").read_text())
        accepted = state["accepted_tasks"]["T1"]
        assert accepted["accepted_product_sha"] == subprocess.run(
            ["git", "-C", str(r), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        assert state["active_task_id"] is None
        assert state.get("active_task_worktree") is None
        assert state["status"] == "COMPLETE"

        calls = [
            json.loads(line)
            for line in capture.read_text().splitlines()
            if line.strip()
        ]
        goal_calls = [
            row
            for row in calls
            if row["args"] and row["args"][-1].startswith("/goal ")
        ]
        assert len(goal_calls) == 1
        worker = goal_calls[0]
        assert Path(worker["cwd"]).resolve() != r.resolve()
        assert Path(worker["cwd"]).resolve().is_relative_to(
            (sd / "tasks" / "workspaces").resolve()
        )
        assert not Path(worker["cwd"]).exists()
        assert ca.ensure_supervisor_task_workspace(
            r,
            state_dir=sd,
        )["status"] == "COMPLETE"

def test_goal_engine_uses_goal_and_compact_logging(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        capture=Path(dh)/"capture.jsonl"
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH", bd+os.pathsep+os.environ.get("PATH",""))
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(capture))
        rc=ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","Do the bounded test objective","--max-cycles","1","--max-turns","5"])
        assert rc == 0
        calls=[json.loads(x) for x in capture.read_text().splitlines()]
        run_call=next(x for x in calls if x["args"] and x["args"][-1].startswith("/goal "))
        assert run_call["args"][-1].startswith("/goal ")
        assert "--exclude-dynamic-system-prompt-sections" in run_call["args"]
        sd=ca.repo_state_dir(r)
        log=json.loads((sd/"logs"/"goal-0001.json").read_text())
        assert "stdout" not in log
        assert log["usage"]["input_tokens"] == 10
        assert json.loads((sd/"state.json").read_text())["status"]=="COMPLETE"


def test_goal_engine_does_not_promote_explicit_continue(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH", bd+os.pathsep+os.environ.get("PATH",""))
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        monkeypatch.setenv("FAKE_GOAL_RESULT","AUTONOMY_STATUS: CONTINUE\\nAUTONOMY_SUMMARY: more work")
        rc=ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","test","--max-cycles","1","--max-turns","5","--security-scanners","off"])
        assert rc == 4
        assert json.loads((ca.repo_state_dir(r)/"state.json").read_text())["status"]=="LIMIT_REACHED"


def test_cross_provider_fallback_preserves_role_models(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        capture=Path(dh)/"capture.jsonl"
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH", bd+os.pathsep+os.environ.get("PATH",""))
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(capture))
        monkeypatch.setenv("FAIL_NATIVE","1")
        monkeypatch.setenv("OPENROUTER_API_KEY","key-for-test-only-12345")
        rc=ca.main([
            "run","--model-qualification","off","--repo",str(r),"--objective","test","--max-cycles","1","--max-turns","5",
            "--verifier-model","native-review","--researcher-model","native-research",
            "--fallback-provider","openrouter","--fallback-model","fallback-main",
            "--fallback-verifier-model","fallback-review","--fallback-researcher-model","fallback-research",
        ])
        assert rc == 0
        calls=[json.loads(x) for x in capture.read_text().splitlines() if x.strip()]
        fallback=[x for x in calls if x.get("base")=="https://openrouter.ai/api" and "-p" in x["args"]]
        assert fallback
        args=fallback[-1]["args"]
        agents=json.loads(args[args.index("--agents")+1])
        assert agents["autonomy-verifier"]["model"]=="fallback-review"
        assert agents["autonomy-researcher"]["model"]=="fallback-research"


def test_headless_goal_is_fail_closed_for_permission_prompts(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        capture=Path(dh)/"capture.jsonl"
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH", bd+os.pathsep+os.environ.get("PATH",""))
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(capture))
        assert ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","test","--max-cycles","1","--max-turns","5"]) == 0
        call=next(json.loads(x) for x in capture.read_text().splitlines() if '"-p"' in x)
        assert "--permission-prompts" in call["args"]
        assert call["args"][call["args"].index("--permission-prompts")+1] == "none"
        assert "--no-session-persistence" not in call["args"]


def test_challenger_is_no_shell_nonpersistent_and_repo_unchanged(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        capture=Path(dh)/"capture.jsonl"
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH", bd+os.pathsep+os.environ.get("PATH",""))
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(capture))
        before=ca.git_snapshot(r)
        rc=ca.main([
            "run","--model-qualification","off","--repo",str(r),"--objective","test","--max-cycles","1","--max-turns","5",
            "--challenger-policy","final","--challenger-model","challenger-model",
        ])
        assert rc == 0
        after=ca.git_snapshot(r)
        assert before == after
        calls=[json.loads(x) for x in capture.read_text().splitlines()]
        ch=next(x for x in calls if "independent, HARD READ-ONLY challenger" in x["args"][-1])
        assert "--no-session-persistence" in ch["args"]
        assert "--permission-prompts" in ch["args"]
        tools=ch["args"][ch["args"].index("--tools")+1]
        denied=ch["args"][ch["args"].index("--disallowed-tools")+1]
        assert "Bash" not in tools
        assert "Bash" in denied and "mcp__*" in denied


def test_model_qualification_is_nonpersistent_and_repo_unchanged(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r)
        capture=Path(dh)/"capture.jsonl"
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH", bd+os.pathsep+os.environ.get("PATH",""))
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(capture))
        before=ca.git_snapshot(r)
        rc=ca.main(["models","qualify","--repo",str(r),"--provider","native","--model","candidate","--level","readonly"])
        assert rc == 0
        assert before == ca.git_snapshot(r)
        calls=[json.loads(x) for x in capture.read_text().splitlines()]
        call=next(x for x in calls if "MODEL_QUALIFICATION:" in x["args"][-1])
        assert "--no-session-persistence" in call["args"]
        assert "--permission-prompts" in call["args"]
        assert call["args"][call["args"].index("--tools")+1] == "Read,Glob,Grep"


def test_new_objective_clears_old_challenger_state(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH", bd+os.pathsep+os.environ.get("PATH",""))
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        sd=ca.activate(r)
        state=json.loads((sd/"state.json").read_text())
        state.update({
            "objective":"old objective",
            "stagnant_cycles":9,
            "last_progress_fingerprint":"old",
            "last_challenger_verdict":"FAIL",
            "last_challenger_findings":"old finding",
        })
        ca.json_dump(sd/"state.json", state)
        assert ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","new objective","--max-cycles","1","--max-turns","5"]) == 0
        out=json.loads((sd/"state.json").read_text())
        assert out["objective"] == "new objective"
        assert out.get("last_challenger_verdict") is None
        assert out.get("last_challenger_findings") is None
        assert out.get("last_security_review_verdict") == "PASS"
        assert out.get("last_security_review_findings") is None
        assert out.get("stagnant_cycles") == 0


def test_fallback_log_preserves_both_route_attempts(monkeypatch):
    monkeypatch.setattr(ca.time, "sleep", lambda _seconds: None)
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH", bd+os.pathsep+os.environ.get("PATH",""))
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        monkeypatch.setenv("FAIL_NATIVE","1")
        monkeypatch.setenv("OPENROUTER_API_KEY","key-for-test-only-12345")
        assert ca.main([
            "run","--model-qualification","off","--repo",str(r),"--objective","test","--max-cycles","1","--max-turns","5",
            "--fallback-provider","openrouter","--fallback-model","fallback-main",
        ]) == 0
        log=json.loads((ca.repo_state_dir(r)/"logs"/"goal-0001.json").read_text())
        assert log["provider_fallback_used"] is True
        assert len(log["route_attempts"]) == 2
        assert log["route_attempts"][0]["provider"]["provider"] == "native"
        assert log["route_attempts"][1]["provider"]["provider"] == "openrouter"


def test_auto_qualifies_unseen_explicit_main_route(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        calls=[]
        def fake_qualify(qargs):
            calls.append(qargs)
            values={"gateway_url":qargs.gateway_url,"gateway_token_env":qargs.gateway_token_env,"gateway_discovery":qargs.gateway_discovery,"isolate_provider_profile":qargs.isolate_provider_profile,"gateway_hints":qargs.gateway_hints}
            ca.save_model_qualification(qargs.provider, qargs.model, {
                "compatible": True,
                "qualification_level": "AUTONOMOUS_FULL",
                "qualified_at": ca.utcnow(),
                "route_fingerprint": ca.qualification_route_fingerprint(qargs.provider, qargs.model, values),
            })
            return 0
        monkeypatch.setattr(ca, "qualify_model", fake_qualify)
        args=ca.build_parser().parse_args([
            "run","--repo",str(r),"--objective","x","--model","candidate-main",
        ])
        ca.ensure_model_qualification(args, r, args.model, role="main", required_level="AUTONOMOUS_FULL")
        assert len(calls) == 1
        assert calls[0].level == "full"
        assert calls[0].provider == "native"
        rec=ca.qualification_record("native", "candidate-main")
        assert rec and rec["qualification_level"] == "AUTONOMOUS_FULL"


def test_auto_qualification_inside_run_uses_existing_lease(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        calls=[]

        def fake_unlocked(qargs, root, sd):
            calls.append((qargs, root, sd))
            values={"gateway_url":qargs.gateway_url,"gateway_token_env":qargs.gateway_token_env,"gateway_discovery":qargs.gateway_discovery,"isolate_provider_profile":qargs.isolate_provider_profile,"gateway_hints":qargs.gateway_hints}
            ca.save_model_qualification(qargs.provider, qargs.model, {
                "compatible": True,
                "qualification_level": "AUTONOMOUS_FULL",
                "qualified_at": ca.utcnow(),
                "route_fingerprint": ca.qualification_route_fingerprint(qargs.provider, qargs.model, values),
                "stages": [],
            })
            return 0

        monkeypatch.setattr(ca, "_qualify_model_unlocked", fake_unlocked)
        monkeypatch.setattr(ca, "qualify_model", lambda _qargs: (_ for _ in ()).throw(AssertionError("public locked qualifier must not be re-entered")))
        args=ca.build_parser().parse_args(["run","--repo",str(r),"--objective","x","--model","candidate-main"])
        rec=ca.ensure_model_qualification(
            args, r, args.model, role="main", required_level="AUTONOMOUS_FULL",
            caller_holds_lease=True,
        )
        assert len(calls) == 1
        assert calls[0][1] == r
        assert calls[0][2] == ca.repo_state_dir(r)
        assert rec and rec["qualification_level"] == "AUTONOMOUS_FULL"


def test_non_native_main_requires_identifiable_model_for_qualification(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        args=ca.build_parser().parse_args([
            "run","--repo",str(r),"--objective","x","--provider","openrouter",
        ])
        with pytest.raises(SystemExit, match="requires an explicit --model"):
            ca.ensure_model_qualification(args, r, None, role="main", required_level="AUTONOMOUS_FULL")


def test_model_qualification_off_is_explicit_escape_hatch(monkeypatch, capsys):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        args=ca.build_parser().parse_args([
            "run","--repo",str(r),"--objective","x","--model","candidate-main","--model-qualification","off",
        ])
        ca.ensure_model_qualification(args, r, args.model, role="main", required_level="AUTONOMOUS_FULL")
        assert "continuing only because --model-qualification off" in capsys.readouterr().err


def test_installer_refuses_unrelated_launcher_collision():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as install:
        homep=Path(home); bindir=homep/".local"/"bin"; bindir.mkdir(parents=True)
        launcher=bindir/"claude-auto"; launcher.write_text("unrelated\n")
        env=os.environ.copy(); env.update({"HOME":home,"CLAUDE_AUTONOMY_HOME":str(Path(install)/"pack")})
        cp=subprocess.run(["bash",str(ROOT/"install.sh"),"--no-plugins"],cwd=ROOT,env=env,text=True,capture_output=True)
        assert cp.returncode == 2
        assert launcher.read_text() == "unrelated\n"


def test_uninstaller_refuses_marker_path_mismatch():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as install:
        homep=Path(home); bindir=homep/".local"/"bin"; bindir.mkdir(parents=True)
        dest=Path(install)/"pack"
        env=os.environ.copy(); env.update({"HOME":home,"CLAUDE_AUTONOMY_HOME":str(dest),"CLAUDE_AUTONOMY_BIN":str(bindir)})
        cp=subprocess.run(["bash",str(ROOT/"install.sh"),"--no-plugins"],cwd=ROOT,env=env,text=True,capture_output=True)
        assert cp.returncode == 0, cp.stderr
        marker=dest/".claude-autonomy-install.json"
        obj=json.loads(marker.read_text()); obj["canonical_install_path"]="/tmp/not-this-install"; marker.write_text(json.dumps(obj))
        cp=subprocess.run(["bash",str(dest/"uninstall.sh"),"--purge-state"],env=env,text=True,capture_output=True)
        assert cp.returncode != 0
        assert dest.exists()


def test_installer_refuses_home_as_install_destination():
    with tempfile.TemporaryDirectory() as home:
        bindir=Path(home)/".local"/"bin"; bindir.mkdir(parents=True)
        env=os.environ.copy(); env.update({"HOME":home,"CLAUDE_AUTONOMY_HOME":home,"CLAUDE_AUTONOMY_BIN":str(bindir)})
        cp=subprocess.run(["bash",str(ROOT/"install.sh"),"--no-plugins"],cwd=ROOT,env=env,text=True,capture_output=True)
        assert cp.returncode != 0
        assert "unsafe install destination" in (cp.stderr+cp.stdout).lower()


def test_install_then_marked_purge_removes_only_package_destination():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as install:
        bindir=Path(home)/".local"/"bin"; bindir.mkdir(parents=True)
        dest=Path(install)/"pack"; sentinel=Path(install)/"keep.txt"; sentinel.write_text("keep")
        env=os.environ.copy(); env.update({"HOME":home,"CLAUDE_AUTONOMY_HOME":str(dest),"CLAUDE_AUTONOMY_BIN":str(bindir)})
        cp=subprocess.run(["bash",str(ROOT/"install.sh"),"--no-plugins"],cwd=ROOT,env=env,text=True,capture_output=True)
        assert cp.returncode == 0, cp.stderr
        assert dest.exists() and (dest/".claude-autonomy-install.json").exists()
        cp=subprocess.run(["bash",str(dest/"uninstall.sh"),"--purge-state"],env=env,text=True,capture_output=True)
        assert cp.returncode == 0, cp.stderr
        assert not dest.exists()
        assert sentinel.read_text() == "keep"
        assert not (bindir/"claude-auto").exists()


def test_installer_refuses_unrelated_broken_symlink_collision():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as install:
        bindir=Path(home)/".local"/"bin"; bindir.mkdir(parents=True)
        launcher=bindir/"claude-auto"
        launcher.symlink_to("/tmp/definitely-not-claude-auto-missing-target")
        env=os.environ.copy(); env.update({
            "HOME":home,
            "CLAUDE_AUTONOMY_HOME":str(Path(install)/"pack"),
            "CLAUDE_AUTONOMY_BIN":str(bindir),
        })
        cp=subprocess.run(["bash",str(ROOT/"install.sh"),"--no-plugins"],cwd=ROOT,env=env,text=True,capture_output=True)
        assert cp.returncode == 2
        assert launcher.is_symlink()
        assert os.readlink(launcher) == "/tmp/definitely-not-claude-auto-missing-target"


def test_balanced_profile_relaxes_only_main_worker_and_keeps_env_boundary():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh:
        r = Path(td); git_init(r)
        (r / "README.md").write_text("x\n")
        os.environ["CLAUDE_AUTONOMY_HOME"] = dh
        try:
            sd = ca.activate(r)
            st = json.loads((sd / "settings-balanced-external.json").read_text())
            assert st["sandbox"]["enabled"] is True
            assert st["sandbox"]["allowUnsandboxedCommands"] is True
            assert st["permissions"]["blockReadsOutsideWorkingDirectories"] is False
            assert str(r.resolve()) in st["permissions"]["additionalDirectories"]
            cred_files = st["sandbox"]["credentials"]["files"]
            assert {"path": "~/.claude/.credentials.json", "mode": "deny"} in cred_files
            assert {"path": "~/.claude.json", "mode": "deny"} in cred_files
            deny = st["permissions"]["deny"]
            assert "Read(./.env)" in deny
            assert "Read(./.env.*)" not in deny
            assert "Read(./**/*.pem)" not in deny
            ro = json.loads((sd / "settings-readonly.json").read_text())
            assert ro["sandbox"]["allowUnsandboxedCommands"] is False
            assert ro["permissions"]["blockReadsOutsideWorkingDirectories"] is True
            assert "Edit" in ro["permissions"]["deny"]
        finally:
            del os.environ["CLAUDE_AUTONOMY_HOME"]


def test_strict_profile_retains_broader_secret_denials_and_no_escape_hatch():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh:
        r = Path(td); git_init(r); (r / "README.md").write_text("x\n")
        os.environ["CLAUDE_AUTONOMY_HOME"] = dh
        try:
            sd = ca.activate(r)
            st = json.loads((sd / "settings-strict-external.json").read_text())
            assert st["sandbox"]["enabled"] is True
            assert st["sandbox"]["allowUnsandboxedCommands"] is False
            assert st["permissions"]["blockReadsOutsideWorkingDirectories"] is True
            assert str(r.resolve()) in st["permissions"]["additionalDirectories"]
            deny = st["permissions"]["deny"]
            assert "Read(./.env.*)" in deny
            assert "Read(./**/*.pem)" in deny
        finally:
            del os.environ["CLAUDE_AUTONOMY_HOME"]


def test_container_runtime_is_not_broadly_unsandboxed_in_balanced_or_strict():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh:
        r = Path(td); git_init(r)
        (r / "Dockerfile").write_text("FROM scratch\n")
        (r / "compose.yaml").write_text("services: {}\n")
        os.environ["CLAUDE_AUTONOMY_HOME"] = dh
        try:
            p = ca.profile_repo(r)
            assert "Dockerfile" in p.container_files
            assert "compose.yaml" in p.container_files
            sd = ca.activate(r)
            bal = json.loads((sd / "settings-balanced-external.json").read_text())
            strict = json.loads((sd / "settings-strict-external.json").read_text())
            for settings in (bal, strict):
                excluded = settings["sandbox"].get("excludedCommands", [])
                assert "docker *" not in excluded
                assert "docker-compose *" not in excluded
                pre = json.dumps(settings["hooks"].get("PreToolUse", []))
                assert "docker_guard.py" in pre
        finally:
            del os.environ["CLAUDE_AUTONOMY_HOME"]


def test_isolated_full_requires_explicit_attestation(monkeypatch):
    monkeypatch.delenv("CLAUDE_AUTO_ISOLATED_FULL", raising=False)
    monkeypatch.delenv("CLAUDE_AUTO_ISOLATION_ATTESTATION", raising=False)
    try:
        ca.permission_mode_for_profile("auto", "isolated-full")
    except SystemExit as exc:
        assert "attested disposable container/VM" in str(exc)
    else:
        raise AssertionError("isolated-full must fail closed without explicit attestation")


def test_isolated_full_uses_bypass_only_after_explicit_attestation(monkeypatch):
    monkeypatch.setenv("CLAUDE_AUTO_ISOLATED_FULL", "1")
    monkeypatch.setenv("CLAUDE_AUTO_ISOLATION_ATTESTATION", "container")
    assert ca.permission_mode_for_profile("auto", "isolated-full") == "bypassPermissions"
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh:
        r = Path(td); git_init(r); (r / "README.md").write_text("x\n")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        sd = ca.activate(r)
        st = json.loads((sd / "settings-isolated-full-external.json").read_text())
        assert st["sandbox"]["enabled"] is False


def test_default_run_uses_balanced_profile_settings(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        capture=Path(dh)/"capture.jsonl"
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH", bd+os.pathsep+os.environ.get("PATH", ""))
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(capture))
        assert ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","test","--max-cycles","1","--max-turns","5"]) == 0
        call=next(json.loads(x) for x in capture.read_text().splitlines() if '"-p"' in x)
        settings_path = call["args"][call["args"].index("--settings") + 1]
        assert settings_path.endswith("settings-balanced-external.json")
        assert call["args"][call["args"].index("--permission-mode") + 1] == "auto"


def test_profiles_command_lists_all_supported_profiles(capsys):
    assert ca.main(["profiles"]) == 0
    obj = json.loads(capsys.readouterr().out)
    names = {x["name"] for x in obj["profiles"]}
    assert names == {"balanced", "strict", "isolated-full", "unattended"}
    assert next(x for x in obj["profiles"] if x["name"] == "balanced")["default"] is True


def test_strict_run_uses_strict_settings(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        capture=Path(dh)/"capture.jsonl"
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH", bd+os.pathsep+os.environ.get("PATH", ""))
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(capture))
        assert ca.main(["run","--model-qualification","off","--repo",str(r),"--profile","strict","--objective","test","--max-cycles","1","--max-turns","5"]) == 0
        call=next(json.loads(x) for x in capture.read_text().splitlines() if '"-p"' in x)
        settings_path = call["args"][call["args"].index("--settings") + 1]
        assert settings_path.endswith("settings-strict-external.json")
        assert call["args"][call["args"].index("--permission-mode") + 1] == "auto"


def test_isolated_full_cli_uses_bypass_only_when_attested(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        capture=Path(dh)/"capture.jsonl"
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH", bd+os.pathsep+os.environ.get("PATH", ""))
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(capture))
        monkeypatch.setenv("CLAUDE_AUTO_ISOLATED_FULL", "1")
        monkeypatch.setenv("CLAUDE_AUTO_ISOLATION_ATTESTATION", "disposable")
        assert ca.main(["run","--model-qualification","off","--repo",str(r),"--profile","isolated-full","--objective","test","--max-cycles","1","--max-turns","5"]) == 0
        call=next(json.loads(x) for x in capture.read_text().splitlines() if '"-p"' in x)
        assert call["args"][call["args"].index("--permission-mode") + 1] == "bypassPermissions"
        settings_path = call["args"][call["args"].index("--settings") + 1]
        assert settings_path.endswith("settings-isolated-full-external.json")


def test_explicit_profile_persists_for_repository_resume(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        capture=Path(dh)/"capture.jsonl"
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH", bd+os.pathsep+os.environ.get("PATH", ""))
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(capture))
        assert ca.main(["run","--model-qualification","off","--repo",str(r),"--profile","strict","--objective","test","--max-cycles","1","--max-turns","5"]) == 0
        capture.write_text("")
        # No --profile on resume: preserve strict instead of silently relaxing to balanced.
        assert ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","test","--max-cycles","1","--max-turns","5"]) == 0
        call=next(json.loads(x) for x in capture.read_text().splitlines() if '"-p"' in x)
        settings_path = call["args"][call["args"].index("--settings") + 1]
        assert settings_path.endswith("settings-strict-external.json")
        assert json.loads((ca.repo_state_dir(r)/"state.json").read_text())["autonomy_profile"] == "strict"


def test_plan_protocol_parser_tolerates_multiline_json_string():
    text = 'PLAN_CONTROL: {"verdict":"READY","plan_markdown":"line one\nline two","tasks":[],"acceptance_criteria":[]}'
    obj = ca.parse_json_protocol(text, "PLAN_CONTROL")
    assert obj and obj["verdict"] == "READY"
    assert "line two" in obj["plan_markdown"]


def test_goal_only_creates_validated_external_plan_before_worker(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        capture=Path(dh)/"capture.jsonl"; make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH", bd+os.pathsep+os.environ.get("PATH", ""))
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(capture))
        assert ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","Build feature X","--max-cycles","1","--max-turns","5"]) == 0
        sd=ca.repo_state_dir(r)
        state=json.loads((sd/"state.json").read_text())
        assert state["plan_status"] == "VALIDATED"
        assert state["plan_version"] == 1
        assert (sd/"plans"/"current-plan.json").exists()
        assert not (r/"IMPLEMENTATION_PLAN.md").exists()
        prompts=[json.loads(x)["args"][-1] for x in capture.read_text().splitlines() if x.strip()]
        planner=next(i for i,p in enumerate(prompts) if "PLAN_CONTROL:" in p)
        simulation=next(i for i,p in enumerate(prompts) if "REVIEW STAGE: preflight" in p and "PLAN_SIMULATION:" in p)
        redteam=next(i for i,p in enumerate(prompts) if "REVIEW STAGE: preflight" in p and "PLAN_REDTEAM:" in p)
        worker=next(i for i,p in enumerate(prompts) if p.startswith("/goal "))
        assert planner < simulation < redteam < worker


def test_supplied_plan_is_candidate_and_validated_before_execution(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r)
        supplied=r/"IMPLEMENTATION_PLAN.md"; supplied.write_text("# Candidate\n\nBuild A then B.\n")
        capture=Path(dh)/"capture.jsonl"; make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH", bd+os.pathsep+os.environ.get("PATH", ""))
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(capture))
        assert ca.main(["run","--model-qualification","off","--repo",str(r),"--plan","IMPLEMENTATION_PLAN.md","--max-cycles","1","--max-turns","5"]) == 0
        sd=ca.repo_state_dir(r); state=json.loads((sd/"state.json").read_text())
        assert state["plan_source_kind"] == "supplied-plan"
        planner_prompt=next(json.loads(x)["args"][-1] for x in capture.read_text().splitlines() if "PLAN_CONTROL:" in json.loads(x)["args"][-1])
        assert "Build A then B" in planner_prompt
        assert "candidate, never automatically authoritative" in planner_prompt


def test_material_worker_fix_forces_plan_revision_before_next_round(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH", bd+os.pathsep+os.environ.get("PATH", ""))
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        monkeypatch.setenv("FAKE_GOAL_RESULT", "AUTONOMY_STATUS: CONTINUE\nAUTONOMY_SUMMARY: schema mismatch found\nAUTONOMY_PLAN_IMPACT: MATERIAL\nAUTONOMY_PLAN_CHANGE: root cause is schema assumption; revise migration and downstream API tasks\nAUTONOMY_PHASE_BOUNDARY: NO")
        assert ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","test","--max-cycles","1","--max-turns","5"]) == 4
        sd=ca.repo_state_dir(r); state=json.loads((sd/"state.json").read_text())
        assert state["plan_version"] == 2
        current=json.loads((sd/"plans"/"current-plan.json").read_text())
        assert "schema assumption" in current["revision_reason"]
        assert state["plan_status"] == "VALIDATED"


def test_phase_boundary_triggers_plan_stress_review(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        capture=Path(dh)/"capture.jsonl"; make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH", bd+os.pathsep+os.environ.get("PATH", ""))
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(capture))
        monkeypatch.setenv("FAKE_GOAL_RESULT", "AUTONOMY_STATUS: CONTINUE\nAUTONOMY_SUMMARY: phase one done\nAUTONOMY_PLAN_IMPACT: NONE\nAUTONOMY_PHASE_BOUNDARY: YES")
        assert ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","test","--max-cycles","1","--max-turns","5"]) == 4
        prompts=[json.loads(x)["args"][-1] for x in capture.read_text().splitlines() if x.strip()]
        assert any("REVIEW STAGE: phase" in p and "PLAN_SIMULATION:" in p for p in prompts)
        assert any("REVIEW STAGE: phase" in p and "PLAN_REDTEAM:" in p for p in prompts)


def test_final_adversarial_review_can_reject_false_completion(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH", bd+os.pathsep+os.environ.get("PATH", ""))
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        monkeypatch.setenv("FAKE_FINAL_SIMULATION_VERDICT", "REVISE")
        monkeypatch.setenv("FAKE_FINAL_SIMULATION_SCOPE", "IMPLEMENTATION")
        assert ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","test","--max-cycles","1","--max-turns","5"]) == 4
        state=json.loads((ca.repo_state_dir(r)/"state.json").read_text())
        assert state["status"] == "LIMIT_REACHED"
        assert state.get("plan_revalidation_findings")


def test_plan_gate_fails_closed_after_bounded_redteam_revision_loop(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH", bd+os.pathsep+os.environ.get("PATH", ""))
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        monkeypatch.setenv("FAKE_REDTEAM_VERDICT", "REVISE")
        assert ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","test","--max-plan-revisions","2","--max-cycles","1","--max-turns","5"]) == 6
        state=json.loads((ca.repo_state_dir(r)/"state.json").read_text())
        assert state["plan_version"] == 2
        assert state["plan_status"] == "VALIDATION_FAILED"


def test_nontrivial_local_fix_triggers_whole_plan_remediation_review(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        capture=Path(dh)/"capture.jsonl"; make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH", bd+os.pathsep+os.environ.get("PATH", ""))
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(capture))
        monkeypatch.setenv("FAKE_GOAL_RESULT", "AUTONOMY_STATUS: CONTINUE\nAUTONOMY_SUMMARY: repaired parser after root cause analysis\nAUTONOMY_PLAN_IMPACT: LOCAL\nAUTONOMY_PLAN_CHANGE: parser fix with bounded call-site impact\nAUTONOMY_PHASE_BOUNDARY: NO")
        assert ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","test","--max-cycles","1","--max-turns","5"]) == 4
        prompts=[json.loads(x)["args"][-1] for x in capture.read_text().splitlines() if x.strip()]
        assert any("REVIEW STAGE: remediation" in p and "PLAN_SIMULATION:" in p for p in prompts)
        assert any("REVIEW STAGE: remediation" in p and "PLAN_REDTEAM:" in p for p in prompts)
        state=json.loads((ca.repo_state_dir(r)/"state.json").read_text())
        assert state.get("approved_remediation") == "parser fix with bounded call-site impact"


def test_metrics_include_plan_control_usage(monkeypatch, capsys):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH", bd+os.pathsep+os.environ.get("PATH", ""))
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        assert ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","test","--max-cycles","1","--max-turns","5"]) == 0
        capsys.readouterr()
        assert ca.show_metrics(r) == 0
        report=json.loads(capsys.readouterr().out)
        # planner + preflight simulation + preflight redteam + worker + final simulation + final redteam
        assert report["logs_with_usage"] >= 6
        assert report["estimated_cost_usd"] >= 0.06
        assert any(":plan-planner" in k for k in report["by_provider_model"])
        assert any(":plan-simulation" in k for k in report["by_provider_model"])
        assert any(":plan-redteam" in k for k in report["by_provider_model"])


def test_parser_defaults_to_unlimited_outer_rounds():
    args = ca.build_parser().parse_args(["run", "--objective", "x"])
    assert args.max_cycles == 0
    assert args.max_stagnant_cycles == 3
    assert args.max_total_turns == 0
    assert args.max_wall_seconds == 0


def test_strict_and_readonly_fail_closed_and_inherit_secret_denials():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        os.environ["CLAUDE_AUTONOMY_HOME"] = dh
        try:
            sd=ca.activate(r)
            strict=json.loads((sd/"settings-strict-external.json").read_text())
            ro=json.loads((sd/"settings-readonly.json").read_text())
            for st in (strict, ro):
                assert st["sandbox"]["failIfUnavailable"] is True
                assert st["sandbox"]["allowUnsandboxedCommands"] is False
                assert "Read(./.env.*)" in st["permissions"]["deny"]
                assert "Read(./**/*.pem)" in st["permissions"]["deny"]
            assert ro["env"]["CLAUDE_AUTONOMY_PROFILE"] == "readonly"
        finally:
            del os.environ["CLAUDE_AUTONOMY_HOME"]


def test_balanced_preserves_auto_defaults_and_adds_telemetry_and_write_fence():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        os.environ["CLAUDE_AUTONOMY_HOME"] = dh
        try:
            sd=ca.activate(r)
            st=json.loads((sd/"settings-balanced-external.json").read_text())
            assert st["sandbox"]["failIfUnavailable"] is False
            assert st["autoMode"]["environment"][0] == "$defaults"
            assert "classifyAllShell" not in st.get("autoMode", {})
            assert st["env"]["CLAUDE_AUTO_REPO_ROOT"] == str(r.resolve())
            assert any(
                all(tool in x.get("matcher", "") for tool in ("Write", "Edit", "NotebookEdit", "Bash"))
                and any("write_boundary_guard.py" in h.get("command", "") for h in x.get("hooks", []))
                for x in st["hooks"]["PreToolUse"]
            )
            assert st["hooks"]["PermissionDenied"]
            assert st["hooks"]["StopFailure"]
            deny=st["permissions"]["deny"]
            assert "Read(~/.ssh/**)" in deny
            assert "Read(~/.aws/**)" in deny
        finally:
            del os.environ["CLAUDE_AUTONOMY_HOME"]


def test_write_boundary_guard_denies_outside_and_allows_inside():
    guard=ROOT/"hooks"/"write_boundary_guard.py"
    with tempfile.TemporaryDirectory() as td:
        root=Path(td).resolve(); inside=root/"a.txt"; outside=root.parent/"outside-rc6.txt"
        env=os.environ.copy(); env["CLAUDE_AUTO_REPO_ROOT"] = str(root)
        def call(path):
            cp=subprocess.run([sys.executable,str(guard)],input=json.dumps({"tool_name":"Write","tool_input":{"file_path":str(path)}}),text=True,capture_output=True,env=env,check=True)
            return json.loads(cp.stdout)["hookSpecificOutput"]["permissionDecision"]
        assert call(inside) == "allow"
        assert call(outside) == "deny"


def test_timeout_kills_process_tree_and_returns_typed_result():
    with tempfile.TemporaryDirectory() as td:
        script=Path(td)/"sleep.py"
        script.write_text("import time\ntime.sleep(30)\n")
        started=time.monotonic()
        cp=ca.run([sys.executable,str(script)],timeout=1)
        elapsed=time.monotonic()-started
        assert cp.returncode == 124
        assert getattr(cp,"timed_out",False) is True
        assert elapsed < 8
        outcome,_=ca.classify_claude_outcome(cp,None,"",[])
        assert outcome == "TIMEOUT_RETRYABLE"


def test_structured_turn_and_budget_limits_are_recoverable():
    cp=subprocess.CompletedProcess(["claude"],1,"","")
    assert ca.classify_claude_outcome(cp,{"subtype":"error_max_turns"},"",[])[0] == "TURN_LIMIT"
    assert ca.classify_claude_outcome(cp,{"subtype":"error_max_budget_usd"},"",[])[0] == "BUDGET_LIMIT"


def test_stop_failure_classifies_provider_and_account_failures():
    cp=subprocess.CompletedProcess(["claude"],1,"","")
    assert ca.classify_claude_outcome(cp,None,"",[{"event":"StopFailure","error":"overloaded","error_details":"busy"}])[0] == "TRANSIENT_PROVIDER"
    assert ca.classify_claude_outcome(cp,None,"",[{"event":"StopFailure","error":"model_not_found","error_details":"gone"}])[0] == "MODEL_UNAVAILABLE"
    assert ca.classify_claude_outcome(cp,None,"",[{"event":"StopFailure","error":"billing_error","error_details":"billing"}])[0] == "EXTERNAL_BLOCKER"


def test_goal_sets_headless_and_stop_hook_cap(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        capture=Path(dh)/"capture.jsonl"; make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH",bd+os.pathsep+os.environ.get("PATH","")); monkeypatch.setenv("CLAUDE_AUTONOMY_HOME",dh); monkeypatch.setenv("FAKE_CLAUDE_CAPTURE",str(capture))
        assert ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","test","--max-cycles","1","--max-turns","60"]) == 0
        call=next(json.loads(x) for x in capture.read_text().splitlines() if '"-p"' in x and "/goal " in x)
        assert call["stop_cap"] == "60"
        assert call["headless"] == "1"
        assert "AskUserQuestion" in call["args"][call["args"].index("--disallowed-tools")+1]


def test_nested_claude_launch_is_refused(monkeypatch):
    monkeypatch.setenv("CLAUDECODE","1")
    with pytest.raises(SystemExit) as exc:
        ca.main(["run","--model-qualification","off","--objective","x","--max-cycles","1"])
    assert "top-level shell" in str(exc.value)


def test_full_model_qualification_exercises_auto_mutation_goal_and_subagent(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); capture=Path(dh)/"capture.jsonl"; make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH",bd+os.pathsep+os.environ.get("PATH","")); monkeypatch.setenv("CLAUDE_AUTONOMY_HOME",dh); monkeypatch.setenv("FAKE_CLAUDE_CAPTURE",str(capture))
        before=ca.git_snapshot(r)
        assert ca.main(["models","qualify","--repo",str(r),"--provider","native","--model","candidate","--level","full"]) == 0
        assert before == ca.git_snapshot(r)
        rec=ca.qualification_record("native","candidate")
        assert rec["qualification_level"] == "AUTONOMOUS_FULL"
        assert [x["name"] for x in rec["stages"]] == ["readonly","auto_execution","safe_mutation","goal_continuity","subagent_evaluator","code_review","security_review"]
        assert all(x["ok"] for x in rec["stages"])


def test_progress_fingerprint_changes_on_structured_checkpoint_without_git_change():
    snap={"head":"x","worktree_diff_sha256":"a","index_diff_sha256":"b","untracked_sha256":"c"}
    a=ca.progress_fingerprint("CONTINUE","same",snap,{"plan_version":1,"progress_checkpoint":{"completed_task_ids":["T1"]}})
    b=ca.progress_fingerprint("CONTINUE","same",snap,{"plan_version":1,"progress_checkpoint":{"completed_task_ids":["T1","T2"]}})
    assert a != b


def test_transactional_install_preserves_runtime_state():
    # Manifest must be current before this test is run.
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as install:
        bindir=Path(home)/".local"/"bin"; bindir.mkdir(parents=True)
        dest=Path(install)/"pack"
        env=os.environ.copy(); env.update({"HOME":home,"CLAUDE_AUTONOMY_HOME":str(dest),"CLAUDE_AUTONOMY_BIN":str(bindir)})
        cp=subprocess.run(["bash",str(ROOT/"install.sh"),"--no-plugins"],cwd=ROOT,env=env,text=True,capture_output=True)
        assert cp.returncode == 0, cp.stderr
        state=dest/"repos"/"sentinel"; state.mkdir(parents=True); (state/"state.json").write_text('{"keep":true}\n')
        cp=subprocess.run(["bash",str(ROOT/"install.sh"),"--no-plugins"],cwd=ROOT,env=env,text=True,capture_output=True)
        assert cp.returncode == 0, cp.stderr
        assert (state/"state.json").read_text() == '{"keep":true}\n'
        assert "Transactional upgrade verification" in cp.stdout


def test_p7_rc3_identity_upgrade_preserves_durable_state():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as install:
        bindir = Path(home) / ".local" / "bin"
        bindir.mkdir(parents=True)
        dest = Path(install) / "pack"
        env = os.environ.copy()
        env.update({
            "HOME": home,
            "CLAUDE_AUTONOMY_HOME": str(dest),
            "CLAUDE_AUTONOMY_BIN": str(bindir),
        })
        first = subprocess.run(
            ["bash", str(ROOT / "install.sh"), "--no-plugins"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
        )
        assert first.returncode == 0, first.stderr

        marker_path = dest / ".claude-autonomy-install.json"
        marker = json.loads(marker_path.read_text())
        install_uuid = marker["install_uuid"]

        # Model an identified accepted RC3 installation while keeping the test
        # runnable from the extracted RC4 archive (which intentionally has no
        # Git history). Installer replacement semantics depend on the install
        # marker/state layout, not on executing old RC3 code.
        (dest / "VERSION").write_text("1.0.0-rc3\n")
        marker["version"] = "1.0.0-rc3"
        marker_path.write_text(json.dumps(marker, indent=2) + "\n")
        (dest / "README.md").write_text("synthetic rc3 package payload\n")

        repo_state = dest / "repos" / "fixture"
        (repo_state / "git-trust").mkdir(parents=True)
        (repo_state / "planning-repair").mkdir()
        state_bytes = b'{"schema_version":5,"objective":"preserve me"}\n'
        trust_bytes = b'{"schema_version":2,"source_sha256":"abc"}\n'
        plan_bytes = b'{"schema_version":1,"canonical_plan":"PLAN.md"}\n'
        (repo_state / "state.json").write_bytes(state_bytes)
        (repo_state / "git-trust" / "policy.json").write_bytes(trust_bytes)
        (repo_state / "planning-repair" / "policy.json").write_bytes(plan_bytes)
        registry = dest / "model-registry.json"
        registry.write_bytes(b'{"keep":"registry"}\n')

        assert (
            subprocess.run(
                [str(bindir / "claude-auto"), "--version"],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            == "claude-auto 1.0.0-rc3"
        )

        upgraded = subprocess.run(
            ["bash", str(ROOT / "install.sh"), "--no-plugins"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
        )
        assert upgraded.returncode == 0, upgraded.stderr
        assert (dest / "VERSION").read_text().strip() == "1.0.0-rc4"
        new_marker = json.loads(marker_path.read_text())
        assert new_marker["version"] == "1.0.0-rc4"
        assert new_marker["install_uuid"] == install_uuid
        assert (repo_state / "state.json").read_bytes() == state_bytes
        assert (repo_state / "git-trust" / "policy.json").read_bytes() == trust_bytes
        assert (repo_state / "planning-repair" / "policy.json").read_bytes() == plan_bytes
        assert registry.read_bytes() == b'{"keep":"registry"}\n'
        assert (dest / "README.md").read_text() != "synthetic rc3 package payload\n"


def test_p7_post_swap_upgrade_failure_restores_previous_exact_package_and_state():
    with (
        tempfile.TemporaryDirectory() as home,
        tempfile.TemporaryDirectory() as install,
        tempfile.TemporaryDirectory() as badsrc,
    ):
        bindir = Path(home) / ".local" / "bin"
        bindir.mkdir(parents=True)
        dest = Path(install) / "pack"
        env = os.environ.copy()
        env.update({
            "HOME": home,
            "CLAUDE_AUTONOMY_HOME": str(dest),
            "CLAUDE_AUTONOMY_BIN": str(bindir),
        })
        first = subprocess.run(
            ["bash", str(ROOT / "install.sh"), "--no-plugins"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
        )
        assert first.returncode == 0, first.stderr

        state = dest / "repos" / "keep"
        state.mkdir(parents=True)
        sentinel = state / "state.json"
        sentinel.write_bytes(b"known-good-state\n")
        before_version = (dest / "VERSION").read_bytes()
        before_readme = (dest / "README.md").read_bytes()
        before_user_layer = (dest / "lib" / "user_layer.py").read_bytes()
        before_marker = (dest / ".claude-autonomy-install.json").read_bytes()

        candidate = Path(badsrc) / "candidate"
        shutil.copytree(
            ROOT,
            candidate,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
        )
        (candidate / "lib" / "user_layer.py").write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "raise SystemExit(9 if 'install' in sys.argv[1:] else 0)\n"
        )
        rebuild = subprocess.run(
            [sys.executable, str(candidate / "scripts" / "build_manifest.py"), "--write"],
            cwd=candidate,
            text=True,
            capture_output=True,
        )
        assert rebuild.returncode == 0, rebuild.stderr

        failed = subprocess.run(
            ["bash", str(candidate / "install.sh"), "--no-plugins"],
            cwd=candidate,
            env=env,
            text=True,
            capture_output=True,
        )
        assert failed.returncode != 0
        assert "restoring the previous known-good installation" in failed.stderr
        assert (dest / "VERSION").read_bytes() == before_version
        assert (dest / "README.md").read_bytes() == before_readme
        assert (dest / "lib" / "user_layer.py").read_bytes() == before_user_layer
        assert (dest / ".claude-autonomy-install.json").read_bytes() == before_marker
        assert sentinel.read_bytes() == b"known-good-state\n"
        launcher = bindir / "claude-auto"
        assert launcher.is_symlink()
        assert Path(os.readlink(launcher)) == dest / "bin" / "claude-auto"
        version = subprocess.run(
            [str(launcher), "--version"],
            env=env,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        assert version == "claude-auto 1.0.0-rc4"


def test_p7_keep_state_uninstall_removes_package_but_preserves_durable_state():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as install:
        bindir = Path(home) / ".local" / "bin"
        bindir.mkdir(parents=True)
        dest = Path(install) / "pack"
        env = os.environ.copy()
        env.update({
            "HOME": home,
            "CLAUDE_AUTONOMY_HOME": str(dest),
            "CLAUDE_AUTONOMY_BIN": str(bindir),
        })
        installed = subprocess.run(
            ["bash", str(ROOT / "install.sh"), "--no-plugins"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
        )
        assert installed.returncode == 0, installed.stderr

        state = dest / "repos" / "fixture" / "state.json"
        state.parent.mkdir(parents=True)
        state.write_bytes(b"durable-state\n")
        marker = dest / ".claude-autonomy-install.json"
        assert marker.is_file()

        removed = subprocess.run(
            ["bash", str(dest / "uninstall.sh"), "--keep-state"],
            env=env,
            text=True,
            capture_output=True,
        )
        assert removed.returncode == 0, removed.stderr
        assert state.read_bytes() == b"durable-state\n"
        assert marker.is_file()
        assert not (bindir / "claude-auto").exists()
        for name in (
            "bin", "docs", "hooks", "lib", "scripts", "templates", "tests",
            "LICENSE", "NOTICE", "MANIFEST.sha256", "VERSION",
            "install.sh", "uninstall.sh",
        ):
            assert not (dest / name).exists(), name


def test_transactional_install_rejects_bad_candidate_without_touching_live_install():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as install, tempfile.TemporaryDirectory() as badsrc:
        bindir=Path(home)/".local"/"bin"; bindir.mkdir(parents=True)
        dest=Path(install)/"pack"
        env=os.environ.copy(); env.update({"HOME":home,"CLAUDE_AUTONOMY_HOME":str(dest),"CLAUDE_AUTONOMY_BIN":str(bindir)})
        first=subprocess.run(["bash",str(ROOT/"install.sh"),"--no-plugins"],cwd=ROOT,env=env,text=True,capture_output=True)
        assert first.returncode == 0, first.stderr
        (dest/"repos"/"keep").mkdir(parents=True); sentinel=dest/"repos"/"keep"/"state.json"; sentinel.write_text("known-good\n")
        live_version=(dest/"VERSION").read_text()
        candidate=Path(badsrc)/"candidate"; shutil.copytree(ROOT,candidate,ignore=shutil.ignore_patterns("__pycache__",".pytest_cache"))
        # Deliberately corrupt a manifest-covered file after copying the valid manifest.
        (candidate/"README.md").write_text((candidate/"README.md").read_text()+"\nCORRUPTED CANDIDATE\n")
        bad=subprocess.run(["bash",str(candidate/"install.sh"),"--no-plugins"],cwd=candidate,env=env,text=True,capture_output=True)
        assert bad.returncode != 0
        assert (dest/"VERSION").read_text() == live_version
        assert sentinel.read_text() == "known-good\n"
        assert (bindir/"claude-auto").is_symlink()



def test_security_review_gate_parser():
    out=ca.parse_security_review_gate('x\nSECURITY_REVIEW_GATE: {"verdict":"FAIL","summary":"fix auth","findings":["missing authz"]}')
    assert out == {"verdict":"FAIL","summary":"fix auth","findings":["missing authz"]}
    assert ca.parse_security_review_gate("no protocol")["verdict"] == "BLOCKED"


def test_native_security_review_is_readonly_when_origin_available(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd, tempfile.TemporaryDirectory() as rd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n"); git_commit_with_origin(r, Path(rd)/"origin.git")
        capture=Path(dh)/"capture.jsonl"; make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH",bd+os.pathsep+os.environ.get("PATH","")); monkeypatch.setenv("CLAUDE_AUTONOMY_HOME",dh); monkeypatch.setenv("FAKE_CLAUDE_CAPTURE",str(capture))
        before=ca.git_snapshot(r)
        rc=ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","security-gated completion","--max-cycles","1","--max-turns","5"])
        assert rc == 0
        assert before == ca.git_snapshot(r)
        calls=[json.loads(x) for x in capture.read_text().splitlines()]
        native=next(x for x in calls if x["args"][-1] == "/security-review")
        assert "--no-session-persistence" in native["args"]
        assert native["args"][native["args"].index("--permission-prompts")+1] == "none"
        assert "Bash" not in native["args"][native["args"].index("--tools")+1]
        denied=native["args"][native["args"].index("--disallowed-tools")+1]
        assert "Edit" in denied and "Write" in denied and "Bash" in denied and "mcp__*" in denied
        adjudicator=next(x for x in calls if "mandatory HARD READ-ONLY security completion auditor" in x["args"][-1])
        assert "--no-session-persistence" in adjudicator["args"]
        st=json.loads((ca.repo_state_dir(r)/"state.json").read_text())
        assert st["status"] == "COMPLETE"
        assert st["last_security_review_verdict"] == "PASS"
        log=json.loads((ca.repo_state_dir(r)/"logs"/"goal-0001-security-review.json").read_text())
        assert log["verdict"] == "PASS"
        assert log["repository_unchanged"] is True


def test_correctness_fail_reopens_and_resume_requires_passing_rereview(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        capture=Path(dh)/"capture.jsonl"; make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH",bd+os.pathsep+os.environ.get("PATH",""))
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME",dh)
        monkeypatch.setenv("FAKE_CLAUDE_CAPTURE",str(capture))
        monkeypatch.setenv("FAKE_CODE_REVIEW_VERDICT","FAIL")
        monkeypatch.setenv("FAKE_CODE_REVIEW_SUMMARY","state transition defect")
        monkeypatch.setenv("FAKE_CODE_REVIEW_FINDING","resume path loses committed state")

        rc1=ca.main([
            "run","--model-qualification","off","--repo",str(r),
            "--objective","finish correctly","--max-cycles","1","--max-turns","5",
            "--max-stagnant-cycles","0",
        ])
        assert rc1 == 4
        st1=json.loads((ca.repo_state_dir(r)/"state.json").read_text())
        assert st1["last_correctness_review_verdict"] == "FAIL"
        assert st1["last_correctness_review_findings"] == ["resume path loses committed state"]

        monkeypatch.setenv("FAKE_CODE_REVIEW_VERDICT","PASS")
        rc2=ca.main([
            "run","--model-qualification","off","--repo",str(r),
            "--objective","finish correctly","--max-cycles","1","--max-turns","5",
            "--max-stagnant-cycles","0",
        ])
        assert rc2 == 0
        calls=[json.loads(x) for x in capture.read_text().splitlines()]
        goals=[x for x in calls if x["args"] and "/goal " in x["args"][-1]]
        assert len(goals) >= 2
        assert "resume path loses committed state" in goals[-1]["args"][-1]
        st2=json.loads((ca.repo_state_dir(r)/"state.json").read_text())
        assert st2["status"] == "COMPLETE"
        assert st2["last_correctness_review_verdict"] == "PASS"
        assert st2["last_correctness_review_findings"] is None


def test_security_review_fail_reopens_worker_and_feeds_findings(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        capture=Path(dh)/"capture.jsonl"; make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH",bd+os.pathsep+os.environ.get("PATH","")); monkeypatch.setenv("CLAUDE_AUTONOMY_HOME",dh); monkeypatch.setenv("FAKE_CLAUDE_CAPTURE",str(capture))
        monkeypatch.setenv("FAKE_SECURITY_VERDICT","FAIL"); monkeypatch.setenv("FAKE_SECURITY_SUMMARY","authz defect"); monkeypatch.setenv("FAKE_SECURITY_FINDING","endpoint lacks authorization check")
        rc=ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","fix feature securely","--max-cycles","2","--max-turns","5","--max-stagnant-cycles","0"])
        assert rc == 4
        calls=[json.loads(x) for x in capture.read_text().splitlines()]
        goals=[x for x in calls if x["args"] and "/goal " in x["args"][-1]]
        assert len(goals) == 2
        assert "endpoint lacks authorization check" in goals[1]["args"][-1]
        st=json.loads((ca.repo_state_dir(r)/"state.json").read_text())
        assert st["last_security_review_verdict"] == "FAIL"
        assert st["last_security_review_findings"] == ["endpoint lacks authorization check"]
        assert st["status"] == "LIMIT_REACHED"


def test_security_review_native_unavailable_falls_back(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd, tempfile.TemporaryDirectory() as rd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n"); git_commit_with_origin(r, Path(rd)/"origin.git")
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH",bd+os.pathsep+os.environ.get("PATH","")); monkeypatch.setenv("CLAUDE_AUTONOMY_HOME",dh); monkeypatch.setenv("FAKE_SECURITY_NATIVE_FAIL","1")
        rc=ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","test","--max-cycles","1","--max-turns","5"])
        assert rc == 0
        st=json.loads((ca.repo_state_dir(r)/"state.json").read_text())
        assert st["status"] == "COMPLETE"
        assert st["last_security_review_verdict"] == "PASS"


def test_security_review_precedes_optional_challenger(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        capture=Path(dh)/"capture.jsonl"; make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH",bd+os.pathsep+os.environ.get("PATH","")); monkeypatch.setenv("CLAUDE_AUTONOMY_HOME",dh); monkeypatch.setenv("FAKE_CLAUDE_CAPTURE",str(capture))
        rc=ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","test","--max-cycles","1","--max-turns","5","--challenger-policy","final","--challenger-model","reviewer"])
        assert rc == 0
        calls=[json.loads(x) for x in capture.read_text().splitlines()]
        security_index=next(i for i,x in enumerate(calls) if "mandatory HARD READ-ONLY security completion auditor" in x["args"][-1])
        challenger_index=next(i for i,x in enumerate(calls) if "independent, HARD READ-ONLY challenger" in x["args"][-1])
        assert security_index < challenger_index


# --- legacy adversarial regressions: each corresponds to a reproduced legacy audit gap. ---

def test_supervisor_lease_blocks_second_writer(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        sd=ca.repo_state_dir(r)
        with ca.SupervisorLease(sd, r):
            with pytest.raises(SystemExit, match="already owns this repository"):
                with ca.SupervisorLease(sd, r):
                    pass


def test_corrupt_state_recovers_previous_generation(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        sd=ca.activate(r)
        st=ca.load_json(sd/"state.json", {})
        st["objective"]="generation-one"; ca.json_dump(sd/"state.json", st)
        st["objective"]="generation-two"; ca.json_dump(sd/"state.json", st)
        (sd/"state.json").write_text("{broken")
        recovered=ca.load_json(sd/"state.json", {})
        assert recovered["objective"] == "generation-one"
        assert recovered.get("state_recovered_at")
        assert "state_recovery_reason" in recovered


def test_corrupt_state_without_valid_backup_fails_closed(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        sd=ca.activate(r)
        (sd/"state.json").write_text("{broken")
        for name in ("state.prev.json","state.prev.sha256"):
            try: (sd/name).unlink()
            except FileNotFoundError: pass
        with pytest.raises(ca.StateCorruptionError):
            ca.load_json(sd/"state.json", {})


def test_repository_move_preserves_state_identity(monkeypatch):
    with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as dh:
        b=Path(base); a=b/"repo-a"; moved=b/"repo-b"; a.mkdir(); git_init(a); (a/"README.md").write_text("x\n")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        sd1=ca.activate(a); st=ca.load_json(sd1/"state.json", {}); st["objective"]="keep-me"; ca.json_dump(sd1/"state.json",st)
        shutil.move(str(a), str(moved))
        sd2=ca.activate(moved)
        assert sd1 == sd2
        assert ca.load_json(sd2/"state.json", {})["objective"] == "keep-me"


def test_replacement_repository_at_same_path_gets_new_state(monkeypatch):
    with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as dh:
        p=Path(base)/"repo"; p.mkdir(); git_init(p); (p/"README.md").write_text("old\n")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        sd1=ca.activate(p); st=ca.load_json(sd1/"state.json", {}); st["objective"]="old-objective"; ca.json_dump(sd1/"state.json",st)
        shutil.rmtree(p); p.mkdir(); git_init(p); (p/"README.md").write_text("new\n")
        sd2=ca.activate(p)
        assert sd1 != sd2
        assert ca.load_json(sd2/"state.json", {}).get("objective") is None


def test_readonly_snapshot_detects_ignored_file_mutation():
    with tempfile.TemporaryDirectory() as td:
        r=Path(td); git_init(r); (r/".gitignore").write_text("ignored.dat\n"); (r/"ignored.dat").write_text("one\n")
        a=ca.git_snapshot(r); (r/"ignored.dat").write_text("two\n"); b=ca.git_snapshot(r)
        assert a["ignored_sha256"] != b["ignored_sha256"]
        assert ca._repo_snapshot_unchanged(a,b) is False


def test_false_complete_cannot_bypass_fresh_failing_verification(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r)
        (r/"pyproject.toml").write_text("[project]\nname='x'\nversion='0.0.1'\n[tool.pytest.ini_options]\n")
        (r/"tests").mkdir(); (r/"tests/test_ok.py").write_text("def test_ok():\n    assert True\n")
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH",bd+os.pathsep+os.environ.get("PATH","")); monkeypatch.setenv("CLAUDE_AUTONOMY_HOME",dh)
        monkeypatch.setenv("FAKE_GOAL_MUTATE_FAILING_PYTEST","1")
        rc=ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","introduce change","--max-cycles","1","--max-turns","5","--verification-timeout","60","--trust-repo-scripts"])
        assert rc == 4
        st=ca.load_json(ca.repo_state_dir(r)/"state.json", {})
        assert st["status"] == "LIMIT_REACHED"
        assert st.get("last_verification_findings")
        final=json.loads((ca.repo_state_dir(r)/"verification-final.json").read_text())
        assert any(x.get("verdict") == "FAIL" for x in final["receipts"])


def test_unchanged_preexisting_failure_is_not_false_blocker(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r)
        (r/"pyproject.toml").write_text("[project]\nname='x'\nversion='0.0.1'\n[tool.pytest.ini_options]\n")
        (r/"tests").mkdir(); (r/"tests/test_old_fail.py").write_text("def test_old_fail():\n    assert False\n")
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH",bd+os.pathsep+os.environ.get("PATH","")); monkeypatch.setenv("CLAUDE_AUTONOMY_HOME",dh)
        rc=ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","documentation-only objective","--max-cycles","1","--max-turns","5","--verification-timeout","60","--trust-repo-scripts"])
        assert rc == 0
        final=json.loads((ca.repo_state_dir(r)/"verification-final.json").read_text())
        assert any(x.get("verdict") == "BASELINE_FAILURE_UNCHANGED" for x in final["receipts"])


def test_no_origin_security_review_uses_portable_fallback(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        capture=Path(dh)/"capture.jsonl"; make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH",bd+os.pathsep+os.environ.get("PATH","")); monkeypatch.setenv("CLAUDE_AUTONOMY_HOME",dh); monkeypatch.setenv("FAKE_CLAUDE_CAPTURE",str(capture))
        assert ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","test","--max-cycles","1","--max-turns","5"]) == 0
        calls=[json.loads(x) for x in capture.read_text().splitlines()]
        assert not any(x["args"] and x["args"][-1] == "/security-review" for x in calls)
        assert any("mandatory HARD READ-ONLY security completion auditor" in x["args"][-1] for x in calls)


def test_portable_security_review_blocked_still_fails_closed(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH",bd+os.pathsep+os.environ.get("PATH","")); monkeypatch.setenv("CLAUDE_AUTONOMY_HOME",dh); monkeypatch.setenv("FAKE_SECURITY_VERDICT","BLOCKED")
        rc=ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","test","--max-cycles","1","--max-turns","5"])
        assert rc == 3
        st=ca.load_json(ca.repo_state_dir(r)/"state.json", {})
        assert st["status"] == "BLOCKED" and st["last_security_review_verdict"] == "BLOCKED"


def test_qualification_fingerprint_invalidates_gateway_change(monkeypatch):
    with tempfile.TemporaryDirectory() as dh:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        monkeypatch.setattr(ca, "_claude_version_text", lambda: "2.1.283")
        a={"gateway_url":"https://one.example","gateway_token_env":"KEY","gateway_discovery":False,"isolate_provider_profile":False,"gateway_hints":True}
        b=dict(a); b["gateway_url"]="https://two.example"
        fp=ca.qualification_route_fingerprint("custom","m1",a)
        rec={"compatible":True,"qualification_level":"AUTONOMOUS_FULL","qualified_at":ca.utcnow(),"route_fingerprint":fp}
        ca.save_model_qualification("custom","m1",rec)
        assert ca.qualification_is_fresh(ca.qualification_record("custom","m1"), fp)
        assert not ca.qualification_is_fresh(ca.qualification_record("custom","m1"), ca.qualification_route_fingerprint("custom","m1",b))


def test_native_default_route_is_qualified(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        calls=[]
        def fake(qargs):
            calls.append(qargs)
            values={"gateway_url":qargs.gateway_url,"gateway_token_env":qargs.gateway_token_env,"gateway_discovery":qargs.gateway_discovery,"isolate_provider_profile":qargs.isolate_provider_profile,"gateway_hints":qargs.gateway_hints}
            ca.save_model_qualification(qargs.provider, qargs.model,{"compatible":True,"qualification_level":"AUTONOMOUS_FULL","qualified_at":ca.utcnow(),"route_fingerprint":ca.qualification_route_fingerprint(qargs.provider,qargs.model,values)})
            return 0
        monkeypatch.setattr(ca,"qualify_model",fake)
        args=ca.build_parser().parse_args(["run","--repo",str(r),"--objective","x"])
        ca.ensure_model_qualification(args,r,None,role="main",required_level="AUTONOMOUS_FULL")
        assert len(calls)==1 and calls[0].model is None and calls[0].provider=="native"


def test_qualification_budget_exhaustion_stops_later_stages_without_zero_budget_launch(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("legacy qualification budget test\n")
        capture=Path(dh)/"capture.jsonl"
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH",bd+os.pathsep+os.environ.get("PATH",""))
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME",dh)
        monkeypatch.setenv("FAKE_CLAUDE_CAPTURE",str(capture))
        rc=ca.main([
            "models","qualify","--repo",str(r),"--level","full",
            "--max-total-budget-usd","0.01","--max-turns","12",
        ])
        assert rc == 6
        calls=[json.loads(x) for x in capture.read_text().splitlines() if x.strip()]
        print_calls=[c for c in calls if "-p" in c.get("args",[])]
        assert len(print_calls) == 1
        args=print_calls[0]["args"]
        assert "--max-budget-usd" in args
        assert float(args[args.index("--max-budget-usd")+1]) > 0.0
        rec=ca.qualification_record("native",None)
        assert rec and [st["name"] for st in rec.get("stages",[])] == ["readonly"]
        assert rec["qualification_level"] == "FAILED"


def test_global_usage_accounts_control_plane_reviews(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH",bd+os.pathsep+os.environ.get("PATH","")); monkeypatch.setenv("CLAUDE_AUTONOMY_HOME",dh)
        assert ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","test","--max-cycles","1","--max-turns","5","--max-total-budget-usd","1.0"]) == 0
        st=ca.load_json(ca.repo_state_dir(r)/"state.json", {})
        # Planner, simulation, red-team, worker, correctness adjudicator and security auditor each report cost.
        assert float(st.get("total_cost_usd",0)) >= 0.06


def test_docker_guard_blocks_host_escape_and_allows_repo_local_build():
    guard=ROOT/"hooks"/"docker_guard.py"
    with tempfile.TemporaryDirectory() as td:
        root=Path(td).resolve()
        env=os.environ.copy(); env["CLAUDE_AUTO_REPO_ROOT"]=str(root)
        def decide(command):
            cp=subprocess.run(
                [sys.executable,str(guard)],
                input=json.dumps({"tool_input":{"command":command}}),
                text=True,capture_output=True,check=True,env=env,
            )
            return json.loads(cp.stdout)["hookSpecificOutput"]["permissionDecision"]
        assert decide("docker build -t app .") == "allow"
        assert decide("docker run --rm app pytest") == "allow"
        assert decide("docker run --privileged app") == "deny"
        assert decide("docker run -v /:/host app") == "deny"
        assert decide("docker run -v /var/run/docker.sock:/var/run/docker.sock app") == "deny"


def test_weaker_nested_sandbox_requires_explicit_attestation(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh:
        r=Path(td); git_init(r); monkeypatch.setenv("CLAUDE_AUTONOMY_HOME",dh)
        sd=ca.activate(r); prof=json.loads((sd/"profile.json").read_text())
        monkeypatch.delenv("CLAUDE_AUTO_ALLOW_WEAKER_NESTED_SANDBOX",raising=False)
        settings=ca.make_settings(sd,"external","balanced",prof)
        assert settings["sandbox"].get("enableWeakerNestedSandbox") is False
        monkeypatch.setenv("CLAUDE_AUTO_ALLOW_WEAKER_NESTED_SANDBOX","1")
        settings=ca.make_settings(sd,"external","balanced",prof)
        if ca._inside_container():
            assert settings["sandbox"].get("enableWeakerNestedSandbox") is True


def test_runtime_event_logger_does_not_persist_raw_command_secret(monkeypatch):
    with tempfile.TemporaryDirectory() as sd:
        env=os.environ.copy(); env["CLAUDE_AUTONOMY_STATE_DIR"]=sd
        secret="weird-secret-value-XYZ987654"
        payload={"hook_event_name":"PermissionDenied","tool_name":"Bash","tool_input":{"command":f"tool --password {secret}"},"reason":"denied"}
        cp=subprocess.run([sys.executable,str(ROOT/"hooks"/"runtime_event_logger.py")],input=json.dumps(payload),text=True,capture_output=True,env=env)
        assert cp.returncode==0
        raw=(Path(sd)/"runtime-events.jsonl").read_text()
        assert secret not in raw and "--password" not in raw


def test_supervisor_signal_checkpoints_interrupted_state(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        monkeypatch.setattr(ca, "require_supported_claude", lambda: None)
        def interrupted(_args):
            os.kill(os.getpid(), signal.SIGTERM)
            raise AssertionError("signal handler did not interrupt")
        monkeypatch.setattr(ca, "_do_run_goal_unlocked", interrupted)
        args=ca.build_parser().parse_args(["run","--repo",str(r),"--objective","x","--model-qualification","off"])
        assert ca.do_run_goal(args) == 128 + signal.SIGTERM
        state=ca.load_json(ca.repo_state_dir(r)/"state.json", {})
        assert state["status"] == "INTERRUPTED"
        assert state["last_result_status"] == "CONTINUE"
        assert state["last_interrupt_signal"] == signal.SIGTERM


def test_runtime_event_log_rotates_before_append(monkeypatch):
    with tempfile.TemporaryDirectory() as sd:
        state=Path(sd); path=state/"runtime-events.jsonl"
        path.write_bytes(b"x"*(5*1024*1024+1))
        env=os.environ.copy(); env["CLAUDE_AUTONOMY_STATE_DIR"]=sd
        payload={"hook_event_name":"StopFailure","error":"rate_limit","last_assistant_message":"x"}
        cp=subprocess.run([sys.executable,str(ROOT/"hooks"/"runtime_event_logger.py")],input=json.dumps(payload),text=True,capture_output=True,env=env)
        assert cp.returncode == 0
        assert (state/"runtime-events.jsonl.1").exists()
        assert path.stat().st_size < 100000


def test_hermetic_session_omits_user_project_local_setting_sources(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        capture=Path(dh)/"capture.jsonl"
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH",bd+os.pathsep+os.environ.get("PATH","")); monkeypatch.setenv("CLAUDE_AUTONOMY_HOME",dh)
        monkeypatch.setenv("FAKE_CLAUDE_CAPTURE",str(capture))
        assert ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","test","--max-cycles","1","--max-turns","5","--session-settings","hermetic"]) == 0
        calls=[json.loads(x) for x in capture.read_text().splitlines() if x.strip()]
        goal=next(c for c in calls if c.get("args") and c["args"][-1].startswith("/goal "))
        idx=goal["args"].index("--setting-sources")
        assert goal["args"][idx+1] == ""


def test_service_unit_restarts_retryable_exit_but_stops_terminal_states(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as home:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME",dh); monkeypatch.setenv("HOME",home)
        monkeypatch.setattr(ca.Path,"home",staticmethod(lambda: Path(home)))
        monkeypatch.setattr(ca.shutil,"which",lambda _name: None)
        args=ca.build_parser().parse_args(["service","install","--repo",str(r),"--objective","resume me"])
        assert ca.service_action(args) == 0
        unit=ca._service_unit_path(r)
        text=unit.read_text()
        assert "Restart=on-failure" in text
        prevent = next(x for x in text.splitlines() if x.startswith("RestartPreventExitStatus"))
        assert prevent == "RestartPreventExitStatus=3 4 5 6 8"
        assert "7" not in prevent, "retryable provider exhaustion must be restartable by the resume service"
        assert "130" not in prevent
        assert " run --repo " in text and "--resume-config" in text
        st=ca.load_json(ca.repo_state_dir(r)/"state.json",{})
        assert st["objective"] == "resume me"




def test_service_new_objective_does_not_inherit_unattended_authority(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as home:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME",dh); monkeypatch.setenv("HOME",home)
        monkeypatch.setattr(ca.Path,"home",staticmethod(lambda: Path(home)))
        monkeypatch.setattr(ca.shutil,"which",lambda _name: None)
        sd=ca.activate(r)
        st=ca.load_json(sd/"state.json",{})
        st["objective"]="old objective"
        st["autonomy_profile"]="unattended"
        st["resume_config"]={"profile":"unattended","session_settings":"hermetic"}
        ca.json_dump(sd/"state.json",st)

        args=ca.build_parser().parse_args([
            "service","install","--repo",str(r),"--objective","new objective"
        ])
        assert ca.service_action(args) == 0
        updated=ca.load_json(sd/"state.json",{})
        assert updated["objective"] == "new objective"
        assert updated["autonomy_profile"] == "balanced"
        assert updated["resume_config"]["profile"] == "balanced"
        assert updated["resume_config"]["session_settings"] == "hermetic"


def test_resume_config_restores_route_and_limits(monkeypatch):
    args=ca.build_parser().parse_args(["run","--objective","x","--model","m1","--provider","openrouter","--max-total-budget-usd","2.5","--profile","strict","--session-settings","hermetic"])
    cfg=ca._capture_resume_config(args)
    resumed=ca.build_parser().parse_args(["run","--objective","x","--resume-config"])
    ca._apply_resume_config(resumed,{"resume_config":cfg})
    assert resumed.model == "m1"
    assert resumed.provider == "openrouter"
    assert resumed.max_total_budget_usd == 2.5
    assert resumed.profile == "strict"
    assert resumed.session_settings == "hermetic"


def test_runtime_verify_is_selective_and_reopens_on_failure(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"package.json").write_text(json.dumps({"scripts":{"start":"node app.js"}})); (r/"app.js").write_text("console.log('ok')\n")
        make_fake_claude(Path(bd)); monkeypatch.setenv("PATH",bd+os.pathsep+os.environ.get("PATH","")); monkeypatch.setenv("CLAUDE_AUTONOMY_HOME",dh)
        monkeypatch.setenv("FAKE_RUNTIME_VERIFY_VERDICT","FAIL")
        rc=ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","test","--max-cycles","1","--max-turns","5"])
        assert rc == 4
        st=ca.load_json(ca.repo_state_dir(r)/"state.json",{})
        assert st.get("last_runtime_verify_verdict") == "FAIL"
        assert st.get("last_result_status") == "CONTINUE"


def test_runtime_verify_unavailable_is_nonblocking(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"package.json").write_text(json.dumps({"scripts":{"start":"node app.js"}})); (r/"app.js").write_text("console.log('ok')\n")
        make_fake_claude(Path(bd)); monkeypatch.setenv("PATH",bd+os.pathsep+os.environ.get("PATH","")); monkeypatch.setenv("CLAUDE_AUTONOMY_HOME",dh); monkeypatch.setenv("FAKE_RUNTIME_VERIFY_NATIVE_FAIL","1")
        assert ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","test","--max-cycles","1","--max-turns","5","--security-scanners","off"]) == 0
        st=ca.load_json(ca.repo_state_dir(r)/"state.json",{})
        assert st.get("last_runtime_verify_verdict") == "SKIP"


def test_default_headless_balanced_uses_hermetic_setting_sources(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        project = r / ".claude" / "settings.json"
        project.parent.mkdir(parents=True)
        project.write_text(json.dumps({
            "sandbox": {"filesystem": {"denyWrite": ["docs/sdp/**"]}},
            "permissions": {"deny": ["Bash(git merge *)"]},
        }))
        capture=Path(dh)/"capture.jsonl"
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH", bd+os.pathsep+os.environ.get("PATH",""))
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(capture))
        assert ca.main(["run","--model-qualification","off","--repo",str(r),"--objective","test","--max-cycles","1","--max-turns","5"]) == 0
        calls=[json.loads(x) for x in capture.read_text().splitlines() if x.strip()]
        goal=next(x for x in calls if x.get("args") and x["args"][-1].startswith("/goal "))
        idx=goal["args"].index("--setting-sources")
        assert goal["args"][idx+1] == ""


def test_explicit_compatibility_retains_user_project_local_setting_sources(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        capture=Path(dh)/"capture.jsonl"
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH", bd+os.pathsep+os.environ.get("PATH",""))
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(capture))
        assert ca.main([
            "run","--model-qualification","off","--repo",str(r),"--objective","test",
            "--max-cycles","1","--max-turns","5","--session-settings","compatibility",
        ]) == 0
        calls=[json.loads(x) for x in capture.read_text().splitlines() if x.strip()]
        goal=next(x for x in calls if x.get("args") and x["args"][-1].startswith("/goal "))
        idx=goal["args"].index("--setting-sources")
        assert goal["args"][idx+1] == "user,project,local"


def test_interactive_balanced_start_remains_compatibility(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dh, tempfile.TemporaryDirectory() as bd:
        r=Path(td); git_init(r); (r/"README.md").write_text("x\n")
        capture=Path(dh)/"capture.jsonl"
        make_fake_claude(Path(bd))
        monkeypatch.setenv("PATH", bd+os.pathsep+os.environ.get("PATH",""))
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", dh)
        monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(capture))
        assert ca.main(["start","--repo",str(r),"--objective","test"]) == 0
        calls=[json.loads(x) for x in capture.read_text().splitlines() if x.strip()]
        call=calls[-1]
        idx=call["args"].index("--setting-sources")
        assert call["args"][idx+1] == "user,project,local"
