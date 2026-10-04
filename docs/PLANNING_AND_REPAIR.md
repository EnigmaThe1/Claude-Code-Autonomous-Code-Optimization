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
