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
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from authority_set import AuthoritySetError, _resolve_selector, _tree
from execution import run_repository_command
from git_trust import trusted_git_env
from governance_contract import canonical_json_bytes
from repair_envelope import generated_repair_path_reason
from task_spec import selector_matches_path


MAX_HELPER_PROCESS_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_GENERATED_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_GENERATED_OUTPUT_FILES = 10_000


class PlanningHelperError(ValueError):
    pass


HelperRunner = Callable[..., dict[str, Any]]


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _git(
    root: Path,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        env=trusted_git_env(root),
    )


def _visible_files(root: Path) -> list[str]:
    cp = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        capture_output=True,
        env=trusted_git_env(root),
    )
    if cp.returncode != 0:
        detail = bytes(cp.stderr or cp.stdout or b"").decode(
            "utf-8", errors="replace"
        )
        raise PlanningHelperError(
            f"unable to enumerate planning helper view: {detail[:1600]}"
        )
    out: list[str] = []
    for raw in bytes(cp.stdout).split(b"\0"):
        if not raw:
            continue
        try:
            rel = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PlanningHelperError(
                "planning helpers require UTF-8 repository paths"
            ) from exc
        path = root / rel
        try:
            st = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(st.st_mode):
            out.append(rel)
    return sorted(set(out), key=lambda item: item.encode("utf-8"))


def _resolve_selector_files(
    root: Path,
    selectors: list[str],
    *,
    require_each: bool,
) -> list[str]:
    visible = _visible_files(root)
    matched: set[str] = set()
    for selector in selectors:
        current = [
            rel for rel in visible
            if selector_matches_path(selector, rel)
        ]
        if require_each and not current:
            raise PlanningHelperError(
                f"planning helper input selector resolves no current file: {selector}"
            )
        matched.update(current)
    return sorted(matched, key=lambda item: item.encode("utf-8"))


def _regular_file_record(path: Path, rel: str) -> dict[str, Any]:
    try:
        st = path.lstat()
    except FileNotFoundError as exc:
        raise PlanningHelperError(
            f"planning helper file disappeared during materialisation: {rel}"
        ) from exc
    if stat.S_ISLNK(st.st_mode):
        raise PlanningHelperError(
            f"planning helper view must not materialise symlinks: {rel}"
        )
    if not stat.S_ISREG(st.st_mode):
        raise PlanningHelperError(
            f"planning helper view requires regular files: {rel}"
        )
    payload = path.read_bytes()
    return {
        "path": rel,
        "kind": "file",
        "git_mode": "100755" if st.st_mode & 0o111 else "100644",
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _materialise_file(source: Path, target: Path, mode: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target, follow_symlinks=False)
    target.chmod(0o755 if mode == "100755" else 0o644)


def _scan_view(view: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}

    def walk(directory: Path) -> None:
        try:
            entries = sorted(
                os.scandir(directory),
                key=lambda item: os.fsencode(item.name),
            )
        except OSError as exc:
            raise PlanningHelperError(
                f"unable to inspect planning helper temporary view: {exc}"
            ) from exc
        for entry in entries:
            path = Path(entry.path)
            rel = path.relative_to(view).as_posix()
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise PlanningHelperError(
                    f"unable to inspect planning helper output {rel!r}: {exc}"
                ) from exc
            if stat.S_ISDIR(st.st_mode):
                walk(path)
                continue
            if stat.S_ISREG(st.st_mode):
                payload = path.read_bytes()
                out[rel] = {
                    "kind": "file",
                    "git_mode": "100755" if st.st_mode & 0o111 else "100644",
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
                continue
            if stat.S_ISLNK(st.st_mode):
                out[rel] = {
                    "kind": "symlink",
                    "target": os.readlink(path),
                }
                continue
            out[rel] = {
                "kind": "special",
                "mode": stat.S_IFMT(st.st_mode),
            }

    walk(view)
    return out


def _changed_paths(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> list[str]:
    return sorted(
        {
            rel
            for rel in set(before) | set(after)
            if before.get(rel) != after.get(rel)
        },
        key=lambda item: item.encode("utf-8"),
    )


def _matches_any(selectors: list[str], rel: str) -> bool:
    return any(selector_matches_path(selector, rel) for selector in selectors)


def _output_state(
    *,
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    output_selectors: list[str],
) -> list[dict[str, Any]]:
    paths = {
        rel for rel in set(before) | set(after)
        if _matches_any(output_selectors, rel)
    }
    result: list[dict[str, Any]] = []
    total = 0
    for rel in sorted(paths, key=lambda item: item.encode("utf-8")):
        current = after.get(rel)
        if current is None:
            result.append({"path": rel, "state": "deleted"})
            continue
        if current.get("kind") != "file":
            raise PlanningHelperError(
                f"planning helper output is not a regular file: {rel}"
            )
        total += int(current.get("size", 0))
        if total > MAX_GENERATED_OUTPUT_BYTES:
            raise PlanningHelperError(
                "planning helper generated output exceeds maximum total size"
            )
        result.append({
            "path": rel,
            "state": "file",
            "git_mode": current["git_mode"],
            "size": current["size"],
            "sha256": current["sha256"],
        })
    if len(result) > MAX_GENERATED_OUTPUT_FILES:
        raise PlanningHelperError(
            "planning helper generated too many output files"
        )
    return result


def _run_reconciler_once(
    coordinator_root: Path,
    worktree: Path,
    envelope: dict[str, Any],
    contract: dict[str, Any],
    *,
    runner: HelperRunner,
) -> dict[str, Any]:
    helper = contract["helper"]
    set_id = str(contract["set_id"])
    caps = helper["capabilities"]
    if caps.get("network") or caps.get("read_external"):
        raise PlanningHelperError(
            f"reconciler {helper['id']!r} requests capabilities that P5 v1 does not grant"
        )
    output_selectors = list(helper["outputs"])
    if not output_selectors:
        raise PlanningHelperError(
            f"reconciler {helper['id']!r} declares no generated outputs"
        )

    input_paths = _resolve_selector_files(
        worktree,
        list(helper["inputs"]),
        require_each=True,
    )
    current_output_paths = _resolve_selector_files(
        worktree,
        output_selectors,
        require_each=False,
    )
    for rel in current_output_paths:
        denial = generated_repair_path_reason(
            coordinator_root,
            envelope,
            rel,
            set_id=set_id,
        )
        if denial:
            raise PlanningHelperError(
                f"reconciler {helper['id']!r} declares non-generated output {rel!r}: {denial}"
            )

    materialised = sorted(
        set(input_paths) | set(current_output_paths),
        key=lambda item: item.encode("utf-8"),
    )
    input_records: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(
        prefix=f"claude-auto-plan-reconciler-{helper['id']}-"
    ) as td:
        view = Path(td).resolve()
        for rel in materialised:
            record = _regular_file_record(worktree / rel, rel)
            input_records.append(record)
            _materialise_file(
                worktree / rel,
                view / rel,
                record["git_mode"],
            )

        cwd = view if helper["cwd"] == "." else (view / helper["cwd"]).resolve()
        try:
            cwd.relative_to(view)
        except ValueError as exc:
            raise PlanningHelperError(
                f"reconciler {helper['id']!r} cwd escapes temporary view"
            ) from exc
        cwd.mkdir(parents=True, exist_ok=True)

        before = _scan_view(view)
        result = runner(
            view,
            list(helper["argv"]),
            timeout=int(helper["timeout_seconds"]),
            trust_repo_scripts=False,
            unrestricted_host=False,
            read_only_root=False,
            working_directory=cwd,
            hidden_paths=[coordinator_root, worktree],
            max_output_bytes=MAX_HELPER_PROCESS_OUTPUT_BYTES,
            read_allowlist_only=True,
        )
        if int(result.get("returncode", 1)) != 0:
            detail = str(
                result.get("stderr")
                or result.get("stdout")
                or "reconciler failed"
            )
            raise PlanningHelperError(
                f"reconciler {helper['id']!r} failed under "
                f"{result.get('execution_boundary') or 'unknown'} with exit "
                f"{result.get('returncode')}: {detail[:1800]}"
            )
        if not bool(result.get("sandboxed", False)):
            raise PlanningHelperError(
                f"reconciler {helper['id']!r} did not run inside a verified isolation boundary"
            )
        if not bool(result.get("environment_scrubbed", False)):
            raise PlanningHelperError(
                f"reconciler {helper['id']!r} did not run with a scrubbed environment"
            )

        after = _scan_view(view)
        mutations = _changed_paths(before, after)
        for rel in mutations:
            if not _matches_any(output_selectors, rel):
                raise PlanningHelperError(
                    f"reconciler {helper['id']!r} mutated undeclared output path: {rel}"
                )
            denial = generated_repair_path_reason(
                coordinator_root,
                envelope,
                rel,
                set_id=set_id,
            )
            if denial:
                raise PlanningHelperError(
                    f"reconciler {helper['id']!r} output {rel!r} is not admitted generated authority: {denial}"
                )
            current = after.get(rel)
            if current is not None and current.get("kind") != "file":
                raise PlanningHelperError(
                    f"reconciler {helper['id']!r} produced non-regular output: {rel}"
                )

        outputs = _output_state(
            before=before,
            after=after,
            output_selectors=output_selectors,
        )
        for row in outputs:
            denial = generated_repair_path_reason(
                coordinator_root,
                envelope,
                row["path"],
                set_id=set_id,
            )
            if denial:
                raise PlanningHelperError(
                    f"reconciler {helper['id']!r} output {row['path']!r} is not admitted generated authority: {denial}"
                )

        payloads: dict[str, dict[str, Any]] = {}
        for row in outputs:
            rel = row["path"]
            if row["state"] == "deleted":
                payloads[rel] = {"state": "deleted"}
                continue
            payload = (view / rel).read_bytes()
            payloads[rel] = {
                "state": "file",
                "git_mode": row["git_mode"],
                "payload": payload,
            }

    semantic_output = outputs
    return {
        "helper_id": helper["id"],
        "set_id": set_id,
        "helper_contract_sha256": _digest(helper),
        "input_state_sha256": _digest(input_records),
        "input_paths": [row["path"] for row in input_records],
        "mutated_paths": mutations,
        "output_state": semantic_output,
        "output_state_sha256": _digest(semantic_output),
        "stdout_sha256": hashlib.sha256(
            str(result.get("stdout") or "").encode("utf-8")
        ).hexdigest(),
        "stderr_sha256": hashlib.sha256(
            str(result.get("stderr") or "").encode("utf-8")
        ).hexdigest(),
        "execution_boundary": result.get("execution_boundary"),
        "sandboxed": True,
        "environment_scrubbed": True,
        "_payloads": payloads,
    }


def _apply_payloads(
    worktree: Path,
    payloads: dict[str, dict[str, Any]],
) -> None:
    for rel in sorted(payloads, key=lambda item: item.encode("utf-8")):
        row = payloads[rel]
        target = worktree / rel
        if row["state"] == "deleted":
            try:
                if target.is_symlink() or target.is_file():
                    target.unlink()
                elif target.exists():
                    raise PlanningHelperError(
                        f"generated output deletion target is not a regular file: {rel}"
                    )
            except OSError as exc:
                raise PlanningHelperError(
                    f"unable to delete generated planning output {rel!r}: {exc}"
                ) from exc
            continue

        try:
            if target.is_symlink():
                raise PlanningHelperError(
                    f"generated output target became a symlink: {rel}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = bytes(row["payload"])
            fd, tmp_name = tempfile.mkstemp(
                prefix=".claude-auto-generated-",
                dir=str(target.parent),
            )
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                tmp = Path(tmp_name)
                tmp.chmod(0o755 if row["git_mode"] == "100755" else 0o644)
                os.replace(tmp, target)
            finally:
                try:
                    Path(tmp_name).unlink()
                except FileNotFoundError:
                    pass
        except OSError as exc:
            raise PlanningHelperError(
                f"unable to apply generated planning output {rel!r}: {exc}"
            ) from exc


def _dirty_paths(worktree: Path) -> set[str]:
    cp = subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ],
        capture_output=True,
        env=trusted_git_env(worktree),
    )
    if cp.returncode != 0:
        raise PlanningHelperError(
            "unable to inspect planning repair worktree before reconciliation"
        )
    fields = [item for item in bytes(cp.stdout).split(b"\0") if item]
    out: set[str] = set()
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if len(record) < 4:
            raise PlanningHelperError("unexpected Git porcelain record")
        status_code = record[:2].decode("ascii", errors="replace")
        raw_path = record[3:]
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PlanningHelperError(
                "planning reconciliation requires UTF-8 paths"
            ) from exc
        out.add(path)
        if ("R" in status_code or "C" in status_code) and index < len(fields):
            try:
                out.add(fields[index].decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise PlanningHelperError(
                    "planning reconciliation requires UTF-8 paths"
                ) from exc
            index += 1
    return out


def normalise_generated_to_base(
    coordinator_root: Path,
    worktree: Path,
    envelope: dict[str, Any],
) -> list[str]:
    """Remove partial generated state while preserving Architect repairable edits."""
    base = str(envelope["base_sha"])
    reset = _git(worktree, "reset", "-q", "--mixed", "HEAD")
    if reset.returncode != 0:
        raise PlanningHelperError(
            "unable to normalise planning repair index before reconciliation"
        )

    normalised: list[str] = []
    base_generated = set(envelope.get("generated_paths", []))
    for rel in sorted(_dirty_paths(worktree), key=lambda item: item.encode("utf-8")):
        denial = generated_repair_path_reason(
            coordinator_root,
            envelope,
            rel,
        )
        if denial:
            continue
        target = worktree / rel
        if rel in base_generated:
            cp = _git(worktree, "checkout", "-q", base, "--", rel)
            if cp.returncode != 0:
                raise PlanningHelperError(
                    f"unable to restore base generated planning member: {rel}"
                )
            normalised.append(rel)
            continue
        try:
            if target.is_symlink() or target.is_file():
                target.unlink()
                normalised.append(rel)
            elif target.exists():
                raise PlanningHelperError(
                    f"partial generated planning path is not a regular file: {rel}"
                )
        except OSError as exc:
            raise PlanningHelperError(
                f"unable to remove partial generated planning member {rel!r}: {exc}"
            ) from exc
    return normalised


def _git_blob_bytes(
    root: Path,
    object_id: str,
    *,
    maximum: int = 32 * 1024 * 1024,
) -> bytes:
    size = subprocess.run(
        ["git", "-C", str(root), "cat-file", "-s", object_id],
        text=True,
        capture_output=True,
        env=trusted_git_env(root),
    )
    if size.returncode != 0:
        raise PlanningHelperError(
            f"unable to inspect exact planning helper blob {object_id}"
        )
    try:
        count = int(size.stdout.strip())
    except ValueError as exc:
        raise PlanningHelperError(
            f"invalid exact planning helper blob size for {object_id}"
        ) from exc
    if count < 0 or count > maximum:
        raise PlanningHelperError(
            f"planning helper input blob {object_id} exceeds maximum supported size"
        )
    cp = subprocess.run(
        ["git", "-C", str(root), "cat-file", "blob", object_id],
        capture_output=True,
        env=trusted_git_env(root),
    )
    if cp.returncode != 0:
        raise PlanningHelperError(
            f"unable to read exact planning helper blob {object_id}"
        )
    payload = bytes(cp.stdout)
    if len(payload) != count:
        raise PlanningHelperError(
            f"planning helper blob {object_id} changed size during read"
        )
    return payload


def _exact_ref_inputs(
    root: Path,
    ref: str,
    selectors: list[str],
) -> list[dict[str, Any]]:
    try:
        tree = _tree(root, ref)
    except AuthoritySetError as exc:
        raise PlanningHelperError(str(exc)) from exc
    out: dict[str, dict[str, Any]] = {}
    total = 0
    for selector in selectors:
        try:
            matches = _resolve_selector(selector, tree, required=True)
        except AuthoritySetError as exc:
            raise PlanningHelperError(str(exc)) from exc
        for entry in matches:
            mode = entry["mode"]
            kind = entry["type"]
            rel = entry["path"]
            if mode == "120000":
                raise PlanningHelperError(
                    f"validator input must not resolve a symlink: {rel}"
                )
            if mode == "160000" or kind == "commit":
                raise PlanningHelperError(
                    f"validator input must not cross a gitlink/submodule boundary: {rel}"
                )
            if kind != "blob" or mode not in {"100644", "100755"}:
                raise PlanningHelperError(
                    f"validator input must resolve a regular Git blob: {rel}"
                )
            payload = _git_blob_bytes(root, entry["object"])
            total += len(payload)
            if total > MAX_GENERATED_OUTPUT_BYTES:
                raise PlanningHelperError(
                    "validator exact input materialisation exceeds maximum total size"
                )
            out[rel] = {
                "path": rel,
                "git_mode": mode,
                "blob": entry["object"],
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "_payload": payload,
            }
    return [
        out[key]
        for key in sorted(out, key=lambda item: item.encode("utf-8"))
    ]


def _run_validator_once(
    coordinator_root: Path,
    candidate_sha: str,
    contract: dict[str, Any],
    *,
    runner: HelperRunner,
) -> dict[str, Any]:
    helper = contract["helper"]
    caps = helper["capabilities"]
    if caps.get("network") or caps.get("read_external"):
        raise PlanningHelperError(
            f"validator {helper['id']!r} requests capabilities that P5 v1 does not grant"
        )
    inputs = _exact_ref_inputs(
        coordinator_root,
        candidate_sha,
        list(helper["inputs"]),
    )
    input_paths = {row["path"] for row in inputs}

    with tempfile.TemporaryDirectory(
        prefix=f"claude-auto-plan-validator-{helper['id']}-"
    ) as td:
        view = Path(td).resolve()
        for row in inputs:
            target = view / row["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bytes(row["_payload"]))
            target.chmod(0o755 if row["git_mode"] == "100755" else 0o644)

        cwd = view if helper["cwd"] == "." else (view / helper["cwd"]).resolve()
        try:
            cwd.relative_to(view)
        except ValueError as exc:
            raise PlanningHelperError(
                f"validator {helper['id']!r} cwd escapes temporary view"
            ) from exc
        cwd.mkdir(parents=True, exist_ok=True)

        before = _scan_view(view)
        result = runner(
            view,
            list(helper["argv"]),
            timeout=int(helper["timeout_seconds"]),
            trust_repo_scripts=False,
            unrestricted_host=False,
            read_only_root=False,
            working_directory=cwd,
            hidden_paths=[coordinator_root],
            max_output_bytes=MAX_HELPER_PROCESS_OUTPUT_BYTES,
            read_allowlist_only=True,
        )
        if not bool(result.get("sandboxed", False)):
            raise PlanningHelperError(
                f"validator {helper['id']!r} did not run inside a verified isolation boundary"
            )
        if not bool(result.get("environment_scrubbed", False)):
            raise PlanningHelperError(
                f"validator {helper['id']!r} did not run with a scrubbed environment"
            )
        after = _scan_view(view)
        mutations = _changed_paths(before, after)
        for rel in mutations:
            if rel in input_paths:
                raise PlanningHelperError(
                    f"validator {helper['id']!r} mutated exact candidate input: {rel}"
                )
            if not _matches_any(list(helper["outputs"]), rel):
                raise PlanningHelperError(
                    f"validator {helper['id']!r} mutated undeclared ephemeral output: {rel}"
                )
            current = after.get(rel)
            if current is not None and current.get("kind") != "file":
                raise PlanningHelperError(
                    f"validator {helper['id']!r} produced non-regular ephemeral output: {rel}"
                )

        output_state = _output_state(
            before=before,
            after=after,
            output_selectors=list(helper["outputs"]),
        )
    return {
        "helper_id": helper["id"],
        "set_id": contract["set_id"],
        "helper_contract_sha256": _digest(helper),
        "candidate_sha": candidate_sha,
        "input_state_sha256": _digest([
            {
                key: value
                for key, value in row.items()
                if key != "_payload"
            }
            for row in inputs
        ]),
        "input_paths": sorted(input_paths),
        "mutated_paths": mutations,
        "ephemeral_output_state": output_state,
        "returncode": int(result.get("returncode", 1)),
        "timed_out": bool(result.get("timed_out", False)),
        "stdout_sha256": hashlib.sha256(
            str(result.get("stdout") or "").encode("utf-8")
        ).hexdigest(),
        "stderr_sha256": hashlib.sha256(
            str(result.get("stderr") or "").encode("utf-8")
        ).hexdigest(),
        "execution_boundary": result.get("execution_boundary"),
        "sandboxed": True,
        "environment_scrubbed": True,
    }


def run_planning_validators(
    coordinator_root: Path,
    candidate_sha: str,
    envelope: dict[str, Any],
    *,
    runner: HelperRunner = run_repository_command,
) -> dict[str, Any]:
    coordinator_root = coordinator_root.expanduser().resolve()
    contracts = list(envelope.get("validator_contracts", []))
    receipts: list[dict[str, Any]] = []
    for contract in sorted(
        contracts,
        key=lambda item: (item["set_id"], item["helper"]["id"]),
    ):
        first = _run_validator_once(
            coordinator_root,
            candidate_sha,
            contract,
            runner=runner,
        )
        second = _run_validator_once(
            coordinator_root,
            candidate_sha,
            contract,
            runner=runner,
        )
        if (
            first["input_state_sha256"] != second["input_state_sha256"]
            or first["returncode"] != second["returncode"]
            or first["timed_out"] != second["timed_out"]
        ):
            raise PlanningHelperError(
                f"validator {contract['helper']['id']!r} produced nondeterministic pass/fail results"
            )
        if first["timed_out"]:
            raise PlanningHelperError(
                f"validator {contract['helper']['id']!r} timed out"
            )
        if first["returncode"] != 0:
            raise PlanningHelperError(
                f"validator {contract['helper']['id']!r} failed with exit {first['returncode']}"
            )
        receipt = dict(first)
        receipt["determinism_runs"] = [
            {
                "execution_boundary": first["execution_boundary"],
                "returncode": first["returncode"],
                "timed_out": first["timed_out"],
            },
            {
                "execution_boundary": second["execution_boundary"],
                "returncode": second["returncode"],
                "timed_out": second["timed_out"],
            },
        ]
        receipts.append(receipt)

    semantic = {
        "schema_version": 1,
        "repair_envelope_sha256": envelope["repair_envelope_sha256"],
        "candidate_sha": candidate_sha,
        "receipts": receipts,
    }
    return {
        **semantic,
        "validator_receipt_bundle_sha256": _digest(semantic),
    }


def load_reconciler_bundle(
    path: Path,
    *,
    repair_envelope_sha256: str,
    base_sha: str,
) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanningHelperError(
            f"reconciler receipt bundle is unreadable or malformed: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise PlanningHelperError("reconciler receipt bundle must be a JSON object")
    semantic = {
        "schema_version": value.get("schema_version"),
        "repair_envelope_sha256": value.get("repair_envelope_sha256"),
        "base_sha": value.get("base_sha"),
        "receipts": value.get("receipts"),
        "normalised_generated_paths": value.get("normalised_generated_paths"),
    }
    if semantic["schema_version"] != 1:
        raise PlanningHelperError("unsupported reconciler receipt bundle schema")
    if semantic["repair_envelope_sha256"] != repair_envelope_sha256:
        raise PlanningHelperError(
            "reconciler receipt bundle RepairEnvelope binding is stale"
        )
    if semantic["base_sha"] != base_sha:
        raise PlanningHelperError(
            "reconciler receipt bundle base SHA binding is stale"
        )
    if not isinstance(semantic["receipts"], list):
        raise PlanningHelperError("reconciler receipt bundle receipts are malformed")
    if not isinstance(semantic["normalised_generated_paths"], list):
        raise PlanningHelperError(
            "reconciler receipt bundle normalised path evidence is malformed"
        )
    actual = _digest(semantic)
    if value.get("reconciler_receipt_bundle_sha256") != actual:
        raise PlanningHelperError(
            "reconciler receipt bundle semantic integrity check failed"
        )
    return value


def run_planning_reconcilers(
    coordinator_root: Path,
    worktree: Path,
    envelope: dict[str, Any],
    *,
    runner: HelperRunner = run_repository_command,
) -> dict[str, Any]:
    coordinator_root = coordinator_root.expanduser().resolve()
    worktree = worktree.expanduser().resolve()
    contracts = list(envelope.get("reconciler_contracts", []))
    if not contracts:
        return {
            "schema_version": 1,
            "repair_envelope_sha256": envelope["repair_envelope_sha256"],
            "base_sha": envelope["base_sha"],
            "receipts": [],
            "reconciler_receipt_bundle_sha256": _digest([]),
            "normalised_generated_paths": [],
        }

    normalised = normalise_generated_to_base(
        coordinator_root,
        worktree,
        envelope,
    )
    receipts: list[dict[str, Any]] = []
    for contract in sorted(
        contracts,
        key=lambda item: (item["set_id"], item["helper"]["id"]),
    ):
        first = _run_reconciler_once(
            coordinator_root,
            worktree,
            envelope,
            contract,
            runner=runner,
        )
        second = _run_reconciler_once(
            coordinator_root,
            worktree,
            envelope,
            contract,
            runner=runner,
        )
        if (
            first["input_state_sha256"] != second["input_state_sha256"]
            or first["output_state_sha256"] != second["output_state_sha256"]
            or first["mutated_paths"] != second["mutated_paths"]
        ):
            raise PlanningHelperError(
                f"reconciler {contract['helper']['id']!r} is nondeterministic across clean runs"
            )

        _apply_payloads(worktree, first["_payloads"])
        receipt = {
            key: value
            for key, value in first.items()
            if key != "_payloads"
        }
        receipt["determinism_runs"] = [
            {
                "execution_boundary": first["execution_boundary"],
                "output_state_sha256": first["output_state_sha256"],
            },
            {
                "execution_boundary": second["execution_boundary"],
                "output_state_sha256": second["output_state_sha256"],
            },
        ]
        receipts.append(receipt)

    semantic = {
        "schema_version": 1,
        "repair_envelope_sha256": envelope["repair_envelope_sha256"],
        "base_sha": envelope["base_sha"],
        "receipts": receipts,
        "normalised_generated_paths": normalised,
    }
    return {
        **semantic,
        "reconciler_receipt_bundle_sha256": _digest(semantic),
    }
