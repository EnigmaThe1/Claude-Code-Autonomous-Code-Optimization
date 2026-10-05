# RC4 P3 — ExecutionEnvelope, Task Activation and Diff Admission Protocol

Status: **IMPLEMENTED AND QUALIFIED**

Date: 2026-10-05

Branch: `release/1.0.0-rc4`

P2 closure: `1c63a031387e96ee827f72925c8be02bba85c343`

P2 qualified implementation: `3342f289c4f9903e96e5bd21575af86998c73451`

P3 qualified implementation: `0488dd23b9fe3e2ed65d84877ce411dfb18bf14f`

P3 qualified documentation head: `2cb45905b5f574c8311b67837eec3b71d2427674`

Qualification evidence:

- implementation/topology head: 439/439 tests passed, then 439/439 passed again under coverage;
- documentation head: 439/439 tests passed, then 439/439 passed again under coverage;
- critical regression groups: 15/15, 19/19, 25/25, 16/16 and 18/18 passed;
- package static checks and public-baseline reference audit passed;
- aggregate measured coverage at closure: 64% across the expanded RC4-P3 code surface;
- no RC4 promotion, tag or `main` update was performed by P3.

## 1. Purpose

P3 turns one verified repository-owned TaskSpec into a package-owned active **ExecutionEnvelope**.

P3 is the enforcement/admission phase between deterministic TaskSpec resolution and P4's dedicated task-worktree/acceptance flow.

P3 must ensure that:

- the model cannot activate arbitrary task authority;
- direct file tools cannot edit repository product paths outside the active task envelope;
- obvious shell writes cannot escape the envelope;
- every completed Claude tool batch is reconciled against exact repository state;
- staged/target diffs cannot contain out-of-envelope product paths;
- runtime scratch is never promotable merely because it was writable;
- protected planning/governance/package state retains precedence in every runtime profile;
- an out-of-envelope mutation cannot be accepted through Claude Auto's promotion path.

P3 does **not** provide a disposable task worktree or final accepted-task verifier/promotion transaction. Those remain P4.

## 2. Current Claude Code hook contract

The P3 design was rechecked against the current official Claude Code hooks reference on 2026-10-05:

`https://code.claude.com/docs/en/hooks`

Relevant current behavior:

- `PreToolUse` fires before a tool call and can block it.
- `PostToolUse` fires after a successful tool call; side effects have already happened.
- parallel tool calls can produce concurrent `PostToolUse` hooks.
- `PostToolBatch` fires exactly once after every tool call in the batch resolves and before the next model call.
- `PostToolBatch` can block the agentic loop before the next model call.

Therefore P3 uses:

1. `PreToolUse` for deterministic pre-execution authority checks;
2. one package-owned `PostToolBatch` repository-state gate for authoritative after-batch reconciliation.

P3 does not pretend a post hook can undo filesystem/network side effects that already occurred.

## 3. Task-owned mode

A repository is in task-owned mode when:

- a governance contract declares one or more TaskSources;
- the current TaskSourceSet has been resolved and its digest is bound to the current durable state generation.

In task-owned mode:

- the outer supervisor/package owns task selection and activation;
- a Claude worker may recommend a task but cannot activate one through a worker-invokable authority command;
- no active ExecutionEnvelope means no authorised product mutation round;
- ordinary RC3 repositories with no TaskSources remain unchanged.

## 4. Task readiness and selection

P3 computes a deterministic dependency-safe frontier from the **verified current TaskSourceSet**.

A local dependency is satisfied only by a durable accepted-task record whose:

- task ID matches;
- accepted TaskSpec SHA-256 matches the current dependency TaskSpec SHA-256;
- accepted product SHA is an ancestor of the current product base.

P3 itself does not create accepted-task records; P4 will define accepted-task verification/promotion.

A dependency that is absent from the local TaskSourceSet in non-strict dependency mode is an unresolved external dependency and blocks automatic activation unless a later explicit external-dependency protocol resolves it.

P3 may expose deterministic READY candidates sorted by Task ID. Free-form TaskSpec metadata never grants or expands authority.

### 4.1 Activation authority

P3 adds package APIs for activation. Operator-facing activation is top-level-operator-only.

A Claude worker must not be able to run a CLI command that grants itself a different TaskSpec.

The supervisor may call the same package activation function internally after deterministic selection.

## 5. ExecutionEnvelope v1

Canonical semantic envelope:

```json
{
  "schema_version": 1,
  "task_id": "T123",
  "task_source_set_sha256": "...",
  "task_spec_sha256": "...",
  "authority_snapshot_sha256": "...",
  "product_base_sha": "...",
  "direct_edit_paths": ["src/component/**", "tests/component/**", "docs/evidence/T123/**"],
  "promotion_paths": ["src/component/**", "tests/component/**", "docs/evidence/T123/**"],
  "runtime_scratch_paths": [".build-cache/**"],
  "protected_paths": ["...exact current authority/control paths..."],
  "baseline_wip_sha256": "...",
  "accepted_tasks_sha256": "..."
}
```

Package persistence may add non-semantic audit fields such as creation time, but those fields are excluded from `execution_envelope_sha256`.

The semantic envelope binds the exact:

- current TaskSourceSet;
- active TaskSpec authority digest;
- current AuthoritySet snapshot;
- product/base commit;
- pre-task WIP identity;
- accepted-dependency state used to prove readiness.

Any bound identity change makes the envelope stale.

## 6. Path classes and precedence

For TaskSpec v1:

`direct_edit_paths = owned_paths + evidence_paths`

`promotion_paths = owned_paths + evidence_paths`

`runtime_scratch_paths = runtime_scratch_paths`

Precedence is:

```text
package/external-state protection
    >
repository governance/protected paths
    >
runtime scratch non-promotion rule
    >
task promotion/direct-edit allow
    >
model/tool request
```

A path matching both promotion and scratch selectors is treated as **scratch/non-promotable**. This fail-closed rule removes ambiguous overlap without requiring a model decision.

A protected path remains denied even when a broad task selector such as `**` would otherwise match.

## 7. Package-owned external state protection

P3 explicitly protects semantic-control state in every runtime profile.

The worker must not mutate:

- `CLAUDE_AUTONOMY_STATE_DIR`;
- the active ExecutionEnvelope record;
- TaskSourceSet records;
- governance snapshots;
- task violation/admission evidence;
- package runtime code when the installed package root is outside the selected product repository.

This protection remains active in Unattended and Isolated Full.

Unattended may grant broad host/runtime authority, but it does not grant authority to rewrite the harness's own durable semantic controls.

When the package runtime root and target repository are the same path in a source-development/test context, P3 must not blanket-protect the entire product repository merely because it contains harness source. Durable external state remains protected.

## 8. Baseline WIP

Before task activation, P3 captures an exact baseline for repository state.

At minimum:

- exact HEAD;
- staged tracked paths and index identities;
- unstaged tracked paths and exact current filesystem identities;
- visible untracked paths and exact current filesystem identities;
- deterministic baseline digest.

Ignored build/cache files are not treated as promotable merely because they are ignored.

### 8.1 Activation blockers

P3 rejects activation when:

- HEAD differs from the exact TaskSourceSet/product base;
- the current TaskSourceSet or AuthoritySet snapshot is stale/corrupt;
- another task/envelope is active;
- any staged WIP already exists;
- pre-existing dirty/untracked WIP intersects the new task's promotion/direct/scratch selectors;
- a dependency is unresolved;
- repository topology required for safe task enforcement is unavailable.

Unrelated pre-existing unstaged/untracked WIP may remain only if it is outside every active task selector and remains byte-identical throughout the task round.

This prevents the task from silently absorbing user WIP.

## 9. No HEAD movement during P3 task mutation rounds

P3 deliberately keeps the active task's `product_base_sha` fixed.

While an ExecutionEnvelope is active:

- raw worker `git commit` is denied;
- raw worker `git push` is denied;
- raw history-moving operations such as reset/rebase/merge/checkout-switch that would change the task base are denied;
- an opaque command that nevertheless changes HEAD is detected by the after-batch gate and becomes an envelope violation.

This keeps the P2 TaskSourceSet and AuthoritySet identities current throughout P3.

P4 will introduce the package-owned task worktree/commit/acceptance transaction.

## 10. Direct file-tool gate

`Edit`, `Write` and `NotebookEdit`:

- retain existing protected-path precedence;
- when target is inside the selected repository and an active envelope exists, require the exact target path to match `direct_edit_paths`;
- deny paths matching runtime scratch only when they are not also legitimate direct-edit paths; scratch never becomes promotable;
- deny repository product mutation when task-owned mode is current but no active envelope exists;
- preserve existing runtime-profile behavior for legitimate **outside-repository** host operations, except package-owned semantic state always remains protected.

Directory/ancestor mutation is denied when it can delete/replace protected or non-owned repository content.

## 11. Bash/process pre-gate

The existing write-boundary parser is extended rather than replaced.

For statically identifiable writes inside the repository:

- protected path -> deny;
- active task + file target not matching direct/promotion/scratch authority -> deny;
- destructive ancestor target that is not wholly covered by an allowed subtree -> deny;
- task-owned mode with no active envelope -> deny identifiable product writes.

For Git commands while an envelope is active:

- raw `git commit` -> deny in P3;
- raw `git push` -> deny;
- history/base-changing Git operations -> deny;
- staging commands may run, but staged state is authoritative only after the package stage gate validates it.

Opaque repository commands are not declared safe merely because their command string contains no obvious file target. Their effects are checked after the batch.

## 12. Authoritative after-batch mutation gate

P3 adds a package-owned `PostToolBatch` hook.

It runs after every Claude tool batch when a task envelope is active and computes current repository state against the exact activation baseline.

The gate rejects the task round when:

- HEAD changed from `product_base_sha`;
- a protected path changed;
- an unrelated baseline-WIP path changed;
- a new/changed visible tracked or untracked repository path matches neither promotion/direct authority nor runtime scratch authority;
- current staged state violates the stage gate;
- the envelope/TaskSourceSet/governance state became stale or failed integrity validation.

On violation P3:

- writes durable package-owned violation evidence;
- returns a blocking PostToolBatch decision so no next model call proceeds under invalid authority;
- keeps the offending primary-checkout state intact rather than performing destructive automatic cleanup.

P4's disposable task worktree will add safe discard/recovery semantics.

## 13. Workspace delta semantics

P3 compares the **current state against the activation baseline**, not merely against a clean checkout.

Therefore:

- unrelated pre-existing WIP that remains exact is not attributed to the task;
- deleting/modifying that WIP becomes a violation;
- newly dirty paths are task mutations;
- a baseline-dirty path cannot be task-owned because activation rejects that overlap;
- staged state has no pre-existing baseline because activation rejects any existing staging.

Ignored files are not promotion authority. If an ignored path is force-staged, the stage gate evaluates it normally.

## 14. Staged-diff gate

P3 provides a package-owned staged-diff validator.

Requirements:

- NUL-safe Git output;
- exact current HEAD must equal envelope base;
- every staged source/destination path must be in `promotion_paths`;
- any path matching `runtime_scratch_paths` is rejected from promotion;
- protected paths are rejected;
- rename/copy source and destination are both evaluated;
- deletions are evaluated by deleted path;
- executable-bit-only and binary changes use the same path authority;
- gitlink/submodule boundary changes are blocked in P3 unless a later managed-child-repository protocol explicitly supports them.

The stage validator is reusable by P4 before package-owned commit creation.

## 15. Promotion target gate

The existing `promote-ff` broker remains the only package promotion system.

P3 adds an optional active-envelope gate to it.

If an ExecutionEnvelope is active, a candidate target must:

- be a descendant of the exact envelope base under existing broker rules;
- change only `promotion_paths`;
- change no scratch path;
- change no protected path;
- cross no unsupported gitlink/nested-repository boundary;
- preserve existing WIP/broker invariants.

P3 does not mark the task accepted merely because generic promotion passed. P4 adds exact task verification/acceptance semantics.

After a successful base-changing promotion, the old TaskSourceSet/envelope is stale and must be invalidated/re-resolved before another task activation.

## 16. Violation lifecycle

P3 persists a typed violation record containing at least:

- envelope digest;
- task ID;
- base SHA;
- detected current HEAD;
- violating paths/reasons;
- observed repository-state digest;
- hook/tool-batch identity when available;
- timestamp.

While an unresolved violation record is bound to the active envelope:

- further package task activation is blocked;
- package promotion is blocked;
- the operator/status surface explains the violation.

P3 does not automatically delete or reset primary-checkout changes.

A package reconciliation command may clear the violation only after exact repository state again satisfies the active envelope/baseline invariants.

## 17. Deactivation

P3 task deactivation is allowed only when:

- the envelope is current and valid;
- no unresolved violation exists;
- current repository state is exactly the activation baseline.

P3 therefore cannot be used to drop task enforcement while task mutations remain in the primary checkout.

P4 replaces this limitation with isolated task-worktree cleanup/acceptance.

## 18. Operator/inspection surfaces

P3 extends the existing plural `tasks` command family.

Target surface:

```bash
claude-auto tasks status --repo .
claude-auto tasks show TASK_ID --repo .
claude-auto tasks explain TASK_ID --repo .
claude-auto tasks activate TASK_ID --repo .
claude-auto tasks validate-stage --repo .
claude-auto tasks reconcile --repo .
claude-auto tasks deactivate --repo .
```

Rules:

- `status`, `show`, `explain` and `validate-stage` are read-only;
- activation/deactivation/reconciliation are package state-authority operations;
- operator-facing activation/deactivation/reconciliation reject invocation from an active Claude worker;
- the outer supervisor may call package functions directly;
- status/explain reports exact authority layer and reason for every blocker.

## 19. Durable state

P3 populates the schema-9 fields already reserved by P1/P2:

- `active_task_id`;
- `active_task_spec_sha256`;
- `active_execution_envelope_sha256`.

Full ExecutionEnvelope and violation evidence live in package-owned external state.

The envelope loader recomputes semantic integrity and checks:

- state-field bindings;
- current TaskSourceSet integrity;
- current governance snapshot;
- product base/HEAD;
- task record/digest;
- baseline identity constraints appropriate to the requested operation.

No hook trusts raw external JSON without digest/state binding validation.

## 20. Runtime-profile semantics

Governance remains semantic authority, not a permission profile.

Balanced/Strict:

- keep existing repository/host boundaries;
- additionally enforce task envelope authority.

Unattended/Isolated Full:

- may keep their broad host/runtime authority;
- still enforce package state, planning/control and active task product authority;
- cannot use `bypassPermissions` to disable the task envelope.

The task envelope is therefore profile-independent.

## 21. Repository topology in P3

P3 qualification must cover:

- ordinary Git repository;
- linked worktree identity;
- monorepo/shared root files;
- dirty primary checkout with unrelated WIP;
- detached HEAD blocker for mutating activation;
- sparse checkout with required task paths absent;
- shallow/partial clone object/ancestry failure;
- nested repository/submodule boundary;
- symlink product paths;
- case/Unicode/path-edge behavior;
- large task/path sets.

P3 does not create a task worktree; P4 owns that topology transition.

## 22. P3 hostile/qualification matrix

P3 must test at least:

- activate one root task;
- deterministic READY frontier;
- dependency accepted with matching TaskSpec digest;
- stale/mismatched accepted dependency;
- unresolved external dependency;
- active-task collision;
- stale/corrupt TaskSourceSet at activation;
- stale governance at activation;
- HEAD differs from TaskSourceSet base;
- pre-existing staged WIP;
- unrelated unstaged WIP preserved;
- WIP intersects owned/evidence/scratch selector;
- direct Write/Edit inside owned path;
- direct evidence write;
- direct write outside envelope;
- direct write to scratch-only path;
- protected path under broad selector;
- task-owned mode with no active envelope;
- file/directory ancestor destructive write;
- semantic-only Unattended outside-host write still allowed;
- semantic-only Unattended package state write denied;
- semantic-only Unattended out-of-task repository write denied;
- raw `git commit` denied while active;
- raw `git push` denied while active;
- raw HEAD-moving Git operation denied;
- opaque command changes allowed owned path and passes after-batch check;
- opaque command creates allowed scratch and passes;
- opaque command changes outside envelope and blocks batch;
- opaque command changes unrelated baseline WIP and blocks;
- opaque command changes HEAD and blocks;
- parallel tool batch yields one deterministic post-batch verdict;
- staged allowed product path passes;
- staged evidence path passes;
- staged scratch path fails;
- staged outside-envelope path fails;
- staged protected path fails;
- rename both endpoints evaluated;
- deletion evaluated;
- executable-bit-only change evaluated;
- binary file evaluated;
- gitlink change blocked;
- forced staging of ignored out-of-envelope file fails;
- promotion target only inside envelope passes path admission;
- promotion target includes scratch fails;
- promotion target includes protected/outside path fails;
- promotion invalidates old envelope/TaskSourceSet binding;
- corrupted envelope JSON fails closed;
- semantically tampered envelope fails closed;
- state/envelope digest mismatch fails closed;
- violation marker blocks promotion/activation;
- reconcile clears violation only after exact valid state;
- deactivate with task delta fails;
- deactivate at exact baseline succeeds;
- existing RC3/P1/P2 tests remain green.

## 23. Implementation boundary

P3 may implement:

1. `lib/task_authority.py`;
2. `lib/execution_envelope.py`;
3. exact baseline-WIP and task-delta helpers;
4. active-envelope persistence/integrity loading;
5. deterministic readiness/activation;
6. direct-write and Bash task-envelope enforcement;
7. package semantic-state protection;
8. `hooks/task_post_batch_guard.py`;
9. staged-diff validation;
10. active-envelope promotion-path admission;
11. `tasks show/explain/activate/validate-stage/reconcile/deactivate`;
12. P3 documentation/tests.

P3 must **not** implement:

- package-owned task worktree creation/removal;
- automatic destructive cleanup of primary-checkout violations;
- accepted-task exact-SHA verifier workflow;
- final task commit/acceptance transaction;
- multi-file Planning Repair/RepairEnvelope;
- session migration/adoption;
- concurrent product writers;
- RC4 promotion/tagging.

## 24. Exit criterion

P3 is complete only when:

- a current verified TaskSpec can be activated into one exact package-owned ExecutionEnvelope;
- direct Claude repository edits are bounded by that envelope;
- every tool batch is checked against exact baseline state;
- staged/promotion admission rejects scratch/protected/out-of-envelope paths;
- package semantic state cannot be rewritten through broad runtime profiles;
- task-owned repositories cannot mutate product state without an active envelope;
- no out-of-envelope change can pass Claude Auto's acceptance/promotion boundary;
- RC3/P1/P2 behavior remains green.

Only then may P4 move task execution into a package-owned resumable task worktree.


## 25. Closure record

RC4-P3 is closed at the protocol/implementation level.

The qualified implementation satisfies the exit criteria above and includes:

- deterministic package-owned selection/activation of a dependency-safe repository TaskSpec;
- semantic-integrity-checked ExecutionEnvelope persistence bound to current TaskSourceSet, TaskSpec, AuthoritySet, product base, accepted-dependency state and exact pre-task WIP;
- direct Write/Edit/NotebookEdit and statically visible Bash mutation fencing;
- profile-independent protection of package semantic state and repository governance/control surfaces;
- one authoritative PostToolBatch repository-state gate for opaque/parallel tool effects before the next model turn;
- deterministic violation evidence and non-destructive reconciliation;
- NUL-safe staged-diff admission for ordinary, rename/copy, deletion, mode-only, binary and path-edge changes;
- active-envelope promotion-path admission through the existing promote-ff broker;
- validated promotion handoff that closes stale authority without falsely recording an envelope violation;
- automatic supervisor activation/reuse before mutating headless or supervised interactive workers;
- active TaskSpec context in the headless worker checkpoint without treating free-form TaskSpec metadata/model prose as authority;
- explicit qualification of linked worktrees, detached HEAD, sparse checkout, shallow/missing-history ancestry, missing Git objects, gitlinks/submodules, symlink traversal, Unicode/space/leading-dash paths and large selector sets.

P3 intentionally leaves the following to P4:

- package-owned disposable/resumable task worktree creation and recovery;
- package-owned task commit creation;
- exact-SHA independent task verification and acceptance;
- durable accepted-task records as a completed-task transaction;
- task-worktree cleanup after accepted or abandoned work.

Therefore the next RC4 phase may begin from the P3 closure without changing P3 semantics.
