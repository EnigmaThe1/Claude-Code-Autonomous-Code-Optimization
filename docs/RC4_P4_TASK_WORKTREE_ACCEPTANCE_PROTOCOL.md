# RC4 P4 — Task Worktree, Exact Candidate Verification and Acceptance Protocol

Status: **DRAFT FOR INTERNAL RED-TEAM**

Date: 2026-10-05

Branch: `release/1.0.0-rc4`

P3 closure: `3495f2651579e58523b80977c4893bdbf58886bf`

P3 qualified implementation: `0488dd23b9fe3e2ed65d84877ce411dfb18bf14f`

P3 qualified documentation head: `2cb45905b5f574c8311b67837eec3b71d2427674`

## 1. Purpose

P4 moves task-governed product mutation out of the user's primary checkout and into one package-owned, resumable Git worktree, then turns the resulting exact candidate commit into a durable accepted-task transaction only after deterministic verification and independent exact-SHA review.

P4 is the containment/acceptance phase after P3's ExecutionEnvelope enforcement.

P4 must ensure that:

- a mutating repository task runs in a package-owned linked worktree rather than the user's primary checkout;
- the primary checkout may begin with unrelated dirty WIP without becoming the worker's mutation surface;
- one coordinator state/lease remains authoritative for the repository work unit;
- P3 ExecutionEnvelope semantics continue to apply inside the task worktree;
- the worker cannot mutate the primary checkout, another linked worktree, package semantic state or verifier workspace even in Unattended;
- package-owned candidate assembly stages only admitted product/evidence paths;
- runtime scratch is never included in a candidate commit;
- TaskSpec `verification` strings remain acceptance claims and are never interpreted as shell commands;
- executable verification comes only from existing explicit/discovered package verification command authority;
- a candidate is independently verified at one exact immutable SHA;
- only that verified SHA may be promoted;
- accepted-task state is written only after promotion is durably known to have succeeded, or after a verified no-op acceptance at the unchanged product SHA;
- crashes at any lifecycle point can be reconciled idempotently without inventing acceptance or deleting valuable work.

P4 does **not** implement concurrent product writers, multi-file Planning Repair/RepairEnvelope, cross-task session migration/adoption, or RC4 release promotion/tagging.

## 2. Authority hierarchy

P4 preserves the existing authority hierarchy:

```text
human objective / explicit operator authority
    >
committed repository governance / AuthoritySet
    >
verified current TaskSourceSet
    >
selected current TaskSpec
    >
package-owned TaskWorkspaceRecord
    >
package-owned ExecutionEnvelope
    >
exact candidate commit
    >
deterministic verification receipts
    >
independent exact-SHA verifier attestation
    >
accepted-task record
    >
worker/model prose and tool requests
```

No model-generated text, TaskSpec free-form metadata, commit message, branch name, worktree path or verification prose can expand task authority.

## 3. One coordinator, not a second scheduler

P4 must not create a second repository scheduler merely because Git linked worktrees have distinct worktree identities.

Definitions:

- **coordinator root** — the user's selected primary repository work unit from which the supervisor acquired its existing `SupervisorLease`;
- **coordinator state** — the existing package-owned external state directory for that coordinator root;
- **task root** — the package-owned linked worktree used for the active TaskSpec;
- **verification root** — a disposable detached worktree used only to verify an exact candidate SHA.

All task lifecycle state, accepted-task state, task-workspace records and lifecycle history are owned by the **coordinator state**.

The task root's normal `repo_state_dir(task_root)` identity is not a second authority source.

P4 APIs that operate inside a task/verification worktree must receive or derive an explicit coordinator-state binding. They must not silently switch semantic state merely because `repo_state_dir()` for a linked worktree differs.

## 4. Single-writer invariant

P4 retains one active product-task transaction per coordinator repository.

The existing coordinator `SupervisorLease` remains the writer lease.

P4 does not acquire a second independent supervisor lease for the task root.

Package helper functions may use `acquire_lease=False` only when their caller demonstrably holds the coordinator lease.

Concurrent task worktrees may be introduced in a later phase only with an explicit conflict/reservation protocol. P4 has one active task workspace.

## 5. TaskWorkspaceRecord v1

P4 persists one semantic TaskWorkspaceRecord under coordinator state.

Canonical semantic fields:

```json
{
  "schema_version": 1,
  "task_id": "T123",
  "task_source_set_sha256": "...",
  "task_spec_sha256": "...",
  "authority_snapshot_sha256": "...",
  "execution_envelope_sha256": "...",
  "coordinator_repo_id": "...",
  "product_base_sha": "...",
  "product_branch": "main",
  "task_branch": "claude-auto/task/0123456789abcdef",
  "task_worktree": "/absolute/package-owned/path",
  "primary_baseline_sha256": "...",
  "candidate_sha": null,
  "verified_candidate_sha": null,
  "acceptance_attestation_sha256": null,
  "lifecycle_state": "ACTIVE"
}
```

Non-semantic audit fields may include timestamps, diagnostic summaries and retry metadata.

The semantic digest is persisted as `task_workspace_sha256`.

Allowed lifecycle states are deliberately finite:

- `ACTIVE`
- `CANDIDATE`
- `VERIFYING`
- `VERIFIED_PENDING_PROMOTION`
- `PROMOTING`
- `ACCEPTED_PENDING_CLEANUP`
- `PRIMARY_DRIFT`
- `STALE_BASE`
- `BLOCKED`
- `ABANDONED_PRESERVED`

Unknown states fail closed.

## 6. Package-owned task workspace location

The task worktree lives below coordinator external state, for example:

```text
<coordinator-state>/tasks/workspaces/<task-token>/worktree/
```

The path is derived from package state plus a deterministic bounded token, never directly from unsanitised TaskSpec text.

The branch name uses a bounded hash token rather than raw TaskSpec text:

```text
claude-auto/task/<token>
```

The TaskSpec ID remains in the semantic record and human-facing diagnostics.

P4 must validate that any cleanup/recovery target is beneath the exact package-owned workspace parent before deleting/removing anything.

## 7. Worktree creation

Normal begin sequence while the coordinator lease is held:

1. resolve/revalidate the current TaskSourceSet;
2. compute dependency-safe READY frontier;
3. select one deterministic TaskSpec;
4. capture the coordinator primary checkout baseline;
5. resolve exact current product branch and HEAD;
6. prove no conflicting active TaskWorkspaceRecord exists;
7. create a dedicated branch at the exact product base;
8. create a linked task worktree for that branch;
9. prove the task worktree is clean and on the exact expected branch/base;
10. create the P3 ExecutionEnvelope against the **task root**, with semantic state persisted in coordinator state;
11. persist TaskWorkspaceRecord;
12. only then launch a mutating worker in the task root.

P4 worktree creation uses package-owned Git execution with hooks/fsmonitor/recursive submodule surprises disabled under the existing trusted Git policy.

## 8. Resumability and recovery

If durable active task-workspace state already exists, P4 reconciles rather than blindly creating another workspace.

Recovery cases:

- record + worktree + branch exist and identities match: reuse;
- record + branch exist but package worktree path is missing: re-add the worktree at the recorded branch, then revalidate;
- record + worktree exists but branch is missing/mismatched: fail closed;
- worktree/branch exists without durable active state: treat as orphaned package workspace and require exact reconciliation, never silently adopt;
- durable record is malformed or digest-invalid: fail closed;
- recorded candidate/verified SHA is missing from local Git objects: fail closed;
- primary HEAD changed since recorded base: enter `STALE_BASE` unless the transaction can be proven already promoted as described below.

Recovery never recreates acceptance from model prose.

## 9. Primary checkout isolation

The user's primary checkout is not the worker's product workspace in P4.

At task-workspace creation P4 records a deterministic primary baseline containing at least:

- primary HEAD;
- current branch;
- staged tracked identities;
- unstaged tracked path identities;
- visible untracked path identities;
- a baseline digest.

The primary may already contain unrelated WIP before P4 starts.

That WIP is not copied into the task worktree and does not block task work solely because it overlaps task-owned selectors.

However, the primary baseline is frozen for the active task transaction.

If the primary checkout changes while the worker is active, P4 stops the task round and classifies the transaction as externally drifted/stale rather than guessing whether the worker or human caused the change.

P4 never automatically deletes, resets, stashes or rewrites the user's primary WIP.

## 10. Cross-worktree semantic protection

The worker runs with task root as its repository root.

P4 extends semantic protected locations so the worker cannot mutate:

- coordinator state;
- installed package runtime state;
- the user's coordinator/primary checkout;
- any verification worktree;
- another package task/planning worktree;
- Git common-directory administrative state except through package-owned bounded Git operations.

This protection remains active in Balanced, Strict, Isolated Full and Unattended.

An absolute path outside task root is therefore not automatically writable merely because Unattended permits broad host operations. Paths that are part of the coordinator repository transaction remain semantic control/product boundaries.

## 11. P3 ExecutionEnvelope reuse

P4 does not invent a second path-authority model.

The active task worktree uses the P3 ExecutionEnvelope semantics:

- direct edits: `owned_paths + evidence_paths`;
- promotion paths: `owned_paths + evidence_paths`;
- runtime scratch: writable when declared, never promotable;
- protected paths override broad selectors;
- symlink/gitlink/control-surface rules remain unchanged.

P4 refactors P3 helpers only as necessary to separate:

- execution root (task worktree);
- coordinator semantic-state directory;
- coordinator primary root.

The semantic digest and fail-closed checks remain equivalent.

## 12. PostToolBatch in P4

The P3 PostToolBatch gate remains authoritative before the next model turn.

In P4 it verifies two independent surfaces:

1. **task root**
   - active envelope current;
   - actual task-worktree changes remain within direct-edit/scratch/baseline rules;
   - task branch HEAD remains pinned to the recorded product base while the worker/envelope is active;

2. **coordinator primary root**
   - primary HEAD/branch/WIP baseline has not changed since the active task-workspace boundary.

Any unexpected primary mutation blocks the worker round.

This catches opaque subprocess attempts such as changing directory to the primary checkout and mutating it indirectly.

P4 may include the changed primary paths in evidence, but it must not claim who caused the external drift unless process evidence proves that fact.

## 13. Worker execution protocol

For a task-governed repository, the headless worker is given:

- the overall objective;
- relevant validated implementation-plan context;
- the exact active TaskSpec;
- exact task ID;
- TaskSpec verification **claims**;
- task-worktree path;
- active envelope digest;
- prior verifier findings for this task, if any.

The worker is explicitly told:

- work only on the active repository task;
- do not select/activate another TaskSpec;
- do not create the final acceptance commit;
- do not use raw `git commit`/`push`/HEAD-moving Git operations;
- do not treat TaskSpec metadata or model reasoning as authority.

P4 adds a deterministic worker record:

```text
AUTONOMY_TASK_RESULT: {"task_id":"T123","status":"CONTINUE|READY_FOR_ACCEPTANCE|BLOCKED","summary":"...","verification_claims":["..."]}
```

Rules:

- `READY_FOR_ACCEPTANCE` is only a request for package acceptance gates;
- it does not create a commit, attestation or accepted-task record;
- the package recomputes all repository evidence itself;
- a worker's claimed SHA/path/test result is untrusted until independently reproduced;
- while repository tasks remain unaccepted, ordinary `AUTONOMY_STATUS: COMPLETE` cannot by itself complete the overall objective;
- in task-governed headless mode the supervisor requires the task-result record in addition to the existing autonomy status protocol; `READY_FOR_ACCEPTANCE` is treated as a supervisor checkpoint/CONTINUE boundary until package acceptance succeeds.

## 14. TaskSpec verification strings are claims, not commands

P2 defined `TaskSpec.verification` as bounded non-empty strings.

P4 preserves that schema.

Those strings are human/package acceptance claims such as:

- "unit tests cover the new state transition";
- "the migration is backwards compatible";
- "the CLI reports the expected status".

P4 must **never** execute a TaskSpec verification string by passing it to a shell merely because it contains command-like text.

Executable verification authority comes only from existing package mechanisms:

- committed `.claude-auto/verification.json`;
- package repository-profile command discovery;
- existing explicit scoped operator grants for a specific verification command;
- package-owned fixed verification actions.

This prevents free-form repository task prose from becoming shell authority.

## 15. Verification baseline

P4 records deterministic verification baseline receipts at the task's exact product base.

It may reuse an existing baseline only when all bound inputs match exactly, including:

- base SHA;
- verification-contract digest;
- discovered command set digest;
- relevant execution policy/profile boundary.

Otherwise P4 reruns baseline verification in a disposable verification worktree at the base SHA.

A baseline command failure may later be classified as `BASELINE_FAILURE_UNCHANGED` only under the existing exact signature/exit-code rules.

A baseline run that changes tracked/indexed source is invalid.

## 16. Candidate assembly is package-owned

When the worker reports `READY_FOR_ACCEPTANCE`, the package does not trust the worker's index.

Candidate assembly:

1. validate current task-workspace/envelope/primary baseline;
2. normalise the task worktree index back to its current HEAD without discarding worktree content;
3. enumerate actual tracked changes, deletions and visible untracked files NUL-safely;
4. reject protected, scratch, gitlink or out-of-envelope product changes;
5. stage only exact admitted product/evidence paths;
6. run P3 staged-diff validation;
7. prove no additional staged path exists;
8. create a candidate commit, or classify the task as a no-op candidate.

Ignored untracked files are not force-added merely because a broad owned selector matches them.

Runtime scratch is not staged.

The package never uses `git add .` as an authority decision.

## 17. Candidate commit without moving task HEAD

A non-empty admitted candidate becomes one exact package-created commit object, but **candidate creation does not move the task-worktree branch HEAD**.

P4 uses package-owned Git plumbing:

1. package-owned staging builds the exact admitted index;
2. `git write-tree` produces the exact candidate tree;
3. `git commit-tree <tree> -p <product_base_sha>` creates one candidate commit;
4. a package-owned ref such as `refs/claude-auto/task-candidates/<token>` is atomically updated to keep the candidate reachable;
5. the task worktree branch/HEAD remains pinned to `product_base_sha`;
6. the package resets only the index back to HEAD after candidate sealing, leaving admitted worktree content intact.

Every candidate for the active task is therefore a one-parent exact snapshot whose parent is the same envelope/product base. A repaired candidate replaces the current candidate ref; it does not inherit an old verifier attestation.

This design is required because P3 binds the active ExecutionEnvelope to the exact task-worktree HEAD/base. Moving task HEAD merely to create a verification candidate would make the envelope stale and would prevent clean repair after verifier rejection.

Commit subject:

- use `TaskSpec.commit_subject` when present and valid;
- otherwise use a deterministic bounded package subject containing the TaskSpec ID.

P4 may use the repository's valid configured author identity. If none is configured, it may use an explicit package identity rather than failing an otherwise valid autonomous task solely for missing local Git identity.

`write-tree`, `commit-tree` and `update-ref` are package-owned operations and run under the trusted Git policy; repository hooks are not run.

After candidate sealing:

- task branch HEAD must still equal the envelope base;
- candidate parent must equal the envelope/product base;
- candidate tree must equal the package-staged admitted tree;
- candidate base-to-target diff must pass P3 promotion admission;
- tracked task-worktree content may remain as the worker's editable version of the candidate while the index is normalised back to HEAD;
- remaining scratch/ignored outputs do not affect candidate identity;
- candidate SHA/ref are persisted in TaskWorkspaceRecord;
- any prior verifier result is invalidated unless bound to the same candidate SHA.

## 18. Verified no-op task

P4 supports a task whose requirements are already satisfied at the current product base.

No-op acceptance requires:

- no promotable task-worktree delta;
- exact current base still matches coordinator product HEAD;
- deterministic candidate/base verification is acceptable;
- independent verifier explicitly confirms the TaskSpec claims are already satisfied at that exact SHA.

No synthetic empty commit is required.

The accepted product SHA is the unchanged current product SHA.

This prevents the harness from manufacturing meaningless commits solely to mark a task complete.

## 19. Disposable exact-SHA verification worktree

Candidate verification does not run against mutable primary checkout state.

For a candidate SHA (including a no-op base SHA), P4 creates/reuses a disposable detached verification worktree below coordinator state.

The verification worktree:

- is detached at the exact candidate SHA;
- is not an authority source;
- cannot change coordinator accepted-task state;
- is never used for product promotion;
- may create build/test scratch internally;
- is removed after verification/reconciliation when safe.

Repository-controlled verification commands run through the existing scrubbed/isolation execution boundary.

If a safe required execution boundary is unavailable, the task is `UNVERIFIED`/blocked or follows existing explicit operator-grant semantics; P4 does not silently run repository-controlled verification on the unrestricted host in a profile that forbids it.

## 20. Deterministic candidate verification

At the exact candidate SHA, P4 runs the existing package verification command set rather than TaskSpec prose.

For every executable check P4 records at least:

- category;
- original/effective command;
- exit code;
- timeout;
- normalised output signature;
- execution boundary;
- sandbox/environment-scrub status;
- before/after Git fingerprint;
- tracked-source unchanged flag;
- baseline comparison verdict.

Candidate deterministic verification fails when:

- a new verification failure appears;
- a command times out;
- required safe execution is unavailable;
- verification mutates tracked/indexed source;
- the verification command contract changed relative to the bound candidate context.

Unchanged exact baseline failures may be retained as explicit `BASELINE_FAILURE_UNCHANGED` evidence, never silently reported as PASS.

## 21. Independent Task Verifier

Every task acceptance requires a fresh independent verifier session bound to the exact candidate SHA.

The verifier did not author the candidate.

Verifier input includes:

- overall objective;
- exact normalised TaskSpec authority object;
- TaskSpec verification claims;
- base SHA;
- candidate SHA;
- exact base-to-candidate diff;
- deterministic verification receipts;
- relevant dependency accepted-task records;
- prior rejection findings when applicable.

The verifier runs against the detached exact candidate verification worktree under a hard non-authoring policy.

It returns one protocol:

```text
TASK_ACCEPT_VERIFY: {"verdict":"VERIFIED|REJECTED|BLOCKED","task_id":"T123","candidate_sha":"...","summary":"...","findings":["..."]}
```

A response with a different task ID or SHA is invalid.

`VERIFIED` means the candidate satisfies the TaskSpec and its verification claims with no material unresolved defect.

`REJECTED` returns the task to worker repair on the same task branch.

`BLOCKED` is only for genuinely insufficient/unavailable evidence or external dependency.

Provider/session failure never defaults to VERIFIED.

## 22. Exact-SHA attestation

A VERIFIED task candidate receives a durable package promotion attestation using the existing promotion-attestation mechanism.

The task acceptance contract is package-owned and versioned, for example:

```text
claude-auto/task-acceptance/v1
```

Attestation metadata binds at least:

- task ID;
- TaskSpec digest;
- TaskSourceSet digest;
- ExecutionEnvelope digest;
- base SHA;
- exact candidate SHA;
- deterministic-verification bundle digest;
- independent verifier identity/provider/model;
- verifier findings/summary digest.

The model cannot mint this attestation.

## 23. Promotion admission

The existing `promote-ff` broker remains the only fast-forward product promotion mechanism.

P4 does not create a second merge implementation.

For task acceptance, the broker receives an internal package-bound task admission reference, not arbitrary caller-supplied selectors.

Before promotion it proves:

- candidate equals the exact VERIFIED SHA;
- task acceptance attestation exists for that SHA;
- coordinator product branch is the expected branch;
- current product HEAD equals recorded task base, unless replay reconciliation proves promotion already succeeded;
- candidate is a descendant of base;
- candidate diff satisfies the recorded ExecutionEnvelope promotion paths;
- no scratch/protected/gitlink path is introduced;
- primary WIP is preserved under existing broker invariants;
- configured promotion-policy requirements remain satisfied.

If local WIP prevents a safe fast-forward, P4 keeps `VERIFIED_PENDING_PROMOTION`; it does not stash/reset/delete user WIP.

## 24. Base drift

If the coordinator product HEAD changes before promotion and is not exactly the verified candidate, P4 does not silently rebase/cherry-pick an already verified candidate.

If primary HEAD is unchanged but the primary WIP/branch baseline changes during the task, P4 enters `PRIMARY_DRIFT`; it does not guess that the drift is harmless merely because it is outside the task selectors.

It enters `STALE_BASE`.

The task branch/worktree/candidate/verifier evidence are preserved.

A later bounded refresh protocol may rebuild/replay onto a new base only if it:

- re-resolves current governance/TaskSourceSet;
- proves the same TaskSpec authority is still current;
- produces a new candidate SHA;
- invalidates the old exact-SHA verification/attestation;
- reruns deterministic verification and independent Task Verifier.

P4's first implementation may fail closed at `STALE_BASE` rather than automate a risky rebase. Preserving the candidate is preferable to fabricating acceptance.

## 25. Promotion crash reconciliation

P4 treats promotion + accepted-task recording as a crash-recoverable transaction.

Required ordering:

1. exact candidate VERIFIED;
2. attestation persisted;
3. TaskWorkspaceRecord -> `PROMOTING`;
4. invoke package `promote-ff`;
5. prove local/remote result under existing broker semantics;
6. if product HEAD is exact candidate, persist accepted-task record;
7. TaskWorkspaceRecord -> `ACCEPTED_PENDING_CLEANUP`;
8. invalidate stale task-source/envelope state;
9. cleanup verification/task worktrees and merged task branch;
10. re-resolve TaskSourceSet from new product HEAD before selecting another task.

If the process crashes after step 4 but before step 6, resume checks durable Git truth and the exact attestation. If product HEAD already equals the verified candidate, it completes the accepted-task record idempotently rather than replaying promotion.

## 26. AcceptedTaskRecord v1

Full semantic accepted-task evidence lives in coordinator external state.

Canonical semantic form:

```json
{
  "schema_version": 1,
  "task_id": "T123",
  "task_spec_sha256": "...",
  "task_source_set_sha256": "...",
  "authority_snapshot_sha256": "...",
  "execution_envelope_sha256": "...",
  "base_sha": "...",
  "candidate_sha": "...",
  "accepted_product_sha": "...",
  "no_op": false,
  "verification_bundle_sha256": "...",
  "verifier_attestation_sha256": "..."
}
```

The semantic digest is `acceptance_sha256`.

Non-semantic timestamps/history may be stored separately or excluded from the digest.

`state.json.accepted_tasks[task_id]` remains a compact readiness index and must include at least:

- `task_spec_sha256`;
- `accepted_product_sha`;
- `acceptance_sha256`.

P4 readiness must validate the referenced full accepted-task record before trusting the compact index.

## 27. Accepted-task validity

A prior task is accepted for dependency readiness only when:

- full accepted record integrity passes;
- task ID matches;
- current dependency TaskSpec digest matches accepted TaskSpec digest;
- accepted product SHA exists locally;
- accepted product SHA is an ancestor of current product base;
- acceptance attestation/evidence references are internally consistent.

If the TaskSpec digest changes, that task is no longer accepted for the new authority and becomes eligible/required again according to the current graph.

## 28. After acceptance

After accepted-task recording, P4:

1. archives the old TaskWorkspaceRecord/ExecutionEnvelope/violation/verification evidence;
2. invalidates the old active task binding;
3. re-resolves current governance and TaskSourceSet at the newly promoted product HEAD;
4. recomputes readiness using accepted-task records;
5. selects the next deterministic READY task if one exists.

If all current local TaskSpecs are accepted and no unresolved external dependency remains, the repository-task scheduler is complete for that TaskSourceSet generation.

The outer objective still requires its existing whole-system deterministic/correctness/security completion gates before `AUTONOMY_STATUS: COMPLETE`.

Task acceptance is not whole-objective completion.

## 29. Rejected candidate repair

If the independent verifier returns REJECTED:

- candidate SHA/ref and verification evidence remain available for audit until superseded/archived;
- accepted-task state is unchanged;
- active task remains the same TaskSpec;
- task branch HEAD remains at the original envelope base;
- verifier findings are persisted and included in the next worker checkpoint;
- worker may continue editing the same admitted task worktree;
- package seals a new exact candidate snapshot from the current worktree against the same base;
- all exact-SHA verification/attestation gates rerun for the new candidate.

A prior candidate's verifier attestation never transfers to a new SHA.

## 30. Abort and preservation

P4 must not destroy valuable unaccepted task work by default.

Safe abort cases:

- clean worktree at base: remove worktree/branch and archive abort state;
- only disposable declared runtime scratch exists: scratch may be removed with the package worktree after audit;
- admitted product changes exist: preserve them before freeing the active workspace.

A preservation mechanism may use the same package-owned candidate plumbing to create a WIP preservation commit from admitted product changes and then anchor it under a package preservation ref/branch. The task branch itself need not move. Preservation is allowed only after the same P3 path admission proves all preserved product changes are inside the task envelope.

If out-of-envelope/unknown changes exist, automatic cleanup is refused and the workspace is left preserved for operator reconciliation.

An abort/preserve record is not an accepted-task record and cannot satisfy dependencies.

## 31. Interactive supervised sessions

P4 may run `claude-auto start` inside the package task worktree.

The interactive Claude session is bound to that one task workspace.

Profile hot switching may resume the same Claude session only while the task workspace/envelope remains current.

P4 does not migrate one interactive Claude session across an accepted task into a different task worktree. Cross-task session migration/adoption remains out of scope.

After the interactive session ends, top-level package/operator task candidate/verify/accept actions may process the workspace.

Headless autonomous runs may automatically perform candidate/verify/accept between worker rounds under the existing outer supervisor lease.

## 32. Remote promotion

P4 reuses existing remote-aware `promote-ff` semantics when an existing promotion policy/configuration supplies remote/branch/expected-base authority.

P4 does not invent a second repository remote-promotion configuration format.

Remote task acceptance still requires:

- exact verified candidate SHA;
- exact expected remote base;
- lease-protected push;
- final remote reconciliation;
- accepted-task recording only after durable remote/local outcome is known according to the configured promotion contract.

A lost client response must be reconciled against remote truth before retry.

## 33. Verification and repository-controlled code

Tests/builders are repository-controlled code.

P4 candidate verification therefore reuses the existing execution boundary, scrubbed environment, command authorisation and profile semantics.

Strict must not fall back to unsafe host execution.

Balanced/Isolated/Unattended retain their existing explicit execution semantics.

Task acceptance never treats "the model says tests pass" as deterministic evidence.

## 34. Protected Git/control state

Worker-facing P4 guards continue to prohibit raw:

- `git commit`;
- `git push`;
- `git merge`;
- `git rebase`;
- `git reset --hard`;
- checkout/switch/branch operations that move task authority;
- worktree administration;
- direct edits to Git administrative files.

Package-owned P4 Git operations run through trusted Git helpers and are not exposed as worker-granted authority.

## 35. CLI/operator surfaces

P4 extends the plural `tasks` family without changing P3 read-only semantics.

Target diagnostic/operator surface:

```bash
claude-auto tasks status --repo .
claude-auto tasks show TASK_ID --repo .
claude-auto tasks explain TASK_ID --repo .
claude-auto tasks workspace --repo .
claude-auto tasks begin [TASK_ID] --repo .
claude-auto tasks candidate --repo .
claude-auto tasks verify --repo .
claude-auto tasks accept --repo .
claude-auto tasks preserve --repo .
claude-auto tasks cleanup --repo .
```

Rules:

- `status/show/explain/workspace` are read-only;
- begin/candidate/verify/accept/preserve/cleanup are top-level operator/package authority operations;
- worker process trees cannot invoke them to self-promote;
- the headless outer supervisor may call the same internal functions directly while it holds the coordinator lease.

P3 `tasks activate/validate-stage/reconcile/deactivate` remain available for P3 diagnostics/recovery, but the normal P4 autonomous mutation path uses a task workspace.

## 36. Durable state additions

P4 may add coordinator state fields such as:

- `active_task_workspace_sha256`;
- `active_task_worktree`;
- `active_task_branch`;
- `active_task_candidate_sha`;
- `active_task_verified_sha`;
- `accepted_tasks` compact index;
- `task_scheduler_generation`;
- `task_acceptance_last_error`.

Full records live below package-owned coordinator state, for example:

```text
tasks/
  task-source-set.json
  execution-envelope.json
  violation.json
  workspace-active.json
  workspaces/<token>/...
  candidates/<sha>.json
  verification/<sha>.json
  accepted/<task-id-token>.json
  history/...
```

All semantic records have explicit schema versions and integrity digests.

## 37. Worktree identity/topology rules

P4 qualification must cover:

- ordinary repository;
- dirty primary checkout;
- primary WIP overlapping task-owned paths;
- linked coordinator worktree;
- package task worktree beneath external state;
- worktree path with spaces/Unicode;
- stale worktree registration;
- missing task worktree with surviving task branch;
- task branch missing while worktree survives;
- worktree pruned externally;
- detached verification worktree;
- shallow/partial clone missing candidate/base object;
- submodule/gitlink task path rejection;
- nested repository inside owned path;
- symlink escape;
- sparse checkout on primary;
- task worktree checkout completeness;
- large monorepo/path selector sets.

P4 should prefer failing closed with preserved task evidence over destructive recovery.

## 38. P4 hostile/qualification matrix

P4 must test at least:

- begin one READY task creates one package task worktree;
- deterministic task token/branch naming;
- worktree state is owned by coordinator state, not linked-worktree repo state;
- second begin reuses valid active workspace;
- two supervisors cannot begin two task workspaces;
- primary dirty unrelated WIP does not appear in task worktree;
- primary WIP overlapping task selectors does not become worker input;
- worker direct absolute write to primary is denied in Balanced;
- worker direct absolute write to primary is denied in Unattended semantic mode;
- opaque Bash mutation of primary is caught after batch;
- human/out-of-band primary drift pauses task without deleting anything;
- task worktree owned-path edit allowed;
- task worktree evidence edit allowed;
- task worktree scratch allowed but never candidate-staged;
- out-of-envelope task-worktree edit blocks;
- package candidate assembly resets worker-controlled index only, preserving worktree content;
- pre-staged allowed path is normalised and restaged package-side;
- staged scratch rejected;
- staged protected/outside path rejected;
- rename/copy/deletion/mode/binary candidate paths admitted correctly;
- untracked admitted product file staged;
- ignored untracked file is not force-added;
- no-op task produces no synthetic commit;
- commit subject from TaskSpec used safely;
- missing Git identity uses explicit package fallback;
- candidate SHA persisted and exact;
- candidate parent is exact product base and task branch HEAD remains at base;
- package candidate ref keeps the commit reachable without moving the task branch;
- candidate creation disables repository hooks;
- TaskSpec verification text resembling shell is never executed as a command;
- explicit verification contract command is executed through existing safe boundary;
- discovered verification command remains bounded;
- strict profile refuses unsafe verification fallback;
- unchanged baseline failure classified explicitly;
- new candidate verification failure rejects;
- verification mutation of tracked/indexed source rejects;
- verification timeout/unavailable boundary never becomes PASS;
- verifier runs on exact detached candidate SHA;
- verifier cannot mutate accepted-task/coordinator state;
- verifier SHA/task mismatch rejects protocol;
- verifier rejection returns findings to same task;
- repaired replacement candidate invalidates old attestation while task HEAD stays at base;
- exact VERIFIED candidate receives task attestation;
- unverified candidate promotion refused;
- wrong candidate SHA promotion refused;
- primary WIP conflict leaves VERIFIED_PENDING_PROMOTION;
- successful local FF records accepted task;
- no-op VERIFIED task records acceptance at unchanged SHA;
- crash after FF before accepted record reconciles idempotently;
- crash after accepted record before cleanup completes cleanup only;
- accepted record digest corruption fails closed;
- compact accepted index mismatch fails closed;
- changed TaskSpec digest invalidates prior acceptance for readiness;
- accepted dependency SHA missing from local objects blocks;
- accepted dependency SHA non-ancestor blocks;
- accepted dependency valid ancestor unlocks dependent task;
- promotion refreshes TaskSourceSet before next selection;
- next READY task selected deterministically;
- all accepted tasks transition outer scheduler to final whole-objective gates;
- primary WIP/branch drift preserves candidate and enters PRIMARY_DRIFT;
- stale product base preserves candidate and enters STALE_BASE;
- candidate is never silently rebased under old verification;
- clean abort removes workspace safely;
- admitted dirty abort preserves work on package branch;
- out-of-envelope dirty abort refuses destructive cleanup;
- stale worktree recovery re-adds from exact recorded branch;
- missing branch/worktree inconsistencies fail closed;
- package cleanup cannot escape package workspace parent;
- linked coordinator worktree uses correct coordinator state;
- Unicode/space task-worktree paths work;
- P3/RC3/P1/P2 regressions remain green.

## 39. Implementation boundary

P4 may implement:

1. `lib/task_workspace.py` for task-worktree lifecycle/recovery;
2. `lib/task_acceptance.py` for candidate/verification/attestation/acceptance state;
3. coordinator-state-aware refactor of P3 task/envelope helpers;
4. task-worktree-aware hook settings/environment;
5. cross-worktree primary-checkout semantic protection;
6. primary-baseline PostToolBatch checks;
7. package-owned candidate staging/commit;
8. exact-SHA disposable verification worktree;
9. deterministic candidate verification using existing verification command authority;
10. independent Task Verifier protocol;
11. existing promotion-broker task admission integration;
12. accepted-task integrity records/readiness validation;
13. headless supervisor automatic task candidate/verify/accept progression;
14. P4 task CLI diagnostics/operator actions;
15. P4 documentation/tests.

P4 must **not** implement:

- concurrent active product-task writers;
- automatic arbitrary rebase/cherry-pick of stale verified candidates;
- cross-task Claude session migration/adoption;
- multi-file Planning Repair/RepairEnvelope;
- new generic remote-promotion policy competing with existing broker policy;
- arbitrary shell execution from TaskSpec verification prose;
- destructive primary-checkout cleanup;
- RC4 `main` promotion, release tag or release-final manifest.

## 40. Exit criterion

P4 is complete only when:

- a dependency-safe TaskSpec is executed in a package-owned resumable worktree;
- the worker cannot mutate the primary checkout or semantic task state even through opaque commands/Unattended;
- candidate assembly is package-owned and includes only P3-admitted promotable paths;
- TaskSpec verification prose cannot become command authority;
- deterministic verification runs against exact base/candidate states using existing safe command authority;
- an independent verifier attests one exact candidate SHA;
- only that exact verified candidate can be promoted;
- promotion + accepted-task recording is crash-recoverable and idempotent;
- no-op tasks can be accepted without fake commits;
- accepted-task records are integrity checked and drive dependency readiness;
- stale bases, dirty primary WIP and interrupted cleanup preserve work rather than silently deleting/rebasing it;
- after acceptance, current TaskSources are re-resolved before selecting the next task;
- all existing RC3/P1/P2/P3 behavior remains green.

Only then may a later RC4 phase generalise multi-file planning repair/session migration/concurrency or move toward final RC4 release qualification.
