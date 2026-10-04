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
import re
from typing import Any

from governance_contract import GovernanceContractError, canonical_json_bytes, normalise_repo_selector


MAX_TASK_ID_LENGTH = 128
MAX_TASK_SELECTORS = 4096
MAX_DEPENDENCIES = 4096
MAX_VERIFICATION_ITEMS = 512
MAX_METADATA_BYTES = 64 * 1024

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TASK_KEYS = {
    "schema_version",
    "id",
    "authority_sets",
    "depends_on",
    "owned_paths",
    "evidence_paths",
    "runtime_scratch_paths",
    "verification",
    "commit_subject",
    "metadata",
}

# These anchors are protected even if a particular optional file is not present
# in the exact commit. Exact current authority/control paths are added by callers.
_MANDATORY_PROTECTED_ANCHORS = (
    ".claude-auto/governance.json",
    ".claude-auto/verification.json",
    ".claude-auto/future-control",
    ".claude/settings.json",
    ".mcp.json",
    ".gitattributes",
    ".gitmodules",
    "CLAUDE.md",
)


class TaskSpecError(ValueError):
    pass


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _selector_regex(selector: str) -> re.Pattern[str]:
    out = ["^"]
    i = 0
    while i < len(selector):
        ch = selector[i]
        if ch == "*":
            if i + 1 < len(selector) and selector[i + 1] == "*":
                while i + 1 < len(selector) and selector[i + 1] == "*":
                    i += 1
                out.append(".*")
            else:
                out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    out.append("$")
    return re.compile("".join(out))


def selector_matches_path(selector: str, path: str) -> bool:
    return bool(_selector_regex(selector).fullmatch(path))


def _unique_strings(
    value: Any,
    *,
    where: str,
    maximum: int,
    normalise_paths: bool = False,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise TaskSpecError(f"{where} must be a bounded list")
    out: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str) or not raw.strip():
            raise TaskSpecError(f"{where} entries must be non-empty strings")
        try:
            item = normalise_repo_selector(raw) if normalise_paths else raw.strip()
        except GovernanceContractError as exc:
            raise TaskSpecError(f"{where}: {exc}") from exc
        if item in seen:
            raise TaskSpecError(f"{where} contains duplicate entry {item!r}")
        seen.add(item)
        out.append(item)
    return out


def _validate_task_id(value: Any, *, where: str) -> str:
    if not isinstance(value, str):
        raise TaskSpecError(f"{where} must be a string")
    task_id = value.strip()
    if len(task_id) > MAX_TASK_ID_LENGTH or not _TASK_ID_RE.fullmatch(task_id):
        raise TaskSpecError(
            f"{where} must match {_TASK_ID_RE.pattern} and be at most {MAX_TASK_ID_LENGTH} characters"
        )
    return task_id


def _selector_hits_protected(selector: str, protected_paths: set[str]) -> str | None:
    if selector == ".claude-auto" or selector.startswith(".claude-auto/"):
        return ".claude-auto/"
    if selector == ".claude" or selector.startswith(".claude/"):
        return ".claude/"
    for path in sorted(protected_paths | set(_MANDATORY_PROTECTED_ANCHORS)):
        if selector_matches_path(selector, path):
            return path
    return None


def normalise_task_spec(
    raw: Any,
    *,
    source_id: str,
    source_authority_sets: set[str],
    known_authority_sets: set[str],
    protected_paths: set[str],
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise TaskSpecError(f"task from source {source_id!r} must be an object")
    actual = set(raw)
    if actual != _TASK_KEYS:
        missing = sorted(_TASK_KEYS - actual)
        unknown = sorted(actual - _TASK_KEYS)
        detail: list[str] = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if unknown:
            detail.append("unknown " + ", ".join(unknown))
        raise TaskSpecError(f"TaskSpec fields are invalid: {'; '.join(detail)}")
    if raw["schema_version"] != 1:
        raise TaskSpecError("TaskSpec.schema_version must be 1")

    task_id = _validate_task_id(raw["id"], where="TaskSpec.id")
    authority_sets = _unique_strings(
        raw["authority_sets"], where=f"TaskSpec {task_id}.authority_sets", maximum=256
    )
    if not authority_sets:
        raise TaskSpecError(f"TaskSpec {task_id}.authority_sets must not be empty")
    unknown_sets = sorted(set(authority_sets) - known_authority_sets)
    if unknown_sets:
        raise TaskSpecError(
            f"TaskSpec {task_id} references unknown AuthoritySet(s): " + ", ".join(unknown_sets)
        )
    escalated = sorted(set(authority_sets) - source_authority_sets)
    if escalated:
        raise TaskSpecError(
            f"TaskSpec {task_id} exceeds source {source_id!r} AuthoritySet ceiling: "
            + ", ".join(escalated)
        )

    dependencies = _unique_strings(
        raw["depends_on"], where=f"TaskSpec {task_id}.depends_on", maximum=MAX_DEPENDENCIES
    )
    for dep in dependencies:
        _validate_task_id(dep, where=f"TaskSpec {task_id}.depends_on entry")
    if task_id in dependencies:
        raise TaskSpecError(f"TaskSpec {task_id} cannot depend on itself")

    path_fields: dict[str, list[str]] = {}
    for key in ("owned_paths", "evidence_paths", "runtime_scratch_paths"):
        selectors = _unique_strings(
            raw[key],
            where=f"TaskSpec {task_id}.{key}",
            maximum=MAX_TASK_SELECTORS,
            normalise_paths=True,
        )
        for selector in selectors:
            hit = _selector_hits_protected(selector, protected_paths)
            if hit is not None:
                raise TaskSpecError(
                    f"TaskSpec {task_id}.{key} selector {selector!r} overlaps protected authority/control path {hit!r}"
                )
        path_fields[key] = selectors

    verification = _unique_strings(
        raw["verification"],
        where=f"TaskSpec {task_id}.verification",
        maximum=MAX_VERIFICATION_ITEMS,
    )
    if not verification:
        raise TaskSpecError(f"TaskSpec {task_id}.verification must not be empty")

    commit_subject = raw["commit_subject"]
    if commit_subject is not None:
        if not isinstance(commit_subject, str) or len(commit_subject) > 512:
            raise TaskSpecError(f"TaskSpec {task_id}.commit_subject must be null or at most 512 characters")
        if any(ord(ch) < 32 and ch not in "\t" for ch in commit_subject):
            raise TaskSpecError(f"TaskSpec {task_id}.commit_subject contains control characters")

    metadata = raw["metadata"]
    if not isinstance(metadata, dict):
        raise TaskSpecError(f"TaskSpec {task_id}.metadata must be an object")
    try:
        metadata_bytes = canonical_json_bytes(metadata)
    except (TypeError, ValueError) as exc:
        raise TaskSpecError(f"TaskSpec {task_id}.metadata must be JSON-compatible") from exc
    if len(metadata_bytes) > MAX_METADATA_BYTES:
        raise TaskSpecError(f"TaskSpec {task_id}.metadata exceeds maximum size")

    canonical = {
        "schema_version": 1,
        "id": task_id,
        "authority_sets": sorted(authority_sets),
        "depends_on": sorted(dependencies),
        "owned_paths": sorted(path_fields["owned_paths"]),
        "evidence_paths": sorted(path_fields["evidence_paths"]),
        "runtime_scratch_paths": sorted(path_fields["runtime_scratch_paths"]),
        "verification": sorted(verification),
        "commit_subject": commit_subject,
        "metadata": metadata,
    }
    authority_object = {
        key: canonical[key]
        for key in (
            "schema_version",
            "id",
            "authority_sets",
            "depends_on",
            "owned_paths",
            "evidence_paths",
            "runtime_scratch_paths",
            "verification",
        )
    }
    return {
        "task": canonical,
        "task_spec_sha256": _digest(authority_object),
        "record_sha256": _digest(canonical),
        "source_id": source_id,
    }


def validate_task_graph(
    records: list[dict[str, Any]],
    *,
    strict_dependencies: bool,
) -> list[str]:
    ids = [record["task"]["id"] for record in records]
    if len(set(ids)) != len(ids):
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        raise TaskSpecError("duplicate TaskSpec id(s): " + ", ".join(duplicates))

    known = set(ids)
    external: set[str] = set()
    graph: dict[str, list[str]] = {}
    for record in records:
        task = record["task"]
        task_id = task["id"]
        deps = list(task["depends_on"])
        graph[task_id] = deps
        for dep in deps:
            if dep not in known:
                if strict_dependencies:
                    raise TaskSpecError(f"TaskSpec {task_id} depends on missing task {dep}")
                external.add(dep)

    visit: dict[str, int] = {}
    path: list[str] = []
    path_index: dict[str, int] = {}
    for start in sorted(known):
        if visit.get(start, 0) == 2:
            continue
        frames: list[tuple[str, int]] = [(start, 0)]
        while frames:
            task_id, dep_index = frames[-1]
            if visit.get(task_id, 0) == 0:
                visit[task_id] = 1
                path_index[task_id] = len(path)
                path.append(task_id)

            deps = graph.get(task_id, [])
            descended = False
            while dep_index < len(deps):
                dep = deps[dep_index]
                dep_index += 1
                frames[-1] = (task_id, dep_index)
                if dep not in known:
                    continue
                state = visit.get(dep, 0)
                if state == 0:
                    frames.append((dep, 0))
                    descended = True
                    break
                if state == 1:
                    start_index = path_index.get(dep, 0)
                    cycle = path[start_index:] + [dep]
                    raise TaskSpecError("TaskSpec dependency cycle: " + " -> ".join(cycle))
            if descended:
                continue

            frames.pop()
            visit[task_id] = 2
            path_index.pop(task_id, None)
            if path and path[-1] == task_id:
                path.pop()

    return sorted(external)
