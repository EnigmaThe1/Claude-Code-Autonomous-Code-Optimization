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

from typing import Any

COMPLEXITY_DIMENSIONS = (
    "scope_breadth",
    "component_coupling",
    "integration_surface",
    "data_state",
    "security_authority",
    "runtime_deployment",
    "failure_recovery",
    "uncertainty_research",
)
ALLOWED_REQUIREMENT_SOURCES = {
    "objective",
    "operator",
    "repository",
    "derived",
    "external",
}


def nonempty_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip()
    ]


def complexity_score(material: dict[str, Any]) -> tuple[int, str, list[str]]:
    """Return deterministic complexity score, minimum level and errors."""
    errors: list[str] = []
    evidence = material.get("complexity_evidence")
    if not isinstance(evidence, dict):
        return 0, "simple", ["complexity_evidence must be an object"]
    dimensions = evidence.get("dimensions")
    if not isinstance(dimensions, dict):
        return 0, "simple", [
            "complexity_evidence.dimensions must be an object"
        ]

    values: list[int] = []
    for name in COMPLEXITY_DIMENSIONS:
        value = dimensions.get(name)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= 3
        ):
            errors.append(
                f"complexity_evidence.dimensions.{name} must be an integer "
                "from 0 to 3"
            )
            continue
        values.append(value)

    if not nonempty_strings(evidence.get("rationale")):
        errors.append(
            "complexity_evidence.rationale must contain at least one "
            "explanation"
        )

    score = sum(values)
    critical = sum(1 for value in values if value == 3)
    if score >= 14 or critical >= 3:
        minimum = "complex"
    elif score >= 6:
        minimum = "standard"
    else:
        minimum = "simple"
    return score, minimum, errors


def complexity_rank(level: str) -> int:
    return {"simple": 0, "standard": 1, "complex": 2}.get(level, -1)


def validate_scope_baseline(scope: dict[str, Any]) -> list[str]:
    """Validate the independent pre-planning scope/complexity baseline."""
    errors: list[str] = []
    verdict = str(scope.get("verdict") or "").upper()
    if verdict not in {"READY", "BLOCKED"}:
        errors.append("scope verdict must be READY or BLOCKED")

    declared = str(scope.get("complexity") or "").lower()
    score, minimum, score_errors = complexity_score(scope)
    errors.extend(score_errors)
    if declared not in {"simple", "standard", "complex"}:
        errors.append("scope complexity must be simple, standard or complex")
    elif complexity_rank(declared) < complexity_rank(minimum):
        errors.append(
            f"scope complexity {declared} understates deterministic minimum "
            f"{minimum} (score={score})"
        )

    rows = scope.get("requirements")
    if not isinstance(rows, list) or not rows:
        errors.append("scope requirements must be a non-empty list")
        return errors

    seen: set[str] = set()
    allowed_kinds = {"explicit", "necessary-derived", "constraint"}
    for index, row in enumerate(rows):
        label = f"scope.requirements[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{label} must be an object")
            continue
        sid = str(row.get("id") or "").strip()
        if not sid:
            errors.append(f"{label}.id must be a non-empty string")
        elif sid in seen:
            errors.append(f"duplicate scope requirement id: {sid}")
        else:
            seen.add(sid)

        if (
            not isinstance(row.get("statement"), str)
            or not row["statement"].strip()
        ):
            errors.append(f"{label}.statement must be a non-empty string")

        kind = str(row.get("kind") or "").strip().lower()
        if kind not in allowed_kinds:
            errors.append(
                f"{label}.kind must be explicit, necessary-derived or constraint"
            )

        source = str(row.get("source") or "").strip().lower()
        if source not in ALLOWED_REQUIREMENT_SOURCES:
            errors.append(
                f"{label}.source must be one of "
                + ", ".join(sorted(ALLOWED_REQUIREMENT_SOURCES))
            )

    if not nonempty_strings(scope.get("mandatory_concerns")):
        errors.append("scope mandatory_concerns must be a non-empty list")
    return errors


def validate_plan_against_scope(
    plan: dict[str, Any],
    scope: dict[str, Any],
) -> list[str]:
    """Require the plan to cover the independently derived scope baseline."""
    errors: list[str] = []
    plan_level = str(plan.get("complexity") or "").lower()
    scope_level = str(scope.get("complexity") or "").lower()
    if complexity_rank(plan_level) < complexity_rank(scope_level):
        errors.append(
            f"plan complexity {plan_level or 'missing'} is below independent "
            f"scope baseline {scope_level or 'missing'}"
        )

    plan_dimensions = (
        plan.get("complexity_evidence", {}).get("dimensions", {})
        if isinstance(plan.get("complexity_evidence"), dict)
        else {}
    )
    scope_dimensions = (
        scope.get("complexity_evidence", {}).get("dimensions", {})
        if isinstance(scope.get("complexity_evidence"), dict)
        else {}
    )
    for dimension in COMPLEXITY_DIMENSIONS:
        plan_score = plan_dimensions.get(dimension)
        scope_score = scope_dimensions.get(dimension)
        if (
            isinstance(plan_score, int)
            and not isinstance(plan_score, bool)
            and isinstance(scope_score, int)
            and not isinstance(scope_score, bool)
            and plan_score < scope_score
        ):
            errors.append(
                f"plan complexity dimension {dimension}={plan_score} is "
                f"below independent scope baseline {scope_score}"
            )

    scope_rows = (
        scope.get("requirements")
        if isinstance(scope.get("requirements"), list)
        else []
    )
    required_scope_ids = {
        str(row.get("id")).strip()
        for row in scope_rows
        if isinstance(row, dict)
        and isinstance(row.get("id"), str)
        and str(row.get("id")).strip()
    }
    covered: set[str] = set()
    plan_rows = (
        plan.get("requirements")
        if isinstance(plan.get("requirements"), list)
        else []
    )
    for index, row in enumerate(plan_rows):
        if not isinstance(row, dict):
            continue
        scope_ids = set(nonempty_strings(row.get("scope_ids")))
        unknown = sorted(scope_ids - required_scope_ids)
        if unknown:
            errors.append(
                f"requirements[{index}] maps unknown scope ids: "
                + ", ".join(unknown)
            )
        covered.update(scope_ids & required_scope_ids)

    missing = sorted(required_scope_ids - covered)
    if missing:
        errors.append(
            "independent scope requirements are missing from the plan: "
            + ", ".join(missing[:40])
        )

    task_ids = {
        str(row.get("id")).strip()
        for row in plan.get("tasks", [])
        if isinstance(row, dict)
        and isinstance(row.get("id"), str)
        and str(row.get("id")).strip()
    }
    concerns = nonempty_strings(scope.get("mandatory_concerns"))
    concern_rows = (
        plan.get("concern_coverage")
        if isinstance(plan.get("concern_coverage"), list)
        else []
    )
    covered_concerns: set[str] = set()
    for index, row in enumerate(concern_rows):
        label = f"concern_coverage[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{label} must be an object")
            continue
        concern = str(row.get("concern") or "").strip()
        if concern not in concerns:
            errors.append(
                f"{label}.concern must match an independent mandatory concern"
            )
            continue
        covered_concerns.add(concern)
        mapped_tasks = nonempty_strings(row.get("task_ids"))
        if not mapped_tasks:
            errors.append(f"{label}.task_ids must be a non-empty list")
        unknown = sorted(set(mapped_tasks) - task_ids)
        if unknown:
            errors.append(
                f"{label} references unknown task ids: "
                + ", ".join(unknown)
            )
        if not nonempty_strings(row.get("verification")):
            errors.append(f"{label}.verification must be a non-empty list")

    missing_concerns = [
        concern
        for concern in concerns
        if concern not in covered_concerns
    ]
    if missing_concerns:
        errors.append(
            "independent mandatory concerns are missing plan coverage: "
            + "; ".join(missing_concerns[:20])
        )
    return errors
