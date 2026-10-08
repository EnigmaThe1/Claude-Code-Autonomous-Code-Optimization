# RC5 — Complexity-Aware Plan-First Completeness Protocol

RC5 strengthens the universal plan-first control path. It is domain-neutral and does not encode any product, company, repository, framework or architecture as a special case.

## Release objective

A vague or high-level software objective must never grant product-mutation authority directly. The supervisor must first produce a durable implementation plan whose depth is proportional to the demonstrated complexity of the objective and repository.

The RC5 authority order is:

```text
operator objective / supplied requirements
  -> independent scope + complexity baseline
  -> repository and targeted external research
  -> planner complexity evidence (never below independent baseline)
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

Before planning, an independent hard read-only scope analyst extracts explicit and technically necessary requirements plus a conservative task-relevant complexity baseline. Unrelated repository size does not by itself make a small objective complex. Malformed or deterministically inconsistent scope output is fed back through a bounded autonomous repair loop; only a valid scope result that identifies a genuine external/product dependency may block immediately. The planner must map every baseline requirement and may classify upward conservatively but may not classify below that baseline or its own deterministic score. The independent Plan Verifier separately challenges attempts to understate complexity or omit scope.

## Mandatory requirement traceability

Every executable plan carries explicit requirements and one canonical traceability row per requirement. The row must bind the requirement to concrete task IDs, deterministic verification expectations and acceptance evidence. Tasks must declare their requirement IDs and implementation scope.

RC5 does not use arbitrary task-count quotas. Instead, every complexity dimension scored 2 or 3 must have explicit evidence mapping that dimension to requirements, concrete tasks, verification and, where the dimension is architecture-relevant, architecture decisions. High integration, security, runtime/deployment and failure/recovery scores also trigger dimension-appropriate verification-level requirements.

Complex objectives additionally require substantive requirements, architecture, implementation, verification and operations sections. Independent review may require more decomposition whenever catch-all tasks or shallow sections would hide material work, but the harness must not manufacture filler tasks merely to satisfy a number.

## Multi-file plan bundles

Complex plans may be represented as one JSON authority record plus multiple durable Markdown sections. RC5 persists emitted `plan_sections` into a versioned private plan bundle. Artifact references are state-root-relative so durable plan identity is not coupled to one machine-specific absolute path. This keeps the executable graph machine-checkable while allowing architecture and implementation detail to scale without forcing one monolithic document.

## No coding on the fly

A worker does not receive implementation authority until all preflight gates pass. Deterministic completeness failures are fed back to the planner as revision findings. Simulation, red-team or Plan Verifier findings also force a new plan version. Material discoveries during implementation return through the existing versioned plan-repair path before affected coding continues.

## Plan Verifier

The Plan Verifier is an independent hard read-only role. It checks requirement coverage, proportional planning depth, complexity classification, architecture/dependency coherence, task granularity, verification/regression strategy, failure/recovery and operational coverage, and false-completion paths.

The planner cannot self-certify the plan.

## Compatibility

Simple and standard objectives retain the same plan-before-code lifecycle without being forced into an oversized document. All plans still require an auditable requirement-to-task-to-verification-to-acceptance chain. Existing supplied plans remain candidates: the planner derives RC5 traceability around their substantive requirements rather than silently dropping them.

RC5 remains fully repository- and domain-neutral.
