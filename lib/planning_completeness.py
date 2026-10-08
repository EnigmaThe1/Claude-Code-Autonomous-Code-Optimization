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

import re
from pathlib import Path
from typing import Any

from planning_complexity import (
    ALLOWED_REQUIREMENT_SOURCES,
    COMPLEXITY_DIMENSIONS,
    complexity_rank,
    complexity_score,
    nonempty_strings,
)

_COMPLEX_REQUIRED_SECTIONS = {
    "requirements",
    "architecture",
    "implementation",
    "verification",
    "operations",
}
_ARCHITECTURE_RELEVANT_DIMENSIONS = {
    "component_coupling",
    "integration_surface",
    "data_state",
    "security_authority",
    "runtime_deployment",
    "failure_recovery",
}
_DIMENSION_VERIFICATION_LEVELS = {
    "integration_surface": {"integration", "system"},
    "security_authority": {"security"},
    "runtime_deployment": {"system", "operational"},
    "failure_recovery": {"system", "operational"},
}

def validate_plan_completeness(plan: dict[str, Any]) -> list[str]:
    """Fail closed when a plan has no auditable requirement-to-evidence chain."""
    errors: list[str] = []
    declared = str(plan.get("complexity") or "").lower()
    score, minimum, score_errors = complexity_score(plan)
    errors.extend(score_errors)
    if declared not in {"simple", "standard", "complex"}:
        errors.append("complexity must be simple, standard or complex")
    elif complexity_rank(declared) < complexity_rank(minimum):
        errors.append(
            f"declared complexity {declared} understates deterministic minimum "
            f"{minimum} (score={score})"
        )

    requirements = plan.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        return errors + ["requirements must be a non-empty list"]

    requirement_ids: list[str] = []
    requirement_seen: set[str] = set()
    for index, row in enumerate(requirements):
        label = f"requirements[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{label} must be an object")
            continue
        rid = str(row.get("id") or "").strip()
        if not rid:
            errors.append(f"{label}.id must be a non-empty string")
        elif rid in requirement_seen:
            errors.append(f"duplicate requirement id: {rid}")
        else:
            requirement_seen.add(rid)
            requirement_ids.append(rid)
        if (
            not isinstance(row.get("statement"), str)
            or not row["statement"].strip()
        ):
            errors.append(f"{label}.statement must be a non-empty string")
        source = str(row.get("source") or "").strip().lower()
        if source not in ALLOWED_REQUIREMENT_SOURCES:
            errors.append(
                f"{label}.source must be one of "
                + ", ".join(sorted(ALLOWED_REQUIREMENT_SOURCES))
            )

    criteria = nonempty_strings(plan.get("acceptance_criteria"))
    criteria_set = set(criteria)
    if not criteria:
        errors.append("acceptance_criteria must be a non-empty list")

    tasks = plan.get("tasks") if isinstance(plan.get("tasks"), list) else []
    if not tasks:
        errors.append("tasks must be a non-empty list")
    task_ids = {
        str(task.get("id")).strip()
        for task in tasks
        if isinstance(task, dict)
        and isinstance(task.get("id"), str)
        and str(task.get("id")).strip()
    }
    task_req_map: dict[str, set[str]] = {}
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue
        tid = str(task.get("id") or "").strip()
        mapped = set(nonempty_strings(task.get("requirement_ids")))
        if not mapped:
            errors.append(
                f"tasks[{index}].requirement_ids must be a non-empty list"
            )
        unknown = sorted(mapped - requirement_seen)
        if unknown:
            errors.append(
                f"task {tid or index} maps unknown requirement ids: "
                + ", ".join(unknown)
            )
        if tid:
            task_req_map[tid] = mapped
        scope = nonempty_strings(task.get("implementation_scope"))
        if not scope:
            errors.append(
                f"tasks[{index}].implementation_scope must be a non-empty list"
            )

    architecture = plan.get("architecture")
    if architecture is None:
        architecture = []
    if not isinstance(architecture, list):
        errors.append("architecture must be a list")
        architecture = []
    architecture_ids: set[str] = set()
    for index, row in enumerate(architecture):
        label = f"architecture[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{label} must be an object")
            continue
        aid = str(row.get("id") or "").strip()
        if not aid:
            errors.append(f"{label}.id must be a non-empty string")
        elif aid in architecture_ids:
            errors.append(f"duplicate architecture id: {aid}")
        else:
            architecture_ids.add(aid)
        if (
            not isinstance(row.get("decision"), str)
            or not row["decision"].strip()
        ):
            errors.append(f"{label}.decision must be a non-empty string")
        mapped = set(nonempty_strings(row.get("requirement_ids")))
        if not mapped:
            errors.append(
                f"{label}.requirement_ids must be a non-empty list"
            )
        unknown = sorted(mapped - requirement_seen)
        if unknown:
            errors.append(
                f"architecture {aid or index} maps unknown requirement ids: "
                + ", ".join(unknown)
            )

    traceability = plan.get("traceability")
    if not isinstance(traceability, list) or not traceability:
        errors.append("traceability must be a non-empty list")
        traceability = []
    trace_by_requirement: dict[str, list[dict[str, Any]]] = {}
    traced_tasks: set[str] = set()
    traced_architecture: set[str] = set()
    traced_acceptance: set[str] = set()
    for index, row in enumerate(traceability):
        label = f"traceability[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{label} must be an object")
            continue
        rid = str(row.get("requirement_id") or "").strip()
        if rid not in requirement_seen:
            errors.append(
                f"{label}.requirement_id must name an emitted requirement"
            )
            continue
        trace_by_requirement.setdefault(rid, []).append(row)

        mapped_tasks = nonempty_strings(row.get("task_ids"))
        if not mapped_tasks:
            errors.append(f"{label}.task_ids must be a non-empty list")
        for tid in mapped_tasks:
            if tid not in task_ids:
                errors.append(f"{label} references unknown task id {tid}")
                continue
            traced_tasks.add(tid)
            if rid not in task_req_map.get(tid, set()):
                errors.append(
                    f"{label} maps {rid} to task {tid}, but that task does not "
                    "declare the requirement"
                )

        mapped_arch = nonempty_strings(row.get("architecture_ids"))
        for aid in mapped_arch:
            if aid not in architecture_ids:
                errors.append(
                    f"{label} references unknown architecture id {aid}"
                )
            else:
                traced_architecture.add(aid)

        mapped_acceptance = nonempty_strings(
            row.get("acceptance_criteria")
        )
        if not mapped_acceptance:
            errors.append(
                f"{label}.acceptance_criteria must be a non-empty list"
            )
        for criterion in mapped_acceptance:
            if criterion not in criteria_set:
                errors.append(
                    f"{label} references acceptance criterion not present "
                    "in the plan"
                )
            else:
                traced_acceptance.add(criterion)

        if not nonempty_strings(row.get("verification")):
            errors.append(f"{label}.verification must be a non-empty list")
        if not nonempty_strings(row.get("acceptance_evidence")):
            errors.append(
                f"{label}.acceptance_evidence must be a non-empty list"
            )

    for rid in requirement_ids:
        rows = trace_by_requirement.get(rid, [])
        if not rows:
            errors.append(
                f"requirement {rid} has no requirement-to-task-to-verification "
                "traceability row"
            )
        elif len(rows) > 1:
            errors.append(
                f"requirement {rid} must have one canonical traceability row"
            )

    untraced_tasks = sorted(task_ids - traced_tasks)
    if untraced_tasks:
        errors.append(
            "tasks are not reachable from any requirement trace: "
            + ", ".join(untraced_tasks[:40])
        )

    untraced_acceptance = [
        criterion
        for criterion in criteria
        if criterion not in traced_acceptance
    ]
    if untraced_acceptance:
        errors.append(
            "acceptance criteria are not reachable from requirement traces: "
            + "; ".join(untraced_acceptance[:20])
        )

    verification_strategy = plan.get("verification_strategy")
    if (
        not isinstance(verification_strategy, list)
        or not verification_strategy
    ):
        errors.append("verification_strategy must be a non-empty list")
        verification_strategy = []
    verification_levels: set[str] = set()
    for index, row in enumerate(verification_strategy):
        label = f"verification_strategy[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{label} must be an object")
            continue
        level = str(row.get("level") or "").strip().lower()
        if not level:
            errors.append(f"{label}.level must be a non-empty string")
        else:
            verification_levels.add(level)
        if not isinstance(row.get("scope"), str) or not row["scope"].strip():
            errors.append(f"{label}.scope must be a non-empty string")
        mapped = set(nonempty_strings(row.get("requirement_ids")))
        if not mapped:
            errors.append(
                f"{label}.requirement_ids must be a non-empty list"
            )
        unknown = sorted(mapped - requirement_seen)
        if unknown:
            errors.append(
                f"{label} maps unknown requirement ids: "
                + ", ".join(unknown)
            )

    orphan_architecture = sorted(
        architecture_ids - traced_architecture
    )
    if orphan_architecture:
        errors.append(
            "architecture decisions are not reachable from requirement traces: "
            + ", ".join(orphan_architecture[:40])
        )

    dimensions = (
        plan.get("complexity_evidence", {}).get("dimensions", {})
        if isinstance(plan.get("complexity_evidence"), dict)
        else {}
    )
    required_dimensions = {
        name
        for name in COMPLEXITY_DIMENSIONS
        if isinstance(dimensions.get(name), int)
        and not isinstance(dimensions.get(name), bool)
        and dimensions.get(name) >= 2
    }
    coverage = plan.get("complexity_coverage")
    if coverage is None:
        coverage = []
    if not isinstance(coverage, list):
        errors.append("complexity_coverage must be a list")
        coverage = []

    coverage_by_dimension: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(coverage):
        label = f"complexity_coverage[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{label} must be an object")
            continue
        dimension = str(row.get("dimension") or "").strip()
        if dimension not in COMPLEXITY_DIMENSIONS:
            errors.append(
                f"{label}.dimension must name a known complexity dimension"
            )
            continue
        if dimension in coverage_by_dimension:
            errors.append(
                f"duplicate complexity coverage dimension: {dimension}"
            )
            continue
        coverage_by_dimension[dimension] = row

        mapped_requirements = set(
            nonempty_strings(row.get("requirement_ids"))
        )
        if not mapped_requirements:
            errors.append(
                f"{label}.requirement_ids must be a non-empty list"
            )
        unknown_requirements = sorted(
            mapped_requirements - requirement_seen
        )
        if unknown_requirements:
            errors.append(
                f"{label} references unknown requirement ids: "
                + ", ".join(unknown_requirements)
            )

        mapped_tasks = set(nonempty_strings(row.get("task_ids")))
        if not mapped_tasks:
            errors.append(f"{label}.task_ids must be a non-empty list")
        unknown_tasks = sorted(mapped_tasks - task_ids)
        if unknown_tasks:
            errors.append(
                f"{label} references unknown task ids: "
                + ", ".join(unknown_tasks)
            )

        mapped_architecture = set(
            nonempty_strings(row.get("architecture_ids"))
        )
        unknown_architecture = sorted(
            mapped_architecture - architecture_ids
        )
        if unknown_architecture:
            errors.append(
                f"{label} references unknown architecture ids: "
                + ", ".join(unknown_architecture)
            )
        if (
            dimension in required_dimensions
            and dimension in _ARCHITECTURE_RELEVANT_DIMENSIONS
            and not mapped_architecture
        ):
            errors.append(
                f"{label}.architecture_ids must cover high-impact "
                f"{dimension}"
            )

        if not nonempty_strings(row.get("verification")):
            errors.append(
                f"{label}.verification must be a non-empty list"
            )

    missing_dimension_coverage = sorted(
        required_dimensions - set(coverage_by_dimension)
    )
    if missing_dimension_coverage:
        errors.append(
            "high-impact complexity dimensions are missing plan coverage: "
            + ", ".join(missing_dimension_coverage)
        )

    for dimension, allowed_levels in _DIMENSION_VERIFICATION_LEVELS.items():
        if (
            dimension in required_dimensions
            and not (verification_levels & allowed_levels)
        ):
            errors.append(
                f"high-impact {dimension} requires verification level "
                + " or ".join(sorted(allowed_levels))
            )

    if declared == "complex":
        sections = plan.get("plan_sections")
        if not isinstance(sections, list):
            errors.append("complex plans require plan_sections")
            sections = []
        section_ids: set[str] = set()
        for index, row in enumerate(sections):
            label = f"plan_sections[{index}]"
            if not isinstance(row, dict):
                errors.append(f"{label} must be an object")
                continue
            sid = str(row.get("id") or "").strip().lower()
            if not sid:
                errors.append(f"{label}.id must be a non-empty string")
                continue
            if sid in section_ids:
                errors.append(f"duplicate complex plan section id: {sid}")
            section_ids.add(sid)
            if (
                not isinstance(row.get("title"), str)
                or not row["title"].strip()
            ):
                errors.append(f"{label}.title must be a non-empty string")
            if (
                not isinstance(row.get("content"), str)
                or len(row["content"].strip()) < 80
            ):
                errors.append(
                    f"{label}.content must contain substantive planning detail"
                )
        missing_sections = sorted(_COMPLEX_REQUIRED_SECTIONS - section_ids)
        if missing_sections:
            errors.append(
                "complex plan is missing required planning sections: "
                + ", ".join(missing_sections)
            )

    return errors


def persist_plan_sections(
    plan_dir: Path,
    version: int,
    plan: dict[str, Any],
) -> list[str]:
    """Persist structured sections as an auditable multi-file plan bundle."""
    sections = plan.get("plan_sections")
    if not isinstance(sections, list) or not sections:
        return []
    target = plan_dir / f"plan-v{version:04d}-sections"
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths: list[str] = []
    used: set[str] = set()
    for index, row in enumerate(sections, start=1):
        if not isinstance(row, dict):
            continue
        raw_id = str(row.get("id") or f"section-{index}").strip().lower()
        slug = (
            re.sub(r"[^a-z0-9._-]+", "-", raw_id).strip("-")
            or f"section-{index}"
        )
        if slug in used:
            slug = f"{slug}-{index}"
        used.add(slug)
        title = str(row.get("title") or raw_id).strip()
        content = str(row.get("content") or "").strip()
        path = target / f"{index:02d}-{slug}.md"
        path.write_text(f"# {title}\n\n{content}\n")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        paths.append(path.relative_to(plan_dir).as_posix())
    return paths
