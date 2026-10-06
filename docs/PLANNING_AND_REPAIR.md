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


## Repository-owned planning authority

Repositories may keep planning authority inside Git. RC4 uses one AuthoritySet/RepairEnvelope model for both the simple one-file case and structured multi-file planning.

### Legacy one-file compatibility

The existing setup remains valid:

```bash
claude-auto planning-repair configure --repo . --plan IMPLEMENTATION_PLAN.md
claude-auto run --repo . --plan IMPLEMENTATION_PLAN.md
```

That configuration is normalised into a synthetic one-member `default` AuthoritySet. The same RepairEnvelope lifecycle described below is used, so one-file repositories do not require a separate repair engine.

### Multi-file AuthoritySets

A repository may instead define `.claude-auto/governance.json` with one or more named AuthoritySets. Each planning member declares its role and repair class:

- **repairable** — source planning data the Planning Repair Architect may directly edit;
- **immutable** — requirements/contracts that are input evidence but cannot be changed by the repair;
- **generated** — projections that can be changed only by declared deterministic reconcilers.

AuthoritySets may also declare bounded validators/reconcilers and may own TaskSources such as structured task ledgers.

If more than one AuthoritySet exists, the repair domain is explicit package/operator authority:

```bash
claude-auto planning-repair begin --repo . \
  --authority-set domain-a \
  --reason "repair dependency ordering"

claude-auto planning-repair architect --repo . \
  --authority-set domain-a \
  --reason "repair dependency ordering"
```

Repeat `--authority-set` only when a deliberate repair must span multiple named domains. The Planning Repair Architect cannot select another set for itself.

### RepairEnvelope

At repair start Claude Auto derives and persists an integrity-bound RepairEnvelope containing the exact:

- product base and product branch;
- selected AuthoritySets and current member identities;
- repairable, immutable and generated members/selectors;
- allowed new repairable/generated selectors;
- validator and reconciler contracts;
- TaskSource contract digest;
- protected planning-control/executable paths;
- worktree-independent base authority-content digest.

The Architect can directly mutate only RepairEnvelope-admitted **repairable** paths. It cannot directly modify immutable/generated members, governance/control state or another AuthoritySet. Existing repairable deletion requires explicit package-approved `delete_paths`.

### Deterministic generated-state reconciliation

If selected AuthoritySets contain generated members, the Architect stops at a reconciliation boundary after editing repairable sources. Claude Auto then runs each declared reconciler against only its declared exact inputs and outputs.

Reconcilers cannot silently gain network/external-read authority. Output mutations outside declared generated selectors are rejected. Determinism is checked package-side; nondeterministic generated bytes are not accepted.

The resulting candidate is sealed only after generated state has been reconciled.

### Exact candidate validation

Before any model verifier can approve a candidate, Claude Auto mechanically validates the exact candidate SHA:

1. rebuild the candidate AuthoritySet snapshot from committed Git objects;
2. confirm governance, selected-set topology and immutable members remain valid;
3. compare the narrow immutable planning-control/executable digest so governance/helper executable/verification-control bytes cannot change silently;
4. classify every changed path as allowed repairable or reconciler-produced generated state;
5. resolve candidate TaskSources from the exact candidate SHA without overwriting live runtime TaskSource state;
6. reject cycles, invalid dependencies, missing required planning members, case/Unicode collisions, symlink/gitlink authority and other fail-closed authority violations;
7. run declared validators through the bounded helper execution boundary.

TaskSource/helper **data** inputs may be selected repairable planning members. P1 still protects them broadly from ordinary product workers, but P5 does not mistake legitimate selected data repairs for changes to executable planning control.

### Independent exact-SHA Planning Verifier

Only after the mechanical gates pass does the independent read-only Planning Verifier run. Its protocol is bound to:

- exact candidate SHA;
- exact RepairEnvelope digest;
- selected AuthoritySets;
- base and candidate authority-content evidence;
- exact candidate TaskSource graph;
- deterministic reconciler/validator receipt digests;
- accepted/completed-task context relevant to backward impact.

A `VERIFIED` outcome creates an enriched package promotion attestation for exactly that SHA. `REJECTED` or `BLOCKED` creates no promotion authority.

The `planning-repair verify` command preserves the legacy one-command workflow: if exact candidate validation has not yet been run, the package performs the mandatory mechanical validation/validator gates first, then invokes the independent verifier.

### Protected promotion

The promotion broker admits only the exact independently verified candidate. A generic `claude-auto promote-ff` cannot bypass planning authority merely because the caller did not explicitly request the planning-repair contract: if the target changes an existing or selector-matching planning member, the enriched planning attestation is required automatically.

The attestation is bound to the exact product base as well as the target SHA, RepairEnvelope and evidence digests. An attestation for candidate A, a stale base or a different authority-content state cannot authorise candidate B.

### Interrupted repair and advancing product branches

Planning repair state is durable. If execution stops after worktree creation, candidate creation, reconciliation, validation, verification or during base refresh, the next operation reconciles the recorded state rather than creating duplicate planning authority.

If the product branch advances while a P5 repair is active, `refresh-base`:

- proves the new product base descends from the recorded base;
- refuses automatic refresh if governance, helper executable/verification-control bytes, selected RepairEnvelope contract shape or TaskSource authority changed;
- derives a fresh RepairEnvelope at the new base;
- recognises a previously completed/lost-response rebase from Git truth;
- recovers bounded incomplete refresh markers;
- rebases/reopens the candidate as required;
- clears stale candidate validation/verifier evidence.

Any candidate SHA or authority-envelope change requires validation and independent verification again.

### Runtime profiles do not widen planning authority

RepairEnvelope authority is semantic authority, not a Claude runtime profile. Balanced, Strict, Unattended and Isolated Full do not change which planning files the Architect may modify. In particular, Unattended cannot directly rewrite immutable/generated planning members, governance/control state or another AuthoritySet, and the dedicated Architect still has Bash/NotebookEdit disabled.

### Semantic decision boundary

Planning Repair may add technical implementation work required by the existing objective, but it may not invent a genuinely unresolved product decision. If objective/repository evidence cannot resolve the decision, the Architect must return `SEMANTIC_DECISION`/BLOCKED and the package returns authority to the operator rather than redefining the requested product.

