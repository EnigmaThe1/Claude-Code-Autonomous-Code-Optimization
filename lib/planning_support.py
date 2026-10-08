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
import textwrap
from pathlib import Path
from typing import Any

from process_runner import run
from repo_runtime import compact_profile, git_snapshot, plan_state_dir
from runtime_paths import ensure_private_dir, utcnow
from state_store import json_dump, sha256_text
from planning_completeness import persist_plan_sections


def build_readonly_evidence(root: Path, sd: Path) -> Path:
    """Capture exact Git evidence outside the repository for restricted reviewers."""
    evidence_dir = sd / "readonly-evidence"
    ensure_private_dir(evidence_dir)

    def capture(args: list[str], label: str, limit: int = 220_000) -> dict[str, Any]:
        cp = run(["git", "-C", str(root), *args], timeout=30)
        text = cp.stdout if cp.returncode == 0 else (cp.stderr or "")
        digest = sha256_text(text)
        truncated = len(text) > limit
        full_copy: str | None = None
        rendered = text
        if truncated:
            full_path = evidence_dir / f"{label}-{digest[:16]}.txt"
            if not full_path.exists():
                full_path.write_text(text)
                try:
                    full_path.chmod(0o600)
                except OSError:
                    pass
            full_copy = str(full_path)
            rendered = _bounded_prompt_text(
                text,
                limit,
                pointer=full_copy,
                label=f"git {label}",
            )
        return {
            "command": ["git", *args],
            "returncode": cp.returncode,
            "truncated": truncated,
            "sha256": digest,
            "chars": len(text),
            "full_copy": full_copy,
            "output": rendered,
        }

    evidence = {
        "captured_at": utcnow(),
        "repo_root": str(root),
        "snapshot": git_snapshot(root),
        "status": capture(["status", "--short", "--branch"], "status"),
        "worktree_diff": capture(["diff", "--no-ext-diff", "--binary"], "worktree-diff"),
        "index_diff": capture(["diff", "--no-ext-diff", "--cached", "--binary"], "index-diff"),
        "recent_log": capture(["log", "-20", "--oneline", "--decorate", "--no-color"], "recent-log", 40_000),
    }
    snapshot_hash = _git_snapshot_context_hash(evidence["snapshot"])
    path = evidence_dir / f"bundle-{snapshot_hash[:16]}.json"
    json_dump(path, evidence)
    return path


def _normalise_plan_control(
    obj: dict[str, Any],
    objective: str,
) -> dict[str, Any]:
    verdict = str(obj.get("verdict", "BLOCKED")).upper()
    if verdict not in {"READY", "BLOCKED"}:
        verdict = "BLOCKED"
    complexity = str(obj.get("complexity", "standard")).lower()
    if complexity not in {"simple", "standard", "complex"}:
        complexity = "standard"
    plan_md = obj.get("plan_markdown")
    if not isinstance(plan_md, str):
        plan_md = ""
    tasks = obj.get("tasks") if isinstance(obj.get("tasks"), list) else []
    criteria = (
        obj.get("acceptance_criteria")
        if isinstance(obj.get("acceptance_criteria"), list)
        else []
    )
    assumptions = (
        obj.get("assumptions")
        if isinstance(obj.get("assumptions"), list)
        else []
    )
    risks = obj.get("risks") if isinstance(obj.get("risks"), list) else []
    blockers = (
        obj.get("blockers")
        if isinstance(obj.get("blockers"), list)
        else []
    )
    complexity_evidence = (
        obj.get("complexity_evidence")
        if isinstance(obj.get("complexity_evidence"), dict)
        else {}
    )
    requirements = (
        obj.get("requirements")
        if isinstance(obj.get("requirements"), list)
        else []
    )
    architecture = (
        obj.get("architecture")
        if isinstance(obj.get("architecture"), list)
        else []
    )
    traceability = (
        obj.get("traceability")
        if isinstance(obj.get("traceability"), list)
        else []
    )
    verification_strategy = (
        obj.get("verification_strategy")
        if isinstance(obj.get("verification_strategy"), list)
        else []
    )
    plan_sections = (
        obj.get("plan_sections")
        if isinstance(obj.get("plan_sections"), list)
        else []
    )
    return {
        "verdict": verdict,
        "complexity": complexity,
        "objective": objective,
        "plan_markdown": plan_md,
        "tasks": tasks,
        "acceptance_criteria": criteria,
        "assumptions": assumptions,
        "risks": risks,
        "blockers": blockers,
        "complexity_evidence": complexity_evidence,
        "requirements": requirements,
        "architecture": architecture,
        "traceability": traceability,
        "verification_strategy": verification_strategy,
        "plan_sections": plan_sections,
        "summary": str(obj.get("summary", ""))[:2000],
    }

def extract_source_plan_task_ids(source_text: str) -> list[str]:
    """Extract explicit task IDs from structured JSON or YAML-like plan sources.

    This is deliberately conservative: free-form prose is not guessed at. When a
    supplied plan has an explicit task ledger, however, the plan-control layer preserves that ledger
    rather than allowing a planner to silently collapse or rename its steps.
    """
    text = source_text or ""
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            obj = json.loads(text)
        except Exception:
            obj = None
        rows: Any = None
        if isinstance(obj, dict):
            rows = obj.get("tasks")
        elif isinstance(obj, list):
            rows = obj
        if isinstance(rows, list):
            out: list[str] = []
            for row in rows:
                if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"].strip():
                    out.append(row["id"].strip())
            if out:
                return out

    pattern = re.compile(
        r"""(?mx)
        ^\s*-\s*id\s*:\s*
        (?:
            "([^"]+)"
          | '([^']+)'
          | ([^#\s]+)
        )
        \s*(?:\#.*)?$
        """
    )
    out = []
    for match in pattern.finditer(text):
        value = next((group for group in match.groups() if group is not None), "")
        value = value.strip()
        if value:
            out.append(value)
    return out


def validate_source_plan_coverage(source_text: str, plan: dict[str, Any]) -> list[str]:
    """Require every explicit source-plan task ID to survive into the plan graph."""
    source_ids = extract_source_plan_task_ids(source_text)
    if not source_ids:
        return []
    errors: list[str] = []
    seen: set[str] = set()
    duplicates: set[str] = set()
    for task_id in source_ids:
        if task_id in seen:
            duplicates.add(task_id)
        seen.add(task_id)
    if duplicates:
        errors.append("source plan contains duplicate task ids: " + ", ".join(sorted(duplicates)))

    plan_ids = {
        str(task.get("id")).strip()
        for task in plan.get("tasks", [])
        if isinstance(task, dict) and isinstance(task.get("id"), str) and str(task.get("id")).strip()
    }
    missing = [task_id for task_id in source_ids if task_id not in plan_ids]
    if missing:
        preview = ", ".join(missing[:40])
        suffix = f" (+{len(missing) - 40} more)" if len(missing) > 40 else ""
        errors.append(
            f"planner omitted or renamed {len(missing)} explicit source task id(s): "
            + preview + suffix
        )
    return errors


def validate_plan_graph(plan: dict[str, Any]) -> list[str]:
    """Deterministically validate the planner's executable task graph."""
    errors: list[str] = []
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return ["tasks must be a non-empty list"]

    ids: list[str] = []
    graph: dict[str, list[str]] = {}
    seen: set[str] = set()
    for index, task in enumerate(tasks):
        label = f"tasks[{index}]"
        if not isinstance(task, dict):
            errors.append(f"{label} must be an object")
            continue
        raw_id = task.get("id")
        task_id = raw_id.strip() if isinstance(raw_id, str) else ""
        if not task_id:
            errors.append(f"{label}.id must be a non-empty string")
        elif task_id in seen:
            errors.append(f"duplicate task id: {task_id}")
        else:
            seen.add(task_id)
            ids.append(task_id)

        title = task.get("title")
        if not isinstance(title, str) or not title.strip():
            errors.append(f"{label}.title must be a non-empty string")

        deps = task.get("depends_on")
        if not isinstance(deps, list):
            errors.append(f"{label}.depends_on must be a list")
            deps = []
        clean_deps: list[str] = []
        dep_seen: set[str] = set()
        for dep in deps:
            if not isinstance(dep, str) or not dep.strip():
                errors.append(f"{label}.depends_on entries must be non-empty strings")
                continue
            dep_id = dep.strip()
            if dep_id in dep_seen:
                errors.append(f"{label}.depends_on contains duplicate dependency {dep_id}")
                continue
            dep_seen.add(dep_id)
            clean_deps.append(dep_id)
        if task_id and task_id not in graph:
            graph[task_id] = clean_deps

        verification = task.get("verification")
        if not isinstance(verification, list) or not verification:
            errors.append(f"{label}.verification must be a non-empty list")
        elif any(not isinstance(item, str) or not item.strip() for item in verification):
            errors.append(f"{label}.verification entries must be non-empty strings")

        risk = task.get("risk")
        if risk not in {"low", "medium", "high"}:
            errors.append(f"{label}.risk must be low, medium or high")

    known = set(ids)
    for task_id, deps in graph.items():
        for dep in deps:
            if dep == task_id:
                errors.append(f"task {task_id} cannot depend on itself")
            elif dep not in known:
                errors.append(f"task {task_id} depends on unknown task {dep}")

    # Detect dependency cycles without trusting planner ordering. Use an
    # explicit stack so very deep plans cannot escape validation through Python's
    # recursion limit.
    visit: dict[str, int] = {}
    reported_cycles: set[tuple[str, ...]] = set()

    for start_task in ids:
        if visit.get(start_task, 0) == 2:
            continue
        frames: list[tuple[str, int]] = [(start_task, 0)]
        path: list[str] = []
        path_index: dict[str, int] = {}

        while frames:
            task_id, dep_index = frames[-1]
            state = visit.get(task_id, 0)
            if state == 0:
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
                dep_state = visit.get(dep, 0)
                if dep_state == 0:
                    frames.append((dep, 0))
                    descended = True
                    break
                if dep_state == 1:
                    start = path_index.get(dep, 0)
                    cycle = tuple(path[start:] + [dep])
                    if cycle not in reported_cycles:
                        reported_cycles.add(cycle)
                        errors.append("dependency cycle: " + " -> ".join(cycle))
            if descended:
                continue

            frames.pop()
            visit[task_id] = 2
            path_index.pop(task_id, None)
            if path and path[-1] == task_id:
                path.pop()

    criteria = plan.get("acceptance_criteria")
    if not isinstance(criteria, list) or not criteria:
        errors.append("acceptance_criteria must be a non-empty list")
    elif any(not isinstance(item, str) or not item.strip() for item in criteria):
        errors.append("acceptance_criteria entries must be non-empty strings")
    return errors


def validate_progress_checkpoint(
    plan: dict[str, Any],
    checkpoint: dict[str, Any] | None,
    *,
    require_partition: bool = False,
) -> list[str]:
    """Validate worker progress against the exact validated task graph."""
    if not isinstance(checkpoint, dict):
        return ["AUTONOMY_PROGRESS is missing or invalid"]

    task_rows = plan.get("tasks") if isinstance(plan.get("tasks"), list) else []
    graph = {
        str(task.get("id")).strip(): [
            str(dep).strip() for dep in task.get("depends_on", [])
            if isinstance(dep, str) and dep.strip()
        ]
        for task in task_rows
        if isinstance(task, dict) and isinstance(task.get("id"), str) and str(task.get("id")).strip()
    }
    task_ids = set(graph)
    errors: list[str] = []

    parsed: dict[str, list[str]] = {}
    for key in ("completed_task_ids", "remaining_task_ids"):
        raw = checkpoint.get(key)
        if not isinstance(raw, list):
            errors.append(f"{key} must be a list")
            parsed[key] = []
            continue
        values: list[str] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                errors.append(f"{key} entries must be non-empty strings")
                continue
            task_id = item.strip()
            if task_id in seen:
                errors.append(f"{key} contains duplicate task id {task_id}")
                continue
            seen.add(task_id)
            values.append(task_id)
            if task_id not in task_ids:
                errors.append(f"{key} references unknown task {task_id}")
        parsed[key] = values

    completed = set(parsed["completed_task_ids"])
    remaining = set(parsed["remaining_task_ids"])
    overlap = completed & remaining
    if overlap:
        errors.append("task ids cannot be both completed and remaining: " + ", ".join(sorted(overlap)))

    if require_partition and task_ids and completed | remaining != task_ids:
        missing = sorted(task_ids - (completed | remaining))
        extra = sorted((completed | remaining) - task_ids)
        if missing:
            errors.append("progress checkpoint omits task ids: " + ", ".join(missing))
        if extra:
            errors.append("progress checkpoint contains non-plan task ids: " + ", ".join(extra))

    if require_partition and remaining:
        errors.append(
            "COMPLETE progress checkpoint still has remaining task ids: "
            + ", ".join(sorted(remaining))
        )
    if require_partition and task_ids and completed != task_ids:
        missing_completed = sorted(task_ids - completed)
        if missing_completed:
            errors.append(
                "COMPLETE progress checkpoint has not completed task ids: "
                + ", ".join(missing_completed)
            )

    for task_id in sorted(completed):
        missing_deps = [dep for dep in graph.get(task_id, []) if dep not in completed]
        if missing_deps:
            errors.append(
                f"completed task {task_id} has incomplete dependencies: "
                + ", ".join(sorted(missing_deps))
            )

    verification = checkpoint.get("verification")
    if not isinstance(verification, dict) or not verification:
        errors.append("verification must be a non-empty object")
    else:
        for key, value in verification.items():
            if not isinstance(key, str) or not key.strip():
                errors.append("verification keys must be non-empty strings")
                continue
            if not isinstance(value, str) or value.upper() not in {"PASS", "FAIL", "UNKNOWN"}:
                errors.append(f"verification[{key!r}] must be PASS, FAIL or UNKNOWN")
                continue
            if require_partition and value.upper() != "PASS":
                errors.append(
                    f"COMPLETE progress verification[{key!r}] must be PASS, got {value.upper()}"
                )
    return errors


def _persist_plan_version(
    sd: Path,
    state: dict[str, Any],
    plan: dict[str, Any],
    *,
    source_kind: str,
    source_ref: str | None,
    source_hash: str,
    reason: str,
) -> dict[str, Any]:
    pdir = plan_state_dir(sd)
    version = int(state.get("plan_version", 0) or 0) + 1
    enriched = dict(plan)
    enriched.update({
        "schema_version": 2,
        "version": version,
        "created_at": utcnow(),
        "source_kind": source_kind,
        "source_ref": source_ref,
        "source_hash": source_hash,
        "revision_reason": reason,
    })
    section_artifacts = persist_plan_sections(pdir, version, enriched)
    enriched["section_artifacts"] = section_artifacts
    canonical = json.dumps(
        enriched,
        sort_keys=True,
        separators=(",", ":"),
    )
    enriched["plan_hash"] = sha256_text(canonical)
    json_dump(pdir / f"plan-v{version:04d}.json", enriched)
    md = (
        enriched.get("plan_markdown")
        or "# Implementation plan\n\n"
        "No narrative plan was emitted. See the JSON task graph.\n"
    )
    md_path = pdir / f"plan-v{version:04d}.md"
    md_path.write_text(md.rstrip() + "\n")
    try:
        md_path.chmod(0o600)
    except OSError:
        pass
    json_dump(pdir / "current-plan.json", enriched)
    current_md = pdir / "current-plan.md"
    current_md.write_text(md.rstrip() + "\n")
    try:
        current_md.chmod(0o600)
    except OSError:
        pass
    state.update({
        "plan_version": version,
        "plan_hash": enriched["plan_hash"],
        "plan_source_kind": source_kind,
        "plan_source_ref": source_ref,
        "plan_source_hash": source_hash,
        "plan_status": "CANDIDATE",
    })
    # A newly-versioned candidate must earn validation in the current
    # repository context; never carry a previous generation's validation token
    # forward.
    state.pop("plan_validation_context_hash", None)
    state.pop("plan_validation_git_head", None)
    state.pop("plan_validation_git_snapshot_hash", None)
    json_dump(sd / "state.json", state)
    return enriched

def _bounded_prompt_text(text: str, limit: int, *, pointer: str | None = None, label: str = "content") -> str:
    if len(text) <= limit:
        return text

    # Keep an explicit coverage ledger for every omitted region.  At most ~64
    # index rows are emitted even for very large inputs, so the ledger itself
    # cannot crowd the useful prompt out.
    chunk_size = max(32_000, (len(text) + 63) // 64)
    chunks = []
    for index, start in enumerate(range(0, len(text), chunk_size), start=1):
        end = min(len(text), start + chunk_size)
        chunks.append(
            f"{index:03d}:{start}-{end - 1}:sha256={sha256_text(text[start:end])}"
        )
    full_copy = pointer or "UNAVAILABLE"
    coverage_instruction = (
        "Before issuing a verdict, use the Read tool to inspect the full_copy in "
        "sequential chunks covering every indexed range below; do not infer or "
        "silently skip omitted middle sections."
        if pointer else
        "No readable full_copy was supplied; treat omitted middle sections as "
        "unverified rather than silently assuming they are safe."
    )
    ledger = (
        f"[LARGE_INPUT_INDEX label={label!r} chars={len(text)} "
        f"sha256={sha256_text(text)} full_copy={full_copy}]\n"
        f"{coverage_instruction}\n"
        + "\n".join(chunks)
        + "\n[END_LARGE_INPUT_INDEX]\n"
    )
    omission = f"\n...[{label} middle omitted from inline prompt; use indexed full_copy above]...\n"
    remaining = max(2_000, limit - len(ledger) - len(omission))
    half = max(1_000, remaining // 2)
    return ledger + text[:half] + omission + text[-half:]


def _planner_prompt(
    *,
    objective: str,
    prof: dict[str, Any],
    source_kind: str,
    source_ref: str | None,
    candidate_text: str | None,
    current_plan: dict[str, Any] | None,
    revision_reason: str | None,
    review_findings: list[dict[str, Any]] | None,
    state_dir: Path | None = None,
) -> str:
    candidate = _bounded_prompt_text(
        candidate_text or "",
        180_000,
        label="candidate plan/source",
    )
    current = _bounded_prompt_text(
        json.dumps(current_plan or {}, separators=(",", ":")),
        180_000,
        pointer=(
            str(state_dir / "plans" / "current-plan.json")
            if state_dir is not None and current_plan
            else None
        ),
        label="current durable plan",
    )
    findings_raw = json.dumps(
        review_findings or [],
        separators=(",", ":"),
    )
    findings_pointer: str | None = None
    if state_dir is not None and review_findings:
        findings_path = (
            state_dir
            / "plans"
            / f"review-findings-{sha256_text(findings_raw)[:16]}.json"
        )
        if not findings_path.exists():
            findings_path.write_text(findings_raw)
            try:
                findings_path.chmod(0o600)
            except OSError:
                pass
        findings_pointer = str(findings_path)
    findings = _bounded_prompt_text(
        findings_raw,
        80_000,
        pointer=findings_pointer,
        label="review findings",
    )
    return textwrap.dedent(f"""
    You are the HARD READ-ONLY implementation-plan controller. Do not modify
    the repository. Reconcile the user's objective with current repository
    reality and repository-local instructions before accepting any plan.

    This is PLAN-FIRST control. A supplied plan is a candidate, never
    automatically authoritative. If there is no supplied plan, analyse the
    objective and repository, perform targeted read-only research when a
    version-sensitive external fact materially affects the design, and create
    a complete implementation plan BEFORE any product coding.

    ORIGINAL OBJECTIVE:
    {objective}

    REPOSITORY PROFILE:
    {json.dumps(compact_profile(prof), separators=(',', ':'))}

    PLAN SOURCE KIND: {source_kind}
    PLAN SOURCE REFERENCE: {source_ref or 'none'}

    SUPPLIED PLAN/OBJECTIVE CONTENT WHEN EXTERNAL OR PROVIDED INLINE:
    {candidate or 'none; inspect the referenced repository file if one is named, otherwise generate from the objective'}

    CURRENT DURABLE PLAN WHEN REVISING:
    {current or 'none'}

    REVISION TRIGGER:
    {revision_reason or 'initial plan validation'}

    DETERMINISTIC/SIMULATION/RED-TEAM/VERIFIER FINDINGS TO RECONCILE:
    {findings or 'none'}

    Planning depth must scale with demonstrated complexity. Never reduce a
    complex objective to generic catch-all tasks and never assume material
    architecture can safely be discovered during coding.

    Classify complexity from eight universal dimensions, each scored 0..3:
    scope_breadth, component_coupling, integration_surface, data_state,
    security_authority, runtime_deployment, failure_recovery and
    uncertainty_research. Explain the score in rationale. Do not game the
    score downward: deterministic validation and an independent Plan Verifier
    challenge the classification.

    Decompose the objective into explicit stable requirements. Every task must
    have a unique stable ID, non-empty title, dependency list, verification
    list, valid risk, non-empty requirement_ids and non-empty
    implementation_scope. Emit exactly one canonical traceability row per
    requirement binding requirement -> task(s) -> verification -> acceptance
    evidence. Preserve substantive supplied requirements and ordering; do not
    silently drop source-plan work to make the graph smaller. If a supplied
    plan contains explicit task/step IDs, preserve those IDs exactly.

    Validate architecture, dependencies, task ordering, interfaces,
    migration/state transitions, backwards compatibility, verification,
    rollback/recovery, security boundaries, deployment/runtime behaviour,
    operational observability and failure recovery when applicable.

    For complex plans, provide substantive multi-section detail for
    requirements, architecture, implementation, verification and operations.
    At least eight concrete tasks, three architecture decisions and three
    verification levels are deterministic safety floors, not targets. Use
    more decomposition whenever the objective requires it. Simple and
    standard plans should remain proportionate but still trace every
    requirement.

    Resolve ordinary technical ambiguity from repository evidence and
    reversible engineering judgement. Only block for a genuinely
    consequential unresolved product/business requirement, unavailable
    external fact/credential, or mutually incompatible requirement.

    Return exactly one single-line JSON protocol record and nothing after it:
    PLAN_CONTROL: {{"verdict":"READY|BLOCKED","complexity":"simple|standard|complex","complexity_evidence":{{"dimensions":{{"scope_breadth":0,"component_coupling":0,"integration_surface":0,"data_state":0,"security_authority":0,"runtime_deployment":0,"failure_recovery":0,"uncertainty_research":0}},"rationale":["..."]}},"summary":"...","plan_markdown":"...","requirements":[{{"id":"R001","statement":"...","source":"objective|operator|repository|derived|external"}}],"acceptance_criteria":["..."],"architecture":[{{"id":"A001","title":"...","decision":"...","requirement_ids":["R001"]}}],"tasks":[{{"id":"T001","title":"...","depends_on":[],"verification":["..."],"risk":"low|medium|high","requirement_ids":["R001"],"implementation_scope":["..."]}}],"traceability":[{{"requirement_id":"R001","architecture_ids":["A001"],"task_ids":["T001"],"verification":["..."],"acceptance_criteria":["..."],"acceptance_evidence":["..."]}}],"verification_strategy":[{{"level":"unit|integration|system|regression|security|operational","scope":"...","requirement_ids":["R001"]}}],"plan_sections":[{{"id":"requirements|architecture|implementation|verification|operations","title":"...","content":"..."}}],"assumptions":["..."],"risks":["..."],"blockers":["..."]}}
    Use READY only when the emitted plan is detailed enough to undergo
    deterministic completeness validation plus independent simulation,
    red-team and Plan Verifier gates.
    """).strip()

def _assessment_prompt(
    *,
    kind: str,
    stage: str,
    objective: str,
    plan: dict[str, Any],
    state: dict[str, Any],
    main_summary: str | None,
    evidence_path: Path | None = None,
    plan_pointer: Path | None = None,
) -> str:
    assert kind in {"simulation", "redteam", "verifier"}
    if kind == "simulation":
        role = (
            "Simulate execution of the plan as a read-only tabletop exercise. "
            "Walk state/dependency transitions, partial failures, retries, "
            "rollback and likely integration outcomes."
        )
        prefix = "PLAN_SIMULATION"
    elif kind == "redteam":
        role = (
            "Red-team the plan/system as an independent pre-mortem. Assume it "
            "failed and search aggressively for concrete reasons: bad "
            "assumptions, races, partial failure, stale state, malformed input, "
            "dependency loss, resource pressure, security boundary mistakes, "
            "backward incompatibility, rollback failure, observability gaps "
            "and false-completion criteria."
        )
        prefix = "PLAN_REDTEAM"
    else:
        role = (
            "Act as the independent Plan Verifier. Check that complexity is not "
            "understated; every requirement is represented; planning depth is "
            "proportional; tasks are concrete rather than catch-all "
            "placeholders; architecture, interfaces, data/state, security, "
            "runtime, failure/recovery and operations are covered when "
            "applicable; and requirement-to-task-to-verification-to-acceptance "
            "traceability can prove completion before coding starts."
        )
        prefix = "PLAN_VERIFIER"

    return textwrap.dedent(f"""
    You are an independent HARD READ-ONLY {kind} reviewer. Do not modify the
    repository.
    {role}

    REVIEW STAGE: {stage}
    ORIGINAL OBJECTIVE:
    {objective}

    DURABLE PLAN (bounded inline representation plus indexed full-source
    coverage when large):
    {_bounded_prompt_text(json.dumps(plan, separators=(',', ':')), 220_000, pointer=str(plan_pointer) if plan_pointer else None, label="durable plan")}

    CURRENT IMPLEMENTATION CHECKPOINT:
    {json.dumps({"cycle": state.get("cycle"), "last_summary": main_summary or state.get("last_summary"), "last_git_head": state.get("last_git_head")}, separators=(',', ':'))}

    EXTERNAL READ-ONLY GIT EVIDENCE BUNDLE:
    {str(evidence_path) if evidence_path else 'not required for this stage'}
    {('Read this external file when exact current diff/status/history evidence is needed.' if evidence_path else '')}

    For preflight, phase-boundary or remediation review, judge whether the plan
    remains safe, complete and coherent before more material implementation.
    For final review, inspect current repository reality and determine whether
    a material plan flaw or implementation/system defect prevents satisfying
    the original objective. Do not fail for stylistic preference or
    hypothetical risks with no plausible impact.

    Return exactly one single-line JSON protocol record and nothing after it:
    {prefix}: {{"verdict":"PASS|REVISE|BLOCKED","scope":"PLAN|IMPLEMENTATION|REQUIREMENT","summary":"...","findings":["..."],"scenarios":["..."]}}
    PASS means no material unresolved issue was found. REVISE means concrete
    work is needed. BLOCKED is reserved for a genuine external/human
    dependency.
    """).strip()

def _hash_file_streaming(path: Path) -> dict[str, Any]:
    """Hash a control-context file completely without loading it wholesale."""
    h = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
                total += len(chunk)
    except OSError as exc:
        return {"error": type(exc).__name__}
    return {"sha256": h.hexdigest(), "bytes_hashed": total}


def _planning_context_fingerprint(root: Path, prof: dict[str, Any]) -> str:
    """Fingerprint files that define how the plan should be interpreted.

    Ordinary implementation-source edits do not invalidate the cached plan by
    themselves.  Repository instructions, manifests, CI/container definitions and
    Claude-local configuration do, because they can change requirements,
    dependencies, verification or authority boundaries underneath a cached plan.
    """
    paths: set[str] = set()
    for key in ("manifests", "repo_instruction_files", "ci_files", "container_files", "existing_claude_config"):
        value = prof.get(key, [])
        if isinstance(value, list):
            paths.update(str(x) for x in value[:200] if isinstance(x, str))
    evidence: dict[str, Any] = {}
    root = root.resolve()
    for rel in sorted(paths):
        p = (root / rel).resolve()
        try:
            p.relative_to(root)
        except ValueError:
            evidence[rel] = {"error": "outside-root"}
            continue
        if p.is_file():
            evidence[rel] = _hash_file_streaming(p)
        else:
            evidence[rel] = {"missing": True}
    return sha256_text(json.dumps(evidence, sort_keys=True, separators=(",", ":"), default=str))


def _git_snapshot_context_hash(snapshot: dict[str, Any]) -> str:
    material = {
        key: snapshot.get(key)
        for key in (
            "branch", "head", "status_short", "worktree_diff_sha256",
            "index_diff_sha256", "untracked_sha256", "ignored_sha256",
        )
    }
    return sha256_text(json.dumps(material, sort_keys=True, separators=(",", ":"), default=str))


def _validation_anchor_compatible(root: Path, state: dict[str, Any]) -> bool:
    """Reuse validation only for repository state the supervisor actually observed.

    A descendant commit alone is insufficient: another process may have changed
    the repository while claude-auto was stopped.  Claude Auto therefore requires the
    current full Git snapshot to match the last snapshot it durably recorded,
    then separately verifies that the original validation anchor remains in the
    current history.
    """
    anchor = state.get("plan_validation_git_head")
    expected_snapshot_hash = (
        state.get("last_git_snapshot_hash")
        or state.get("plan_validation_git_snapshot_hash")
    )
    if not anchor or not expected_snapshot_hash:
        # Older state must revalidate once under Claude Auto to establish both anchors.
        return False

    current_snapshot = git_snapshot(root)
    if _git_snapshot_context_hash(current_snapshot) != expected_snapshot_hash:
        return False
    head = current_snapshot.get("head")
    if not head:
        return False
    if head == anchor:
        return True
    ancestor = run(["git", "-C", str(root), "merge-base", "--is-ancestor", str(anchor), str(head)])
    return ancestor.returncode == 0
