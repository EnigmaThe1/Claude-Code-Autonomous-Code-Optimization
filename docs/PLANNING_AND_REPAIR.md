# Planning and repair

## Authority chain

```text
user objective + acceptance criteria
            |
            v
validated implementation plan
            |
            v
implementation decisions
```

The plan is a controlled route to the requested result, not permission to change the requested result.

## Before implementation

A supplied plan is reconciled against the objective, current repository, repository instructions, dependencies, verification and relevant technical constraints. If only an objective is supplied, Claude Auto derives the smallest complete plan needed to implement it.

The candidate is independently simulated and red-teamed. Material findings require a new plan version and another validation pass.

## During implementation

Contained bugs can be diagnosed, repaired, tested and continued without replanning.

A non-trivial local repair is checked for impact on completed and remaining work.

A material architecture, schema, API, dependency, security or requirement conflict pauses implementation for root-cause diagnosis and a versioned plan repair. The repaired plan must pass simulation/red-team validation before work resumes.

## Scope discipline

Plan repair may add **technical implementation work** that is necessary to satisfy an existing objective. A missing migration, compatibility layer, recovery task or verification step may therefore be added when evidence proves it is required.

Plan repair is not authority to add unrelated product functionality. Useful but unrequested features remain outside scope unless they are required by an existing acceptance criterion or the operator changes the objective.

## Backward and forward impact

Every material repair asks two questions:

- does this invalidate already-completed work?
- does this break assumptions or ordering in remaining work?

This prevents a locally attractive fix from quietly corrupting the rest of the programme.

## Completion

A worker's completion declaration is only a candidate. The supervisor checks the final implementation against the plan and acceptance criteria, runs deterministic verification, then independent correctness/security review. Material findings reopen implementation.

## Unresolvable conflict

If objective, repository evidence and required constraints cannot be reconciled within configured repair bounds, Claude Auto reports a blocker instead of redefining the objective.


## Repository-owned canonical plans

Some repositories keep the authoritative implementation plan inside Git. Claude Auto can make that authority explicit:

```bash
claude-auto planning-repair configure --repo . --plan IMPLEMENTATION_PLAN.md
claude-auto run --repo . --plan IMPLEMENTATION_PLAN.md
```

After configuration, autonomous execution must use that exact `--plan`. Claude Auto refuses to maintain a second generated/supplied planning authority in parallel.

In Balanced and Strict profiles, the normal product worker cannot edit the canonical plan directly. Material plan repair uses this chain:

```text
material repository evidence
        |
        v
dedicated planning worktree + repair branch
        |
        v
Planning Repair Architect
(one canonical file only)
        |
        v
exact candidate commit SHA
        |
        v
independent read-only Planning Verifier
        |
        v
VERIFIED attestation bound to exact SHA
        |
        v
protected local/optional-remote promotion
        |
        v
re-read promoted canonical plan
        |
        v
simulation + red-team + executable plan regeneration
        |
        v
implementation resumes
```

The architect may repair plan-preserving technical defects but may not resolve a genuinely ambiguous product decision. A `SEMANTIC_DECISION` result blocks and returns authority to the operator instead of redefining the requested product.

## Interrupted repair and advancing product branches

Planning repair state is durable. If execution stops after worktree creation, after candidate commit, after verification or during base refresh, the next run reconciles the existing work rather than creating a duplicate repair.

If the product branch advances while a repair is in progress, `refresh-base` distinguishes:

- no candidate yet: advance the empty repair worktree to the new base;
- candidate already contains the new base: adopt it without replay;
- candidate still based on the old base: rebase only the plan-repair commit(s);
- interrupted refresh: abort/reconcile the recorded attempt and retry deterministically.

Any candidate SHA change invalidates the previous verifier result and requires independent verification again.

## Exact-SHA promotion

Attestation is not a prose convention. The promotion broker mechanically binds a verifier record to the target SHA and contract. An attestation for SHA A cannot authorize SHA B.

If a generic `promote-ff` target changes the configured canonical plan, the broker automatically requires the repository planning-repair contract even when the caller did not request one explicitly. Ordinary non-plan fast-forwards remain available without that planning-specific attestation unless another promotion policy is configured.
