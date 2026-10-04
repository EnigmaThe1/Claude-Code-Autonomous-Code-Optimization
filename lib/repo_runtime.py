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
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

from environment_policy import sanitised_subprocess_env
from process_runner import run
from repo_identity import repo_state_dir, repository_identity
from repo_profile import profile_repo
from runtime_paths import data_home, ensure_private_dir, package_root, utcnow
from settings_policy import AUTONOMY_PROFILES, make_readonly_settings, make_settings
from state_store import json_dump, load_json


def make_runtime_agents(sd: Path, verifier_model: str | None = None, researcher_model: str | None = None) -> str:
    agents = load_json(sd / "agents.json", {})
    if verifier_model and "autonomy-verifier" in agents:
        agents["autonomy-verifier"]["model"] = verifier_model
    if researcher_model and "autonomy-researcher" in agents:
        agents["autonomy-researcher"]["model"] = researcher_model
    return json.dumps(agents, separators=(",", ":"))


def plan_state_dir(sd: Path) -> Path:
    return ensure_private_dir(sd / "plans")


def prune_runtime_history(sd: Path, max_log_files: int = 2000, max_log_bytes: int = 500 * 1024 * 1024) -> None:
    logs = sd / "logs"
    if logs.is_dir():
        files=[]
        for p in logs.iterdir():
            try:
                if p.is_file(): files.append((p.stat().st_mtime,p.stat().st_size,p))
            except OSError: pass
        files.sort()
        total=sum(x[1] for x in files)
        while len(files)>max_log_files or total>max_log_bytes:
            _,size,p=files.pop(0)
            try: p.unlink(); total-=size
            except OSError: pass
    events=sd/"runtime-events.jsonl"
    try:
        if events.exists() and events.stat().st_size > 50*1024*1024:
            old=sd/"runtime-events.previous.jsonl"
            if old.exists(): old.unlink()
            events.replace(old)
    except OSError:
        pass


def activate(root: Path, dry_run: bool = False) -> Path:
    prof = profile_repo(root)
    sd = repo_state_dir(root)
    if dry_run:
        print(json.dumps({"would_write": str(sd), "profile": asdict(prof)}, indent=2))
        return sd
    ensure_private_dir(data_home())
    ensure_private_dir(data_home() / "repos")
    ensure_private_dir(sd)
    ensure_private_dir(sd / "logs")
    prune_runtime_history(sd)
    json_dump(sd / "profile.json", asdict(prof))
    identity = repository_identity(root)
    state = load_json(sd / "state.json", {})
    if not state:
        state = {
            "schema_version": 8,
            "created_at": utcnow(),
            "updated_at": utcnow(),
            "repo_root": str(root.resolve()),
            "repo_id": prof.repo_id,
            "repo_identity": identity,
            "objective": None,
            "status": "READY",
            "cycle": 0,
            "last_git_head": prof.git_head,
            "last_summary": None,
            "last_session_id": None,
            "last_result_status": None,
            "blocker": None,
            "plan_version": 0,
            "plan_status": None,
            "pending_permission_request": None,
            "permission_grants": [],
            "permission_decisions": [],
            "permission_requests": [],
            "active_permission_objective_hash": None,
        }
    else:
        state["updated_at"] = utcnow()
        state["repo_root"] = str(root.resolve())
        state["repo_id"] = prof.repo_id
        state["repo_identity"] = identity
        state["schema_version"] = max(int(state.get("schema_version", 1)), 8)
        state.setdefault("pending_permission_request", None)
        state.setdefault("permission_grants", [])
        state.setdefault("permission_decisions", [])
        state.setdefault("permission_requests", [])
        state.setdefault("active_permission_objective_hash", None)
    json_dump(sd / "state.json", state)
    prof_dict = asdict(prof)
    # Balanced aliases preserve the previous filenames while explicit profile files
    # make the selected autonomy posture visible and auditable.
    json_dump(sd / "settings.json", make_settings(sd, "external", "balanced", prof_dict))
    json_dump(sd / "settings-external.json", make_settings(sd, "external", "balanced", prof_dict))
    json_dump(sd / "settings-hybrid.json", make_settings(sd, "hybrid", "balanced", prof_dict))
    for autonomy_profile in AUTONOMY_PROFILES:
        for memory_mode in ("external", "hybrid"):
            json_dump(
                sd / f"settings-{autonomy_profile}-{memory_mode}.json",
                make_settings(sd, memory_mode, autonomy_profile, prof_dict),
            )
    json_dump(sd / "settings-readonly.json", make_readonly_settings(sd, root))
    for src_name, dst_name in (("agents.json", "agents.json"), ("system-prompt.md", "system-prompt.md")):
        src = package_root() / "templates" / src_name
        dst = sd / dst_name
        shutil.copy2(src, dst)
        try:
            dst.chmod(0o600)
        except OSError:
            pass
    return sd


def _command_sha256(cmd: list[str], cwd: Path | None = None) -> str | None:
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=sanitised_subprocess_env(),
        )
    except OSError:
        return None
    h = hashlib.sha256()
    assert proc.stdout is not None
    while True:
        chunk = proc.stdout.read(1024 * 1024)
        if not chunk:
            break
        h.update(chunk)
    rc = proc.wait()
    return h.hexdigest() if rc == 0 else None


def _hash_path_exact(path: Path) -> bytes:
    """Return an exact stable digest for a filesystem entry without size cut-offs."""
    h = hashlib.sha256()
    try:
        if path.is_symlink():
            h.update(b"symlink\0")
            h.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
            return h.digest()
        if not path.is_file():
            st = path.lstat()
            h.update(f"nonfile:{st.st_mode}:{st.st_size}".encode())
            return h.digest()
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        return h.digest()
    except OSError:
        h.update(b"<unreadable-or-missing>")
        return h.digest()


def _listed_files_digest(root: Path, args: list[str]) -> str | None:
    cp = subprocess.run(
        ["git", "-C", str(root), *args, "-z"],
        capture_output=True,
        env=sanitised_subprocess_env(),
    )
    if cp.returncode != 0:
        return None
    h = hashlib.sha256()
    for raw in sorted(x for x in cp.stdout.split(b"\0") if x):
        h.update(raw + b"\0")
        rel = raw.decode("utf-8", errors="surrogateescape")
        h.update(_hash_path_exact(root / rel))
    return h.hexdigest()


def _untracked_digest(root: Path, max_total_bytes: int | None = None) -> str | None:
    # max_total_bytes is retained for API compatibility but intentionally ignored:
    # completion/source identity must never fall back to size/mtime-only evidence.
    return _listed_files_digest(root, ["ls-files", "--others", "--exclude-standard"])


def _ignored_digest(root: Path, max_total_bytes: int = 20 * 1024 * 1024) -> str | None:
    # Ignored files are predominantly caches/build outputs and are deliberately
    # not completion-source identity. Keep their reviewer signal bounded.
    cp = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--others", "-i", "--exclude-standard", "-z"],
        capture_output=True,
        env=sanitised_subprocess_env(),
    )
    if cp.returncode != 0:
        return None
    h = hashlib.sha256()
    budget = max_total_bytes
    for raw in sorted(x for x in cp.stdout.split(b"\0") if x):
        h.update(raw + b"\0")
        rel = raw.decode("utf-8", errors="surrogateescape")
        entry = root / rel
        try:
            st = entry.lstat()
            if entry.is_symlink():
                h.update(b"symlink\0" + os.readlink(entry).encode("utf-8", errors="surrogateescape"))
            elif entry.is_file() and st.st_size <= budget:
                with entry.open("rb") as fh:
                    data = fh.read()
                h.update(data)
                budget -= len(data)
            else:
                h.update(f"size={st.st_size}".encode())
        except OSError:
            h.update(b"<unreadable>")
    return h.hexdigest()


def _verification_contract_digest(root: Path) -> str | None:
    path = root / ".claude-auto" / "verification.json"
    if not path.exists() and not path.is_symlink():
        return None
    return _hash_path_exact(path).hex()


def git_snapshot(root: Path) -> dict[str, Any]:
    def val(args: list[str]) -> str | None:
        cp = run(["git", "-C", str(root), *args])
        return cp.stdout.strip() if cp.returncode == 0 and cp.stdout.strip() else None
    status = val(["status", "--short"])
    return {
        "branch": val(["branch", "--show-current"]),
        "head": val(["rev-parse", "HEAD"]),
        "status_short": status,
        "worktree_diff_sha256": _command_sha256(["git", "-C", str(root), "diff", "--no-ext-diff", "--binary"]),
        "index_diff_sha256": _command_sha256(["git", "-C", str(root), "diff", "--no-ext-diff", "--cached", "--binary"]),
        "untracked_sha256": _untracked_digest(root),
        "ignored_sha256": _ignored_digest(root),
        "verification_contract_sha256": _verification_contract_digest(root),
    }

def compact_profile(prof: dict[str, Any]) -> dict[str, Any]:
    return {
        "languages": prof.get("languages", []),
        "manifests": prof.get("manifests", [])[:30],
        "instructions": prof.get("repo_instruction_files", [])[:20],
        "ci": prof.get("ci_files", [])[:20],
        "containers": prof.get("container_files", [])[:20],
        "build_test_hints": prof.get("build_test_hints", {}),
        "toolchain_status": prof.get("toolchain_status", []),
        "plugins": prof.get("recommended_plugins", []),
        "lsp_candidates": prof.get("lsp_candidates", []),
    }
