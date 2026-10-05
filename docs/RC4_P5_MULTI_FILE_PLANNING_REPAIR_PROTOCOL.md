# RC4 P5 — Multi-file Planning Repair and RepairEnvelope Protocol

Status: **FROZEN FOR IMPLEMENTATION**

Date: 2026-10-05

Branch: `release/1.0.0-rc4`

P4 formal closure: `10cd379de5bb5adf78c558cf5583138ebfc526fe`

P4 final hostile/recovery qualification: `3e287b61a1ba8fe1084fada3766616dabc62633c`

P4 closure qualification evidence:

- 479/479 tests passed in the full regression suite;
- 479/479 tests passed again under coverage;
- aggregate measured coverage: 65%;
- critical regression groups: 15/15, 19/19, 25/25, 16/16 and 18/18 passed;
- branch/version identity, public-baseline audit and package static checks passed;
- `main` remained unchanged at RC3.

## 1. Purpose

P5 generalises the existing one-file repository-owned Planning Repair flow into one AuthoritySet-based repair engine.

The existing exact-SHA chain remains:

```text
repository evidence
        ↓
package-owned RepairEnvelope
        ↓
dedicated planning repair worktree
        ↓
Planning Repair Architect
(repairable members only)
        ↓
package-owned reconciler generation
(generated members only)
        ↓
whole-authority / TaskSource validation
        ↓
exact planning candidate SHA
        ↓
declared validators
        ↓
independent Planning Verifier
        ↓
exact-SHA + authority-content attestation
        ↓
existing protected fast-forward promotion
```

P5 must make this work for both:

- a legacy one-file canonical plan;
- a multi-file AuthoritySet containing sources, manifests, task ledgers, traceability, contracts, projections and other protected planning members.

There must not be one repair system for `PLAN.md` and another for multi-file repositories.

## 2. P5 security invariant

The Planning Repair Architect never chooses or widens its own planning authority.

The package creates one exact RepairEnvelope before the architect starts.

Authority precedence is:

```text
human objective / acceptance criteria
    >
committed governance contract / legacy planning policy
    >
exact base AuthoritySet
    >
package-owned RepairEnvelope
    >
declared validator/reconciler contracts
    >
exact candidate AuthoritySet
    >
independent exact-SHA verifier attestation
    >
architect/model requests
```

Free-form model prose, helper stdout, generated metadata, a candidate commit message or a task ledger cannot expand the RepairEnvelope.

The governance contract itself is never automated planning-repair authority.

## 3. One-file compatibility

A legacy canonical-plan policy is represented as the P1 synthetic AuthoritySet:

- set ID: `default`;
- one exact canonical plan member;
- role: `source`;
- repair class: `repairable`;
- no validators;
- no reconcilers.

A newly-started one-file repair therefore uses the same P5 RepairEnvelope, Architect guard, candidate validation, verifier and promotion path as a multi-file repair.

P6 remains responsible for migrating/reconciling **already in-flight** legacy planning-repair state from older schemas. P5 does not silently rewrite an active old repair record.

## 4. AuthoritySet selection

The model cannot select AuthoritySets.

The package determines the selected planning domain from one of these bounded sources:

1. explicit top-level operator `--authority-set SET_ID` arguments;
2. the exact active TaskSpec AuthoritySet list when an autonomous material plan repair is triggered by that task;
3. the legacy canonical-plan mapping when the repository has legacy one-file policy;
4. one unambiguous AuthoritySet when exactly one set exists.

If selection is ambiguous, P5 blocks and requires explicit operator/package authority. It does not guess.

Multiple selected sets are allowed when the repair legitimately spans declared planning domains.

## 5. Worktree-specific P1 identity and P5 authority-content identity

P1 AuthoritySet snapshot digests intentionally include repository/worktree identity and branch state. P5 does not weaken or redefine that property.

A repair worktree and the coordinator checkout may therefore have different P1 `snapshot_sha256` values for the same Git tree.

P5 additionally defines a worktree-independent semantic planning digest:

```text
authority_content_sha256
```

computed from the canonical subset:

- governance blob identity;
- source mode;
- normalised AuthoritySet IDs and member records;
- member path/role/repair/required/Git mode/blob identities;
- TaskSource contract digest;
- control-surface records/digest;
- protected-path set.

It excludes:

- repository/worktree identity;
- branch identity;
- timestamps;
- runtime sandbox evidence.

The exact candidate Git SHA is bound separately.

This digest is used only for cross-worktree planning-content identity. It does not replace P1 `snapshot_sha256`.

## 6. RepairEnvelope v1

Canonical semantic RepairEnvelope:

```json
{
  "schema_version": 1,
  "base_sha": "...",
  "product_branch": "main",
  "coordinator_authority_snapshot_sha256": "...",
  "base_authority_content_sha256": "...",
  "governance_blob": "...",
  "source_mode": "contract|legacy|contract+legacy",
  "selected_authority_sets": ["set-a"],
  "repair_reason_sha256": "...",
  "base_members": [],
  "repairable_paths": [],
  "immutable_paths": [],
  "generated_paths": [],
  "repairable_selectors": [],
  "immutable_selectors": [],
  "generated_selectors": [],
  "allowed_new_repairable_selectors": [],
  "allowed_new_generated_selectors": [],
  "validator_contracts": [],
  "reconciler_contracts": [],
  "task_source_contract_digest": "...",
  "protected_control_paths": []
}
```

The semantic digest is:

```text
repair_envelope_sha256
```

Runtime/audit fields such as creation time are excluded.

The envelope stores both:

- exact resolved base membership;
- the selected raw member declarations needed to decide whether a **new** candidate path is allowed to become an authority member.

## 7. Mutability classes

P5 uses the P1 governance vocabulary unchanged.

### 7.1 `immutable`

Automated planning repair cannot modify, delete, rename or replace it.

If a candidate changes an immutable member, P5 rejects the candidate.

### 7.2 `repairable`

The Planning Repair Architect may directly Edit/Write it when the exact path is admitted by the RepairEnvelope.

A new path may be directly created only when it matches an explicitly selected repairable pattern declaration that permits new membership.

### 7.3 `generated`

The Planning Repair Architect cannot directly Edit/Write/Delete it.

Only a package-owned declared reconciler may create, modify or delete generated members.

## 8. Shared members and cross-domain precedence

A path may match declarations in more than one AuthoritySet.

P5 evaluates every proposed changed/new authority path against **all committed declarations**, not only the selected set.

A direct repairable change is allowed only when every matching authority declaration:

- belongs to the selected AuthoritySet closure; and
- classifies the path as `repairable`.

A reconciler output is allowed only when every matching authority declaration:

- belongs to the selected AuthoritySet closure; and
- classifies the path as `generated`.

Any match against:

- an unselected AuthoritySet;
- an immutable declaration;
- the governance contract;
- a control surface;

wins as a denial.

This prevents selecting one domain from becoming an indirect way to modify another domain.

## 9. New authority members

A new authority member is never model-chosen free space.

A new repairable path may be created only when:

- the selected committed governance contract contains a repairable pattern that matches it;
- the same path matches no immutable/generated/unselected declaration;
- the path is a regular repository-local file;
- the candidate AuthoritySet rebuild resolves it deterministically.

A new generated path may be created only by a declared reconciler and only when the selected committed governance contract contains a generated pattern that matches it.

Exact non-pattern declarations do not authorise arbitrary sibling/new paths.

Deletion of a member is permitted only when its declaration remains valid after candidate AuthoritySet resolution. A required exact member cannot simply disappear. A required pattern must continue to satisfy its declared requirement.

## 10. Planning repair lifecycle

P5 extends the existing durable planning-repair transaction instead of creating a second planning-repair state store.

New repairs use a finite lifecycle such as:

- `PREPARING`
- `ACTIVE`
- `RECONCILING`
- `CANDIDATE`
- `VERIFIED`
- `PROMOTING`
- `SEMANTIC_DECISION`
- `BLOCKED`

The package persists `PREPARING` before creating planning worktree/branch topology.

Recovery reconciles exact recorded branch/worktree identities. It never adopts an unrelated existing worktree merely because its name looks similar.

## 11. Planning Repair Architect

The Architect runs only in the dedicated planning-repair worktree.

Allowed mutation tools remain:

- `Edit`
- `Write`

P5 continues to deny:

- Bash;
- NotebookEdit;
- arbitrary Git mutation;
- MCP mutation tools.

The P5 planning-repair guard reads the package-owned RepairEnvelope and permits direct file mutation only for exact RepairEnvelope-admitted repairable paths/selectors.

The Architect cannot:

- edit immutable members;
- directly edit generated members;
- edit the governance contract;
- edit product source merely to make planning validation pass;
- select a different AuthoritySet;
- change validators/reconcilers;
- grant itself new paths.

## 12. Architect protocol and package-owned deletion

P5 extends the Architect protocol to support package-validated deletions without granting Bash:

```text
PLANNING_REPAIR_ARCHITECT:
{
  "verdict":"READY|BLOCKED",
  "classification":"PLAN_PRESERVING|SEMANTIC_DECISION",
  "summary":"...",
  "delete_paths":["..."]
}
```

`delete_paths` is a request, not authority.

The package executes a requested deletion only when the exact path is a RepairEnvelope-admitted repairable member/new-membership transition.

The package rejects:

- immutable/generated deletions;
- unselected-domain deletions;
- paths outside the repository;
- symlink/gitlink/special-file deletion;
- deletion that makes candidate AuthoritySet resolution invalid.

This preserves the Architect’s no-Bash boundary while supporting legitimate multi-file membership repair.

## 13. Generated-member reconciliation

Generated planning files are package-owned outputs.

For each selected reconciler, P5 uses the committed helper contract:

- ID;
- argv;
- cwd;
- inputs;
- outputs;
- timeout;
- capabilities.

P5 v1 grants **no network and no external-read capability**. If a selected reconciler declares non-empty `network` or `read_external`, the repair blocks.

The package:

1. resolves declared inputs against the exact repair candidate/base tree;
2. materialises the declared inputs plus current declared output files into a fresh temporary view;
3. hides the live repository and host-sensitive namespaces;
4. runs the reconciler with bounded runtime/output;
5. records every created/modified/deleted path in the temporary view;
6. rejects any mutation outside the reconciler’s declared outputs;
7. rejects any actual output not admitted by selected generated-member declarations;
8. repeats the reconciler from an identical clean input view;
9. requires identical output path set, bytes and Git mode across both runs;
10. only then copies the deterministic generated outputs into the repair worktree.

No reconciler receives implicit credentials, host sockets or live-repository write access.

## 14. Validator execution

Validators use the same committed helper contract boundary.

P5 v1 grants no validator network/external-read capability.

Validators run against an exact candidate materialisation, not against mutable primary-checkout state.

The package requires:

- declared inputs only;
- bounded runtime/output;
- no host fallback when the required isolation boundary is unavailable;
- candidate planning inputs remain unchanged;
- stable pass/fail result across a second clean run.

Declared validator outputs are ephemeral evidence only. They are never copied into the candidate unless a separate selected reconciler owns those paths as generated planning members.

A non-zero exit, timeout, unavailable safe boundary or candidate mutation is a validation failure.

## 15. Package-owned reconciliation order

Candidate preparation order is fixed:

1. Architect edits repairable members only;
2. package validates direct repairable delta;
3. package applies admitted `delete_paths`;
4. package runs required reconcilers;
5. package validates generated delta;
6. package stages only exact RepairEnvelope-admitted repairable/generated paths;
7. package creates the planning candidate commit;
8. package rebuilds candidate AuthoritySet;
9. package resolves candidate TaskSources/graph if configured;
10. package runs validators against exact candidate;
11. independent Planning Verifier reviews the exact candidate SHA.

The Architect never stages or commits the acceptance boundary itself.

## 16. Candidate AuthoritySet validation

For every candidate P5 rebuilds:

```python
build_authority_snapshot(root, candidate_sha)
```

and derives:

- exact candidate AuthoritySet membership;
- candidate `authority_content_sha256`;
- candidate protected/control state.

Candidate acceptance requires:

- governance blob unchanged from the RepairEnvelope;
- control surfaces unchanged;
- immutable members unchanged;
- every direct Architect change is selected repairable authority;
- every generated change is a declared reconciler output;
- unselected AuthoritySet semantic content unchanged;
- no unexpected symlink/gitlink/special-file authority member;
- no case/Unicode membership collision;
- all required selectors still resolve.

A candidate that makes governance/AuthoritySet parsing invalid is rejected before independent verification.

## 17. Candidate TaskSource validation

A repairable planning member may be a task ledger.

P5 therefore adds a read-only candidate-ref resolution path to P2:

```python
resolve_task_sources(root, ref=candidate_sha, persist=False)
```

Rules:

- existing `persist=True` runtime resolution remains HEAD-only;
- no current durable TaskSourceSet is overwritten while validating a candidate;
- built-in sources read blobs from the exact candidate tree;
- adapters receive exact candidate inputs under the existing bounded isolation/determinism rules;
- task uniqueness/dependencies/cycles/path authority are validated against candidate AuthoritySets;
- candidate TaskSourceSet digest becomes verifier/attestation evidence.

P5 never checks out the candidate into the primary checkout merely to validate a repaired ledger.

## 18. Worktree-independent candidate authority evidence

P5 records both:

- the exact P1 candidate snapshot generated in the verification context;
- candidate `authority_content_sha256`.

The independent verifier and promotion gate bind:

- candidate Git SHA;
- RepairEnvelope digest;
- candidate authority-content digest;
- candidate TaskSourceSet digest when applicable;
- reconciler receipt digest;
- validator receipt digest.

This permits exact semantic comparison across the planning repair worktree and coordinator checkout while preserving P1 worktree identity semantics.

## 19. Independent Planning Verifier

The verifier did not author the repair.

It operates read-only against the exact candidate SHA and receives:

- product objective;
- repair reason;
- selected AuthoritySet IDs;
- RepairEnvelope;
- exact base/candidate planning diff;
- base and candidate authority summaries/digests;
- candidate TaskSourceSet/graph changes;
- reconciler deterministic receipts;
- validator receipts;
- backward impact on accepted tasks/work;
- forward impact on remaining tasks/work.

Protocol remains exact-SHA and includes the envelope identity:

```text
PLANNING_REPAIR_VERIFY:
{
  "verdict":"VERIFIED|REJECTED|BLOCKED",
  "candidate_sha":"...",
  "repair_envelope_sha256":"...",
  "summary":"...",
  "findings":["..."]
}
```

A mismatched SHA/envelope ID is invalid.

A semantic product decision the committed evidence cannot resolve is BLOCKED/SEMANTIC_DECISION, never silently invented.

## 20. Planning attestation

P5 keeps the existing repository planning-repair promotion contract rather than introducing a second contract family.

A VERIFIED candidate attestation metadata/evidence binds at least:

- exact candidate SHA;
- RepairEnvelope SHA-256;
- selected AuthoritySet IDs;
- base authority-content SHA-256;
- candidate authority-content SHA-256;
- candidate TaskSourceSet SHA-256 when configured;
- reconciler receipt bundle SHA-256;
- validator receipt bundle SHA-256;
- verifier findings/evidence digest.

Any candidate SHA change invalidates the old verifier result and attestation.

## 21. Generic promotion cannot bypass P5

The existing `promote-ff` broker currently auto-requires planning attestation for a changed legacy canonical plan.

P5 extends that automatic gate to AuthoritySets.

For a proposed base→target promotion the broker uses **base committed governance** to detect planning-sensitive changed paths.

Planning-repair attestation is automatically required when any changed path:

- is an existing base authority member;
- matches any committed planning-authority member selector, including a path that would become a new member;
- is a planning control surface.

A governance-contract change itself is not authorised by ordinary P5 planning repair and must fail closed / require a separate fresh governance-authority cycle.

When P5 attestation is required, the broker additionally verifies the attested candidate authority-content digest against the exact target commit.

A generic caller cannot bypass Planning Repair by omitting `--attestation-contract`.

## 22. Product-base advancement

P5 reuses the existing durable `refresh-base` concept.

If the product branch advances:

### 22.1 No candidate yet

The package may advance the empty repair worktree only after re-resolving governance and rebuilding a fresh RepairEnvelope.

If selected authority, mutability/helper contracts or governance changed, the old envelope is invalidated.

### 22.2 Candidate already contains new base

The package may adopt the new base only after rerunning whole-authority/TaskSource/helper validation. Verification/attestation is invalidated.

### 22.3 Candidate still based on old base

The package may use the existing bounded repair-branch rebase transaction when the new base is a descendant.

After any rebase:

- candidate SHA changes;
- verifier/attestation is invalidated;
- reconcilers are rerun;
- candidate AuthoritySet/TaskSources are rebuilt;
- validators rerun;
- independent verifier reruns.

A rebase conflict or changed governance/helper authority preserves the repair and blocks automatic promotion. P5 does not invent semantic merge decisions.

Interrupted refresh remains explicitly recoverable/idempotent.

## 23. Repair rejection and retry

A validator or independent-verifier rejection does not delete the repair worktree.

The repair remains the same package-selected RepairEnvelope unless base/governance changes invalidate it.

Findings are persisted and returned to the Architect.

A repaired candidate is a new exact SHA and must repeat:

- reconciliation;
- whole-authority validation;
- candidate TaskSource resolution;
- validators;
- independent verification;
- attestation.

No evidence transfers from an old candidate SHA.

## 24. Planning-repair guard

`planning_repair_guard.py` is generalised from one exact plan path to one package-owned RepairEnvelope.

It remains fail-closed.

For Edit/Write:

- target must resolve lexically and physically inside the planning repair worktree;
- no symlink traversal;
- target must be RepairEnvelope-admitted `repairable`;
- generated/immutable/unselected/control paths deny;
- allowed new repairable path must match an admitted pattern declaration.

Bash and NotebookEdit remain denied to the Architect.

The guard does not grant reconciler authority; reconcilers are separate package operations.

## 25. RepairEnvelope and helper state integrity

RepairEnvelope, helper receipts, validator receipts and candidate authority evidence live in package-owned external state.

Every semantic record has:

- schema version;
- deterministic SHA-256 digest;
- exact base/candidate bindings where relevant.

Hooks/helpers never trust raw JSON without verifying its digest and current repair-state binding.

## 26. CLI/operator surfaces

The existing `planning-repair` family remains the entrypoint.

P5 may extend it with:

```bash
claude-auto planning-repair status --repo .
claude-auto planning-repair begin --authority-set SET --repo .
claude-auto planning-repair architect --repo .
claude-auto planning-repair reconcile --repo .
claude-auto planning-repair validate --repo .
claude-auto planning-repair verify --repo .
claude-auto planning-repair refresh-base --repo .
claude-auto planning-repair promote --repo .
claude-auto planning-repair abort --repo .
```

Legacy `configure --plan PLAN.md` remains supported.

AuthoritySet selection/configuration is top-level package/operator authority. The Architect cannot invoke commands that widen its RepairEnvelope.

## 27. P5 qualification matrix

P5 must test at least:

- legacy one-file plan uses P5 RepairEnvelope core;
- one repairable file, no helpers;
- two repairable planning files;
- repairable task ledger + traceability file;
- immutable requirements + repairable ledger;
- generated projection from reconciler;
- generated-only stale projection repair;
- Architect direct edit of generated path denied;
- Architect immutable edit denied;
- Architect unselected-domain edit denied;
- Architect governance-contract edit denied;
- Architect Bash/NotebookEdit denied;
- allowed new repairable member through selected pattern;
- new path matching unselected/immutable selector denied;
- package-validated repairable deletion;
- required exact member deletion rejected;
- reconciler network request denied;
- reconciler external-read request denied;
- reconciler modifies undeclared output path;
- reconciler output matches repairable rather than generated selector;
- reconciler nondeterministic bytes;
- reconciler nondeterministic path set;
- generated create/update/delete deterministic success;
- validator pass;
- validator non-zero failure;
- validator timeout;
- validator tries host escape;
- validator mutates candidate input;
- candidate AuthoritySet collision;
- candidate required selector resolves zero;
- repaired JSON/JSONL/TOML task ledger validates at candidate SHA;
- repaired adapter-backed ledger validates from candidate exact inputs;
- duplicate task ID across repaired ledgers rejected;
- repaired dependency cycle rejected;
- candidate TaskSource validation does not overwrite current persisted runtime TaskSourceSet;
- exact verifier SHA mismatch rejected;
- RepairEnvelope digest mismatch rejected;
- stale planning attestation rejected;
- generic promote-ff of existing AuthoritySet member without planning attestation denied;
- generic promote-ff adding a new matching AuthoritySet member without attestation denied;
- unselected AuthoritySet remains byte/semantic identical;
- product branch advances before candidate;
- product branch advances after candidate;
- advancing base changes governance/helper contract and invalidates old envelope;
- interrupted reconciliation reruns deterministically;
- interrupted refresh-base recovers;
- interrupted promotion/lost response remains idempotent under existing broker;
- large multi-file AuthoritySet;
- Unicode/space paths;
- symlink/gitlink authority member rejection;
- Unattended does not bypass planning RepairEnvelope;
- all RC3/P1/P2/P3/P4 regressions remain green.

## 28. Implementation sequence inside P5

P5 implementation should proceed in independently qualified slices:

1. worktree-independent authority-content digest;
2. candidate-ref read-only TaskSource resolution;
3. RepairEnvelope derivation/persistence and AuthoritySet selection;
4. multi-file planning-repair guard;
5. Architect multi-file edit/delete protocol;
6. bounded deterministic reconciler runner;
7. exact candidate AuthoritySet/TaskSource validation;
8. validator runner;
9. independent verifier + attestation enrichment;
10. AuthoritySet-aware implicit promotion gate;
11. advancing-base/interruption reconciliation;
12. one-file compatibility/docs/hostile qualification.

Do not mix all P5 changes into one unqualified commit.

## 29. Implementation boundary

P5 may modify/extend:

- `lib/authority_set.py`;
- `lib/task_sources.py`;
- `lib/planning_repair.py`;
- `hooks/planning_repair_guard.py`;
- `lib/workspace_recovery.py`;
- `lib/promotion_policy.py`;
- `lib/cli_schema.py`;
- planning-repair docs/tests.

A small helper module is acceptable if it prevents `planning_repair.py` from becoming untestable, but P5 must not create:

- a second promotion broker;
- a second repository state store;
- a second TaskSource parser;
- a second sandbox implementation.

P5 must not implement P6 session/migration/shadow-mode work or P7 release promotion/qualification.

## 30. Exit criterion

P5 is complete only when:

- legacy one-file and multi-file planning repair use the same RepairEnvelope core;
- Architect direct edits are limited to selected repairable authority;
- immutable and generated direct edits are impossible;
- generated members are produced only by declared deterministic reconcilers;
- all declared validators run through bounded package-owned execution;
- exact candidate AuthoritySet invariants are mechanically rebuilt and checked;
- repaired task ledgers are resolved/graph-validated from the exact candidate SHA;
- independent verifier is bound to exact candidate SHA + RepairEnvelope;
- attestation binds exact SHA + authority-content digest + helper evidence;
- generic promotion cannot bypass planning attestation for any AuthoritySet member/new matching member;
- advancing-base/interruption semantics preserve work and invalidate stale evidence;
- existing one-file RC3 behavior remains compatible;
- all P1–P4 behavior stays green.

Only then may P6 begin migration/session adoption/shadow validation work.
