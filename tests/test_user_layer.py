import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOD_PATH = ROOT / "lib" / "user_layer.py"
spec = importlib.util.spec_from_file_location("claude_auto_user_layer", MOD_PATH)
ul = importlib.util.module_from_spec(spec)
sys.modules["claude_auto_user_layer"] = ul
spec.loader.exec_module(ul)


def test_user_layer_install_preserves_existing_settings_and_is_idempotent(monkeypatch):
    with tempfile.TemporaryDirectory() as home:
        hp = Path(home)
        monkeypatch.setenv("HOME", home)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        ch = hp / ".claude"
        ch.mkdir()
        existing = {
            "theme": "dark",
            "hooks": {
                "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "echo existing"}]}]
            },
        }
        (ch / "settings.json").write_text(json.dumps(existing))

        first = ul.install_user_layer(ROOT)
        second = ul.install_user_layer(ROOT)
        assert first["install_uuid"] == second["install_uuid"]

        settings = json.loads((ch / "settings.json").read_text())
        assert settings["theme"] == "dark"
        blob = json.dumps(settings)
        assert "echo existing" in blob
        assert blob.count(ul.HOOK_TAG) == 1
        assert blob.count(ul.PROMPT_HOOK_TAG) == 1
        assert (ch / "rules" / "claude-auto" / "core.md").exists()
        assert (ch / "agents" / "claude-auto" / "autonomy-verifier.md").exists()
        assert (ch / "skills" / "claude-auto" / "SKILL.md").exists()
        assert (ch / "skills" / "profile" / "SKILL.md").exists()
        st = ul.status()
        assert st["installed"] and st["hook_present"] and st["owned_files_present"]


def test_user_layer_remove_preserves_unrelated_user_config(monkeypatch):
    with tempfile.TemporaryDirectory() as home:
        hp = Path(home)
        monkeypatch.setenv("HOME", home)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        ch = hp / ".claude"
        ch.mkdir()
        custom = ch / "agents" / "my-agent.md"
        custom.parent.mkdir(parents=True)
        custom.write_text("mine")
        (ch / "settings.json").write_text(json.dumps({"model": "keep-me"}))
        ul.install_user_layer(ROOT)
        result = ul.remove_user_layer()
        assert result["removed"]
        settings = json.loads((ch / "settings.json").read_text())
        assert settings["model"] == "keep-me"
        assert ul.HOOK_TAG not in json.dumps(settings)
        assert ul.PROMPT_HOOK_TAG not in json.dumps(settings)
        assert custom.read_text() == "mine"
        assert not (ch / "rules" / "claude-auto").exists()
        assert not (ch / "agents" / "claude-auto").exists()
        assert not (ch / "skills" / "claude-auto").exists()
        assert not (ch / "skills" / "profile").exists()


def test_installer_global_layer_does_not_modify_disposable_repo():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as install, tempfile.TemporaryDirectory() as repo:
        hp = Path(home)
        bindir = hp / ".local" / "bin"
        bindir.mkdir(parents=True)
        dest = Path(install) / "pack"
        rp = Path(repo)
        subprocess.run(["git", "init", "-q", str(rp)], check=True)
        (rp / "README.md").write_text("hello\n")
        before = {str(p.relative_to(rp)): p.read_bytes() for p in rp.rglob("*") if p.is_file() and ".git" not in p.parts}
        env = os.environ.copy()
        env.update({"HOME": home, "CLAUDE_AUTONOMY_HOME": str(dest), "CLAUDE_AUTONOMY_BIN": str(bindir)})
        cp = subprocess.run(["bash", str(ROOT / "install.sh")], cwd=rp, env=env, text=True, capture_output=True, timeout=30)
        assert cp.returncode == 0, cp.stderr
        after = {str(p.relative_to(rp)): p.read_bytes() for p in rp.rglob("*") if p.is_file() and ".git" not in p.parts}
        assert before == after
        assert (hp / ".claude" / ".claude-auto-integration.json").exists()
        assert (hp / ".claude" / "rules" / "claude-auto" / "core.md").exists()


def test_global_session_context_is_small_and_repo_read_only(monkeypatch):
    hook = ROOT / "hooks" / "global_session_context.py"
    with tempfile.TemporaryDirectory() as repo:
        rp = Path(repo)
        subprocess.run(["git", "init", "-q", str(rp)], check=True)
        (rp / "pyproject.toml").write_text("[project]\nname='x'\n")
        (rp / "README.md").write_text("hello\n")
        before = {str(p.relative_to(rp)): p.read_bytes() for p in rp.rglob("*") if p.is_file() and ".git" not in p.parts}
        env = os.environ.copy(); env["CLAUDE_PROJECT_DIR"] = str(rp)
        cp = subprocess.run([sys.executable, str(hook)], cwd=rp, env=env, text=True, capture_output=True)
        assert cp.returncode == 0
        assert cp.stdout.startswith("CLAUDE_AUTO_SESSION_CONTEXT=")
        assert len(cp.stdout) < 2000
        assert '"python"' in cp.stdout
        after = {str(p.relative_to(rp)): p.read_bytes() for p in rp.rglob("*") if p.is_file() and ".git" not in p.parts}
        assert before == after

def test_user_layer_refuses_preexisting_namespaced_directory_without_marker(monkeypatch):
    with tempfile.TemporaryDirectory() as home:
        hp = Path(home)
        monkeypatch.setenv("HOME", home)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        conflict = hp / ".claude" / "rules" / "claude-auto"
        conflict.mkdir(parents=True)
        (conflict / "mine.md").write_text("user-owned")
        try:
            ul.install_user_layer(ROOT)
        except SystemExit as exc:
            assert "Refusing to overwrite" in str(exc)
        else:
            raise AssertionError("expected conflict refusal")
        assert (conflict / "mine.md").read_text() == "user-owned"


def test_user_layer_respects_claude_config_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cfg:
        monkeypatch.setenv("HOME", home)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", cfg)
        ul.install_user_layer(ROOT)
        cp = Path(cfg)
        assert (cp / ".claude-auto-integration.json").exists()
        assert (cp / "rules" / "claude-auto" / "core.md").exists()
        assert not (Path(home) / ".claude" / ".claude-auto-integration.json").exists()


def test_global_claude_auto_skill_is_user_invoked_only():
    skill = (ROOT / "templates" / "user" / "skills" / "claude-auto" / "SKILL.md").read_text()
    assert "disable-model-invocation: true" in skill
    assert "allowed-tools:" not in skill
    assert "Bash(claude-auto" not in skill


def _fake_claude(path: Path, log: Path) -> None:
    path.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "printf '%s\\n' \"$*\" >> \"${CLAUDE_PLUGIN_LOG:?}\"\n"
        "exit 0\n"
    )
    path.chmod(0o755)


def test_full_installer_with_plugins_installs_core_plugins_user_scope_preserves_settings_and_uninstalls_cleanly():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as install, tempfile.TemporaryDirectory() as repo:
        hp = Path(home)
        bindir = hp / ".local" / "bin"
        bindir.mkdir(parents=True)
        fakebin = hp / "fakebin"
        fakebin.mkdir()
        plugin_log = hp / "claude-plugin.log"
        _fake_claude(fakebin / "claude", plugin_log)

        ch = hp / ".claude"
        ch.mkdir()
        existing_hook = {"matcher": "startup", "hooks": [{"type": "command", "command": "echo keep-me"}]}
        (ch / "settings.json").write_text(json.dumps({"theme": "dark", "hooks": {"SessionStart": [existing_hook]}}))

        rp = Path(repo)
        subprocess.run(["git", "init", "-q", str(rp)], check=True)
        (rp / "README.md").write_text("do not change me\n")
        before = {str(p.relative_to(rp)): p.read_bytes() for p in rp.rglob("*") if p.is_file() and ".git" not in p.parts}

        dest = Path(install) / "pack"
        env = os.environ.copy()
        env.update({
            "HOME": home,
            "CLAUDE_AUTONOMY_HOME": str(dest),
            "CLAUDE_AUTONOMY_BIN": str(bindir),
            "CLAUDE_PLUGIN_LOG": str(plugin_log),
            "PATH": str(fakebin) + os.pathsep + env.get("PATH", ""),
        })
        cp = subprocess.run(["bash", str(ROOT / "install.sh"), "--with-plugins"], cwd=rp, env=env, text=True, capture_output=True, timeout=60)
        assert cp.returncode == 0, cp.stderr

        after = {str(p.relative_to(rp)): p.read_bytes() for p in rp.rglob("*") if p.is_file() and ".git" not in p.parts}
        assert before == after
        settings = json.loads((ch / "settings.json").read_text())
        assert settings["theme"] == "dark"
        blob = json.dumps(settings)
        assert "echo keep-me" in blob
        assert blob.count(ul.HOOK_TAG) == 1
        assert blob.count(ul.PROMPT_HOOK_TAG) == 1
        assert (ch / "skills" / "profile" / "SKILL.md").exists()

        calls = plugin_log.read_text().splitlines()
        assert any("plugin marketplace add anthropics/claude-plugins-official --scope user" in x for x in calls)
        for name in ["claude-code-setup", "context7", "serena"]:
            assert any(f"plugin install {name}@claude-plugins-official --scope user" in x for x in calls)

        # The globally installed status command must work even outside a repository.
        assert (dest / "COMMANDS.md").exists()
        cp = subprocess.run([str(bindir / "claude-auto"), "global", "status"], cwd=home, env=env, text=True, capture_output=True, timeout=20)
        assert cp.returncode == 0, cp.stderr
        status = json.loads(cp.stdout)
        assert status["installed"] and status["hook_present"] and status["owned_files_present"]

        cp = subprocess.run(["bash", str(dest / "uninstall.sh")], cwd=home, env=env, text=True, capture_output=True, timeout=30)
        assert cp.returncode == 0, cp.stderr
        settings = json.loads((ch / "settings.json").read_text())
        assert settings["theme"] == "dark"
        assert "echo keep-me" in json.dumps(settings)
        assert ul.HOOK_TAG not in json.dumps(settings)
        assert ul.PROMPT_HOOK_TAG not in json.dumps(settings)
        assert not (ch / "rules" / "claude-auto").exists()
        assert not (ch / "agents" / "claude-auto").exists()
        assert not (ch / "skills" / "claude-auto").exists()
        assert not (ch / "skills" / "profile").exists()
        assert not (dest / "COMMANDS.md").exists()
        assert before == {str(p.relative_to(rp)): p.read_bytes() for p in rp.rglob("*") if p.is_file() and ".git" not in p.parts}


def test_minimal_install_skips_global_plugins():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as install:
        hp = Path(home)
        bindir = hp / ".local" / "bin"
        bindir.mkdir(parents=True)
        fakebin = hp / "fakebin"
        fakebin.mkdir()
        plugin_log = hp / "claude-plugin.log"
        _fake_claude(fakebin / "claude", plugin_log)
        env = os.environ.copy()
        env.update({
            "HOME": home,
            "CLAUDE_AUTONOMY_HOME": str(Path(install) / "pack"),
            "CLAUDE_AUTONOMY_BIN": str(bindir),
            "CLAUDE_PLUGIN_LOG": str(plugin_log),
            "PATH": str(fakebin) + os.pathsep + env.get("PATH", ""),
        })
        cp = subprocess.run(["bash", str(ROOT / "install.sh"), "--no-plugins"], env=env, text=True, capture_output=True, timeout=30)
        assert cp.returncode == 0, cp.stderr
        assert not plugin_log.exists() or plugin_log.read_text() == ""


def test_uninstall_ignores_tampered_marker_paths_and_preserves_extra_namespace_files(monkeypatch):
    with tempfile.TemporaryDirectory() as home:
        hp = Path(home)
        monkeypatch.setenv("HOME", home)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        ch = hp / ".claude"
        ch.mkdir()
        outside = ch / "agents" / "my-agent.md"
        outside.parent.mkdir(parents=True)
        outside.write_text("keep outside\n")

        ul.install_user_layer(ROOT)
        extra = ch / "agents" / "claude-auto" / "my-extra.md"
        extra.write_text("keep inside namespace\n")

        marker = ch / ul.MARKER_NAME
        obj = json.loads(marker.read_text())
        obj.setdefault("owned_files", []).append(str(outside))
        obj["owned_dirs"] = [str(ch / "agents")]
        marker.write_text(json.dumps(obj))

        result = ul.remove_user_layer()
        assert result["removed"]
        assert outside.read_text() == "keep outside\n"
        assert extra.read_text() == "keep inside namespace\n"
        assert not (ch / "agents" / "claude-auto" / "autonomy-verifier.md").exists()


def test_interrupted_install_marker_is_not_reported_as_installed_and_can_recover(monkeypatch):
    with tempfile.TemporaryDirectory() as home:
        hp = Path(home)
        monkeypatch.setenv("HOME", home)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        ch = hp / ".claude"
        ch.mkdir()

        original = ul.copy_owned_tree
        calls = {"n": 0}
        def fail_once(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated interruption")
            return original(src, dst)
        monkeypatch.setattr(ul, "copy_owned_tree", fail_once)
        try:
            ul.install_user_layer(ROOT)
        except RuntimeError as exc:
            assert "simulated interruption" in str(exc)
        else:
            raise AssertionError("expected simulated interruption")

        marker = ch / ul.MARKER_NAME
        obj = json.loads(marker.read_text())
        assert obj["state"] == "installing"
        assert not ul.status()["installed"]

        monkeypatch.setattr(ul, "copy_owned_tree", original)
        recovered = ul.install_user_layer(ROOT)
        assert recovered["state"] == "installed"
        assert ul.status()["installed"]


def test_user_layer_refuses_foreign_marker(monkeypatch):
    with tempfile.TemporaryDirectory() as home:
        hp = Path(home)
        monkeypatch.setenv("HOME", home)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        ch = hp / ".claude"
        ch.mkdir()
        (ch / ul.MARKER_NAME).write_text(json.dumps({"package_id": "something-else"}))
        try:
            ul.install_user_layer(ROOT)
        except SystemExit as exc:
            assert "foreign" in str(exc).lower()
        else:
            raise AssertionError("expected foreign marker refusal")


def test_user_layer_refuses_malformed_existing_hook_shapes_before_mutation(monkeypatch):
    with tempfile.TemporaryDirectory() as home:
        hp = Path(home)
        monkeypatch.setenv("HOME", home)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        ch = hp / ".claude"
        ch.mkdir()
        (ch / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": "not-a-list"}}))
        try:
            ul.install_user_layer(ROOT)
        except SystemExit as exc:
            assert "malformed SessionStart" in str(exc)
        else:
            raise AssertionError("expected malformed hook refusal")
        assert not (ch / ul.MARKER_NAME).exists()
        assert not (ch / "rules" / "claude-auto").exists()


def test_user_layer_refuses_malformed_user_prompt_hook_before_mutation(monkeypatch):
    with tempfile.TemporaryDirectory() as home:
        hp = Path(home)
        monkeypatch.setenv("HOME", home)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        ch = hp / ".claude"
        ch.mkdir()
        original = {"hooks": {"UserPromptSubmit": "not-a-list"}, "theme": "keep"}
        (ch / "settings.json").write_text(json.dumps(original))
        try:
            ul.install_user_layer(ROOT)
        except SystemExit as exc:
            assert "malformed UserPromptSubmit" in str(exc)
        else:
            raise AssertionError("expected malformed hook refusal")
        assert json.loads((ch / "settings.json").read_text()) == original
        assert not (ch / ul.MARKER_NAME).exists()
        assert not (ch / "skills" / "profile").exists()


def test_user_layer_preserves_unrelated_user_prompt_hooks(monkeypatch):
    with tempfile.TemporaryDirectory() as home:
        hp = Path(home)
        monkeypatch.setenv("HOME", home)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        ch = hp / ".claude"
        ch.mkdir()
        existing = {"hooks": [{"type": "command", "command": "echo user-owned"}]}
        (ch / "settings.json").write_text(json.dumps({
            "hooks": {"UserPromptSubmit": [existing]}
        }))
        ul.install_user_layer(ROOT)
        settings = json.loads((ch / "settings.json").read_text())
        blob = json.dumps(settings)
        assert "echo user-owned" in blob
        assert blob.count(ul.PROMPT_HOOK_TAG) == 1
        ul.remove_user_layer()
        settings = json.loads((ch / "settings.json").read_text())
        blob = json.dumps(settings)
        assert "echo user-owned" in blob
        assert ul.PROMPT_HOOK_TAG not in blob


def test_package_only_then_global_install_remove_control_path():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as install:
        hp = Path(home)
        bindir = hp / ".local" / "bin"
        bindir.mkdir(parents=True)
        dest = Path(install) / "pack"
        env = os.environ.copy()
        env.update({
            "HOME": home,
            "CLAUDE_AUTONOMY_HOME": str(dest),
            "CLAUDE_AUTONOMY_BIN": str(bindir),
        })
        cp = subprocess.run(["bash", str(ROOT / "install.sh"), "--package-only"], env=env, text=True, capture_output=True, timeout=30)
        assert cp.returncode == 0, cp.stderr
        ch = hp / ".claude"
        assert not (ch / ul.MARKER_NAME).exists()

        launcher = bindir / "claude-auto"
        cp = subprocess.run([str(launcher), "global", "install"], env=env, text=True, capture_output=True, timeout=20)
        assert cp.returncode == 0, cp.stderr
        assert (ch / ul.MARKER_NAME).exists()
        assert (ch / "rules" / "claude-auto" / "core.md").exists()

        cp = subprocess.run([str(launcher), "global", "remove"], env=env, text=True, capture_output=True, timeout=20)
        assert cp.returncode == 0, cp.stderr
        assert not (ch / ul.MARKER_NAME).exists()
        assert not (ch / "rules" / "claude-auto").exists()


def test_full_installer_upgrade_is_idempotent_and_preserves_single_hook():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as install:
        hp = Path(home)
        bindir = hp / ".local" / "bin"
        bindir.mkdir(parents=True)
        dest = Path(install) / "pack"
        ch = hp / ".claude"
        ch.mkdir()
        (ch / "settings.json").write_text(json.dumps({"theme": "dark"}))
        env = os.environ.copy()
        env.update({
            "HOME": home,
            "CLAUDE_AUTONOMY_HOME": str(dest),
            "CLAUDE_AUTONOMY_BIN": str(bindir),
        })
        for _ in range(2):
            cp = subprocess.run(["bash", str(ROOT / "install.sh"), "--no-plugins"], env=env, text=True, capture_output=True, timeout=30)
            assert cp.returncode == 0, cp.stderr
        settings = json.loads((ch / "settings.json").read_text())
        assert settings["theme"] == "dark"
        blob = json.dumps(settings)
        assert blob.count(ul.HOOK_TAG) == 1
        assert blob.count(ul.PROMPT_HOOK_TAG) == 1
        assert (ch / "skills" / "profile" / "SKILL.md").exists()
        marker = json.loads((ch / ul.MARKER_NAME).read_text())
        assert marker["state"] == "installed"


def test_default_installer_does_not_install_optional_plugins():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as install, tempfile.TemporaryDirectory() as repo:
        hp=Path(home); bindir=hp/".local"/"bin"; bindir.mkdir(parents=True)
        fakebin=hp/"fakebin"; fakebin.mkdir(); plugin_log=hp/"claude-plugin.log"; _fake_claude(fakebin/"claude",plugin_log)
        rp=Path(repo); subprocess.run(["git","init","-q",str(rp)],check=True); (rp/"README.md").write_text("x\n")
        env=os.environ.copy(); env.update({"HOME":home,"CLAUDE_AUTONOMY_HOME":str(Path(install)/"pack"),"CLAUDE_AUTONOMY_BIN":str(bindir),"CLAUDE_PLUGIN_LOG":str(plugin_log),"PATH":str(fakebin)+os.pathsep+env.get("PATH","")})
        cp=subprocess.run(["bash",str(ROOT/"install.sh")],cwd=rp,env=env,text=True,capture_output=True,timeout=60)
        assert cp.returncode==0, cp.stderr
        assert not plugin_log.exists() or plugin_log.read_text().strip()==""
