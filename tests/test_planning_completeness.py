# Copyright 2026 Bogdan Carp (@EnigmaThe1)
#
# Licensed under the Apache License, Version 2.0.

from pathlib import Path

from planning_completeness import (
    complexity_score,
    persist_plan_sections,
    validate_plan_against_scope,
    validate_plan_completeness,
    validate_scope_baseline,
)


DIMS_SIMPLE = {
    "scope_breadth": 1,
    "component_coupling": 1,
    "integration_surface": 0,
    "data_state": 0,
    "security_authority": 0,
    "runtime_deployment": 0,
    "failure_recovery": 0,
    "uncertainty_research": 0,
}


def _base_plan(complexity="simple", dims=None):
    return {
        "complexity": complexity,
        "complexity_evidence": {
            "dimensions": dict(dims or DIMS_SIMPLE),
            "rationale": ["evidence-based classification"],
        },
        "requirements": [
            {
                "id": "R001",
                "statement": "Deliver required behaviour",
                "source": "objective",
                "scope_ids": ["S001"],
            }
        ],
        "acceptance_criteria": [
            "Required behaviour is independently verified"
        ],
        "architecture": [],
        "tasks": [
            {
                "id": "T001",
                "title": "Implement required behaviour",
                "depends_on": [],
                "verification": ["Run focused and regression checks"],
                "risk": "low",
                "requirement_ids": ["R001"],
                "implementation_scope": ["bounded implementation surface"],
            }
        ],
        "traceability": [
            {
                "requirement_id": "R001",
                "architecture_ids": [],
                "task_ids": ["T001"],
                "verification": ["focused and regression checks"],
                "acceptance_criteria": [
                    "Required behaviour is independently verified"
                ],
                "acceptance_evidence": ["successful verification receipt"],
            }
        ],
        "verification_strategy": [
            {
                "level": "regression",
                "scope": "affected behaviour and prior behaviour",
                "requirement_ids": ["R001"],
            }
        ],
        "plan_sections": [],
    }


def test_simple_plan_has_complete_traceability():
    assert validate_plan_completeness(_base_plan()) == []


def test_rejects_understated_complexity():
    plan = _base_plan()
    plan["complexity_evidence"]["dimensions"] = {
        "scope_breadth": 3,
        "component_coupling": 3,
        "integration_surface": 3,
        "data_state": 2,
        "security_authority": 1,
        "runtime_deployment": 1,
        "failure_recovery": 1,
        "uncertainty_research": 1,
    }
    score, minimum, _ = complexity_score(plan)
    assert score >= 14
    assert minimum == "complex"
    errors = validate_plan_completeness(plan)
    assert any(
        "understates deterministic minimum complex" in item
        for item in errors
    )


def test_rejects_missing_requirement_traceability():
    plan = _base_plan()
    plan["traceability"] = []
    errors = validate_plan_completeness(plan)
    assert any(
        "traceability must be a non-empty list" in item
        for item in errors
    )
    assert any("requirement R001 has no" in item for item in errors)


def test_rejects_complex_catch_all_plan():
    dims = {
        "scope_breadth": 3,
        "component_coupling": 2,
        "integration_surface": 2,
        "data_state": 2,
        "security_authority": 2,
        "runtime_deployment": 1,
        "failure_recovery": 1,
        "uncertainty_research": 1,
    }
    plan = _base_plan("complex", dims)
    errors = validate_plan_completeness(plan)
    assert any(
        "at least 8 concrete implementation tasks" in item
        for item in errors
    )
    assert any(
        "at least 3 architecture decisions" in item
        for item in errors
    )
    assert any(
        "missing required planning sections" in item
        for item in errors
    )


def test_accepts_detailed_complex_plan(tmp_path: Path):
    dims = {
        "scope_breadth": 3,
        "component_coupling": 2,
        "integration_surface": 2,
        "data_state": 2,
        "security_authority": 2,
        "runtime_deployment": 1,
        "failure_recovery": 1,
        "uncertainty_research": 1,
    }
    requirements = [
        {
            "id": f"R{i:03d}",
            "statement": f"Requirement {i}",
            "source": "objective",
        }
        for i in range(1, 9)
    ]
    architecture = [
        {
            "id": f"A{i:03d}",
            "title": f"Decision {i}",
            "decision": f"Use bounded architecture decision {i}",
            "requirement_ids": [f"R{i:03d}"],
        }
        for i in range(1, 4)
    ]
    tasks = [
        {
            "id": f"T{i:03d}",
            "title": f"Implement requirement {i}",
            "depends_on": [] if i == 1 else [f"T{i-1:03d}"],
            "verification": [f"Verify requirement {i}"],
            "risk": "medium",
            "requirement_ids": [f"R{i:03d}"],
            "implementation_scope": [f"component-{i}"],
        }
        for i in range(1, 9)
    ]
    traceability = [
        {
            "requirement_id": f"R{i:03d}",
            "architecture_ids": [f"A{i:03d}"] if i <= 3 else [],
            "task_ids": [f"T{i:03d}"],
            "verification": [f"Verify requirement {i}"],
            "acceptance_criteria": [f"Accept R{i:03d}"],
            "acceptance_evidence": [f"Evidence for requirement {i}"],
        }
        for i in range(1, 9)
    ]
    sections = [
        {
            "id": name,
            "title": name.title(),
            "content": (
                f"Detailed {name} planning. "
                "This section records concrete boundaries, dependencies, "
                "decisions, failure cases, evidence expectations and "
                "implementation consequences for the accepted objective."
            ),
        }
        for name in (
            "requirements",
            "architecture",
            "implementation",
            "verification",
            "operations",
        )
    ]
    plan = {
        "complexity": "complex",
        "complexity_evidence": {
            "dimensions": dims,
            "rationale": [
                "multiple coupled implementation and operational concerns"
            ],
        },
        "requirements": requirements,
        "acceptance_criteria": [
            f"Accept R{i:03d}" for i in range(1, 9)
        ],
        "architecture": architecture,
        "tasks": tasks,
        "traceability": traceability,
        "verification_strategy": [
            {
                "level": "unit",
                "scope": "components",
                "requirement_ids": ["R001"],
            },
            {
                "level": "integration",
                "scope": "interfaces",
                "requirement_ids": ["R002"],
            },
            {
                "level": "regression",
                "scope": "whole accepted surface",
                "requirement_ids": ["R003"],
            },
        ],
        "plan_sections": sections,
    }
    assert validate_plan_completeness(plan) == []
    paths = persist_plan_sections(tmp_path, 1, plan)
    assert len(paths) == 5
    assert all(Path(path).is_file() for path in paths)


def test_independent_scope_baseline_must_be_covered():
    scope = {
        "verdict": "READY",
        "complexity": "standard",
        "complexity_evidence": {
            "dimensions": {
                "scope_breadth": 2,
                "component_coupling": 1,
                "integration_surface": 1,
                "data_state": 1,
                "security_authority": 1,
                "runtime_deployment": 0,
                "failure_recovery": 0,
                "uncertainty_research": 0,
            },
            "rationale": ["independent baseline"],
        },
        "requirements": [
            {
                "id": "S001",
                "statement": "Primary behaviour",
                "kind": "explicit",
                "source": "objective",
            }
        ],
        "mandatory_concerns": ["regression safety"],
    }
    assert validate_scope_baseline(scope) == []
    plan = _base_plan(
        "standard",
        scope["complexity_evidence"]["dimensions"],
    )
    assert validate_plan_against_scope(plan, scope) == []
    plan["requirements"][0]["scope_ids"] = []
    errors = validate_plan_against_scope(plan, scope)
    assert any(
        "independent scope requirements are missing" in item
        for item in errors
    )


def test_plan_cannot_classify_below_independent_scope():
    scope = {
        "verdict": "READY",
        "complexity": "standard",
        "complexity_evidence": {
            "dimensions": {
                "scope_breadth": 2,
                "component_coupling": 1,
                "integration_surface": 1,
                "data_state": 1,
                "security_authority": 1,
                "runtime_deployment": 0,
                "failure_recovery": 0,
                "uncertainty_research": 0,
            },
            "rationale": ["independent baseline"],
        },
        "requirements": [
            {
                "id": "S001",
                "statement": "Primary behaviour",
                "kind": "explicit",
                "source": "objective",
            }
        ],
        "mandatory_concerns": ["regression safety"],
    }
    plan = _base_plan("simple")
    errors = validate_plan_against_scope(plan, scope)
    assert any(
        "below independent scope baseline standard" in item
        for item in errors
    )
