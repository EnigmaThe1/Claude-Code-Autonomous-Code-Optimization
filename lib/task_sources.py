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
from typing import Any, Callable

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


def _git_blob(root: Path, object_id: str) -> bytes:
    size_cp = subprocess.run(
        ["git", "-C", str(root), "cat-file", "-s", object_id],
        text=True,
        capture_output=True,
        env=trusted_git_env(root),
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
        env=trusted_git_env(root),
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
) -> None:
    for record in inputs:
        target = destination / record["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_git_blob(root, record["blob"]))
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
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    caps = source["capabilities"]
    if caps.get("network") or caps.get("read_external"):
        raise TaskSourceError(
            f"adapter {source['id']!r} requests capabilities that P2 does not grant"
        )

    with tempfile.TemporaryDirectory(prefix="claude-auto-adapter-view-") as td:
        view = Path(td).resolve()
        _materialise_inputs(root, inputs, view)
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
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    inputs = _resolve_declared_inputs(source, tree, field="inputs")
    first, ev1 = _run_adapter_once(root, source, inputs, snapshot, runner=runner)
    second, ev2 = _run_adapter_once(root, source, inputs, snapshot, runner=runner)

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
        payload = _git_blob(root, record["blob"])
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


def _resolved_path(root: Path) -> Path:
    return repo_state_dir(root) / "tasks" / "task-source-set.json"


def resolve_task_sources(
    root: Path,
    *,
    runner: AdapterRunner = run_repository_command,
    persist: bool = True,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    try:
        snapshot = build_authority_snapshot(root)
        contract = load_governance_contract(root)
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
                root, source, tree, snapshot, runner=runner
            )
        else:
            records, evidence = _resolve_builtin(root, source, tree, snapshot)
        merged.extend(records)
        source_evidence.append(evidence)
        if len(merged) > MAX_TASKS:
            raise TaskSourceError("merged TaskSourceSet exceeds maximum task count")

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

    canonical_set = {
        "schema_version": 1,
        "authority_snapshot_sha256": snapshot["snapshot_sha256"],
        "product_head": snapshot["product_head"],
        "task_source_contract_digest": snapshot["task_source_contract_digest"],
        "strict_dependencies": bool(contract["tasks"]["strict_dependencies"]),
        "sources": source_evidence,
        "tasks": merged_authority,
        "external_dependencies": external_dependencies,
        "merged_tasks_sha256": merged_tasks_sha256,
    }
    task_source_set_sha256 = _digest(canonical_set)
    result = {
        "status": "READY",
        **canonical_set,
        "task_source_set_sha256": task_source_set_sha256,
    }

    if persist:
        # Keep the durable state lifecycle identical to ordinary Claude Auto
        # activation. Import lazily to avoid a module cycle at import time.
        from repo_runtime import activate
        activate(root)
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
    persisted = load_json(path, {})
    if not isinstance(persisted, dict) or not persisted:
        return {
            "status": "UNRESOLVED",
            "repository": str(root),
            "authority_snapshot_sha256": snapshot["snapshot_sha256"],
        }

    digest = persisted.get("task_source_set_sha256")
    state = load_json(repo_state_dir(root) / "state.json", {})
    current = (
        persisted.get("authority_snapshot_sha256") == snapshot["snapshot_sha256"]
        and persisted.get("product_head") == snapshot["product_head"]
        and persisted.get("task_source_contract_digest") == snapshot["task_source_contract_digest"]
        and isinstance(digest, str)
        and isinstance(state, dict)
        and state.get("task_source_sha256") == digest
    )
    return {
        "status": "READY" if current else "STALE",
        "repository": str(root),
        "authority_snapshot_sha256": snapshot["snapshot_sha256"],
        "task_source_set_sha256": digest,
        "merged_tasks_sha256": persisted.get("merged_tasks_sha256"),
        "task_count": len(persisted.get("tasks", [])) if isinstance(persisted.get("tasks"), list) else 0,
        "external_dependencies": persisted.get("external_dependencies", []),
        "resolved_at": persisted.get("resolved_at"),
    }


def task_source_action(args: Any, *, find_repo_root) -> int:
    root = find_repo_root(getattr(args, "repo", None))
    try:
        if args.tasks_command == "status":
            result = task_source_status(root)
        elif args.tasks_command == "resolve":
            result = resolve_task_sources(root)
        else:
            raise TaskSourceError(f"unsupported tasks command: {args.tasks_command}")
    except (TaskSourceError, OSError) as exc:
        print(json.dumps({"status": "BLOCKED", "repository": str(root), "error": str(exc)}, indent=2))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2 if result.get("status") == "BLOCKED" else 0
