"""Independent legacy findings promoted to Claude Auto regression tests.

These tests encode the desired post-audit behaviour.  Fixed findings are normal
passing tests; confirmed legacy strengths remain lock-ins so Claude Auto cannot regress
while hardening the failure paths.
"""
import importlib.util, json, os, subprocess, sys, tempfile
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("claude_auto", ROOT / "lib" / "claude_auto.py")
ca = importlib.util.module_from_spec(spec); sys.modules["claude_auto"] = ca; spec.loader.exec_module(ca)
ex = sys.modules["execution"]
DG = str(ROOT / "hooks" / "docker_guard.py")
WG = str(ROOT / "hooks" / "write_boundary_guard.py")


def _docker(cmd, repo_root=None):
    env = os.environ.copy()
    if repo_root is not None:
        env["CLAUDE_AUTO_REPO_ROOT"] = str(Path(repo_root).resolve())
    p = subprocess.run(
        [sys.executable, DG],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}),
        text=True,
        capture_output=True,
        env=env,
    )
    return json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"]


def _git(r, *a): subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)

def _new_repo():
    d = Path(tempfile.mkdtemp()); _git(d.parent, "init", "-q", str(d)) if False else subprocess.run(["git","init","-q",str(d)],check=True,capture_output=True)
    _git(d, "config", "user.email", "a@b"); _git(d, "config", "user.name", "a")
    (d / "f.txt").write_text("x"); _git(d, "add", "-A"); _git(d, "commit", "-qm", "init")
    return d


# ---- F1 CRITICAL: supervisor runs repo verification code on the host, unsandboxed
def test_f1_verification_does_not_execute_repo_code_on_host():
    d = _new_repo()
    marker = Path(tempfile.mkdtemp()) / "PWNED"
    (d / "pyproject.toml").write_text('[project]\nname="v"\nversion="0.1"\n[tool.pytest.ini_options]\ntestpaths=["t"]\n')
    (d / "t").mkdir(); (d / "t" / "conftest.py").write_text(f"open({str(marker)!r},'w').write('x')\n")
    (d / "t" / "test_x.py").write_text("def test():assert True\n")
    _git(d, "add", "-A"); _git(d, "commit", "-qm", "t")
    ca._run_verification_command(d, "test", "pytest", 120)
    assert not marker.exists(), "repository test hook executed on the host outside any sandbox"


def test_f1_trusted_override_falls_back_when_bubblewrap_probe_is_unavailable(monkeypatch):
    d = _new_repo()
    monkeypatch.setattr(ex.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        ex,
        "_run_bwrap",
        lambda *_a, **_kw: {
            "returncode": 125,
            "stdout": "",
            "stderr": "bubblewrap sandbox unavailable: namespace denied",
            "timed_out": False,
            "wall_seconds": 0.0,
            "execution_boundary": "unavailable",
            "sandboxed": False,
            "environment_scrubbed": True,
        },
    )
    denied = ex.run_repository_command(d, "printf denied > denied.txt")
    assert denied["execution_boundary"] == "unavailable"
    assert not (d / "denied.txt").exists()

    trusted = ex.run_repository_command(
        d,
        "printf ok > trusted.txt",
        trust_repo_scripts=True,
    )
    assert trusted["execution_boundary"] == "trusted-host"
    assert trusted["environment_scrubbed"] is True
    assert (d / "trusted.txt").read_text() == "ok"


def test_f1_sandbox_projects_only_narrow_readonly_toolchains_from_home(monkeypatch):
    home = Path(tempfile.mkdtemp()).resolve()
    root = Path(tempfile.mkdtemp()).resolve()
    monkeypatch.setenv("HOME", str(home))

    safe = [
        home / ".cargo" / "bin",
        home / ".m2" / "repository",
        home / ".gradle" / "caches",
        home / ".cache" / "pip",
        home / ".nuget" / "packages",
    ]
    for p in safe:
        p.mkdir(parents=True, exist_ok=True)
    for p in (home / ".ssh", home / ".config", home / ".aws", home / ".kube"):
        p.mkdir(parents=True, exist_ok=True)

    projected = set(ex._readonly_toolchain_paths(root))
    for p in safe:
        assert p.resolve() in projected
    assert not any((home / name).resolve() in projected for name in (".ssh", ".config", ".aws", ".kube"))

    settings = ex._sandbox_settings(root, Path(tempfile.mkdtemp()).resolve())
    assert str(home) in settings["filesystem"]["denyRead"]
    for p in safe:
        assert str(p.resolve()) in settings["filesystem"]["allowRead"]


def test_f1_baseline_integrity_rejects_tracked_source_mutation():
    assert ca._baseline_integrity_error([
        {"category": "test", "command": "pytest", "tracked_source_unchanged": True},
    ]) is None
    message = ca._baseline_integrity_error([
        {"category": "build", "command": "make", "tracked_source_unchanged": False},
    ])
    assert message and "before implementation began" in message and "make" in message


# ---- F2 HIGH: adding/altering origin or rewriting root commit orphans durable state
def test_f2_identity_stable_when_origin_added_changed_removed_and_root_rewritten():
    d = _new_repo(); id1 = ca.repo_id(d)
    o1 = Path(tempfile.mkdtemp()) / "o1.git"
    o2 = Path(tempfile.mkdtemp()) / "o2.git"
    subprocess.run(["git","init","--bare","-q",str(o1)],check=True,capture_output=True)
    subprocess.run(["git","init","--bare","-q",str(o2)],check=True,capture_output=True)

    _git(d, "remote", "add", "origin", str(o1))
    assert ca.repo_id(d) == id1, "adding origin changed durable identity"
    _git(d, "remote", "set-url", "origin", str(o2))
    assert ca.repo_id(d) == id1, "changing origin changed durable identity"
    _git(d, "remote", "remove", "origin")
    assert ca.repo_id(d) == id1, "removing origin changed durable identity"

    _git(d, "commit", "--amend", "-qm", "rewritten-root-history")
    assert ca.repo_id(d) == id1, "rewriting the root commit changed durable identity"

    # Repository description text/timestamps are metadata, not durable identity.
    desc = d / ".git" / "description"
    desc.write_text("edited repository description\n")
    assert ca.repo_id(d) == id1, "editing .git/description changed durable identity"

    replacement = desc.with_name("description.tmp")
    replacement.write_text("atomically replaced repository description\n")
    os.replace(replacement, desc)
    assert ca.repo_id(d) == id1, "atomic replacement of .git/description changed durable identity"


def test_migrates_unambiguous_rc8_state_after_mutable_identity_change(monkeypatch):
    d = _new_repo()
    home = Path(tempfile.mkdtemp())
    monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", str(home))

    legacy = ca._legacy_repo_identity(d)
    old_sd = home / "repos" / legacy["key"]
    old_sd.mkdir(parents=True)
    ca.json_dump(old_sd / "state.json", {
        "schema_version": 5,
        "objective": "preserve this objective",
        "repo_root": str(d.resolve()),
        "repo_identity": legacy,
    })

    # Change a field that was part of legacy's key.  Claude Auto's stable key must remain
    # independent and migration must still discover the old state by Git lineage.
    o = Path(tempfile.mkdtemp()) / "o.git"
    subprocess.run(["git", "init", "--bare", "-q", str(o)], check=True, capture_output=True)
    _git(d, "remote", "add", "origin", str(o))

    new_sd = ca.repo_state_dir(d)
    assert new_sd != old_sd
    with ca.SupervisorLease(new_sd, d):
        assert new_sd.exists()
        migrated = ca.load_json(new_sd / "state.json", {})
        assert migrated.get("objective") == "preserve this objective"
    assert not old_sd.exists()
    assert (new_sd / "legacy-migration.json").exists()


def test_does_not_steal_rc8_collided_state_for_wrong_linked_worktree(monkeypatch):
    main = _new_repo()
    wt = Path(tempfile.mkdtemp()) / "linked"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(wt)], check=True, capture_output=True)
    try:
        home = Path(tempfile.mkdtemp())
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", str(home))
        legacy = ca._legacy_repo_identity(main)
        old_sd = home / "repos" / legacy["key"]
        old_sd.mkdir(parents=True)
        ca.json_dump(old_sd / "state.json", {
            "schema_version": 5,
            "objective": "main-only state",
            "repo_root": str(main.resolve()),
            "repo_identity": legacy,
        })
        linked_target = ca.repo_state_dir(wt)
        assert ca._migrate_legacy_state_if_needed(wt, linked_target) is None
        assert old_sd.exists() and not linked_target.exists()
    finally:
        subprocess.run(["git", "-C", str(main), "worktree", "remove", "--force", str(wt)], capture_output=True)


# ---- F3 HIGH: Strict may never opt into host fallback for supervisor repository code
def test_f3_strict_profile_refuses_trusted_host_execution_override():
    with pytest.raises(SystemExit):
        ca.validate_repository_execution_policy("strict", True)
    assert ca.validate_repository_execution_policy("strict", False) is False
    assert ca.validate_repository_execution_policy("balanced", True) is True


# ---- F4 MEDIUM: docker guard is trivially bypassable (regex/word-based, not argv-aware)
def test_f4_docker_guard_blocks_host_escape_variants():
    must_block = [
        "sudo docker run --privileged alpine",
        "env docker run --privileged alpine",
        "/usr/bin/docker run --privileged alpine",
        "docker run --privileged=true alpine",
        "docker run --net=host alpine",
        "docker run --network container:trusted alpine",
        "docker run --cap-add=ALL alpine",
        "docker run --cap-add=SYS_ADMIN alpine",
        "docker run --security-opt seccomp=unconfined alpine",
        "docker run -v /etc:/e alpine",
        "docker run -v $HOME/.ssh:/k alpine",
        "docker run --mount type=bind,src=/etc,dst=/e alpine",
        "docker run --pid='host' alpine",
        "docker run --volumes-from privileged-container alpine",
        "docker run --gpus all alpine",
        "docker exec running-container sh",
        "docker start preexisting-container",
        "podman run --privileged -v /:/host alpine",
        "DOCKER_HOST=tcp://10.0.0.5:2375 docker ps",
        "bash -c 'docker run --privileged alpine'",
        "bash -c 'docker ps'; docker run --privileged alpine",
        "RUNTIME=docker; $RUNTIME run --privileged alpine",
    ]
    missed = [c for c in must_block if _docker(c) != "deny"]
    assert not missed, f"guard allowed host-escape variants: {missed}"


def test_f4_docker_guard_inspects_mutating_compose_files():
    d = _new_repo()
    safe = d / "compose.yml"
    safe.write_text(
        "services:\n"
        "  app:\n"
        "    image: alpine\n"
        "    volumes:\n"
        "      - ./data:/data\n"
    )
    (d / "data").mkdir()
    assert _docker("docker compose -f compose.yml up -d", d) == "allow"
    assert _docker("docker-compose -f compose.yml up -d", d) == "allow"

    safe.write_text(
        "services:\n"
        "  app:\n"
        "    image: alpine\n"
        "    privileged: true\n"
    )
    assert _docker("docker compose -f compose.yml up -d", d) == "deny"

    safe.write_text(
        "services:\n"
        "  app:\n"
        "    image: alpine\n"
        "    volumes:\n"
        "      - /etc:/host-etc:ro\n"
    )
    assert _docker("docker compose -f compose.yml up -d", d) == "deny"


def test_f4_docker_guard_blocks_compose_path_and_dynamic_escape():
    d = _new_repo()
    outside = Path(tempfile.mkdtemp()) / "compose.yml"
    outside.write_text("services:\n  app:\n    image: alpine\n")
    assert _docker(f"docker compose -f {outside} up", d) == "deny"
    assert _docker("docker compose -f - up", d) == "deny"

    (d / "compose.yml").write_text(
        "services:\n"
        "  app:\n"
        "    image: alpine\n"
        "    volumes:\n"
        "      - ${HOME}/.ssh:/keys:ro\n"
    )
    assert _docker("docker compose up", d) == "deny"


def test_f4_docker_guard_allows_benign_mentions_and_repo_pwd_bind():
    d = _new_repo()
    assert _docker("echo docker", d) == "allow"
    assert _docker("grep docker README.md", d) == "allow"
    assert _docker('docker run --rm -v "$PWD:/workspace" alpine true', d) == "allow"


def test_f4_docker_guard_bounds_build_inputs_and_outputs():
    d = _new_repo()
    (d / "Dockerfile").write_text("FROM scratch\n")
    outside = Path(tempfile.mkdtemp())
    outside_df = outside / "Dockerfile"
    outside_df.write_text("FROM scratch\n")

    assert _docker("docker build -f Dockerfile .", d) == "allow"
    assert _docker(f"docker build -f {outside_df} .", d) == "deny"
    assert _docker(f"docker build {outside} -t test-image", d) == "deny"
    assert _docker("docker build --ssh default .", d) == "deny"
    assert _docker("docker build --output type=local,dest=./out .", d) == "allow"
    assert _docker("docker build --output type=local,dest=/tmp/out .", d) == "deny"


def test_f4_docker_guard_denies_inline_compose_path_structures():
    d = _new_repo()
    compose = d / "compose.yml"
    compose.write_text(
        'services:\n'
        '  app:\n'
        '    image: alpine\n'
        '    volumes: ["/:/host:ro"]\n'
    )
    assert _docker("docker compose -f compose.yml up", d) == "deny"
    compose.write_text(
        'services:\n'
        '  app:\n'
        '    build: {context: /root}\n'
    )
    assert _docker("docker compose -f compose.yml up", d) == "deny"


def test_f4_container_guard_hook_applies_without_container_files_and_in_strict():
    d = Path(tempfile.mkdtemp())
    base = {"repo_root": str(d), "container_files": [], "languages": []}
    for profile in ("balanced", "strict"):
        settings = ca.make_settings(Path(tempfile.mkdtemp()), "external", profile, base)
        pre = settings.get("hooks", {}).get("PreToolUse", [])
        rendered = json.dumps(pre)
        assert "docker_guard.py" in rendered


# ---- F5 MEDIUM: baseline-failure comparison is signature-exact -> flaky legacy failures become false blockers
def test_f5_baseline_failure_signature_tolerates_nondeterministic_output():
    a = ca._receipt_signature("E OperationalError: /tmp/pytest-of-root/pytest-12/t.db\n", "")
    b = ca._receipt_signature("E OperationalError: /tmp/pytest-of-root/pytest-13/t.db\n", "")
    assert a == b, "a stable pre-existing failure with a varying temp path is treated as a new failure"


def test_f5_receipt_signature_covers_changed_early_lines_in_long_output():
    prefix_a = "FAIL: original-defect\n" + "\n".join(f"line {i}" for i in range(400))
    prefix_b = "FAIL: different-defect\n" + "\n".join(f"line {i}" for i in range(400))
    assert ca._receipt_signature(prefix_a, "") != ca._receipt_signature(prefix_b, "")


def test_f5_final_verification_never_excuses_timeout_or_shell_execution_failure(monkeypatch):
    d = _new_repo()
    sd = Path(tempfile.mkdtemp())
    state = {
        "verification_baseline": [
            {"category": "test", "command": "pytest", "exit_code": 124, "signature": "same"},
        ],
    }
    prof = {"build_test_hints": {"test": ["pytest"]}}

    class Args:
        verification_timeout = 10
        trust_repo_scripts = False

    def timeout_receipt(*_a, **_kw):
        snap = ca.git_snapshot(d)
        return {
            "category": "test", "command": "pytest", "exit_code": 124,
            "signature": "same", "timed_out": True, "execution_boundary": "srt",
            "tracked_source_unchanged": True, "git_after": snap,
        }

    monkeypatch.setattr(ca, "_run_verification_command", timeout_receipt)
    verdict, _summary, receipts = ca.run_verification_gate(d, sd, prof, state, Args())
    assert verdict == "UNVERIFIED"
    assert receipts[0]["verdict"] == "UNVERIFIED"

    def missing_receipt(*_a, **_kw):
        snap = ca.git_snapshot(d)
        return {
            "category": "test", "command": "pytest", "exit_code": 127,
            "signature": "same", "timed_out": False, "execution_boundary": "srt",
            "tracked_source_unchanged": True, "git_after": snap,
        }

    monkeypatch.setattr(ca, "_run_verification_command", missing_receipt)
    verdict, _summary, receipts = ca.run_verification_gate(d, sd, prof, state, Args())
    assert verdict == "UNVERIFIED"
    assert receipts[0]["verdict"] == "UNVERIFIED"


# ---- F6 MEDIUM: whole ecosystems have no detected verification command (vacuous PASS)
def test_f6_detect_commands_covers_common_ecosystems():
    d = Path(tempfile.mkdtemp()); (d / "build.gradle").write_text("plugins{ id 'java' }")
    (d / "src" / "test" / "java").mkdir(parents=True); (d / "src" / "test" / "java" / "T.java").write_text("class T{}")
    assert ca.detect_commands(d, {"java-kotlin"})["test"], "Gradle project yielded no deterministic test command"


# ---- F7 MEDIUM: govulncheck signals findings with exit 3, misclassified as non-blocking TOOL_ERROR
def test_f7_scanner_classifier_handles_govulncheck_exit3():
    assert ca._scanner_verdict("govulncheck", 3) == "FINDING"


# ---- F8 MEDIUM: transient provider error in a control/review stage is recoverable
def test_f8_control_stage_transient_failure_is_recoverable(monkeypatch):
    d = _new_repo()
    sd = Path(tempfile.mkdtemp())
    calls = []

    def fake_run(_cmd, cwd=None, timeout=None, env=None):
        calls.append((cwd, timeout))
        if len(calls) == 1:
            return subprocess.CompletedProcess(_cmd, 1, stdout="", stderr="HTTP 529 overloaded temporarily unavailable")
        return subprocess.CompletedProcess(_cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(ca, "run", fake_run)
    monkeypatch.setenv("CLAUDE_AUTO_TEST_NO_BACKOFF", "1")
    cp, _text, _sid, _raw, outcome, _reason, _usage, attempts, _wall = ca._run_control_model(
        cmd=["claude", "--print", "probe"],
        root=d,
        sd=sd,
        env={},
        timeout=10,
        max_transient_retries=2,
    )
    assert cp.returncode == 0 and outcome == "SUCCESS"
    assert len(calls) == 2
    assert attempts[0]["outcome"] == "TRANSIENT_PROVIDER"


def test_f8_exhausted_control_transient_is_typed_not_terminal_blocked(monkeypatch):
    d = _new_repo()
    sd = Path(tempfile.mkdtemp())
    monkeypatch.setenv("CLAUDE_AUTO_TEST_NO_BACKOFF", "1")

    def always_transient(_cmd, cwd=None, timeout=None, env=None):
        return subprocess.CompletedProcess(
            _cmd, 1, stdout="", stderr="HTTP 529 overloaded temporarily unavailable"
        )

    monkeypatch.setattr(ca, "run", always_transient)
    with pytest.raises(ca.ControlPlaneRetryableError) as exc:
        ca.run_readonly_plan_agent(
            root=d,
            sd=sd,
            prompt="read-only probe",
            env={},
            provider_detail={"provider": "fake"},
            model=None,
            timeout=10,
        )
    assert exc.value.outcome == "TRANSIENT_PROVIDER"
    assert len(exc.value.meta.get("attempts") or []) == 5


def test_f8_outer_supervisor_checkpoints_exhausted_control_transient(monkeypatch):
    d = _new_repo()
    home = tempfile.mkdtemp()
    monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
    monkeypatch.setattr(ca, "refuse_nested_claude_launch", lambda: None)
    monkeypatch.setattr(ca, "require_supported_claude", lambda: "test")
    sd = ca.repo_state_dir(d)

    original_activate = ca.activate
    original_activate(d)
    monkeypatch.setattr(ca, "activate", lambda _root, dry_run=False: sd)

    def fail_control(_args):
        raise ca.ControlPlaneRetryableError(
            "TRANSIENT_PROVIDER",
            "provider overloaded",
            {"usage": {"total_cost_usd": 0.0}, "wall_seconds": 1.25,
             "attempts": [{"attempt": 1, "outcome": "TRANSIENT_PROVIDER"}]},
        )

    monkeypatch.setattr(ca, "_do_run_goal_unlocked", fail_control)

    class Args:
        repo = str(d)

    assert ca.do_run_goal(Args()) == 7
    state = ca.load_json(sd / "state.json", {})
    assert state["status"] == "WAITING_RETRYABLE_LIMIT"
    assert state["last_result_status"] == "WAITING_RETRYABLE"
    assert state["last_runtime_outcome"] == "TRANSIENT_PROVIDER"


# ---- F9 MEDIUM: every active durable-state writer contends the same lease
def test_f9_state_mutating_entrypoints_hold_lease(monkeypatch):
    d = _new_repo()
    monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", tempfile.mkdtemp())
    sd = ca.repo_state_dir(d)
    monkeypatch.setattr(ca, "refuse_nested_claude_launch", lambda: None)
    monkeypatch.setattr(ca, "require_supported_claude", lambda: None)
    monkeypatch.setattr(ca, "_do_start_unlocked", lambda *_a, **_kw: 0)

    class Args:
        repo = str(d)

    with ca.SupervisorLease(sd, d):
        with pytest.raises(SystemExit):
            ca.do_start(Args())
        with pytest.raises(SystemExit):
            ca.reset_state(d, True)

    assert ca.reset_state(d, True) == 0
    assert not sd.exists()


# ---- F10 LOW: Balanced denies only literal .env, not .env.local/.env.production
def test_f10_balanced_denies_dotenv_variants():
    d = Path(tempfile.mkdtemp())
    (d / ".env.qa").write_text("SECRET=x\n")
    (d / ".env.secret").write_text("SECRET=y\n")
    (d / ".env.example").write_text("PUBLIC_SAMPLE=1\n")
    nested = d / "app"
    nested.mkdir()
    (nested / ".env.custom").write_text("SECRET=z\n")

    settings = ca.make_settings(
        Path(tempfile.mkdtemp()),
        "external",
        "balanced",
        {"repo_root": str(d), "container_files": [], "languages": []},
    )
    deny = settings["permissions"]["deny"]
    rendered = "\n".join(deny)
    assert "Read(./.env.qa)" in deny
    assert "Read(./.env.secret)" in deny
    assert "Read(./app/.env.custom)" in deny
    assert "Read(./.env.example)" not in deny
    assert ".env.local" in rendered


# ---- F11 MEDIUM: obvious Bash writes are fenced before an unsandboxed retry
def test_f11_bash_write_boundary_blocks_obvious_escape():
    repo = Path(tempfile.mkdtemp())
    env = {**os.environ, "CLAUDE_AUTO_REPO_ROOT": str(repo)}

    def dec(command):
        p = subprocess.run(
            [sys.executable, WG],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
            text=True,
            capture_output=True,
            env=env,
            cwd=str(repo),
            check=True,
        )
        return json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"]

    assert dec("echo ok > inside.txt") == "allow"
    assert dec('echo ok > "$PWD/inside.txt"') == "allow"
    assert dec("echo nope > ../escape.txt") == "deny"
    assert dec("touch /tmp/claude-auto-escape") == "deny"
    assert dec('echo nope > "$HOME/escape.txt"') == "deny"
    assert dec('OUT=/tmp/escape.txt; echo nope > "$OUT"') == "deny"
    assert dec("cd /tmp && touch claude-auto-escape") == "deny"
    assert dec("bash -c 'echo nope > /tmp/claude-auto-escape'") == "deny"
    assert dec("mkdir -p sub && cd sub && touch local.txt") == "allow"


# ---- F12 LOW: large review inputs expose an indexed full-source coverage ledger
def test_f12_large_prompt_has_indexed_full_source_pointer():
    text = "".join(chr(65 + (i % 26)) for i in range(300_000))
    rendered = ca._bounded_prompt_text(text, 30_000, pointer="/tmp/full-plan.json", label="durable plan")
    assert "[LARGE_INPUT_INDEX" in rendered
    assert "full_copy=/tmp/full-plan.json" in rendered
    assert "Before issuing a verdict" in rendered
    assert "001:0-" in rendered
    assert ca.sha256_text(text) in rendered


def test_f12_large_git_diff_has_exact_indexed_full_copy():
    d = _new_repo()
    sd = Path(tempfile.mkdtemp())
    large = d / "large.txt"
    large.write_text("baseline\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "large baseline")
    large.write_text("".join(f"changed-{i:06d}-{'x'*80}\n" for i in range(5000)))

    bundle_path = ca.build_readonly_evidence(d, sd)
    bundle = ca.load_json(bundle_path, {})
    diff = bundle["worktree_diff"]
    assert diff["truncated"] is True
    assert diff["full_copy"]
    full = Path(diff["full_copy"])
    assert full.exists()
    text = full.read_text()
    assert ca.sha256_text(text) == diff["sha256"]
    assert "[LARGE_INPUT_INDEX" in diff["output"]
    assert f"full_copy={full}" in diff["output"]


def test_f12_large_planner_findings_get_exact_private_pointer():
    sd = Path(tempfile.mkdtemp())
    (sd / "plans").mkdir()
    findings = [{"verdict": "REVISE", "summary": "x" * 120_000, "findings": ["y" * 20_000]}]
    prompt = ca._planner_prompt(
        objective="o",
        prof={},
        source_kind="objective",
        source_ref=None,
        candidate_text=None,
        current_plan=None,
        revision_reason="test",
        review_findings=findings,
        state_dir=sd,
    )
    assert "[LARGE_INPUT_INDEX" in prompt
    marker = "full_copy="
    start = prompt.index(marker) + len(marker)
    pointer = prompt[start:].split("]", 1)[0].strip()
    assert pointer != "UNAVAILABLE"
    assert Path(pointer).exists()


# ---- Claude Auto plan-cache generation binding
def test_planning_context_changes_for_control_files_not_ordinary_source():
    d = _new_repo()
    (d / "README.md").write_text("initial planning instruction\n")
    (d / "app.py").write_text("VALUE = 1\n")
    prof1 = ca.asdict(ca.profile_repo(d))
    fp1 = ca._planning_context_fingerprint(d, prof1)

    (d / "app.py").write_text("VALUE = 2\n")
    prof2 = ca.asdict(ca.profile_repo(d))
    fp2 = ca._planning_context_fingerprint(d, prof2)
    assert fp2 == fp1, "ordinary implementation-source edits should not force whole-plan revalidation"

    (d / "README.md").write_text("changed planning instruction\n")
    prof3 = ca.asdict(ca.profile_repo(d))
    fp3 = ca._planning_context_fingerprint(d, prof3)
    assert fp3 != fp1, "repository instruction changes must invalidate cached plan validation"


def test_plan_validation_cache_accepts_only_supervisor_observed_descendants():
    d = _new_repo()
    initial = ca.git_snapshot(d)
    anchor_head = initial["head"]
    state = {
        "plan_validation_git_head": anchor_head,
        "plan_validation_git_snapshot_hash": ca._git_snapshot_context_hash(initial),
        "last_git_snapshot_hash": ca._git_snapshot_context_hash(initial),
    }
    assert ca._validation_anchor_compatible(d, state)

    # An out-of-band descendant commit is still an external repository change;
    # ancestry alone must not let it reuse stale plan validation.
    (d / "next.txt").write_text("next\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "external-descendant")
    descendant_snapshot = ca.git_snapshot(d)
    assert descendant_snapshot["head"] != anchor_head
    assert not ca._validation_anchor_compatible(d, state)

    # Once the supervisor has actually observed/persisted that exact snapshot,
    # forward implementation progress may retain the original validation anchor.
    state["last_git_snapshot_hash"] = ca._git_snapshot_context_hash(descendant_snapshot)
    assert ca._validation_anchor_compatible(d, state)

    # Out-of-band dirty changes are rejected too.
    (d / "next.txt").write_text("changed outside supervisor\n")
    assert not ca._validation_anchor_compatible(d, state)
    observed_dirty = ca.git_snapshot(d)
    state["last_git_snapshot_hash"] = ca._git_snapshot_context_hash(observed_dirty)
    assert ca._validation_anchor_compatible(d, state)

    # A history rewind still invalidates even if someone were to update the
    # expected snapshot hash without changing the validation anchor.
    _git(d, "reset", "--hard", anchor_head)
    rewound = ca.git_snapshot(d)
    state["last_git_snapshot_hash"] = ca._git_snapshot_context_hash(rewound)
    state["plan_validation_git_head"] = descendant_snapshot["head"]
    assert not ca._validation_anchor_compatible(d, state)


def test_activate_upgrades_v5_state_to_v6_without_losing_objective(monkeypatch):
    d = _new_repo()
    home = Path(tempfile.mkdtemp())
    monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", str(home))
    sd = ca.repo_state_dir(d)
    sd.mkdir(parents=True)
    ca.json_dump(sd / "state.json", {
        "schema_version": 5,
        "objective": "keep me",
        "status": "READY",
        "cycle": 4,
    })
    ca.activate(d)
    state = ca.load_json(sd / "state.json", {})
    assert state["schema_version"] >= 6
    assert state["objective"] == "keep me"
    assert state["cycle"] == 4


# ---- F14 LOW: worker prose is not a terminal-failure classification signal
def test_f14_worker_summary_prose_does_not_misclassify_failure():
    cp = subprocess.CompletedProcess(["claude"], 1, stdout="", stderr="worker exited with a generic error")
    outcome, _reason = ca.classify_claude_outcome(
        cp,
        None,
        "I reviewed credential handling, permission checks, and a server-down recovery path.",
        [],
    )
    assert outcome == "REAL_ERROR"


# ---- Confirmed STRENGTHS (lock-in; these should pass today) ----
def test_strength_corrupt_state_recovers_previous_generation():
    d = Path(tempfile.mkdtemp()); sj = d / "state.json"
    ca.json_dump(sj, {"schema_version": 5, "objective": "O", "cycle": 1})
    ca.json_dump(sj, {"schema_version": 5, "objective": "O", "cycle": 2})
    sj.write_text('{"torn":')
    rec = ca.load_json(sj, {})
    assert rec.get("objective") == "O" and "state_recovery_reason" in rec

def test_strength_corrupt_state_without_backup_fails_closed():
    d = Path(tempfile.mkdtemp()); sj = d / "state.json"
    ca.json_dump(sj, {"objective": "X", "schema_version": 5}); sj.write_text("{bad")
    (d / "state.prev.json").write_text("{bad too")
    with pytest.raises(ca.StateCorruptionError):
        ca.load_json(sj, {})

def test_strength_write_boundary_blocks_symlink_and_parent_escape():
    import importlib
    wb = str(ROOT / "hooks" / "write_boundary_guard.py")
    repo = Path(tempfile.mkdtemp()); outside = Path(tempfile.mkdtemp())
    (repo / "link").symlink_to(outside)
    def dec(path):
        p = subprocess.run([sys.executable, wb], input=json.dumps({"tool_name": "Write", "tool_input": {"file_path": path}}),
                           text=True, capture_output=True, env={**os.environ, "CLAUDE_AUTO_REPO_ROOT": str(repo)}, cwd=str(repo))
        return json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"]
    assert dec("../x") == "deny" and dec(str(repo / "link" / "x")) == "deny" and dec("src/ok.py") == "allow"

def test_strength_docker_guard_blocks_canonical_privileged():
    d = _new_repo()
    assert _docker("docker run --privileged alpine", d) == "deny"
    assert _docker("docker build -t x .", d) == "allow"


def test_linked_worktree_has_distinct_work_unit_identity():
    d = _new_repo()
    wt = Path(tempfile.mkdtemp()) / "linked"
    subprocess.run(["git", "-C", str(d), "worktree", "add", "-q", str(wt)], check=True, capture_output=True)
    try:
        assert ca.repo_id(d) != ca.repo_id(wt), "linked worktrees must not collide on autonomous state/lease identity"
    finally:
        subprocess.run(["git", "-C", str(d), "worktree", "remove", "--force", str(wt)], capture_output=True)
