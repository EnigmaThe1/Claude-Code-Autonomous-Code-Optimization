from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import claude_auto as ca
import toolchain_preflight as tp
from environment_policy import (
    apply_resume_environment,
    capture_resume_environment,
    home_toolchain_path_entries,
    sanitised_subprocess_env,
    strip_injected_git_config,
)
from execution import _readonly_toolchain_paths
from process_runner import run
from repo_profile import detect_commands
from settings_policy import make_settings
from toolchain_preflight import (
    _rustup_component_names,
    detect_toolchain_requirements,
    normalise_local_shell_entrypoint,
)
from user_layer import hook_object
from verification import _run_verification_command
from workspace_recovery import promote_fast_forward


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = sanitised_subprocess_env()
    return subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        check=check,
        env=env,
    )


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "Test")
    (path / "base.txt").write_text("base\n")
    _git(path, "add", "base.txt")
    _git(path, "commit", "-qm", "base")
    return path


def test_strips_only_process_scoped_git_config_injection():
    env = {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "core.foo",
        "GIT_CONFIG_VALUE_0": "bar",
        "GIT_CONFIG_KEY_1": "core.baz",
        "GIT_CONFIG_VALUE_1": "qux",
        "GIT_CONFIG_PARAMETERS": "'core.x=y'",
        "GIT_SSH_COMMAND": "ssh -F /safe/config",
        "PATH": "/usr/bin",
    }
    strip_injected_git_config(env)
    assert not any(
        key == "GIT_CONFIG_COUNT"
        or key == "GIT_CONFIG_PARAMETERS"
        or key.startswith("GIT_CONFIG_KEY_")
        or key.startswith("GIT_CONFIG_VALUE_")
        for key in env
    )
    assert env["GIT_SSH_COMMAND"] == "ssh -F /safe/config"
    assert env["PATH"] == "/usr/bin"


def test_process_runner_git_survives_poisoned_parent_environment(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        root = _repo(Path(td))
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.delenv("GIT_CONFIG_KEY_0", raising=False)
        monkeypatch.delenv("GIT_CONFIG_VALUE_0", raising=False)
        cp = run(["git", "-C", str(root), "status", "--porcelain"])
        assert cp.returncode == 0, cp.stderr


def test_sanitised_environment_disables_python_bytecode_and_preserves_git_transport():
    env = sanitised_subprocess_env({
        "PATH": "/usr/bin",
        "GIT_CONFIG_COUNT": "99",
        "GIT_SSH_COMMAND": "ssh test",
    })
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "GIT_CONFIG_COUNT" not in env
    assert env["GIT_SSH_COMMAND"] == "ssh test"


def test_resume_environment_restores_path_but_not_inline_git_config():
    with tempfile.TemporaryDirectory() as td:
        root = _repo(Path(td))
        saved = capture_resume_environment(root, {
            "PATH": "/custom/toolchain/bin:/usr/bin",
            "JAVA_HOME": "/custom/jdk",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.foo",
            "GIT_CONFIG_VALUE_0": "bar",
        })
        target = {
            "PATH": "/usr/bin",
            "GIT_CONFIG_COUNT": "7",
            "GIT_CONFIG_KEY_0": "bad",
            "GIT_CONFIG_VALUE_0": "bad",
        }
        apply_resume_environment(saved, target)
        assert target["PATH"] == "/custom/toolchain/bin:/usr/bin"
        assert target["JAVA_HOME"] == "/custom/jdk"
        assert target["PYTHONDONTWRITEBYTECODE"] == "1"
        assert not any(k.startswith("GIT_CONFIG_KEY_") or k.startswith("GIT_CONFIG_VALUE_") for k in target)
        assert "GIT_CONFIG_COUNT" not in target


def test_resume_environment_refuses_external_virtualenv():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as external:
        root = _repo(Path(td))
        saved = capture_resume_environment(root, {
            "PATH": "/usr/bin",
            "VIRTUAL_ENV": external,
        })
        assert "VIRTUAL_ENV" not in saved


def test_resume_environment_keeps_repository_virtualenv():
    with tempfile.TemporaryDirectory() as td:
        root = _repo(Path(td))
        venv = root / ".venv"
        venv.mkdir()
        saved = capture_resume_environment(root, {
            "PATH": "/usr/bin",
            "VIRTUAL_ENV": str(venv),
        })
        assert saved["VIRTUAL_ENV"] == str(venv.resolve())


def test_custom_home_toolchain_path_is_visible_but_sensitive_config_is_not(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        custom = home / ".local" / "share" / "project-x" / "toolchain" / "bin"
        sensitive = home / ".config" / "secret-tool" / "bin"
        custom.mkdir(parents=True)
        sensitive.mkdir(parents=True)
        entries = home_toolchain_path_entries(
            os.pathsep.join([str(custom), str(sensitive), "/usr/bin"]),
            home=home,
        )
        assert custom.resolve() in entries
        assert sensitive.resolve() not in entries

        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("PATH", os.pathsep.join([str(custom), "/usr/bin"]))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        root = Path(td) / "repo"
        root.mkdir()
        assert custom.resolve() in _readonly_toolchain_paths(root)


def test_rust_repo_preallows_only_expected_bootstrap_domains():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sd:
        root = Path(td)
        (root / "Cargo.toml").write_text("[package]\nname='x'\nversion='0.1.0'\n")
        profile = {"repo_root": str(root), "languages": ["rust"], "manifests": ["Cargo.toml"], "container_files": []}
        settings = make_settings(Path(sd), "external", "balanced", profile)
        domains = settings["sandbox"]["network"]["allowedDomains"]
        for expected in (
            "static.rust-lang.org",
            "static.rustup.rs",
            "crates.io",
            "index.crates.io",
            "static.crates.io",
        ):
            assert expected in domains


def test_non_rust_repo_does_not_get_rust_bootstrap_domains():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sd:
        root = Path(td)
        (root / "README.md").write_text("x\n")
        profile = {"repo_root": str(root), "languages": [], "manifests": [], "container_files": []}
        settings = make_settings(Path(sd), "external", "balanced", profile)
        domains = settings["sandbox"].get("network", {}).get("allowedDomains", [])
        assert "static.rust-lang.org" not in domains
        assert "index.crates.io" not in domains


def test_package_owned_promotion_is_narrowly_preapproved_not_raw_git():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sd:
        root = Path(td)
        profile = {"repo_root": str(root), "languages": [], "manifests": [], "container_files": []}
        settings = make_settings(Path(sd), "external", "balanced", profile)
        allow = settings["permissions"]["allow"]
        excluded = settings["sandbox"]["excludedCommands"]
        assert "Bash(claude-auto promote-ff *)" in allow
        assert "claude-auto promote-ff *" in excluded
        assert "Bash(git *)" not in allow
        assert "git *" not in excluded


def test_toolchain_requirements_include_repo_markers_and_verification_contract():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "Cargo.toml").write_text("[package]\nname='x'\nversion='0.1.0'\n")
        (root / "buf.yaml").write_text("version: v2\n")
        (root / "uv.lock").write_text("")
        (root / "justfile").write_text("test:\n\ttrue\n")
        control = root / ".claude-auto"
        control.mkdir()
        (control / "verification.json").write_text(json.dumps({
            "schema_version": 1,
            "commands": {
                "test": ["psql --version && pg_ctl --version && initdb --version"],
            },
        }))
        tools = {x["tool"] for x in detect_toolchain_requirements(root, {"languages": ["rust"]})}
        assert {"cargo", "rustc", "rustfmt", "clippy", "buf", "uv", "just", "psql", "pg_ctl", "initdb"} <= tools


def test_rustup_component_parser_accepts_target_qualified_names(monkeypatch):
    monkeypatch.setattr(tp.shutil, "which", lambda name: f"/fake/{name}" if name == "rustup" else None)
    monkeypatch.setattr(
        tp,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0],
            0,
            "rustfmt-x86_64-unknown-linux-gnu (installed)\nclippy-x86_64-unknown-linux-gnu (installed)\n",
            "",
        ),
    )
    names = _rustup_component_names()
    assert "rustfmt" in names
    assert "clippy" in names


def test_non_executable_shell_entrypoint_is_invoked_through_bash():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        script = root / "verify.sh"
        script.write_text("#!/usr/bin/env bash\necho ok\n")
        script.chmod(0o600)
        effective, note = normalise_local_shell_entrypoint(root, "./verify.sh --check")
        assert effective == "bash ./verify.sh --check"
        assert note and "non-executable" in note


def test_executable_shell_entrypoint_is_not_rewritten():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        script = root / "verify.sh"
        script.write_text("#!/usr/bin/env bash\necho ok\n")
        script.chmod(0o700)
        effective, note = normalise_local_shell_entrypoint(root, "./verify.sh")
        assert effective == "./verify.sh"
        assert note is None


def test_compound_or_non_shell_entrypoint_is_not_rewritten():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        script = root / "tool.py"
        script.write_text("#!/usr/bin/env python3\nprint('x')\n")
        script.chmod(0o600)
        assert normalise_local_shell_entrypoint(root, "./tool.py")[0] == "./tool.py"
        sh = root / "verify.sh"
        sh.write_text("#!/bin/sh\ntrue\n")
        sh.chmod(0o600)
        assert normalise_local_shell_entrypoint(root, "./verify.sh && echo done")[0] == "./verify.sh && echo done"


def test_verification_receipt_preserves_declared_command_and_records_adjustment():
    with tempfile.TemporaryDirectory() as td:
        root = _repo(Path(td))
        script = root / "verify.sh"
        script.write_text("#!/usr/bin/env bash\necho verified\n")
        script.chmod(0o600)
        rec = _run_verification_command(
            root,
            "test",
            "./verify.sh",
            30,
            trust_repo_scripts=True,
        )
        assert rec["exit_code"] == 0
        assert rec["command"] == "./verify.sh"
        assert rec["effective_command"] == "bash ./verify.sh"
        assert rec["command_adjustment"]


def test_non_executable_gradle_and_maven_wrappers_use_bash():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "gradlew").write_text("#!/bin/sh\n")
        (root / "pom.xml").write_text("<project/>")
        (root / "mvnw").write_text("#!/bin/sh\n")
        hints = detect_commands(root, set())
        assert "bash ./gradlew test" in hints["test"]
        assert "bash ./mvnw test" in hints["test"]


def test_fast_forward_preserves_non_overlapping_dirty_wip_exactly():
    with tempfile.TemporaryDirectory() as td:
        root = _repo(Path(td))
        (root / "wip.txt").write_text("clean-base\n")
        _git(root, "add", "wip.txt")
        _git(root, "commit", "-qm", "add wip path")
        base = _git(root, "rev-parse", "HEAD").stdout.strip()

        (root / "target.txt").write_text("target\n")
        _git(root, "add", "target.txt")
        _git(root, "commit", "-qm", "forward target")
        target = _git(root, "rev-parse", "HEAD").stdout.strip()
        _git(root, "reset", "--hard", "-q", base)

        (root / "wip.txt").write_text("local dirty work\n")
        (root / "untracked.note").write_text("keep exactly\n")
        before_wip = (root / "wip.txt").read_bytes()
        before_untracked = (root / "untracked.note").read_bytes()

        result = promote_fast_forward(root, target)
        assert result["status"] == "promoted"
        assert _git(root, "rev-parse", "HEAD").stdout.strip() == target
        assert (root / "wip.txt").read_bytes() == before_wip
        assert (root / "untracked.note").read_bytes() == before_untracked


def test_fast_forward_refuses_target_overlapping_dirty_wip():
    with tempfile.TemporaryDirectory() as td:
        root = _repo(Path(td))
        base = _git(root, "rev-parse", "HEAD").stdout.strip()
        (root / "base.txt").write_text("forward version\n")
        _git(root, "add", "base.txt")
        _git(root, "commit", "-qm", "forward overlap")
        target = _git(root, "rev-parse", "HEAD").stdout.strip()
        _git(root, "reset", "--hard", "-q", base)
        (root / "base.txt").write_text("local WIP\n")
        with pytest.raises(ValueError, match="overlaps pre-existing local WIP"):
            promote_fast_forward(root, target)
        assert _git(root, "rev-parse", "HEAD").stdout.strip() == base
        assert (root / "base.txt").read_text() == "local WIP\n"


def test_fast_forward_refuses_non_descendant_target():
    with tempfile.TemporaryDirectory() as td:
        root = _repo(Path(td))
        original_branch = _git(root, "branch", "--show-current").stdout.strip()
        base = _git(root, "rev-parse", "HEAD").stdout.strip()
        _git(root, "checkout", "--orphan", "unrelated")
        _git(root, "rm", "-rf", ".")
        (root / "unrelated.txt").write_text("unrelated root\n")
        _git(root, "add", "unrelated.txt")
        _git(root, "commit", "-qm", "unrelated root")
        unrelated = _git(root, "rev-parse", "HEAD").stdout.strip()
        _git(root, "checkout", "-q", original_branch)
        assert _git(root, "rev-parse", "HEAD").stdout.strip() == base
        with pytest.raises(ValueError, match="not a descendant"):
            promote_fast_forward(root, unrelated)


def test_fast_forward_is_idempotent_at_target():
    with tempfile.TemporaryDirectory() as td:
        root = _repo(Path(td))
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        result = promote_fast_forward(root, head)
        assert result["status"] == "already-promoted"
        assert result["head"] == head


def test_cli_version_is_exact_version_file():
    assert ca.VERSION == (ca.package_root() / "VERSION").read_text(encoding="utf-8").strip()


def test_generated_hooks_use_python_no_bytecode_mode():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sd:
        root = Path(td)
        profile = {"repo_root": str(root), "languages": [], "manifests": [], "container_files": []}
        settings = make_settings(Path(sd), "external", "balanced", profile)
        hooks = json.dumps(settings.get("hooks", {}))
        assert "python3 -B " in hooks
        assert settings["env"]["PYTHONDONTWRITEBYTECODE"] == "1"

def test_global_session_start_hook_uses_python_no_bytecode_mode():
    hook = hook_object(ca.package_root())
    command = hook["hooks"][0]["command"]
    assert command.startswith("python3 -B ")
    assert "global_session_context.py" in command

def test_documented_package_entrypoints_are_executable():
    package = ca.package_root()
    for rel in ("bin/claude-auto", "install.sh", "uninstall.sh"):
        path = package / rel
        assert path.is_file()
        assert os.access(path, os.X_OK), f"{rel} must retain executable mode in the release tree"

