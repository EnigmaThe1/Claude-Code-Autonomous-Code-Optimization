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

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from authority_set import build_authority_snapshot
from task_authority import ready_frontier
from task_sources import resolve_task_sources
from task_spec import normalise_task_spec, selector_matches_path
from workspace_recovery import promote_fast_forward


def _run(
    root: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=root,
        text=True,
        capture_output=True,
        check=check,
    )


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _run(root, "git", "init", "-q", "-b", "main")
    _run(root, "git", "config", "user.name", "P7 Test")
    _run(root, "git", "config", "user.email", "p7@example.invalid")
    (root / "README.md").write_text("p7 fixture\n")
    _run(root, "git", "add", "README.md")
    _run(root, "git", "commit", "-qm", "base")
    return root


def _task(
    task_id: str,
    *,
    authority_sets: list[str],
    owned_paths: list[str],
    depends_on: list[str] | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "id": task_id,
        "authority_sets": authority_sets,
        "depends_on": depends_on or [],
        "owned_paths": owned_paths,
        "evidence_paths": [f"evidence/{task_id}/**"],
        "runtime_scratch_paths": [f".cache/{task_id}/**"],
        "verification": ["qualification"],
        "commit_subject": None,
        "metadata": {},
    }


def _member(path: str) -> dict:
    return {
        "path": path,
        "role": "source",
        "repair": "repairable",
        "required": True,
    }


def _write_governance(root: Path, contract: dict) -> None:
    path = root / ".claude-auto" / "governance.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract, indent=2) + "\n")
    _run(root, "git", "add", "-A")
    _run(root, "git", "commit", "-qm", "governance and task ledgers")


def test_p7_1000_task_multiledger_multidomain_stress_is_deterministic(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        sets: list[dict] = []
        sources: list[dict] = []
        total = 1000
        per_ledger = 100

        for domain in range(10):
            set_id = f"domain-{domain:02d}"
            plan = f"plans/{set_id}.md"
            plan_path = root / plan
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(f"# {set_id}\n")
            sets.append({
                "id": set_id,
                "members": [_member(plan)],
                "validators": [],
                "reconcilers": [],
            })

            tasks = []
            start = domain * per_ledger
            end = start + per_ledger
            for index in range(start, end):
                task_id = f"T{index:04d}"
                dependency = [f"T{index - 1:04d}"] if index else []
                tasks.append(
                    _task(
                        task_id,
                        authority_sets=[set_id],
                        owned_paths=[f"packages/{set_id}/{task_id}/**"],
                        depends_on=dependency,
                    )
                )
            ledger = f"tasks/{set_id}.json"
            ledger_path = root / ledger
            ledger_path.parent.mkdir(parents=True, exist_ok=True)
            ledger_path.write_text(
                json.dumps({"schema_version": 1, "tasks": tasks})
            )
            sources.append({
                "id": f"ledger-{domain:02d}",
                "kind": "json",
                "authority_sets": [set_id],
                "paths": [ledger],
            })

        contract = {
            "schema_version": 1,
            "planning_authority": {"sets": sets},
            "tasks": {
                "sources": sources,
                "execution_mode": "single-writer",
                "strict_dependencies": True,
            },
            "control_surfaces": [],
        }
        _write_governance(root, contract)

        first = resolve_task_sources(root, persist=False)
        second = resolve_task_sources(root, persist=False)
        assert first["status"] == "READY"
        assert len(first["tasks"]) == total
        assert first["external_dependencies"] == []
        assert first["merged_tasks_sha256"] == second["merged_tasks_sha256"]
        assert first["task_source_set_sha256"] == second["task_source_set_sha256"]
        assert first["tasks"][0]["task"]["id"] == "T0000"
        assert first["tasks"][-1]["task"]["id"] == "T0999"
        assert first["tasks"][100]["task"]["depends_on"] == ["T0099"]


def test_p7_100000_path_selector_stress_is_deterministic():
    paths = [
        f"packages/p{index % 250:03d}/src/module-{index:06d}.py"
        for index in range(100_000)
    ]
    selector = "packages/*/src/**"

    def digest_matches() -> tuple[int, str]:
        digest = hashlib.sha256()
        count = 0
        for path in paths:
            if selector_matches_path(selector, path):
                count += 1
                digest.update(path.encode("utf-8"))
                digest.update(b"\0")
        return count, digest.hexdigest()

    first = digest_matches()
    second = digest_matches()
    assert first == second
    assert first[0] == 100_000


def test_p7_cross_domain_cross_package_task_is_explicit_authority(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        for name in ("a", "b"):
            path = root / "plans" / f"{name}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{name}\n")

        task = _task(
            "CROSS",
            authority_sets=["domain-a", "domain-b"],
            owned_paths=["packages/a/**", "packages/b/**"],
        )
        contract = {
            "schema_version": 1,
            "planning_authority": {
                "sets": [
                    {
                        "id": "domain-a",
                        "members": [_member("plans/a.md")],
                        "validators": [],
                        "reconcilers": [],
                    },
                    {
                        "id": "domain-b",
                        "members": [_member("plans/b.md")],
                        "validators": [],
                        "reconcilers": [],
                    },
                ]
            },
            "tasks": {
                "sources": [{
                    "id": "cross-source",
                    "kind": "static",
                    "authority_sets": ["domain-a", "domain-b"],
                    "tasks": [task],
                }],
                "execution_mode": "single-writer",
                "strict_dependencies": True,
            },
            "control_surfaces": [],
        }
        _write_governance(root, contract)
        result = resolve_task_sources(root, persist=False)
        assert result["status"] == "READY"
        resolved = result["tasks"][0]["task"]
        assert resolved["authority_sets"] == ["domain-a", "domain-b"]
        assert resolved["owned_paths"] == ["packages/a/**", "packages/b/**"]


def test_p7_task_with_no_owned_paths_is_valid_but_cannot_self_expand():
    raw = _task(
        "NOOWN",
        authority_sets=["default"],
        owned_paths=[],
    )
    normalised = normalise_task_spec(
        raw,
        source_id="fixture",
        source_authority_sets={"default"},
        known_authority_sets={"default"},
        protected_paths={"PLAN.md"},
    )
    assert normalised["task"]["owned_paths"] == []
    assert normalised["task"]["evidence_paths"] == ["evidence/NOOWN/**"]


def test_p7_non_git_directory_is_graceful_without_legacy_authority(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = Path(td) / "ordinary-directory"
        root.mkdir()
        assert build_authority_snapshot(root) is None


def test_p7_multiple_remotes_are_explicitly_scoped(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        base_dir = Path(td)
        origin = base_dir / "origin.git"
        mirror = base_dir / "mirror.git"
        subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
        subprocess.run(["git", "init", "--bare", "-q", str(mirror)], check=True)
        root = _repo(base_dir / "repo")
        _run(root, "git", "remote", "add", "origin", str(origin))
        _run(root, "git", "remote", "add", "mirror", str(mirror))
        _run(root, "git", "push", "-q", "origin", "main")
        _run(root, "git", "push", "-q", "mirror", "main")
        base = _run(root, "git", "rev-parse", "HEAD").stdout.strip()

        (root / "target.txt").write_text("target\n")
        _run(root, "git", "add", "target.txt")
        _run(root, "git", "commit", "-qm", "target")
        target = _run(root, "git", "rev-parse", "HEAD").stdout.strip()
        _run(root, "git", "reset", "--hard", "-q", base)

        result = promote_fast_forward(
            root,
            target,
            remote="origin",
            remote_branch="main",
            expected_remote=base,
        )
        assert result["status"] == "promoted-remote"
        origin_head = subprocess.run(
            ["git", "--git-dir", str(origin), "rev-parse", "refs/heads/main"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        mirror_head = subprocess.run(
            ["git", "--git-dir", str(mirror), "rev-parse", "refs/heads/main"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        assert origin_head == target
        assert mirror_head == base


def test_p7_overlapping_ownership_is_dependency_serialised(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", state)
        root = _repo(Path(td) / "repo")
        plan = root / "PLAN.md"
        plan.write_text("plan\n")
        tasks = [
            _task(
                "FIRST",
                authority_sets=["default"],
                owned_paths=["shared/**"],
            ),
            _task(
                "SECOND",
                authority_sets=["default"],
                owned_paths=["shared/**"],
                depends_on=["FIRST"],
            ),
        ]
        contract = {
            "schema_version": 1,
            "planning_authority": {
                "sets": [{
                    "id": "default",
                    "members": [_member("PLAN.md")],
                    "validators": [],
                    "reconcilers": [],
                }]
            },
            "tasks": {
                "sources": [{
                    "id": "tasks",
                    "kind": "static",
                    "authority_sets": ["default"],
                    "tasks": tasks,
                }],
                "execution_mode": "single-writer",
                "strict_dependencies": True,
            },
            "control_surfaces": [],
        }
        _write_governance(root, contract)
        task_set = resolve_task_sources(root, persist=True)
        assert task_set["status"] == "READY"
        assert ready_frontier(root, task_set=task_set) == ["FIRST"]


def test_p7_exact_file_and_nested_directory_ownership_are_distinct():
    exact = _task(
        "ONEFILE",
        authority_sets=["default"],
        owned_paths=["src/one.py"],
    )
    nested = _task(
        "NESTED",
        authority_sets=["default"],
        owned_paths=["packages/service/**"],
    )
    exact_record = normalise_task_spec(
        exact,
        source_id="fixture",
        source_authority_sets={"default"},
        known_authority_sets={"default"},
        protected_paths={"PLAN.md"},
    )
    nested_record = normalise_task_spec(
        nested,
        source_id="fixture",
        source_authority_sets={"default"},
        known_authority_sets={"default"},
        protected_paths={"PLAN.md"},
    )
    assert exact_record["task"]["owned_paths"] == ["src/one.py"]
    assert selector_matches_path("src/one.py", "src/one.py")
    assert not selector_matches_path("src/one.py", "src/two.py")
    assert nested_record["task"]["owned_paths"] == ["packages/service/**"]
    assert selector_matches_path(
        "packages/service/**",
        "packages/service/deep/module/file.py",
    )


def test_p7_scenario_traceability_has_all_64_live_pytest_references():
    root = Path(__file__).resolve().parents[1]
    traceability = json.loads(
        (
            root
            / "docs"
            / "RC4_P7_SCENARIO_TRACEABILITY.json"
        ).read_text(encoding="utf-8")
    )
    assert traceability["schema_version"] == 1
    assert traceability["target"] == "1.0.0-rc4"
    scenarios = traceability["scenarios"]
    assert traceability["scenario_count"] == 64
    assert len(scenarios) == 64
    assert [row["id"] for row in scenarios] == list(range(1, 65))
    assert len({row["scenario"] for row in scenarios}) == 64

    for row in scenarios:
        evidence = row.get("evidence")
        assert isinstance(evidence, list) and evidence, row
        for item in evidence:
            assert item.get("kind") == "pytest", item
            ref = item.get("ref")
            assert isinstance(ref, str) and "::" in ref, item
            rel, function = ref.split("::", 1)
            path = root / rel
            assert path.is_file(), ref
            source = path.read_text(encoding="utf-8")
            assert f"def {function}(" in source, ref


def test_p7_ci_treats_release_pull_request_head_as_development_release():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "ci.yml"
    ).read_text(encoding="utf-8")
    assert 'EFFECTIVE_BRANCH="${GITHUB_HEAD_REF:-$GITHUB_REF_NAME}"' in workflow
    release_expr = (
        "startsWith(github.ref, 'refs/heads/release/') || "
        "startsWith(github.head_ref, 'release/')"
    )
    assert release_expr in workflow
    assert (
        "if: ${{ !(startsWith(github.ref, 'refs/heads/release/') || "
        "startsWith(github.head_ref, 'release/')) }}"
    ) in workflow

def test_p7_interruption_traceability_references_live_evidence():
    root = Path(__file__).resolve().parents[1]
    traceability = json.loads(
        (
            root
            / "docs"
            / "RC4_P7_INTERRUPTION_TRACEABILITY.json"
        ).read_text(encoding="utf-8")
    )
    assert traceability["schema_version"] == 1
    assert traceability["target"] == "1.0.0-rc4"
    entries = traceability["entries"]
    assert traceability["entry_count"] == len(entries)
    assert len(entries) >= 25
    assert len({row["id"] for row in entries}) == len(entries)

    for row in entries:
        evidence = row.get("evidence")
        assert isinstance(evidence, list) and evidence, row
        for item in evidence:
            kind = item.get("kind")
            ref = item.get("ref")
            assert kind in {"pytest", "workflow"}, item
            assert isinstance(ref, str) and ref, item
            if kind == "pytest":
                assert "::" in ref, item
                rel, function = ref.split("::", 1)
                path = root / rel
                assert path.is_file(), ref
                assert f"def {function}(" in path.read_text(
                    encoding="utf-8"
                ), ref
            else:
                path = root / ref
                assert path.is_file(), ref

    finalizer = (
        root / ".github" / "workflows" / "finalize-release-candidate.yml"
    ).read_text(encoding="utf-8")
    assert 'paths:\n      - ".release-finalize"' in finalizer
    assert "sequence=[1-9][0-9]*" in finalizer
    assert "git add MANIFEST.sha256 .release-qualify" in finalizer
    assert "Unexpected finalizer mutation" in finalizer
    assert 'grep -Ev \'^(MANIFEST\\.sha256|\\.release-qualify)$\'' in finalizer
    assert "push --force-with-lease=" in finalizer
    assert "gh workflow run ci.yml" in finalizer
    assert "gh workflow run release-candidate.yml" in finalizer


def test_p7_tag_workflow_requires_exact_successful_main_ci_for_manual_and_automatic_paths():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "tag-accepted-release.yml"
    ).read_text(encoding="utf-8")
    assert 'actions/workflows/ci.yml/runs' in workflow
    assert '-f branch="main" -f status=success' in workflow
    assert 'run.get("head_sha") == sha' in workflow
    assert 'run.get("conclusion") == "success"' in workflow
    assert "No successful main CI found for exact SHA" in workflow
    assert "main_ci_run=" in workflow


def test_p7_tag_workflow_revalidates_remote_branch_heads_immediately_before_tag_write():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "tag-accepted-release.yml"
    ).read_text(encoding="utf-8")
    main_probe = (
        'REMOTE_MAIN="$(git ls-remote --heads origin refs/heads/main '
        '| awk \'{print $1}\')"'
    )
    release_probe = (
        'REMOTE_RELEASE="$(git ls-remote --heads origin '
        '"refs/heads/$RELEASE_BRANCH" | awk \'{print $1}\')"'
    )
    tag_write = 'gh api --method POST "repos/$GITHUB_REPOSITORY/git/refs"'
    assert main_probe in workflow
    assert release_probe in workflow
    assert "Main moved before tagging" in workflow
    assert "Release branch moved before tagging" in workflow
    assert workflow.rfind(main_probe) < workflow.rfind(tag_write)
    assert workflow.rfind(release_probe) < workflow.rfind(tag_write)


def test_p7_finalizer_request_is_marker_only_exact_and_monotonic():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "finalize-release-candidate.yml"
    ).read_text(encoding="utf-8")
    assert "FINALIZE_LINES" in workflow
    assert "must contain exactly two lines: version and sequence" in workflow
    assert 'git diff --name-only -z "$GITHUB_SHA^" "$GITHUB_SHA" --' in workflow
    assert 'REQUEST_PATHS[0]}" != ".release-finalize"' in workflow
    assert 'git show "$GITHUB_SHA^:.release-finalize"' in workflow
    assert "PARENT_SEQUENCE" in workflow
    assert "SEQUENCE <= PARENT_SEQUENCE" in workflow
    assert "Non-monotonic finalization sequence" in workflow


def test_p7_release_qualification_is_bound_to_marker_sequence_and_finalizer_diff():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "release-candidate.yml"
    ).read_text(encoding="utf-8")
    assert "QUALIFY_LINES" in workflow
    assert "FINALIZE_LINES" in workflow
    assert "must contain exactly version, sequence and source_sha lines" in workflow
    assert '"sequence=$QUALIFY_SEQUENCE"' in workflow
    assert 'EXPECTED_SOURCE="$(git rev-parse HEAD^)"' in workflow
    assert 'git diff --name-only -z HEAD^ HEAD --' in workflow
    assert "Unexpected finalizer commit path" in workflow
    assert "only MANIFEST.sha256 and .release-qualify are permitted" in workflow
    assert "Qualification trigger not committed" in workflow


def test_p7_tag_requires_matching_finalization_and_qualification_sequence():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "tag-accepted-release.yml"
    ).read_text(encoding="utf-8")
    assert ".release-qualify and .release-finalize are both required" in workflow
    assert "QUALIFY_SEQUENCE" in workflow
    assert '"sequence=$QUALIFY_SEQUENCE"' in workflow
    assert "Release sequence mismatch" in workflow
    assert '"source_sha=$EXPECTED_SOURCE"' in workflow
