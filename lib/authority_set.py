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
import shlex
import subprocess
import unicodedata
from pathlib import Path
from typing import Any

from git_trust import trusted_git_env
from governance_contract import (
    GOVERNANCE_PATH,
    GovernanceContractError,
    canonical_json_bytes,
    load_governance_contract,
    normalise_repo_selector,
)
from repo_identity import repo_state_dir, repository_identity
from state_store import load_json


MAX_RESOLVED_AUTHORITY_MEMBERS = 100_000

# These are semantic control surfaces only when repository governance is active.
# Legacy single-plan compatibility intentionally preserves RC3's narrower
# behaviour until a repository opts into the RC4 governance contract.
_MANDATORY_CONTROL_SELECTORS = (
    GOVERNANCE_PATH,
    ".claude-auto/verification.json",
    ".mcp.json",
    ".gitattributes",
    ".gitmodules",
    "CLAUDE.md",
    "**/CLAUDE.md",
    ".claude/**",
    ".github/workflows/**",
    ".gitlab-ci.yml",
    "Jenkinsfile",
    "azure-pipelines.yml",
    "bitbucket-pipelines.yml",
    ".circleci/**",
    ".buildkite/**",
)


class AuthoritySetError(ValueError):
    pass


def _git(root: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        text=text,
        capture_output=True,
        env=trusted_git_env(root),
    )


def _rev(root: Path, ref: str) -> str:
    cp = _git(root, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if cp.returncode != 0 or not cp.stdout.strip():
        raise AuthoritySetError(f"unable to resolve authority Git commit: {ref}")
    return cp.stdout.strip().lower()


def _branch(root: Path) -> str | None:
    cp = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if cp.returncode == 0 and cp.stdout.strip():
        return cp.stdout.strip()
    if cp.returncode == 1:
        return None
    raise AuthoritySetError("unable to determine repository branch")


def _tree(root: Path, commit: str) -> dict[str, dict[str, str]]:
    cp = _git(root, "ls-tree", "-rz", "--full-tree", commit, text=False)
    if cp.returncode != 0:
        raise AuthoritySetError("unable to enumerate exact Git tree")
    entries: dict[str, dict[str, str]] = {}
    payload = bytes(cp.stdout)
    for raw in payload.split(b"\0"):
        if not raw:
            continue
        try:
            meta, raw_path = raw.split(b"\t", 1)
            mode_b, kind_b, object_b = meta.split(b" ", 2)
            path = raw_path.decode("utf-8", errors="surrogateescape")
            mode = mode_b.decode("ascii")
            kind = kind_b.decode("ascii")
            object_id = object_b.decode("ascii").lower()
        except (ValueError, UnicodeError) as exc:
            raise AuthoritySetError("unexpected Git tree record") from exc
        entries[path] = {"path": path, "mode": mode, "type": kind, "object": object_id}
    return entries


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


def _has_pattern(selector: str) -> bool:
    return "*" in selector or "?" in selector


def _resolve_selector(
    selector: str,
    tree: dict[str, dict[str, str]],
    *,
    required: bool,
) -> list[dict[str, str]]:
    try:
        selector = normalise_repo_selector(selector)
    except GovernanceContractError as exc:
        raise AuthoritySetError(str(exc)) from exc
    if _has_pattern(selector):
        matcher = _selector_regex(selector)
        matches = [entry for path, entry in tree.items() if matcher.fullmatch(unicodedata.normalize("NFC", path))]
    else:
        entry = tree.get(selector)
        matches = [entry] if entry is not None else []
    matches = [entry for entry in matches if entry is not None]
    if required and not matches:
        raise AuthoritySetError(f"required authority selector resolved to zero tracked files: {selector!r}")
    if len(matches) > MAX_RESOLVED_AUTHORITY_MEMBERS:
        raise AuthoritySetError(f"authority selector resolves to too many files: {selector!r}")
    return sorted(matches, key=lambda item: item["path"].encode("utf-8", errors="surrogateescape"))


def _assert_authority_blob_is_materialised(
    root: Path,
    entry: dict[str, str],
) -> None:
    """Reject canonical Git LFS pointers as unresolved authority content."""
    if entry.get("type") != "blob":
        return
    cp = _git(root, "cat-file", "blob", entry["object"], text=False)
    if cp.returncode != 0:
        raise AuthoritySetError(
            f"unable to read authority member blob: {entry['path']}"
        )
    payload = bytes(cp.stdout)
    if len(payload) > 8192:
        return
    try:
        text_value = payload.decode("utf-8")
    except UnicodeDecodeError:
        return
    lines = text_value.splitlines()
    if not lines or lines[0] != "version https://git-lfs.github.com/spec/v1":
        return
    has_oid = any(
        re.fullmatch(r"oid sha256:[0-9a-fA-F]{64}", line) is not None
        for line in lines[1:]
    )
    has_size = any(
        re.fullmatch(r"size [0-9]+", line) is not None
        for line in lines[1:]
    )
    if has_oid and has_size:
        raise AuthoritySetError(
            "authority member is an unresolved Git LFS pointer rather than "
            f"materialised authority content: {entry['path']}"
        )


def _member_entry(entry: dict[str, str], member: dict[str, Any]) -> dict[str, Any]:
    mode = entry["mode"]
    kind = entry["type"]
    if mode == "120000":
        raise AuthoritySetError(f"authority member must not be a symlink: {entry['path']}")
    if mode == "160000" or kind == "commit":
        raise AuthoritySetError(f"authority member crosses a gitlink/submodule boundary: {entry['path']}")
    if kind != "blob" or mode not in {"100644", "100755"}:
        raise AuthoritySetError(f"authority member must be a regular Git blob: {entry['path']}")
    return {
        "path": unicodedata.normalize("NFC", entry["path"]),
        "role": member["role"],
        "repair": member["repair"],
        "required": bool(member["required"]),
        "git_mode": mode,
        "blob": entry["object"],
    }


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _legacy_policy(root: Path) -> dict[str, Any]:
    policy = load_json(repo_state_dir(root) / "planning-repair" / "policy.json", {})
    if not isinstance(policy, dict) or not policy:
        return {}
    raw = policy.get("canonical_plan")
    if not isinstance(raw, str) or not raw.strip():
        raise AuthoritySetError("legacy planning policy is present but canonical_plan is invalid")
    try:
        canonical = normalise_repo_selector(raw)
    except GovernanceContractError as exc:
        raise AuthoritySetError(f"legacy canonical plan is invalid: {exc}") from exc
    out = dict(policy)
    out["canonical_plan"] = canonical
    return out


def _dirty_tracked_paths(root: Path, commit: str) -> set[str]:
    cp = _git(
        root,
        "diff",
        "--name-only",
        "-z",
        "--no-ext-diff",
        "--no-textconv",
        commit,
        "--",
    )
    if cp.returncode != 0:
        raise AuthoritySetError("unable to inspect tracked working-tree/index divergence")
    return {unicodedata.normalize("NFC", path) for path in cp.stdout.split("\0") if path}


def _helper_control_selectors(contract: dict[str, Any]) -> list[str]:
    selectors: set[str] = set()
    for authority_set in contract["planning_authority"]["sets"]:
        for category in ("validators", "reconcilers"):
            for helper in authority_set[category]:
                for raw in helper.get("inputs", []):
                    selectors.add(raw)
                argv = helper.get("argv", [])
                if argv:
                    candidate = argv[0]
                    if (
                        isinstance(candidate, str)
                        and candidate not in {"python", "python3", "bash", "sh", "node", "ruby", "perl"}
                        and not candidate.startswith("-")
                        and not candidate.startswith("/")
                        and "/" in candidate
                    ):
                        try:
                            selectors.add(normalise_repo_selector(candidate))
                        except GovernanceContractError:
                            pass
                if len(argv) >= 2:
                    candidate = argv[1]
                    if isinstance(candidate, str) and not candidate.startswith("-") and not candidate.startswith("/"):
                        try:
                            selectors.add(normalise_repo_selector(candidate))
                        except GovernanceContractError:
                            pass
    for source in contract["tasks"]["sources"]:
        for key in ("paths", "inputs"):
            raw = source.get(key)
            values = raw if isinstance(raw, list) else [raw]
            for item in values:
                if isinstance(item, str) and item:
                    try:
                        selectors.add(normalise_repo_selector(item))
                    except GovernanceContractError:
                        pass
        if source.get("kind") == "adapter":
            argv = source.get("argv")
            if isinstance(argv, list):
                for candidate in argv[:2]:
                    if isinstance(candidate, str) and candidate and not candidate.startswith("-") and not candidate.startswith("/"):
                        try:
                            selectors.add(normalise_repo_selector(candidate))
                        except GovernanceContractError:
                            pass
    return sorted(selectors)


def _helper_executable_selectors(contract: dict[str, Any]) -> list[str]:
    selectors: set[str] = set()
    for authority_set in contract["planning_authority"]["sets"]:
        for category in ("validators", "reconcilers"):
            for helper in authority_set[category]:
                argv = helper.get("argv", [])
                for candidate in argv[:2]:
                    if (
                        isinstance(candidate, str)
                        and candidate
                        and not candidate.startswith("-")
                        and not candidate.startswith("/")
                        and candidate not in {
                            "python", "python3", "bash", "sh", "node",
                            "ruby", "perl",
                        }
                    ):
                        try:
                            normalised = normalise_repo_selector(candidate)
                        except GovernanceContractError:
                            continue
                        if "/" in normalised:
                            selectors.add(normalised)
    for source in contract["tasks"]["sources"]:
        if source.get("kind") != "adapter":
            continue
        argv = source.get("argv")
        if not isinstance(argv, list):
            continue
        for candidate in argv[:2]:
            if (
                isinstance(candidate, str)
                and candidate
                and not candidate.startswith("-")
                and not candidate.startswith("/")
            ):
                try:
                    normalised = normalise_repo_selector(candidate)
                except GovernanceContractError:
                    continue
                if "/" in normalised:
                    selectors.add(normalised)
    return sorted(selectors)


def _blob_text(root: Path, object_id: str, *, maximum: int = 2 * 1024 * 1024) -> str:
    size = _git(root, "cat-file", "-s", object_id)
    if size.returncode != 0:
        raise AuthoritySetError(f"unable to inspect control-surface blob {object_id}")
    try:
        count = int(size.stdout.strip())
    except ValueError as exc:
        raise AuthoritySetError(f"invalid control-surface blob size for {object_id}") from exc
    if count < 0 or count > maximum:
        raise AuthoritySetError(f"control-surface blob {object_id} exceeds maximum supported size")
    cp = _git(root, "cat-file", "blob", object_id, text=False)
    if cp.returncode != 0:
        raise AuthoritySetError(f"unable to read control-surface blob {object_id}")
    payload = bytes(cp.stdout)
    if len(payload) != count:
        raise AuthoritySetError(f"control-surface blob {object_id} changed size during read")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AuthoritySetError("control-surface JSON must be UTF-8") from exc


def _verification_control_selectors(
    root: Path,
    tree: dict[str, dict[str, str]],
) -> list[str]:
    entry = tree.get(".claude-auto/verification.json")
    if entry is None:
        return []
    if entry["mode"] == "120000" or entry["type"] != "blob":
        raise AuthoritySetError(".claude-auto/verification.json must be a regular committed blob")
    try:
        obj = json.loads(_blob_text(root, entry["object"]))
    except json.JSONDecodeError as exc:
        raise AuthoritySetError(f"invalid committed verification contract JSON: {exc.msg}") from exc
    commands = obj.get("commands") if isinstance(obj, dict) else None
    if not isinstance(commands, dict):
        return []

    selectors: set[str] = set()

    def inspect_command(command: str, *, depth: int = 0) -> None:
        if depth > 3:
            return
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            return
        if not tokens:
            return

        # Shell -c wrappers carry a second command language inside one argv item.
        exe = Path(tokens[0]).name
        if exe in {"bash", "sh", "zsh", "dash"} and "-c" in tokens:
            try:
                index = tokens.index("-c")
                nested = tokens[index + 1]
            except (ValueError, IndexError):
                nested = ""
            if nested:
                inspect_command(nested, depth=depth + 1)

        for token in tokens:
            if not token or token.startswith("-") or token in {"&&", "||", ";", "|"}:
                continue
            # Strip the common command-prefix spelling while keeping all
            # repository-relative path semantics package-owned.
            candidate = token[2:] if token.startswith("./") else token
            if candidate.startswith("/") or candidate.startswith("~"):
                continue
            try:
                normalised = normalise_repo_selector(candidate)
            except GovernanceContractError:
                continue
            if normalised in tree and tree[normalised]["type"] == "blob":
                selectors.add(normalised)

    for rows in commands.values():
        if not isinstance(rows, list):
            continue
        for command in rows:
            if isinstance(command, str) and command.strip():
                inspect_command(command.strip())

    return sorted(selectors)


def _resolve_control_surfaces(
    root: Path,
    contract: dict[str, Any],
    tree: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    selectors = set(_MANDATORY_CONTROL_SELECTORS)
    selectors.update(contract.get("control_surfaces", []))
    selectors.update(_helper_control_selectors(contract))
    selectors.update(_verification_control_selectors(root, tree))
    records: dict[str, dict[str, str]] = {}
    for selector in sorted(selectors):
        for entry in _resolve_selector(selector, tree, required=False):
            path = unicodedata.normalize("NFC", entry["path"])
            if entry["mode"] == "160000" or entry["type"] == "commit":
                # The gitlink itself is a control surface, but never descend into it.
                records[path] = {"path": path, "git_mode": entry["mode"], "object": entry["object"]}
                continue
            records[path] = {"path": path, "git_mode": entry["mode"], "object": entry["object"]}
    return [records[path] for path in sorted(records)]


def planning_repair_control_selectors(
    root: Path,
    ref: str = "HEAD",
) -> list[str]:
    """Selectors for control/config/executable state P5 may never edit directly."""
    root = root.expanduser().resolve()
    commit = _rev(root, ref)
    tree = _tree(root, commit)
    try:
        contract = load_governance_contract(root, commit)
    except GovernanceContractError as exc:
        raise AuthoritySetError(str(exc)) from exc
    if contract is None:
        return []

    selectors = set(_MANDATORY_CONTROL_SELECTORS)
    selectors.update(contract.get("control_surfaces", []))
    selectors.update(_helper_executable_selectors(contract))
    # Verification command discovery yields exact existing repository paths.
    selectors.update(_verification_control_selectors(root, tree))
    return sorted(selectors)


def planning_repair_control_records(
    root: Path,
    ref: str = "HEAD",
) -> list[dict[str, str]]:
    """Exact immutable control/executable records for P5 Planning Repair.

    P1's broader control_surfaces intentionally includes helper/task-source data
    inputs so ordinary product workers cannot mutate them. P5 may repair those
    data inputs when they are selected repairable planning members, so P5 must
    compare only governance/control/executable/verification surfaces here.
    """
    root = root.expanduser().resolve()
    commit = _rev(root, ref)
    tree = _tree(root, commit)
    selectors = planning_repair_control_selectors(root, commit)
    records: dict[str, dict[str, str]] = {}
    for selector in selectors:
        for entry in _resolve_selector(selector, tree, required=False):
            path = unicodedata.normalize("NFC", entry["path"])
            records[path] = {
                "path": path,
                "git_mode": entry["mode"],
                "object": entry["object"],
            }
    return [records[path] for path in sorted(records)]


def planning_repair_control_digest(
    root: Path,
    ref: str = "HEAD",
) -> str:
    return _digest(planning_repair_control_records(root, ref))


def planning_repair_control_paths(
    root: Path,
    ref: str = "HEAD",
) -> list[str]:
    """Control/config/executable paths Planning Repair may never edit directly.

    P1 intentionally protects helper/task-source data inputs from product workers.
    P5 Planning Repair may legitimately repair those data inputs when they are
    selected repairable planning members, so this narrower set excludes data
    inputs while retaining governance, verification and executable controls.
    """
    root = root.expanduser().resolve()
    commit = _rev(root, ref)
    tree = _tree(root, commit)
    selectors = planning_repair_control_selectors(root, commit)
    paths: set[str] = set()
    for selector in selectors:
        for entry in _resolve_selector(selector, tree, required=False):
            paths.add(unicodedata.normalize("NFC", entry["path"]))
    return sorted(paths)


def _build_contract_sets(
    root: Path,
    contract: dict[str, Any],
    tree: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    total = 0
    for declared in contract["planning_authority"]["sets"]:
        resolved: list[dict[str, Any]] = []
        seen_exact: set[str] = set()
        seen_folded: dict[str, str] = {}
        for member in declared["members"]:
            for entry in _resolve_selector(member["path"], tree, required=bool(member["required"])):
                _assert_authority_blob_is_materialised(root, entry)
                record = _member_entry(entry, member)
                path = record["path"]
                if path in seen_exact:
                    raise AuthoritySetError(
                        f"authority selectors overlap ambiguously in set {declared['id']!r}: {path!r}"
                    )
                folded = path.casefold()
                prior = seen_folded.get(folded)
                if prior is not None and prior != path:
                    raise AuthoritySetError(
                        f"case/Unicode-colliding authority members in set {declared['id']!r}: {prior!r}, {path!r}"
                    )
                seen_exact.add(path)
                seen_folded[folded] = path
                resolved.append(record)
                total += 1
                if total > MAX_RESOLVED_AUTHORITY_MEMBERS:
                    raise AuthoritySetError("resolved AuthoritySet membership exceeds safety limit")
        out.append({
            "id": declared["id"],
            "members": sorted(resolved, key=lambda item: item["path"]),
            "validator_digest": _digest(declared["validators"]),
            "reconciler_digest": _digest(declared["reconcilers"]),
        })
    return sorted(out, key=lambda item: item["id"])


def _legacy_set(
    root: Path,
    policy: dict[str, Any],
    tree: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    canonical = policy["canonical_plan"]
    matches = _resolve_selector(canonical, tree, required=True)
    if len(matches) != 1:
        raise AuthoritySetError("legacy canonical plan must resolve to exactly one tracked file")
    _assert_authority_blob_is_materialised(root, matches[0])
    record = _member_entry(
        matches[0],
        {"role": "source", "repair": "repairable", "required": True},
    )
    return [{
        "id": "default",
        "members": [record],
        "validator_digest": _digest([]),
        "reconciler_digest": _digest([]),
    }]


def _assert_legacy_compatible(policy: dict[str, Any], sets: list[dict[str, Any]]) -> None:
    canonical = policy["canonical_plan"]
    matches = [
        member
        for authority_set in sets
        for member in authority_set["members"]
        if member["path"] == canonical
    ]
    if not matches:
        raise AuthoritySetError(
            "legacy canonical planning policy and repository governance contract define divergent authorities"
        )
    if not any(member["role"] == "source" and member["repair"] == "repairable" for member in matches):
        raise AuthoritySetError(
            "legacy canonical plan must remain a repairable source member when both governance systems are present"
        )


def build_authority_snapshot(root: Path, ref: str = "HEAD") -> dict[str, Any] | None:
    root = root.expanduser().resolve()
    legacy = _legacy_policy(root)

    # Preserve RC3 behaviour for an ordinary freshly-initialised/unborn Git
    # repository. Repository governance is defined only by committed Git-tree
    # truth; without a commit there cannot be a committed governance contract.
    # A legacy policy, however, explicitly claims tracked planning authority and
    # therefore must fail closed until that authority can be resolved.
    try:
        commit = _rev(root, ref)
    except AuthoritySetError:
        if not legacy:
            return None
        raise

    tree = _tree(root, commit)
    try:
        contract = load_governance_contract(root, ref)
    except GovernanceContractError as exc:
        raise AuthoritySetError(str(exc)) from exc

    if contract is None and not legacy:
        return None

    if contract is not None:
        sets = _build_contract_sets(root, contract, tree)
        if legacy:
            _assert_legacy_compatible(legacy, sets)
        controls = _resolve_control_surfaces(root, contract, tree)
        task_source_digest = _digest(contract["tasks"])
        governance_blob = contract["_git"]["blob"]
        source_mode = "contract+legacy" if legacy else "contract"
    else:
        sets = _legacy_set(root, legacy, tree)
        controls = []
        task_source_digest = _digest({})
        governance_blob = None
        source_mode = "legacy"

    authority_paths = {
        member["path"]
        for authority_set in sets
        for member in authority_set["members"]
    }
    control_paths = {item["path"] for item in controls}
    protected_paths = sorted(authority_paths | control_paths)

    if ref == "HEAD":
        dirty = _dirty_tracked_paths(root, commit)
        overlapping = sorted(dirty & set(protected_paths))
        if overlapping:
            raise AuthoritySetError(
                "authority/control-surface working tree differs from the exact committed snapshot: "
                + ", ".join(overlapping[:40])
            )

    identity = repository_identity(root)
    snapshot = {
        "schema_version": 1,
        "repository_id": identity["key"],
        "product_head": commit,
        "branch": _branch(root) if ref == "HEAD" else None,
        "governance_blob": governance_blob,
        "source_mode": source_mode,
        "sets": sets,
        "task_source_contract_digest": task_source_digest,
        "control_surfaces": controls,
        "control_surface_digest": _digest(controls),
        "protected_paths": protected_paths,
    }
    snapshot["snapshot_sha256"] = _digest(snapshot)
    return snapshot


_AUTHORITY_CONTENT_KEYS = (
    "governance_blob",
    "source_mode",
    "sets",
    "task_source_contract_digest",
    "control_surfaces",
    "control_surface_digest",
    "protected_paths",
)


def authority_content_record(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return worktree-independent semantic planning authority.

    P1 snapshot_sha256 intentionally binds repository/worktree identity and,
    for HEAD, branch identity. P5 needs a second digest that compares the
    semantic planning content of one exact Git tree across coordinator,
    planning-repair and verifier worktrees without weakening P1 identity.
    """
    if not isinstance(snapshot, dict):
        raise AuthoritySetError("authority snapshot must be an object")
    missing = [key for key in _AUTHORITY_CONTENT_KEYS if key not in snapshot]
    if missing:
        raise AuthoritySetError(
            "authority snapshot is incomplete for content identity: "
            + ", ".join(missing)
        )
    if snapshot.get("schema_version") != 1:
        raise AuthoritySetError("unsupported authority snapshot schema")
    return {
        "schema_version": 1,
        **{key: snapshot[key] for key in _AUTHORITY_CONTENT_KEYS},
    }


def authority_content_sha256(snapshot: dict[str, Any]) -> str:
    return _digest(authority_content_record(snapshot))


def authority_status(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    try:
        snapshot = build_authority_snapshot(root)
    except (AuthoritySetError, OSError) as exc:
        return {
            "repository": str(root),
            "status": "BLOCKED",
            "error": str(exc),
        }
    if snapshot is None:
        return {
            "repository": str(root),
            "status": "UNCONFIGURED",
            "snapshot": None,
        }
    return {
        "repository": str(root),
        "status": "READY",
        "snapshot": snapshot,
    }


def governance_action(args: Any, *, find_repo_root) -> int:
    root = find_repo_root(getattr(args, "repo", None))
    result = authority_status(root)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2 if result.get("status") == "BLOCKED" else 0
