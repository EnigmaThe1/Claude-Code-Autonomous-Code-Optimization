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
import os
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Callable, Mapping

from authority_set import AuthoritySetError, _resolve_selector, _tree, build_authority_snapshot
from execution import run_repository_command
from git_trust import trusted_git_env
from governance_contract import (
    GovernanceContractError,
    canonical_json_bytes,
    load_governance_contract,
)
from repo_identity import repo_state_dir
from runtime_paths import ensure_private_dir, utcnow
from state_store import json_dump, load_json
from task_spec import TaskSpecError, normalise_task_spec, validate_task_graph


MAX_SOURCE_BLOB_BYTES = 32 * 1024 * 1024
MAX_ADAPTER_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_ADAPTER_STDERR_CHARS = 64 * 1024
MAX_TASKS = 100_000


class TaskSourceError(ValueError):
    pass


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _json_no_duplicates(text: str, *, where: str) -> Any:
    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise TaskSourceError(f"{where} contains duplicate JSON key {key!r}")
            out[key] = value
        return out

    try:
        return json.loads(text, object_pairs_hook=hook)
    except TaskSourceError:
        raise
    except json.JSONDecodeError as exc:
        raise TaskSourceError(f"{where} contains invalid JSON: {exc.msg}") from exc


def _git_blob(
    root: Path,
    object_id: str,
    *,
    git_env: Mapping[str, str] | None = None,
) -> bytes:
    size_cp = subprocess.run(
        ["git", "-C", str(root), "cat-file", "-s", object_id],
        text=True,
        capture_output=True,
        env=(dict(git_env) if git_env is not None else trusted_git_env(root)),
    )
    if size_cp.returncode != 0:
        raise TaskSourceError(f"unable to inspect committed task-source blob {object_id}")
    try:
        size = int(size_cp.stdout.strip())
    except ValueError as exc:
        raise TaskSourceError(f"invalid Git blob size for task source {object_id}") from exc
    if size < 0 or size > MAX_SOURCE_BLOB_BYTES:
        raise TaskSourceError(f"task-source blob {object_id} exceeds maximum supported size")

    cp = subprocess.run(
        ["git", "-C", str(root), "cat-file", "blob", object_id],
        capture_output=True,
        env=(dict(git_env) if git_env is not None else trusted_git_env(root)),
    )
    if cp.returncode != 0:
        raise TaskSourceError(f"unable to read committed task-source blob {object_id}")
    payload = bytes(cp.stdout)
    if len(payload) != size:
        raise TaskSourceError(f"committed task-source blob {object_id} changed size during read")
    return payload


def _regular_blob(entry: dict[str, str], *, where: str) -> dict[str, str]:
    mode = entry["mode"]
    kind = entry["type"]
    path = entry["path"]
    if mode == "120000":
        raise TaskSourceError(f"{where} must not resolve a symlink: {path}")
    if mode == "160000" or kind == "commit":
        raise TaskSourceError(f"{where} must not cross a gitlink/submodule boundary: {path}")
    if kind != "blob" or mode not in {"100644", "100755"}:
        raise TaskSourceError(f"{where} must resolve regular committed blobs: {path}")
    return {
        "path": path,
        "git_mode": mode,
        "blob": entry["object"],
    }


def _resolve_declared_inputs(
    source: dict[str, Any],
    tree: dict[str, dict[str, str]],
    *,
    field: str,
) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for selector in source[field]:
        try:
            matches = _resolve_selector(selector, tree, required=True)
        except AuthoritySetError as exc:
            raise TaskSourceError(f"task source {source['id']!r}: {exc}") from exc
        for entry in matches:
            record = _regular_blob(entry, where=f"task source {source['id']!r} {field}")
            if record["path"] in seen:
                raise TaskSourceError(
                    f"task source {source['id']!r} selectors resolve the same file more than once: "
                    f"{record['path']}"
                )
            seen.add(record["path"])
            out.append(record)
    return sorted(out, key=lambda item: item["path"])


def _decode_source(payload: bytes, *, where: str) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TaskSourceError(f"{where} must be UTF-8") from exc


def _task_rows_from_container(value: Any, *, where: str) -> list[Any]:
    if isinstance(value, list):
        rows = value
    elif isinstance(value, dict):
        if set(value) != {"schema_version", "tasks"}:
            raise TaskSourceError(
                f"{where} object must contain exactly schema_version and tasks"
            )
        if value["schema_version"] != 1:
            raise TaskSourceError(f"{where}.schema_version must be 1")
        rows = value["tasks"]
    else:
        raise TaskSourceError(f"{where} must be a TaskSpec array or schema-1 tasks object")
    if not isinstance(rows, list) or len(rows) > MAX_TASKS:
        raise TaskSourceError(f"{where}.tasks must be a bounded list")
    return rows


def _parse_blob_rows(kind: str, payload: bytes, *, where: str) -> list[Any]:
    text = _decode_source(payload, where=where)
    if kind == "json":
        return _task_rows_from_container(_json_no_duplicates(text, where=where), where=where)

    if kind == "jsonl":
        rows: list[Any] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            value = _json_no_duplicates(line, where=f"{where} line {lineno}")
            if not isinstance(value, dict):
                raise TaskSourceError(f"{where} line {lineno} must be one TaskSpec object")
            rows.append(value)
            if len(rows) > MAX_TASKS:
                raise TaskSourceError(f"{where} contains too many TaskSpecs")
        return rows

    if kind == "toml":
        try:
            value = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise TaskSourceError(f"{where} contains invalid TOML: {exc}") from exc
        if set(value) != {"schema_version", "tasks"}:
            raise TaskSourceError(f"{where} TOML must contain exactly schema_version and tasks")
        if value.get("schema_version") != 1:
            raise TaskSourceError(f"{where}.schema_version must be 1")
        rows = value.get("tasks")
        if not isinstance(rows, list) or len(rows) > MAX_TASKS:
            raise TaskSourceError(f"{where}.tasks must be a bounded array")
        return rows

    raise TaskSourceError(f"unsupported built-in task source kind: {kind}")


def _normalise_rows(
    rows: list[Any],
    *,
    source: dict[str, Any],
    snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    known_sets = {item["id"] for item in snapshot["sets"]}
    source_sets = set(source["authority_sets"])
    protected = set(snapshot.get("protected_paths", []))
    records: list[dict[str, Any]] = []
    for raw in rows:
        try:
            record = normalise_task_spec(
                raw,
                source_id=source["id"],
                source_authority_sets=source_sets,
                known_authority_sets=known_sets,
                protected_paths=protected,
            )
        except TaskSpecError as exc:
            raise TaskSourceError(f"task source {source['id']!r}: {exc}") from exc
        records.append(record)
        if len(records) > MAX_TASKS:
            raise TaskSourceError(f"task source {source['id']!r} produced too many TaskSpecs")
    return sorted(records, key=lambda item: item["task"]["id"])


def _adapter_output_rows(stdout: str, *, source_id: str) -> list[Any]:
    if len(stdout.encode("utf-8", errors="replace")) > MAX_ADAPTER_OUTPUT_BYTES:
        raise TaskSourceError(f"adapter {source_id!r} output exceeds maximum supported size")
    value = _json_no_duplicates(stdout, where=f"adapter {source_id!r} stdout")
    return _task_rows_from_container(value, where=f"adapter {source_id!r} stdout")


def _materialise_inputs(
    root: Path,
    inputs: list[dict[str, str]],
    destination: Path,
    *,
    git_env: Mapping[str, str] | None = None,
) -> None:
    for record in inputs:
        target = destination / record["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            _git_blob(root, record["blob"], git_env=git_env)
        )
        try:
            target.chmod(0o755 if record["git_mode"] == "100755" else 0o644)
        except OSError:
            pass


AdapterRunner = Callable[..., dict[str, Any]]


def _run_adapter_once(
    root: Path,
    source: dict[str, Any],
    inputs: list[dict[str, str]],
    snapshot: dict[str, Any],
    *,
    runner: AdapterRunner,
    git_env: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    caps = source["capabilities"]
    if caps.get("network") or caps.get("read_external"):
        raise TaskSourceError(
            f"adapter {source['id']!r} requests capabilities that P2 does not grant"
        )

    with tempfile.TemporaryDirectory(prefix="claude-auto-adapter-view-") as td:
        view = Path(td).resolve()
        _materialise_inputs(
            root,
            inputs,
            view,
            git_env=git_env,
        )
        cwd = view if source["cwd"] == "." else (view / source["cwd"]).resolve()
        try:
            cwd.relative_to(view)
        except ValueError as exc:
            raise TaskSourceError(f"adapter {source['id']!r} cwd escapes materialised input view") from exc
        cwd.mkdir(parents=True, exist_ok=True)

        before = {
            record["path"]: hashlib.sha256((view / record["path"]).read_bytes()).hexdigest()
            for record in inputs
        }
        result = runner(
            view,
            list(source["argv"]),
            timeout=int(source["timeout_seconds"]),
            trust_repo_scripts=False,
            unrestricted_host=False,
            read_only_root=True,
            working_directory=cwd,
            hidden_paths=[root],
            max_output_bytes=MAX_ADAPTER_OUTPUT_BYTES,
            read_allowlist_only=True,
        )
        after = {
            record["path"]: hashlib.sha256((view / record["path"]).read_bytes()).hexdigest()
            for record in inputs
        }
        if after != before:
            raise TaskSourceError(f"adapter {source['id']!r} modified its exact-commit input view")

    rc = int(result.get("returncode", 1))
    if rc != 0:
        detail = str(result.get("stderr") or result.get("stdout") or "adapter failed")
        if len(detail) > MAX_ADAPTER_STDERR_CHARS:
            detail = detail[:MAX_ADAPTER_STDERR_CHARS] + "…"
        raise TaskSourceError(
            f"adapter {source['id']!r} failed under {result.get('execution_boundary') or 'unknown'} "
            f"with exit {rc}: {detail}"
        )

    stdout = str(result.get("stdout") or "")
    rows = _adapter_output_rows(stdout, source_id=source["id"])
    records = _normalise_rows(rows, source=source, snapshot=snapshot)
    evidence = {
        "execution_boundary": result.get("execution_boundary"),
        "sandboxed": bool(result.get("sandboxed", False)),
        "environment_scrubbed": bool(result.get("environment_scrubbed", False)),
        "normalised_output_sha256": _digest([
            {
                "task": item["task"],
                "task_spec_sha256": item["task_spec_sha256"],
                "record_sha256": item["record_sha256"],
            }
            for item in records
        ]),
    }
    if not evidence["sandboxed"]:
        raise TaskSourceError(f"adapter {source['id']!r} did not run inside a verified isolation boundary")
    return records, evidence


def _resolve_adapter(
    root: Path,
    source: dict[str, Any],
    tree: dict[str, dict[str, str]],
    snapshot: dict[str, Any],
    *,
    runner: AdapterRunner,
    git_env: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    inputs = _resolve_declared_inputs(source, tree, field="inputs")
    first, ev1 = _run_adapter_once(
        root,
        source,
        inputs,
        snapshot,
        runner=runner,
        git_env=git_env,
    )
    second, ev2 = _run_adapter_once(
        root,
        source,
        inputs,
        snapshot,
        runner=runner,
        git_env=git_env,
    )

    first_digest = ev1["normalised_output_sha256"]
    second_digest = ev2["normalised_output_sha256"]
    if first_digest != second_digest:
        raise TaskSourceError(
            f"adapter {source['id']!r} is nondeterministic: normalised outputs differ"
        )
    return first, {
        "id": source["id"],
        "kind": "adapter",
        "authority_sets": sorted(source["authority_sets"]),
        "inputs": inputs,
        "normalised_output_sha256": first_digest,
        "determinism_runs": [
            {"execution_boundary": ev1["execution_boundary"], "sha256": first_digest},
            {"execution_boundary": ev2["execution_boundary"], "sha256": second_digest},
        ],
    }


def _resolve_builtin(
    root: Path,
    source: dict[str, Any],
    tree: dict[str, dict[str, str]],
    snapshot: dict[str, Any],
    *,
    git_env: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if source["kind"] == "static":
        records = _normalise_rows(source["tasks"], source=source, snapshot=snapshot)
        return records, {
            "id": source["id"],
            "kind": "static",
            "authority_sets": sorted(source["authority_sets"]),
            "inputs": [],
            "normalised_output_sha256": _digest([
                {
                    "task": item["task"],
                    "task_spec_sha256": item["task_spec_sha256"],
                    "record_sha256": item["record_sha256"],
                }
                for item in records
            ]),
        }

    inputs = _resolve_declared_inputs(source, tree, field="paths")
    all_records: list[dict[str, Any]] = []
    for record in inputs:
        payload = _git_blob(
            root,
            record["blob"],
            git_env=git_env,
        )
        rows = _parse_blob_rows(
            source["kind"],
            payload,
            where=f"task source {source['id']!r} file {record['path']!r}",
        )
        all_records.extend(_normalise_rows(rows, source=source, snapshot=snapshot))
        if len(all_records) > MAX_TASKS:
            raise TaskSourceError(f"task source {source['id']!r} produced too many TaskSpecs")
    all_records.sort(key=lambda item: item["task"]["id"])
    return all_records, {
        "id": source["id"],
        "kind": source["kind"],
        "authority_sets": sorted(source["authority_sets"]),
        "inputs": inputs,
        "normalised_output_sha256": _digest([
            {
                "task": item["task"],
                "task_spec_sha256": item["task_spec_sha256"],
                "record_sha256": item["record_sha256"],
            }
            for item in all_records
        ]),
    }


def _resolved_path(root: Path, *, state_dir: Path | None = None) -> Path:
    base = (
        state_dir.expanduser().resolve()
        if state_dir is not None
        else repo_state_dir(root)
    )
    return base / "tasks" / "task-source-set.json"


def resolve_task_sources(
    root: Path,
    *,
    runner: AdapterRunner = run_repository_command,
    persist: bool = True,
    ref: str = "HEAD",
    git_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if persist and ref != "HEAD":
        raise TaskSourceError(
            "persisted TaskSourceSet resolution is HEAD-only; "
            "candidate refs must use persist=False"
        )
    if persist:
        # Synchronise the ordinary schema-9 lifecycle before deriving any task
        # authority. This keeps later task persistence attached to the same
        # external-state generation as the AuthoritySet snapshot.
        from repo_runtime import activate
        try:
            activate(root)
        except (AuthoritySetError, GovernanceContractError, OSError) as exc:
            raise TaskSourceError(f"unable to activate repository governance before task resolution: {exc}") from exc

    try:
        snapshot = build_authority_snapshot(root, ref)
        exact_ref = (
            str(snapshot["product_head"])
            if isinstance(snapshot, dict)
            else ref
        )
        contract = load_governance_contract(root, exact_ref)
    except (AuthoritySetError, GovernanceContractError) as exc:
        raise TaskSourceError(str(exc)) from exc

    if snapshot is None or contract is None:
        result = {"status": "UNCONFIGURED", "reason": "repository task sources are not configured"}
        if persist:
            _clear_persisted(root)
        return result

    sources = list(contract["tasks"]["sources"])
    if not sources:
        result = {"status": "UNCONFIGURED", "reason": "governance contract has no task sources"}
        if persist:
            _clear_persisted(root)
        return result

    tree = _tree(root, snapshot["product_head"])
    merged: list[dict[str, Any]] = []
    source_evidence: list[dict[str, Any]] = []

    for source in sorted(sources, key=lambda item: item["id"]):
        if source["kind"] == "adapter":
            records, evidence = _resolve_adapter(
                root,
                source,
                tree,
                snapshot,
                runner=runner,
                git_env=git_env,
            )
        else:
            records, evidence = _resolve_builtin(
                root,
                source,
                tree,
                snapshot,
                git_env=git_env,
            )
        merged.extend(records)
        source_evidence.append(evidence)
        if len(merged) > MAX_TASKS:
            raise TaskSourceError("merged TaskSourceSet exceeds maximum task count")

    # Re-read exact authority after all source/adapter work. A concurrent commit,
    # governance edit or control-surface change must invalidate the resolution
    # rather than allowing an old snapshot to be reported/persisted as READY.
    try:
        final_ref = "HEAD" if ref == "HEAD" else str(snapshot["product_head"])
        final_snapshot = build_authority_snapshot(root, final_ref)
    except AuthoritySetError as exc:
        raise TaskSourceError(f"repository authority changed during task resolution: {exc}") from exc
    if (
        not isinstance(final_snapshot, dict)
        or final_snapshot.get("snapshot_sha256") != snapshot.get("snapshot_sha256")
        or final_snapshot.get("product_head") != snapshot.get("product_head")
        or final_snapshot.get("task_source_contract_digest") != snapshot.get("task_source_contract_digest")
    ):
        raise TaskSourceError("repository authority changed during task resolution; retry from the new exact snapshot")

    merged.sort(key=lambda item: item["task"]["id"])
    try:
        external_dependencies = validate_task_graph(
            merged,
            strict_dependencies=bool(contract["tasks"]["strict_dependencies"]),
        )
    except TaskSpecError as exc:
        raise TaskSourceError(str(exc)) from exc

    merged_authority = [
        {
            "task": item["task"],
            "task_spec_sha256": item["task_spec_sha256"],
            "record_sha256": item["record_sha256"],
            "source_id": item["source_id"],
        }
        for item in merged
    ]
    merged_tasks_sha256 = _digest(merged_authority)

    # Runtime execution details prove *how* an adapter was isolated, but are not
    # semantic repository authority. Keep them for audit without making the
    # TaskSourceSet digest depend on whether one host used srt or bubblewrap.
    canonical_sources: list[dict[str, Any]] = []
    runtime_evidence: list[dict[str, Any]] = []
    for evidence in source_evidence:
        canonical_sources.append({
            key: value
            for key, value in evidence.items()
            if key != "determinism_runs"
        })
        if "determinism_runs" in evidence:
            runtime_evidence.append({
                "id": evidence["id"],
                "determinism_runs": evidence["determinism_runs"],
            })

    canonical_set = {
        "schema_version": 1,
        "authority_snapshot_sha256": snapshot["snapshot_sha256"],
        "product_head": snapshot["product_head"],
        "task_source_contract_digest": snapshot["task_source_contract_digest"],
        "strict_dependencies": bool(contract["tasks"]["strict_dependencies"]),
        "sources": canonical_sources,
        "tasks": merged_authority,
        "external_dependencies": external_dependencies,
        "merged_tasks_sha256": merged_tasks_sha256,
    }
    task_source_set_sha256 = _digest(canonical_set)
    result = {
        "status": "READY",
        **canonical_set,
        "adapter_runtime_evidence": runtime_evidence,
        "task_source_set_sha256": task_source_set_sha256,
    }

    if persist:
        path = _resolved_path(root)
        ensure_private_dir(path.parent)
        persisted = dict(result)
        persisted["resolved_at"] = utcnow()
        json_dump(path, persisted)
        state_path = repo_state_dir(root) / "state.json"
        state = load_json(state_path, {})
        if isinstance(state, dict):
            state["task_source_sha256"] = task_source_set_sha256
            json_dump(state_path, state)

        try:
            persisted_snapshot = build_authority_snapshot(root)
        except AuthoritySetError as exc:
            _clear_persisted(root)
            raise TaskSourceError(
                f"repository authority changed while persisting task resolution: {exc}"
            ) from exc
        if (
            not isinstance(persisted_snapshot, dict)
            or persisted_snapshot.get("snapshot_sha256") != snapshot.get("snapshot_sha256")
            or persisted_snapshot.get("product_head") != snapshot.get("product_head")
            or persisted_snapshot.get("task_source_contract_digest") != snapshot.get("task_source_contract_digest")
        ):
            _clear_persisted(root)
            raise TaskSourceError(
                "repository authority changed while persisting task resolution; durable result was discarded"
            )
    return result


def _clear_persisted(root: Path) -> None:
    path = _resolved_path(root)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    state_path = repo_state_dir(root) / "state.json"
    state = load_json(state_path, {})
    if isinstance(state, dict) and state.get("task_source_sha256") is not None:
        state["task_source_sha256"] = None
        json_dump(state_path, state)


_PERSISTED_CANONICAL_KEYS = (
    "schema_version",
    "authority_snapshot_sha256",
    "product_head",
    "task_source_contract_digest",
    "strict_dependencies",
    "sources",
    "tasks",
    "external_dependencies",
    "merged_tasks_sha256",
)


def load_resolved_task_source_set(
    root: Path,
    *,
    require_current: bool = True,
    require_state_binding: bool = True,
    state_dir: Path | None = None,
    authority_root: Path | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    authority_check_root = (
        authority_root.expanduser().resolve()
        if authority_root is not None
        else root
    )
    state_root = (
        state_dir.expanduser().resolve()
        if state_dir is not None
        else repo_state_dir(root)
    )
    path = _resolved_path(root, state_dir=state_root)
    if not path.exists():
        raise TaskSourceError("no persisted TaskSourceSet is available")
    try:
        persisted = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskSourceError(f"persisted TaskSourceSet is unreadable or malformed: {exc}") from exc
    if not isinstance(persisted, dict) or not persisted:
        raise TaskSourceError("persisted TaskSourceSet must be a non-empty JSON object")

    missing = [key for key in _PERSISTED_CANONICAL_KEYS if key not in persisted]
    if missing:
        raise TaskSourceError(
            "persisted TaskSourceSet is incomplete: " + ", ".join(missing)
        )
    canonical = {key: persisted[key] for key in _PERSISTED_CANONICAL_KEYS}
    recorded = persisted.get("task_source_set_sha256")
    if not isinstance(recorded, str) or len(recorded) != 64:
        raise TaskSourceError("persisted TaskSourceSet digest is missing or invalid")
    actual = _digest(canonical)
    if actual != recorded:
        raise TaskSourceError(
            "persisted TaskSourceSet integrity check failed: semantic digest mismatch"
        )

    tasks = canonical.get("tasks")
    if not isinstance(tasks, list):
        raise TaskSourceError("persisted TaskSourceSet tasks must be a list")
    merged_actual = _digest([
        {
            "task": item.get("task"),
            "task_spec_sha256": item.get("task_spec_sha256"),
            "record_sha256": item.get("record_sha256"),
            "source_id": item.get("source_id"),
        }
        for item in tasks
        if isinstance(item, dict)
    ])
    if len(tasks) != len([item for item in tasks if isinstance(item, dict)]):
        raise TaskSourceError("persisted TaskSourceSet contains a malformed task record")
    if merged_actual != canonical.get("merged_tasks_sha256"):
        raise TaskSourceError(
            "persisted TaskSourceSet integrity check failed: merged task digest mismatch"
        )

    state = load_json(state_root / "state.json", {})
    if require_state_binding and (
        not isinstance(state, dict) or state.get("task_source_sha256") != recorded
    ):
        raise TaskSourceError(
            "persisted TaskSourceSet is not bound to the current durable state generation"
        )

    if require_current:
        try:
            snapshot = build_authority_snapshot(authority_check_root)
        except AuthoritySetError as exc:
            raise TaskSourceError(str(exc)) from exc
        if not isinstance(snapshot, dict):
            raise TaskSourceError("repository governance is no longer configured")
        if (
            canonical.get("authority_snapshot_sha256") != snapshot.get("snapshot_sha256")
            or canonical.get("product_head") != snapshot.get("product_head")
            or canonical.get("task_source_contract_digest") != snapshot.get("task_source_contract_digest")
        ):
            raise TaskSourceError(
                "persisted TaskSourceSet is stale for the current repository authority"
            )
    return persisted


def task_source_status(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    try:
        snapshot = build_authority_snapshot(root)
        contract = load_governance_contract(root)
    except (AuthoritySetError, GovernanceContractError) as exc:
        return {"status": "BLOCKED", "repository": str(root), "error": str(exc)}

    if snapshot is None or contract is None or not contract["tasks"]["sources"]:
        return {"status": "UNCONFIGURED", "repository": str(root)}

    path = _resolved_path(root)
    if not path.exists():
        return {
            "status": "UNRESOLVED",
            "repository": str(root),
            "authority_snapshot_sha256": snapshot["snapshot_sha256"],
        }

    try:
        verified = load_resolved_task_source_set(
            root,
            require_current=False,
            require_state_binding=False,
        )
    except TaskSourceError as exc:
        return {
            "status": "BLOCKED",
            "repository": str(root),
            "error": str(exc),
        }

    digest = verified["task_source_set_sha256"]
    state = load_json(repo_state_dir(root) / "state.json", {})
    current = (
        verified.get("authority_snapshot_sha256") == snapshot["snapshot_sha256"]
        and verified.get("product_head") == snapshot["product_head"]
        and verified.get("task_source_contract_digest") == snapshot["task_source_contract_digest"]
        and isinstance(state, dict)
        and state.get("task_source_sha256") == digest
    )
    return {
        "status": "READY" if current else "STALE",
        "repository": str(root),
        "authority_snapshot_sha256": snapshot["snapshot_sha256"],
        "task_source_set_sha256": digest,
        "merged_tasks_sha256": verified.get("merged_tasks_sha256"),
        "task_count": len(verified.get("tasks", [])),
        "external_dependencies": verified.get("external_dependencies", []),
        "resolved_at": verified.get("resolved_at"),
    }


def task_source_action(args: Any, *, find_repo_root) -> int:
    root = find_repo_root(getattr(args, "repo", None))
    try:
        if args.tasks_command == "status":
            result = task_source_status(root)
            rendered = result
        elif args.tasks_command == "resolve":
            result = resolve_task_sources(root)
            if result.get("status") == "READY":
                external = result.get("external_dependencies", [])
                rendered = {
                    "status": "READY",
                    "repository": str(root),
                    "product_head": result.get("product_head"),
                    "authority_snapshot_sha256": result.get("authority_snapshot_sha256"),
                    "task_source_set_sha256": result.get("task_source_set_sha256"),
                    "merged_tasks_sha256": result.get("merged_tasks_sha256"),
                    "source_count": len(result.get("sources", [])),
                    "task_count": len(result.get("tasks", [])),
                    "external_dependency_count": len(external) if isinstance(external, list) else 0,
                    "persisted_path": str(_resolved_path(root)),
                }
            else:
                rendered = result
        else:
            raise TaskSourceError(f"unsupported tasks command: {args.tasks_command}")
    except (TaskSourceError, OSError) as exc:
        print(json.dumps({"status": "BLOCKED", "repository": str(root), "error": str(exc)}, indent=2))
        return 2
    print(json.dumps(rendered, indent=2, sort_keys=True))
    return 2 if result.get("status") == "BLOCKED" else 0
