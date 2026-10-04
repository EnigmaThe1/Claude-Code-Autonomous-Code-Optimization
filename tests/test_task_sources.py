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
import subprocess
import tempfile
from pathlib import Path

import pytest

from authority_set import AuthoritySetError, build_authority_snapshot
from execution import _bwrap_base, _sandbox_settings
from governance_contract import GovernanceContractError, load_governance_contract
from repo_identity import repo_state_dir
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
        args = _bwrap_base(view, read_only_root=True, cwd=view, hidden_paths=[live])
        assert args is not None
        joined = "\n".join(args)
        assert f"--ro-bind\n{view}\n{view}" in joined
        assert f"--tmpfs\n{live}" in joined
        settings = _sandbox_settings(view, base / "home", read_only_root=True, hidden_paths=[live])
        assert str(view) in settings["filesystem"]["denyWrite"]
        assert str(live) in settings["filesystem"]["denyRead"]
