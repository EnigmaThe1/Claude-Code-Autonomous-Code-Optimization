# RC5 — Complexity-Aware Plan-First Completeness Protocol

RC5 strengthens the universal plan-first control path. It is domain-neutral and does not encode any product, company, repository, framework or architecture as a special case.

## Release objective

A vague or high-level software objective must never grant product-mutation authority directly. The supervisor must first produce a durable implementation plan whose depth is proportional to the demonstrated complexity of the objective and repository.

The RC5 authority order is:

```text
operator objective / supplied requirements
  -> repository and targeted external research
  -> complexity evidence
  -> explicit requirements and acceptance criteria
  -> architecture decisions
  -> concrete dependency-ordered implementation tasks
  -> verification strategy and requirement traceability
  -> deterministic completeness gate
  -> independent simulation
  -> independent red-team review
  -> independent plan verifier
  -> VALIDATED plan
  -> implementation authority
```

## Universal complexity model

The planner records eight domain-neutral dimensions from 0 through 3:

- scope breadth;
- component coupling;
- integration surface;
- data/state complexity;
- security/authority complexity;
- runtime/deployment complexity;
- failure/recovery complexity;
- uncertainty/research dependency.

The package independently derives a minimum complexity level. A planner may classify upward conservatively but may not classify below the deterministic minimum. The independent Plan Verifier separately challenges attempts to understate complexity.

## Mandatory requirement traceability

Every executable plan carries explicit requirements and one canonical traceability row per requirement. The row must bind the requirement to concrete task IDs, deterministic verification expectations and acceptance evidence. Tasks must declare their requirement IDs and implementation scope.

For complex objectives, RC5 additionally requires:

- at least eight concrete implementation tasks;
- at least three architecture decisions;
- at least three distinct verification levels;
- substantive requirements, architecture, implementation, verification and operations sections.

The numerical minima are safety floors, not targets. Independent review may require more decomposition whenever catch-all tasks or shallow sections would hide material work.

## Multi-file plan bundles

Complex plans may be represented as one JSON authority record plus multiple durable Markdown sections. RC5 persists emitted `plan_sections` into a versioned private plan bundle. This keeps the executable graph machine-checkable while allowing architecture and implementation detail to scale without forcing one monolithic document.

## No coding on the fly

A worker does not receive implementation authority until all preflight gates pass. Deterministic completeness failures are fed back to the planner as revision findings. Simulation, red-team or Plan Verifier findings also force a new plan version. Material discoveries during implementation return through the existing versioned plan-repair path before affected coding continues.

## Plan Verifier

The Plan Verifier is an independent hard read-only role. It checks requirement coverage, proportional planning depth, complexity classification, architecture/dependency coherence, task granularity, verification/regression strategy, failure/recovery and operational coverage, and false-completion paths.

The planner cannot self-certify the plan.

## Compatibility

Simple and standard objectives retain the same plan-before-code lifecycle without being forced into an oversized document. All plans still require an auditable requirement-to-task-to-verification-to-acceptance chain. Existing supplied plans remain candidates: the planner derives RC5 traceability around their substantive requirements rather than silently dropping them.

RC5 remains fully repository- and domain-neutral.
