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

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from authority_set import AuthoritySetError, build_authority_snapshot
from execution import _bounded_execution_output, _bwrap_base, _sandbox_settings
from cli_schema import build_parser as build_cli_parser
from governance_contract import GovernanceContractError, load_governance_contract
from repo_identity import repo_state_dir
from repo_runtime import activate
from task_sources import TaskSourceError, resolve_task_sources, task_source_status
from task_spec import TaskSpecError, normalise_task_spec, validate_task_graph


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args), cwd=root, text=True, capture_output=True, check=True
    )


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _run(root, "git", "init", "-q")
    _run(root, "git", "config", "user.name", "Test")
    _run(root, "git", "config", "user.email", "test@example.invalid")
    (root / "PLAN.md").write_text("plan\n")
    _run(root, "git", "add", "PLAN.md")
    _run(root, "git", "commit", "-qm", "base")
    return root


def _task(
    task_id: str,
    *,
    depends_on: list[str] | None = None,
    authority_sets: list[str] | None = None,
    owned_paths: list[str] | None = None,
    metadata: dict | None = None,
    commit_subject: str | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "id": task_id,
        "authority_sets": authority_sets or ["default"],
        "depends_on": depends_on or [],
        "owned_paths": owned_paths or [f"src/{task_id}/**"],
        "evidence_paths": [f"evidence/{task_id}/**"],
        "runtime_scratch_paths": [f".cache/{task_id}/**"],
        "verification": ["test"],
        "commit_subject": commit_subject,
        "metadata": metadata or {},
    }


def _authority_set(set_id: str = "default") -> dict:
    return {
        "id": set_id,
        "members": [
            {"path": "PLAN.md", "role": "source", "repair": "repairable", "required": True}
        ],
        "validators": [],
        "reconcilers": [],
    }


def _contract(sources: list[dict], *, strict: bool = True, sets: list[dict] | None = None) -> dict:
    return {
        "schema_version": 1,
        "planning_authority": {"sets": sets or [_authority_set()]},
        "tasks": {
            "sources": sources,
            "execution_mode": "single-writer",
            "strict_dependencies": strict,
        },
        "control_surfaces": [],
    }


def _write_governance(root: Path, value: dict, message: str = "governance") -> None:
    path = root / ".claude-auto" / "governance.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")
    _run(root, "git", "add", ".claude-auto/governance.json")
    _run(root, "git", "commit", "-qm", message)


def _commit_file(root: Path, path: str, content: str, message: str = "source") -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _run(root, "git", "add", path)
    _run(root, "git", "commit", "-qm", message)


def _json_source(source_id: str, path: str) -> dict:
    return {
        "id": source_id,
        "kind": "json",
        "authority_sets": ["default"],
        "paths": [path],
    }


def test_builtin_json_jsonl_toml_and_static_sources_merge(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        _commit_file(
            root,
            "tasks/a.json",
            json.dumps({"schema_version": 1, "tasks": [_task("T1")]}),
            "json tasks",
        )
        _commit_file(root, "tasks/b.jsonl", json.dumps(_task("T2", depends_on=["T1"])) + "\n", "jsonl tasks")
        toml = """schema_version = 1

[[tasks]]
schema_version = 1
id = "T3"
authority_sets = ["default"]
depends_on = ["T2"]
owned_paths = ["src/T3/**"]
evidence_paths = ["evidence/T3/**"]
runtime_scratch_paths = [".cache/T3/**"]
verification = ["test"]
commit_subject = ""
metadata = {}
"""
        _commit_file(root, "tasks/c.toml", toml, "toml tasks")
        sources = [
            _json_source("json", "tasks/a.json"),
            {"id": "jsonl", "kind": "jsonl", "authority_sets": ["default"], "paths": ["tasks/b.jsonl"]},
            {"id": "toml", "kind": "toml", "authority_sets": ["default"], "paths": ["tasks/c.toml"]},
            {"id": "static", "kind": "static", "authority_sets": ["default"], "tasks": [_task("T4", depends_on=["T3"])]},
        ]
        _write_governance(root, _contract(sources))
        result = resolve_task_sources(root, persist=False)
        assert result["status"] == "READY"
        assert [row["task"]["id"] for row in result["tasks"]] == ["T1", "T2", "T3", "T4"]
        assert result["external_dependencies"] == []


def test_source_order_does_not_change_merged_task_digest(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        _commit_file(root, "tasks/a.json", json.dumps([_task("A")]), "a")
        _commit_file(root, "tasks/b.json", json.dumps([_task("B")]), "b")
        a = _json_source("a", "tasks/a.json")
        b = _json_source("b", "tasks/b.json")
        _write_governance(root, _contract([a, b]), "first order")
        first = resolve_task_sources(root, persist=False)
        _write_governance(root, _contract([b, a]), "second order")
        second = resolve_task_sources(root, persist=False)
        assert first["merged_tasks_sha256"] == second["merged_tasks_sha256"]


def test_duplicate_source_id_and_unknown_authority_set_fail_contract(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        value = _contract([
            {"id": "same", "kind": "static", "authority_sets": ["default"], "tasks": []},
            {"id": "same", "kind": "static", "authority_sets": ["default"], "tasks": []},
        ])
        _write_governance(root, value)
        with pytest.raises(GovernanceContractError, match="duplicate task-source id"):
            load_governance_contract(root)

    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        _write_governance(root, _contract([
            {"id": "x", "kind": "static", "authority_sets": ["missing"], "tasks": []}
        ]))
        with pytest.raises(GovernanceContractError, match="unknown AuthoritySet"):
            load_governance_contract(root)


def test_duplicate_task_id_across_sources_fails_closed(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        _write_governance(root, _contract([
            {"id": "a", "kind": "static", "authority_sets": ["default"], "tasks": [_task("T1")]},
            {"id": "b", "kind": "static", "authority_sets": ["default"], "tasks": [_task("T1")]},
        ]))
        with pytest.raises(TaskSourceError, match="duplicate TaskSpec"):
            resolve_task_sources(root, persist=False)


def test_missing_dependency_strict_and_non_strict(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        source = {"id": "a", "kind": "static", "authority_sets": ["default"], "tasks": [_task("T1", depends_on=["EXT"])]}
        _write_governance(root, _contract([source], strict=True), "strict")
        with pytest.raises(TaskSourceError, match="missing task EXT"):
            resolve_task_sources(root, persist=False)
        _write_governance(root, _contract([source], strict=False), "non-strict")
        result = resolve_task_sources(root, persist=False)
        assert result["external_dependencies"] == ["EXT"]


def test_task_authority_ceiling_and_protected_paths_fail(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        sets = [_authority_set("default"), {
            "id": "other",
            "members": [],
            "validators": [],
            "reconcilers": [],
        }]
        source = {
            "id": "a", "kind": "static", "authority_sets": ["default"],
            "tasks": [_task("T1", authority_sets=["other"])],
        }
        _write_governance(root, _contract([source], sets=sets))
        with pytest.raises(TaskSourceError, match="AuthoritySet ceiling"):
            resolve_task_sources(root, persist=False)

    with pytest.raises(TaskSpecError, match="protected authority/control"):
        normalise_task_spec(
            _task("T2", owned_paths=["**"]),
            source_id="s",
            source_authority_sets={"default"},
            known_authority_sets={"default"},
            protected_paths={"PLAN.md", ".claude-auto/governance.json"},
        )


def test_task_digest_excludes_metadata_and_commit_subject():
    base = _task("T1", metadata={"note": "one"}, commit_subject="one")
    changed = _task("T1", metadata={"note": "two"}, commit_subject="two")
    a = normalise_task_spec(
        base,
        source_id="s",
        source_authority_sets={"default"},
        known_authority_sets={"default"},
        protected_paths={"PLAN.md"},
    )
    b = normalise_task_spec(
        changed,
        source_id="s",
        source_authority_sets={"default"},
        known_authority_sets={"default"},
        protected_paths={"PLAN.md"},
    )
    assert a["task_spec_sha256"] == b["task_spec_sha256"]
    assert a["record_sha256"] != b["record_sha256"]

    changed_authority = _task("T1", owned_paths=["src/changed/**"])
    c = normalise_task_spec(
        changed_authority,
        source_id="s",
        source_authority_sets={"default"},
        known_authority_sets={"default"},
        protected_paths={"PLAN.md"},
    )
    assert a["task_spec_sha256"] != c["task_spec_sha256"]


def test_deep_task_graph_cycle_is_iterative():
    records = []
    for i in range(5000):
        task_id = f"T{i:05d}"
        deps = [] if i == 0 else [f"T{i-1:05d}"]
        records.append({"task": {"id": task_id, "depends_on": deps}})
    assert validate_task_graph(records, strict_dependencies=True) == []
    records[0]["task"]["depends_on"] = ["T04999"]
    with pytest.raises(TaskSpecError, match="dependency cycle"):
        validate_task_graph(records, strict_dependencies=True)


def test_source_wip_is_control_surface_and_cannot_change_resolution(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        _commit_file(root, "tasks/a.json", json.dumps([_task("T1")]), "task source")
        _write_governance(root, _contract([_json_source("a", "tasks/a.json")]))
        committed = resolve_task_sources(root, persist=False)
        (root / "tasks/a.json").write_text(json.dumps([_task("T2")]))
        with pytest.raises(TaskSourceError, match="working tree differs"):
            resolve_task_sources(root, persist=False)
        assert committed["tasks"][0]["task"]["id"] == "T1"


def _adapter_source(*, capabilities: dict | None = None) -> dict:
    return {
        "id": "adapter",
        "kind": "adapter",
        "authority_sets": ["default"],
        "argv": ["python3", "tools/export.py"],
        "cwd": ".",
        "inputs": ["tools/export.py", "data/input.txt"],
        "timeout_seconds": 30,
        "capabilities": capabilities or {"network": [], "read_external": []},
    }


def _prepare_adapter_repo(root: Path) -> None:
    _commit_file(root, "tools/export.py", "print('placeholder')\n", "adapter code")
    _commit_file(root, "data/input.txt", "committed\n", "adapter input")


def test_adapter_runs_twice_on_materialised_view_and_hides_live_repo(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "live")
        _prepare_adapter_repo(root)
        _write_governance(root, _contract([_adapter_source()]))
        calls = []

        def fake_runner(view, argv, **kwargs):
            calls.append((view, argv, kwargs))
            assert kwargs["read_only_root"] is True
            assert kwargs["read_allowlist_only"] is True
            assert root in kwargs["hidden_paths"]
            assert (view / "data/input.txt").read_text() == "committed\n"
            return {
                "returncode": 0,
                "stdout": json.dumps([_task("T1")]),
                "stderr": "",
                "execution_boundary": "test-sandbox",
                "sandboxed": True,
                "environment_scrubbed": True,
            }

        result = resolve_task_sources(root, runner=fake_runner, persist=False)
        assert result["status"] == "READY"
        assert len(calls) == 2
        assert calls[0][0] != root


def test_adapter_input_mutation_and_nondeterminism_fail(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "live")
        _prepare_adapter_repo(root)
        _write_governance(root, _contract([_adapter_source()]))

        def mutator(view, argv, **kwargs):
            (view / "data/input.txt").write_text("tampered\n")
            return {
                "returncode": 0,
                "stdout": json.dumps([_task("T1")]),
                "stderr": "",
                "execution_boundary": "test-sandbox",
                "sandboxed": True,
                "environment_scrubbed": True,
            }

        with pytest.raises(TaskSourceError, match="modified its exact-commit input view"):
            resolve_task_sources(root, runner=mutator, persist=False)

        count = {"n": 0}
        def nondeterministic(view, argv, **kwargs):
            count["n"] += 1
            task_id = "T1" if count["n"] == 1 else "T2"
            return {
                "returncode": 0,
                "stdout": json.dumps([_task(task_id)]),
                "stderr": "",
                "execution_boundary": "test-sandbox",
                "sandboxed": True,
                "environment_scrubbed": True,
            }

        with pytest.raises(TaskSourceError, match="nondeterministic"):
            resolve_task_sources(root, runner=nondeterministic, persist=False)


def test_adapter_cannot_request_network_or_external_read(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "live")
        _prepare_adapter_repo(root)
        source = _adapter_source(capabilities={"network": ["example.com"], "read_external": []})
        _write_governance(root, _contract([source]))
        with pytest.raises(TaskSourceError, match="does not grant"):
            resolve_task_sources(root, runner=lambda *_a, **_k: {}, persist=False)


def test_adapter_nonzero_noisy_and_unsandboxed_outputs_fail(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "live")
        _prepare_adapter_repo(root)
        _write_governance(root, _contract([_adapter_source()]))

        def failed(view, argv, **kwargs):
            return {"returncode": 7, "stdout": "", "stderr": "boom", "execution_boundary": "test", "sandboxed": True}
        with pytest.raises(TaskSourceError, match="exit 7"):
            resolve_task_sources(root, runner=failed, persist=False)

        def noisy(view, argv, **kwargs):
            return {
                "returncode": 0,
                "stdout": "hello\n" + json.dumps([_task("T1")]),
                "stderr": "",
                "execution_boundary": "test",
                "sandboxed": True,
                "environment_scrubbed": True,
            }
        with pytest.raises(TaskSourceError, match="invalid JSON"):
            resolve_task_sources(root, runner=noisy, persist=False)

        def unsandboxed(view, argv, **kwargs):
            return {
                "returncode": 0,
                "stdout": json.dumps([_task("T1")]),
                "stderr": "",
                "execution_boundary": "host",
                "sandboxed": False,
                "environment_scrubbed": True,
            }
        with pytest.raises(TaskSourceError, match="verified isolation"):
            resolve_task_sources(root, runner=unsandboxed, persist=False)


def test_adapter_dirty_input_blocks_before_execution(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "live")
        _prepare_adapter_repo(root)
        _write_governance(root, _contract([_adapter_source()]))
        (root / "data/input.txt").write_text("working tree\n")
        with pytest.raises(TaskSourceError, match="working tree differs"):
            resolve_task_sources(root, runner=lambda *_a, **_k: {}, persist=False)


def test_task_status_does_not_execute_adapter_and_resolve_does_not_activate_task(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "live")
        _prepare_adapter_repo(root)
        _write_governance(root, _contract([_adapter_source()]))
        status = task_source_status(root)
        assert status["status"] == "UNRESOLVED"

        calls = {"n": 0}
        def runner(view, argv, **kwargs):
            calls["n"] += 1
            return {
                "returncode": 0,
                "stdout": json.dumps([_task("T1")]),
                "stderr": "",
                "execution_boundary": "test-sandbox",
                "sandboxed": True,
                "environment_scrubbed": True,
            }

        result = resolve_task_sources(root, runner=runner, persist=True)
        assert result["status"] == "READY"
        assert calls["n"] == 2
        durable = json.loads((repo_state_dir(root) / "state.json").read_text())
        assert durable["task_source_sha256"] == result["task_source_set_sha256"]
        assert durable["active_task_id"] is None
        assert durable["active_task_spec_sha256"] is None
        assert durable["active_execution_envelope_sha256"] is None


def test_read_only_execution_recipe_masks_live_repo_and_mounts_view_read_only(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        view = base / "view"
        live = base / "live"
        view.mkdir()
        live.mkdir()
        monkeypatch.setattr("execution.shutil.which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
        args = _bwrap_base(
            view,
            read_only_root=True,
            cwd=view,
            hidden_paths=[live],
            read_allowlist_only=True,
        )
        assert args is not None
        joined = "\n".join(args)
        assert f"--ro-bind\n{view}\n{view}" in joined
        # Both paths live below /tmp in this fixture. Bubblewrap masks /tmp as a
        # whole, then recreates/binds only the adapter view, so the live sibling
        # is hidden without needing a redundant nested tmpfs mount.
        assert "--tmpfs\n/tmp" in joined
        for external_root in ("/etc", "/var", "/opt"):
            if Path(external_root).exists():
                assert f"--tmpfs\n{external_root}" in joined
        assert f"--bind\n{live}\n{live}" not in joined
        assert f"--ro-bind\n{live}\n{live}" not in joined
        settings = _sandbox_settings(
            view,
            base / "home",
            read_only_root=True,
            hidden_paths=[live],
            read_allowlist_only=True,
        )
        assert "/" in settings["filesystem"]["denyRead"]
        assert str(view) in settings["filesystem"]["allowRead"]
        assert str(view) in settings["filesystem"]["denyWrite"]
        assert str(live) in settings["filesystem"]["denyRead"]
        assert settings["network"]["allowedDomains"] == []
        assert settings["network"]["allowLocalBinding"] is False

        allowed_top = {"usr", "bin", "lib", "lib64", "sbin", "proc", "dev"}
        for candidate in Path("/").iterdir():
            if candidate.name in allowed_top or candidate.is_symlink():
                continue
            if candidate.is_dir():
                assert f"--tmpfs\n{candidate}" in joined
            elif candidate.is_file():
                assert f"--ro-bind\n/dev/null\n{candidate}" in joined


def test_malformed_builtin_sources_fail_closed(monkeypatch):
    fixtures = [
        ("json", '{"schema_version":1,"tasks":[],"tasks":[]}', "duplicate JSON key"),
        ("jsonl", '[]\n', "must be one TaskSpec object"),
        ("toml", 'schema_version = 1\ntasks = [', "invalid TOML"),
    ]
    for kind, payload, message in fixtures:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
            monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
            root = _repo(Path(td) / "repo")
            path = f"tasks/source.{kind}"
            _commit_file(root, path, payload, f"{kind} source")
            source = {"id": "s", "kind": kind, "authority_sets": ["default"], "paths": [path]}
            _write_governance(root, _contract([source]))
            with pytest.raises(TaskSourceError, match=message):
                resolve_task_sources(root, persist=False)


def test_missing_and_duplicate_source_selector_resolution_fail(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        _write_governance(root, _contract([_json_source("s", "tasks/missing*.json")]))
        with pytest.raises(TaskSourceError, match="zero tracked files"):
            resolve_task_sources(root, persist=False)

    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        _commit_file(root, "tasks/a.json", json.dumps([_task("T1")]), "source")
        source = {
            "id": "s",
            "kind": "json",
            "authority_sets": ["default"],
            "paths": ["tasks/*.json", "tasks/a.json"],
        }
        _write_governance(root, _contract([source]))
        with pytest.raises(TaskSourceError, match="same file more than once"):
            resolve_task_sources(root, persist=False)


def test_source_symlink_and_gitlink_are_rejected(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        _commit_file(root, "tasks/real.json", json.dumps([_task("T1")]), "real")
        (root / "tasks/link.json").symlink_to("real.json")
        _run(root, "git", "add", "tasks/link.json")
        _run(root, "git", "commit", "-qm", "symlink")
        _write_governance(root, _contract([_json_source("s", "tasks/link.json")]))
        with pytest.raises(TaskSourceError, match="symlink"):
            resolve_task_sources(root, persist=False)

    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        nested = root / "nested"
        nested.mkdir()
        _run(nested, "git", "init", "-q")
        _run(nested, "git", "config", "user.name", "Nested")
        _run(nested, "git", "config", "user.email", "nested@example.invalid")
        (nested / "task.json").write_text(json.dumps([_task("T1")]))
        _run(nested, "git", "add", "task.json")
        _run(nested, "git", "commit", "-qm", "nested")
        nested_head = _run(nested, "git", "rev-parse", "HEAD").stdout.strip()
        _run(root, "git", "update-index", "--add", "--cacheinfo", f"160000,{nested_head},nested")
        _run(root, "git", "commit", "-qm", "gitlink")
        _write_governance(root, _contract([_json_source("s", "nested")]))
        with pytest.raises(TaskSourceError, match="gitlink"):
            resolve_task_sources(root, persist=False)


@pytest.mark.parametrize(
    ("task", "message"),
    [
        (_task("bad id"), "must match"),
        (_task("T1", owned_paths=["../escape"]), "must not contain '..'"),
        (_task("T1", owned_paths=["src/**", "src/**"]), "duplicate entry"),
        (_task("T1", depends_on=["T1"]), "cannot depend on itself"),
    ],
)
def test_malformed_taskspec_fields_fail(task, message):
    with pytest.raises(TaskSpecError, match=message):
        normalise_task_spec(
            task,
            source_id="s",
            source_authority_sets={"default"},
            known_authority_sets={"default"},
            protected_paths={"PLAN.md"},
        )


def test_adapter_output_cannot_expand_authority_or_duplicate_ids(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "live")
        _prepare_adapter_repo(root)
        other = {"id": "other", "members": [], "validators": [], "reconcilers": []}
        _write_governance(root, _contract([_adapter_source()], sets=[_authority_set(), other]))

        def escalator(view, argv, **kwargs):
            return {
                "returncode": 0,
                "stdout": json.dumps([_task("T1", authority_sets=["other"])]),
                "stderr": "",
                "execution_boundary": "test-sandbox",
                "sandboxed": True,
                "environment_scrubbed": True,
            }

        with pytest.raises(TaskSourceError, match="AuthoritySet ceiling"):
            resolve_task_sources(root, runner=escalator, persist=False)

        def duplicate(view, argv, **kwargs):
            return {
                "returncode": 0,
                "stdout": json.dumps([_task("T1"), _task("T1")]),
                "stderr": "",
                "execution_boundary": "test-sandbox",
                "sandboxed": True,
                "environment_scrubbed": True,
            }

        with pytest.raises(TaskSourceError, match="duplicate TaskSpec"):
            resolve_task_sources(root, runner=duplicate, persist=False)


def test_adapter_timeout_and_oversized_output_fail(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "live")
        _prepare_adapter_repo(root)
        _write_governance(root, _contract([_adapter_source()]))

        def timeout(view, argv, **kwargs):
            return {
                "returncode": 124,
                "stdout": "",
                "stderr": "timed out",
                "timed_out": True,
                "execution_boundary": "test-sandbox",
                "sandboxed": True,
                "environment_scrubbed": True,
            }
        with pytest.raises(TaskSourceError, match="exit 124"):
            resolve_task_sources(root, runner=timeout, persist=False)

        huge = "x" * (8 * 1024 * 1024 + 1)
        def oversized(view, argv, **kwargs):
            return {
                "returncode": 0,
                "stdout": huge,
                "stderr": "",
                "execution_boundary": "test-sandbox",
                "sandboxed": True,
                "environment_scrubbed": True,
            }
        with pytest.raises(TaskSourceError, match="maximum supported size"):
            resolve_task_sources(root, runner=oversized, persist=False)


def test_execution_output_cap_turns_oversize_into_fail_closed_result():
    result = {
        "returncode": 0,
        "stdout": "a" * 100,
        "stderr": "b" * 100,
        "execution_boundary": "test",
    }
    bounded = _bounded_execution_output(result, 128)
    assert bounded["returncode"] == 125
    assert bounded["output_limit_exceeded"] is True
    assert "OUTPUT_LIMIT_EXCEEDED" in bounded["stderr"]


def test_task_source_contract_change_changes_set_digest(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        _commit_file(root, "tasks/a.json", json.dumps([_task("T1")]), "source")
        source = _json_source("a", "tasks/a.json")
        _write_governance(root, _contract([source], strict=True), "strict")
        first = resolve_task_sources(root, persist=False)
        _write_governance(root, _contract([source], strict=False), "non-strict")
        second = resolve_task_sources(root, persist=False)
        assert first["merged_tasks_sha256"] == second["merged_tasks_sha256"]
        assert first["task_source_set_sha256"] != second["task_source_set_sha256"]


def test_committed_source_blob_change_changes_task_source_set(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        _commit_file(root, "tasks/a.json", json.dumps([_task("T1")]), "source one")
        source = _json_source("a", "tasks/a.json")
        _write_governance(root, _contract([source]))
        first = resolve_task_sources(root, persist=False)
        (root / "tasks/a.json").write_text(json.dumps([_task("T2")]))
        _run(root, "git", "add", "tasks/a.json")
        _run(root, "git", "commit", "-qm", "source two")
        second = resolve_task_sources(root, persist=False)
        assert first["task_source_set_sha256"] != second["task_source_set_sha256"]
        assert first["merged_tasks_sha256"] != second["merged_tasks_sha256"]


def test_adapter_runtime_boundary_does_not_change_authority_digest(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "live")
        _prepare_adapter_repo(root)
        _write_governance(root, _contract([_adapter_source()]))

        def make_runner(boundary):
            def runner(view, argv, **kwargs):
                return {
                    "returncode": 0,
                    "stdout": json.dumps([_task("T1")]),
                    "stderr": "",
                    "execution_boundary": boundary,
                    "sandboxed": True,
                    "environment_scrubbed": True,
                }
            return runner

        first = resolve_task_sources(root, runner=make_runner("srt"), persist=False)
        second = resolve_task_sources(root, runner=make_runner("bubblewrap"), persist=False)
        assert first["task_source_set_sha256"] == second["task_source_set_sha256"]
        assert first["adapter_runtime_evidence"] != second["adapter_runtime_evidence"]


def test_persisted_status_becomes_stale_after_committed_source_change(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        _commit_file(root, "tasks/a.json", json.dumps([_task("T1")]), "source")
        _write_governance(root, _contract([_json_source("a", "tasks/a.json")]))
        resolved = resolve_task_sources(root, persist=True)
        assert task_source_status(root)["status"] == "READY"

        (root / "tasks/a.json").write_text(json.dumps([_task("T2")]))
        _run(root, "git", "add", "tasks/a.json")
        _run(root, "git", "commit", "-qm", "new task source")
        stale = task_source_status(root)
        assert stale["status"] == "STALE"
        assert stale["task_source_set_sha256"] == resolved["task_source_set_sha256"]

        activate(root)
        durable = json.loads((repo_state_dir(root) / "state.json").read_text())
        assert durable["task_source_sha256"] is None


def test_tasks_cli_surface_parses_status_and_resolve():
    status = build_cli_parser("test").parse_args(["tasks", "status", "--repo", "/tmp/example"])
    assert status.command == "tasks"
    assert status.tasks_command == "status"
    resolve = build_cli_parser("test").parse_args(["tasks", "resolve", "--repo", "/tmp/example"])
    assert resolve.command == "tasks"
    assert resolve.tasks_command == "resolve"


def test_adapter_output_order_normalises_deterministically(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "live")
        _prepare_adapter_repo(root)
        _write_governance(root, _contract([_adapter_source()]))
        count = {"n": 0}

        def reordered(view, argv, **kwargs):
            count["n"] += 1
            rows = [_task("A"), _task("B")]
            if count["n"] % 2 == 0:
                rows.reverse()
            return {
                "returncode": 0,
                "stdout": json.dumps(rows),
                "stderr": "",
                "execution_boundary": "test-sandbox",
                "sandboxed": True,
                "environment_scrubbed": True,
            }

        result = resolve_task_sources(root, runner=reordered, persist=False)
        assert [item["task"]["id"] for item in result["tasks"]] == ["A", "B"]


def test_repository_change_during_adapter_resolution_invalidates_result(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "live")
        _prepare_adapter_repo(root)
        _write_governance(root, _contract([_adapter_source()]))
        calls = {"n": 0}

        def runner(view, argv, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                (root / "README.md").write_text("concurrent change\n")
                _run(root, "git", "add", "README.md")
                _run(root, "git", "commit", "-qm", "concurrent advance")
            return {
                "returncode": 0,
                "stdout": json.dumps([_task("T1")]),
                "stderr": "",
                "execution_boundary": "test-sandbox",
                "sandboxed": True,
                "environment_scrubbed": True,
            }

        with pytest.raises(TaskSourceError, match="authority changed during task resolution"):
            resolve_task_sources(root, runner=runner, persist=False)


def test_nonfinite_metadata_and_unicode_equivalent_verification_are_rejected():
    bad = _task("T1", metadata={"value": float("nan")})
    with pytest.raises(TaskSpecError, match="JSON-compatible"):
        normalise_task_spec(
            bad,
            source_id="s",
            source_authority_sets={"default"},
            known_authority_sets={"default"},
            protected_paths={"PLAN.md"},
        )

    duplicate_unicode = _task("T2")
    duplicate_unicode["verification"] = ["caf\u00e9", "cafe\u0301"]
    with pytest.raises(TaskSpecError, match="duplicate entry"):
        normalise_task_spec(
            duplicate_unicode,
            source_id="s",
            source_authority_sets={"default"},
            known_authority_sets={"default"},
            protected_paths={"PLAN.md"},
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda t: t.update({"authority_sets": []}), "must not be empty"),
        (lambda t: t.update({"depends_on": ["D1", "D1"]}), "duplicate entry"),
        (lambda t: t.update({"owned_paths": ["/absolute/path"]}), "must be relative"),
        (lambda t: t.update({"owned_paths": ["PLAN.md"]}), "protected authority/control"),
        (lambda t: t.update({"owned_paths": [".claude-auto/governance.json"]}), "protected authority/control"),
        (lambda t: t.update({"runtime_scratch_paths": [".git/**"]}), "protected authority/control"),
    ],
)
def test_additional_taskspec_authority_negative_paths(mutate, message):
    task = _task("TNEG")
    mutate(task)
    with pytest.raises(TaskSpecError, match=message):
        normalise_task_spec(
            task,
            source_id="s",
            source_authority_sets={"default"},
            known_authority_sets={"default"},
            protected_paths={"PLAN.md", ".claude-auto/governance.json"},
        )


def test_adapter_external_read_capability_is_not_granted(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "live")
        _prepare_adapter_repo(root)
        source = _adapter_source(capabilities={"network": [], "read_external": ["/etc"]})
        _write_governance(root, _contract([source]))
        with pytest.raises(TaskSourceError, match="does not grant"):
            resolve_task_sources(root, runner=lambda *_a, **_k: {}, persist=False)


def test_task_resolution_does_not_mutate_target_repository(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        _commit_file(root, "tasks/a.json", json.dumps([_task("T1")]), "source")
        _write_governance(root, _contract([_json_source("a", "tasks/a.json")]))
        before = _run(root, "git", "status", "--porcelain=v1").stdout
        resolve_task_sources(root, persist=True)
        after = _run(root, "git", "status", "--porcelain=v1").stdout
        assert before == after == ""


@pytest.mark.skipif(
    shutil.which("srt") is None and shutil.which("bwrap") is None,
    reason="real adapter isolation boundary is not installed",
)
def test_real_adapter_boundary_blocks_live_repo_external_reads_writes_and_network(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "live")
        live_plan = str((root / "PLAN.md").resolve())
        task_json = json.dumps([_task("TREAL")])
        script = f"""import json
import pathlib
import socket

assert pathlib.Path("data/input.txt").read_text() == "committed\\n"

try:
    pathlib.Path("data/input.txt").write_text("tampered\\n")
except OSError:
    pass
else:
    raise SystemExit(41)

try:
    pathlib.Path("/etc/passwd").read_text()
except OSError:
    pass
else:
    raise SystemExit(42)

if pathlib.Path({live_plan!r}).exists():
    raise SystemExit(43)

try:
    sock = socket.create_connection(("1.1.1.1", 53), timeout=0.2)
except OSError:
    pass
else:
    sock.close()
    raise SystemExit(44)

print({task_json!r})
"""
        _commit_file(root, "tools/export.py", script, "real adapter")
        _commit_file(root, "data/input.txt", "committed\n", "real adapter input")
        _write_governance(root, _contract([_adapter_source()]))
        result = resolve_task_sources(root, persist=False)
        assert result["status"] == "READY"
        assert [row["task"]["id"] for row in result["tasks"]] == ["TREAL"]
        assert result["adapter_runtime_evidence"]
        assert all(
            row["sha256"] == result["sources"][0]["normalised_output_sha256"]
            for row in result["adapter_runtime_evidence"][0]["determinism_runs"]
        )
