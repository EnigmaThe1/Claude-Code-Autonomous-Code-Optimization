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
import re
import subprocess
import unicodedata
from pathlib import Path
from typing import Any

from git_trust import trusted_git_env


GOVERNANCE_PATH = ".claude-auto/governance.json"
MAX_GOVERNANCE_BYTES = 2 * 1024 * 1024
MAX_AUTHORITY_SETS = 256
MAX_AUTHORITY_MEMBERS = 20_000
MAX_CONTROL_SURFACES = 4_096
MAX_HELPERS_PER_SET = 1_024
MAX_TASK_SOURCES = 4_096
MAX_ARGV_ITEMS = 256
MAX_HELPER_INPUTS = 4_096
MAX_HELPER_OUTPUTS = 4_096
MAX_PATH_LENGTH = 4_096

_ROLE_VALUES = {"source", "manifest", "task_ledger", "traceability", "contract", "projection", "other"}
_REPAIR_VALUES = {"immutable", "repairable", "generated"}
_TASK_SOURCE_KINDS = {"json", "jsonl", "toml", "static", "adapter"}
_SET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class GovernanceContractError(ValueError):
    pass


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def normalise_repo_selector(raw: str, *, allow_dot: bool = False) -> str:
    if not isinstance(raw, str) or not raw:
        raise GovernanceContractError("repository path selector must be a non-empty string")
    if len(raw) > MAX_PATH_LENGTH:
        raise GovernanceContractError("repository path selector is too long")
    if "\\" in raw:
        raise GovernanceContractError("repository path selectors must use POSIX '/' separators")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise GovernanceContractError("repository path selector contains control characters")
    text = unicodedata.normalize("NFC", raw)
    if text.startswith("/"):
        raise GovernanceContractError("repository path selector must be relative")
    parts = text.split("/")
    if any(part == ".." for part in parts):
        raise GovernanceContractError("repository path selector must not contain '..'")
    if any(part == "" for part in parts):
        raise GovernanceContractError("repository path selector contains an empty path segment")
    if text == ".":
        if allow_dot:
            return text
        raise GovernanceContractError("repository path selector must not be '.'")
    if text.startswith("./"):
        raise GovernanceContractError("repository path selector must not start with './'")
    return text


def _json_no_duplicates(text: str) -> Any:
    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise GovernanceContractError(f"duplicate JSON key is not allowed: {key!r}")
            out[key] = value
        return out

    try:
        return json.loads(text, object_pairs_hook=hook)
    except GovernanceContractError:
        raise
    except json.JSONDecodeError as exc:
        raise GovernanceContractError(f"invalid governance JSON: {exc.msg}") from exc


def _expect_exact_keys(obj: dict[str, Any], required: set[str], *, where: str) -> None:
    actual = set(obj)
    if actual != required:
        missing = sorted(required - actual)
        unknown = sorted(actual - required)
        detail: list[str] = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if unknown:
            detail.append("unknown " + ", ".join(unknown))
        raise GovernanceContractError(f"{where} fields are invalid: {'; '.join(detail)}")


def _validate_string_list(value: Any, *, where: str, maximum: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise GovernanceContractError(f"{where} must be a bounded list")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise GovernanceContractError(f"{where} entries must be non-empty strings")
        out.append(item)
    return out


def _validate_capabilities(value: Any, *, where: str) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        raise GovernanceContractError(f"{where} must be an object")
    _expect_exact_keys(value, {"network", "read_external"}, where=where)
    network = _validate_string_list(value["network"], where=f"{where}.network", maximum=512)
    read_external = _validate_string_list(value["read_external"], where=f"{where}.read_external", maximum=512)
    return {"network": network, "read_external": read_external}


def _validate_helper(value: Any, *, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GovernanceContractError(f"{where} must be an object")
    required = {"id", "argv", "cwd", "inputs", "outputs", "timeout_seconds", "capabilities"}
    _expect_exact_keys(value, required, where=where)
    helper_id = value["id"]
    if not isinstance(helper_id, str) or not _SET_ID_RE.fullmatch(helper_id):
        raise GovernanceContractError(f"{where}.id must be a compact stable identifier")
    argv = _validate_string_list(value["argv"], where=f"{where}.argv", maximum=MAX_ARGV_ITEMS)
    if not argv:
        raise GovernanceContractError(f"{where}.argv must not be empty")
    cwd = value["cwd"]
    if cwd != ".":
        cwd = normalise_repo_selector(cwd)
    inputs = [
        normalise_repo_selector(item)
        for item in _validate_string_list(value["inputs"], where=f"{where}.inputs", maximum=MAX_HELPER_INPUTS)
    ]
    outputs = [
        normalise_repo_selector(item)
        for item in _validate_string_list(value["outputs"], where=f"{where}.outputs", maximum=MAX_HELPER_OUTPUTS)
    ]
    timeout = value["timeout_seconds"]
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1 or timeout > 86_400:
        raise GovernanceContractError(f"{where}.timeout_seconds must be an integer from 1 to 86400")
    return {
        "id": helper_id,
        "argv": argv,
        "cwd": cwd,
        "inputs": inputs,
        "outputs": outputs,
        "timeout_seconds": timeout,
        "capabilities": _validate_capabilities(value["capabilities"], where=f"{where}.capabilities"),
    }


def _validate_helpers(value: Any, *, where: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_HELPERS_PER_SET:
        raise GovernanceContractError(f"{where} must be a bounded list")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        helper = _validate_helper(raw, where=f"{where}[{index}]")
        if helper["id"] in seen:
            raise GovernanceContractError(f"duplicate helper id in {where}: {helper['id']!r}")
        seen.add(helper["id"])
        out.append(helper)
    return out


def _validate_member(value: Any, *, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GovernanceContractError(f"{where} must be an object")
    _expect_exact_keys(value, {"path", "role", "repair", "required"}, where=where)
    path = normalise_repo_selector(value["path"])
    role = value["role"]
    repair = value["repair"]
    required = value["required"]
    if role not in _ROLE_VALUES:
        raise GovernanceContractError(f"{where}.role is unsupported")
    if repair not in _REPAIR_VALUES:
        raise GovernanceContractError(f"{where}.repair is unsupported")
    if not isinstance(required, bool):
        raise GovernanceContractError(f"{where}.required must be boolean")
    return {"path": path, "role": role, "repair": repair, "required": required}


def _validate_authority_set(value: Any, *, index: int) -> dict[str, Any]:
    where = f"planning_authority.sets[{index}]"
    if not isinstance(value, dict):
        raise GovernanceContractError(f"{where} must be an object")
    _expect_exact_keys(value, {"id", "members", "validators", "reconcilers"}, where=where)
    set_id = value["id"]
    if not isinstance(set_id, str) or not _SET_ID_RE.fullmatch(set_id):
        raise GovernanceContractError(f"{where}.id must match {_SET_ID_RE.pattern}")
    members_raw = value["members"]
    if not isinstance(members_raw, list) or len(members_raw) > MAX_AUTHORITY_MEMBERS:
        raise GovernanceContractError(f"{where}.members must be a bounded list")
    members = [_validate_member(item, where=f"{where}.members[{i}]") for i, item in enumerate(members_raw)]
    return {
        "id": set_id,
        "members": members,
        "validators": _validate_helpers(value["validators"], where=f"{where}.validators"),
        "reconcilers": _validate_helpers(value["reconcilers"], where=f"{where}.reconcilers"),
    }


def _validate_task_source(value: Any, *, index: int) -> dict[str, Any]:
    where = f"tasks.sources[{index}]"
    if not isinstance(value, dict):
        raise GovernanceContractError(f"{where} must be an object")
    kind = value.get("kind")
    if kind not in _TASK_SOURCE_KINDS:
        raise GovernanceContractError(f"{where}.kind must be one of {sorted(_TASK_SOURCE_KINDS)}")
    # P1 records task-source declarations but does not execute them. Preserve the
    # complete inert JSON object in the snapshot so P2 can tighten/interpret the
    # source protocol without silently losing security-relevant declaration data.
    try:
        canonical_json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise GovernanceContractError(f"{where} must contain JSON-compatible data") from exc
    return value


def validate_governance_contract(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GovernanceContractError("governance contract must be a JSON object")
    _expect_exact_keys(
        value,
        {"schema_version", "planning_authority", "tasks", "control_surfaces"},
        where="governance contract",
    )
    if value["schema_version"] != 1:
        raise GovernanceContractError("unsupported governance schema_version; expected 1")

    planning = value["planning_authority"]
    if not isinstance(planning, dict):
        raise GovernanceContractError("planning_authority must be an object")
    _expect_exact_keys(planning, {"sets"}, where="planning_authority")
    sets_raw = planning["sets"]
    if not isinstance(sets_raw, list) or len(sets_raw) > MAX_AUTHORITY_SETS:
        raise GovernanceContractError("planning_authority.sets must be a bounded list")
    sets = [_validate_authority_set(item, index=i) for i, item in enumerate(sets_raw)]
    seen_sets: set[str] = set()
    for authority_set in sets:
        if authority_set["id"] in seen_sets:
            raise GovernanceContractError(f"duplicate authority-set id: {authority_set['id']!r}")
        seen_sets.add(authority_set["id"])

    tasks = value["tasks"]
    if not isinstance(tasks, dict):
        raise GovernanceContractError("tasks must be an object")
    _expect_exact_keys(tasks, {"sources", "execution_mode", "strict_dependencies"}, where="tasks")
    sources_raw = tasks["sources"]
    if not isinstance(sources_raw, list) or len(sources_raw) > MAX_TASK_SOURCES:
        raise GovernanceContractError("tasks.sources must be a bounded list")
    sources = [_validate_task_source(item, index=i) for i, item in enumerate(sources_raw)]
    if tasks["execution_mode"] != "single-writer":
        raise GovernanceContractError("governance v1 supports only tasks.execution_mode='single-writer'")
    if not isinstance(tasks["strict_dependencies"], bool):
        raise GovernanceContractError("tasks.strict_dependencies must be boolean")

    surfaces = _validate_string_list(
        value["control_surfaces"],
        where="control_surfaces",
        maximum=MAX_CONTROL_SURFACES,
    )
    surfaces = [normalise_repo_selector(item) for item in surfaces]

    return {
        "schema_version": 1,
        "planning_authority": {"sets": sets},
        "tasks": {
            "sources": sources,
            "execution_mode": "single-writer",
            "strict_dependencies": tasks["strict_dependencies"],
        },
        "control_surfaces": surfaces,
    }


def _git(root: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        text=text,
        capture_output=True,
        env=trusted_git_env(root),
    )


def _exact_tree_entry(root: Path, commit: str, path: str) -> tuple[str, str, str] | None:
    cp = _git(root, "ls-tree", "-z", commit, "--", path)
    if cp.returncode != 0:
        raise GovernanceContractError("unable to inspect governance contract in Git tree")
    rows = [row for row in cp.stdout.split("\0") if row]
    exact: list[tuple[str, str, str]] = []
    for row in rows:
        try:
            meta, listed = row.split("\t", 1)
            mode, kind, object_id = meta.split(" ", 2)
        except ValueError as exc:
            raise GovernanceContractError("unexpected Git tree record for governance contract") from exc
        if listed == path:
            exact.append((mode, kind, object_id))
    if not exact:
        return None
    if len(exact) != 1:
        raise GovernanceContractError("governance contract Git tree entry is ambiguous")
    return exact[0]


def _assert_contract_worktree_not_divergent(root: Path, commit: str) -> None:
    cp = _git(root, "diff", "--quiet", "--no-ext-diff", "--no-textconv", commit, "--", GOVERNANCE_PATH)
    if cp.returncode == 1:
        raise GovernanceContractError(
            "working tree/index governance contract differs from the exact committed governance authority"
        )
    if cp.returncode != 0:
        raise GovernanceContractError("unable to verify governance contract working-tree identity")


def load_governance_contract(root: Path, ref: str = "HEAD") -> dict[str, Any] | None:
    root = root.expanduser().resolve()
    rev = _git(root, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if rev.returncode != 0 or not rev.stdout.strip():
        raise GovernanceContractError(f"unable to resolve governance Git commit: {ref}")
    commit = rev.stdout.strip().lower()
    entry = _exact_tree_entry(root, commit, GOVERNANCE_PATH)
    if entry is None:
        return None
    mode, kind, blob = entry
    if kind != "blob" or mode not in {"100644", "100755"}:
        raise GovernanceContractError("governance contract must be a tracked regular non-symlink file")

    cat = _git(root, "cat-file", "blob", blob, text=False)
    if cat.returncode != 0:
        raise GovernanceContractError("unable to read governance contract blob")
    payload = bytes(cat.stdout)
    if len(payload) > MAX_GOVERNANCE_BYTES:
        raise GovernanceContractError("governance contract exceeds maximum supported size")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GovernanceContractError("governance contract must be UTF-8") from exc

    if ref == "HEAD":
        _assert_contract_worktree_not_divergent(root, commit)

    contract = validate_governance_contract(_json_no_duplicates(text))
    contract["_git"] = {
        "commit": commit,
        "blob": blob.lower(),
        "mode": mode,
        "path": GOVERNANCE_PATH,
    }
    return contract
