# RC4 P6 — Migration, Session Adoption and Shadow Validation Protocol

Status: **IMPLEMENTED AND QUALIFIED**

Date: 2026-10-06

Branch: `release/1.0.0-rc4`

P5 qualified implementation: `8ada95010f6bdea52655db5432b768b3c48b4c82`

P5 formal closure: `ff0f6ba6b914037a816145a1d78cb05cb71091b9`

P6 protocol red-team closure: explicit future-schema zero-write preflight; no silent stale-base WIP rebase; imported acceptance remains claims until P4 re-attestation; external Claude conversation adoption uses current native resume with a fresh forked RC4-owned session; shadow mode is data-only/read-only.

P6 qualified functional baseline: `02b607118148357aea5362a9990e90dc8f19261c`

P6 final qualified implementation/docs/hostile head: `53b741ac3308197dccaeeedfa6d54dd22ea47bdd`

Qualification evidence at the final head:

- 567/567 tests passed;
- the same 567/567 tests passed again under coverage;
- aggregate measured coverage: 16,183 statements / 5,482 misses = 66%;
- critical regression groups: 15/15, 19/19, 25/25, 19/19 and 18/18 passed;
- public-baseline audit, package static checks and branch/version identity passed;
- `main` remained at accepted RC3 SHA `d75f7c48dfe53d1a1759ee7828d9a42f5f744569`;
- no RC4 tag, `main` promotion or release-finalisation request was created by P6.

## 1. Purpose

P6 closes the migration gap between an already-running supervised repository and the RC4 universal governance/runtime model.

P6 must preserve durable engineering progress without treating legacy state, a resumed Claude conversation, or a bespoke harness as trusted authority.

P6 covers:

- package state-schema migration;
- legacy one-file planning-policy migration;
- in-flight legacy planning-repair migration/recovery;
- normalised legacy task-state import;
- explicit Claude conversation/session adoption;
- read-only shadow validation against an existing/bespoke harness.

P6 does not weaken P1-P5 authority, verification, worktree, RepairEnvelope or promotion semantics.

## 2. Security invariant

Migration preserves evidence and progress; it does not mint authority.

Precedence remains:

```text
current Git truth
    >
current committed governance / AuthoritySet
    >
current TaskSourceSet / TaskSpec
    >
current P4/P5 exact-SHA evidence
    >
P6 migration/adoption records
    >
legacy harness state / imported claims
    >
resumed conversation memory
```

Therefore:

- imported "accepted task" claims are not automatically P4 AcceptedTaskRecords;
- imported "verified through SHA" is not a current promotion attestation;
- a legacy verifier result is not silently upgraded to a P5/P4 exact-SHA attestation;
- a resumed Claude session cannot self-expand its current TaskSpec/RepairEnvelope;
- shadow-mode agreement does not mutate product/planning state;
- disagreement is never automatically "fixed" by copying legacy state into RC4.

## 3. State schema v10

P6 advances the primary repository state schema from 9 to 10.

Schema 10 adds migration/adoption bookkeeping such as:

- `state_schema_migration`;
- `legacy_adoption`;
- `session_adoption`;
- `shadow_validation`;
- `migration_generation`.

P6 must stop using an unconditional:

```python
state["schema_version"] = max(old, CURRENT)
```

as the migration mechanism.

Unknown future schemas fail closed.

```text
state.schema_version > CURRENT_STATE_SCHEMA
    -> BLOCK: package is older than durable state
```

Known older schemas run explicit migration steps before normal activation writes current fields.

The schema preflight happens **before** activation persists a new governance snapshot, generated settings, profile output or other current-version semantic state. Read-only repository profiling/governance inspection may be used to decide a migration, but an unknown future state schema must cause a zero-semantic-write refusal.

## 4. Migration transaction

State migration runs while the package owns the existing repository SupervisorLease.

Before the first semantic migration write P6 persists an external migration intent under the repository state directory, for example:

```text
migrations/
  active.json
  history/
```

Canonical migration record binds:

- schema version;
- migration ID;
- source schema;
- target schema;
- repository identity;
- current Git HEAD;
- state.json generation/digest;
- exact paths that may be changed by the migration;
- before-file SHA-256/mode/existence records;
- lifecycle state;
- resulting after-file digests.

Finite lifecycle:

- `PREPARING`
- `APPLYING`
- `VERIFYING`
- `COMPLETED`
- `BLOCKED`

Unknown states fail closed.

Migration writes are crash-safe and idempotent.

A retry after interruption reconciles the exact recorded before/after identities; it never blindly reruns a destructive step.

## 5. Files a migration may change

P6 migration may change only package-owned external state and package-owned migration records.

It must not modify:

- repository product files;
- repository governance;
- Git refs;
- user primary-checkout WIP;
- Claude conversation transcripts;
- repository verification configuration.

A migration that needs repository semantics reads exact Git truth but does not rewrite repository content.

## 6. State migration chain

P6 defines explicit known-schema migration steps.

At minimum:

- pre-schema-8/legacy fields -> existing hardened state layout;
- schema 8 -> schema 9 governance/task defaults;
- schema 9 -> schema 10 migration/session/shadow defaults.

Each step:

1. validates its input schema and required invariants;
2. preserves unknown non-conflicting legacy fields unless they are known unsafe;
3. invalidates stale completion/verification evidence when its authority binding can no longer be proved;
4. writes the next schema;
5. verifies the result before proceeding.

No step skips directly across an unimplemented schema merely because target defaults can be inserted.

## 7. Existing state-directory identity migration

The existing stable repository-identity migration remains the first outer-state reconciliation layer.

P6 does not replace it.

Required ordering:

1. acquire the stable external repository lease;
2. locate/migrate one unambiguous legacy state directory using existing Git-lineage identity rules;
3. refuse migration while an older supervisor is live;
4. only then run schema migration inside the resulting state directory.

Multiple equally plausible legacy directories remain a typed blocker. P6 never merges them heuristically.

## 8. Legacy one-file planning-policy migration

Legacy canonical-plan configuration remains first-class.

P6 does not rewrite the repository merely to convert a one-file policy into a governance contract.

When legacy `planning-repair/policy.json` is present P6 validates:

- schema;
- canonical plan path;
- canonical plan is tracked at the relevant exact base;
- product branch;
- remote/remote branch if configured;
- planning attestation contract.

P6 then records a migration-normalised policy identity that binds the legacy policy bytes to the same synthetic `default` AuthoritySet semantics used by P1/P5.

The legacy policy file remains readable for backwards compatibility.

P6 may add migration metadata, but it must not silently alter its substantive product-branch/remote/plan authority.

## 9. In-flight legacy planning-repair migration

P5 deliberately leaves schema-1 active planning-repair state for P6.

P6 migrates it only after exact reconciliation.

Input evidence may include:

- canonical plan;
- base SHA;
- product branch;
- repair branch;
- worktree path;
- candidate SHA;
- verified SHA;
- architect/verifier metadata;
- refresh state.

### 9.1 Worktree/branch recovery first

Before conversion:

- recover/reconcile the exact recorded legacy worktree/branch using the proven legacy recovery path;
- prove base SHA exists;
- prove recorded branch/worktree identity;
- preserve uncommitted repair work;
- reject unexpected non-planning product changes.

If both branch and worktree are missing, P6 does not fabricate lost repair work.

### 9.2 Derive current RepairEnvelope authority

P6 derives a P5 RepairEnvelope for the **recorded legacy base**, not by assuming current HEAD is equivalent.

For a one-file legacy policy this must resolve to the synthetic one-file AuthoritySet.

Migration requires that the new envelope still authorises exactly the legacy canonical-plan repair surface.

If current committed governance/policy makes the legacy repair ambiguous or broader/narrower in a way that cannot be mechanically reconciled, migration blocks with the worktree preserved.

### 9.3 Candidate migration

If no candidate exists:

- migrate to schema-2 `ACTIVE` or `PREPARING` as appropriate;
- bind the new RepairEnvelope;
- continue through P5 normally.

If an exact legacy candidate exists:

- prove it is based on the recorded base;
- prove its planning delta is inside the newly derived RepairEnvelope;
- rebuild candidate AuthoritySet/TaskSources under P5;
- migrate the exact candidate SHA into schema-2 state.

Legacy `verified_sha`/verifier claims are preserved only as migration evidence.

They are **not** converted into a P5 planning attestation.

The migrated candidate must rerun current reconcilers/validators/independent verifier/attestation before promotion.

## 10. Legacy repair migration crash ordering

In-flight repair migration is itself a transaction.

Required ordering:

1. persist P6 migration intent with exact schema-1 state digest;
2. reconcile branch/worktree;
3. persist new RepairEnvelope;
4. validate exact candidate/worktree delta if present;
5. persist schema-2 active state with migration provenance and stale verifier evidence cleared;
6. verify P5 loader/recovery can reopen it;
7. mark migration completed;
8. archive the legacy state snapshot.

A crash between steps must resume from exact recorded identities.

## 11. Normalised legacy state-adoption document

P6 accepts an operator-provided read-only state-adoption document.

Canonical v1 shape is conceptually:

```json
{
  "schema_version": 1,
  "source_system": "legacy-harness",
  "source_state_id": "...",
  "product_sha": "...",
  "verified_through_sha": "...",
  "authority_snapshot_sha256": "...",
  "active_task": {
    "id": "T123",
    "task_spec_sha256": "...",
    "base_sha": "..."
  },
  "accepted_tasks": [
    {
      "id": "T100",
      "task_spec_sha256": "...",
      "accepted_product_sha": "..."
    }
  ],
  "blockers": [],
  "reservations": []
}
```

The import format is project-neutral.

P6 public tests use neutral synthetic repositories.

No real project name/path is hard-coded into the runtime.

## 12. Adoption import is evidence, not authority

Importing the document creates a package-owned `AdoptionRecord`.

It does **not** directly write:

- `accepted_tasks`;
- active ExecutionEnvelope;
- P4 TaskWorkspaceRecord;
- planning attestation;
- completion verification state.

The importer first validates:

- input is one top-level-operator-selected regular file, not a symlink/device;
- document size is bounded (P6 v1 target: <= 2 MiB);
- document schema/UTF-8;
- current repository identity;
- current Git HEAD/product SHA relationship;
- current AuthoritySet/TaskSourceSet;
- referenced Git objects;
- task IDs and TaskSpec digests;
- accepted-product ancestry;
- conflicts with current P4/P5 active state.

Unresolvable claims are retained as rejected/discrepant evidence, not silently dropped or trusted.

## 13. Re-establishing accepted task progress

Imported accepted-task claims preserve progress without fabricating P4 acceptance.

For a claim to become a current P4 AcceptedTaskRecord, RC4 must re-establish current acceptance.

Preferred path:

1. resolve current exact TaskSpec;
2. prove imported task digest/product SHA relationship;
3. verify the current product state already contains the claimed work;
4. run current deterministic verification as required;
5. run current independent Task Verifier against the exact adopted product SHA;
6. create a current P4 no-op acceptance/attestation.

This may re-verify work but does not require reimplementing it.

Until re-attestation completes, imported accepted tasks are `ADOPTED_CLAIM`, not dependency-unlocking current acceptance.

## 14. Imported active task

An imported active task is treated as a requested mapping, not self-granted authority.

P6 resolves the current TaskSpec and checks:

- exact task ID;
- current TaskSpec digest;
- dependency readiness;
- imported base SHA relationship;
- current P4/P5 state.

If current primary checkout is clean for the relevant task, P6 may begin/reuse the normal P4 task workspace for that task.

If uncommitted primary WIP intersects the imported task's owned/evidence authority, P6 stops before normal P4 begin and requires explicit WIP adoption/checkpointing.

Declared runtime scratch/cache is not treated as durable implementation progress merely because it exists in the primary checkout.

It never silently discards or ignores that WIP.

## 15. Explicit primary-WIP adoption

P6 may provide an explicit top-level operator action to preserve legacy active-task WIP.

The operation:

1. requires a current resolved TaskSpec;
2. captures exact primary WIP baseline;
3. requires the imported/current active-task base to equal the primary committed HEAD used for adoption; stale-base WIP is preserved but not automatically rebased;
4. proves every promotable task-related dirty/untracked path is inside current TaskSpec owned/evidence authority and outside protected/control state;
5. begins a clean P4 task worktree at the exact committed base;
6. copies only exact admitted owned/evidence implementation content into the task worktree;
7. verifies byte/mode identity;
8. leaves the primary checkout unchanged;
9. records an adoption receipt binding source primary WIP digest to the P4 task workspace.

Ignored build/cache files and runtime scratch are not scanned or copied by default. If an operator explicitly asks to preserve declared scratch, P6 may copy it as non-promotable workspace scratch under the existing envelope, but scratch can never become accepted product history.

Unexpected/out-of-envelope WIP blocks automatic adoption.

P6 does not stash/reset/delete primary WIP and does not silently rebase stale legacy WIP.

## 16. Imported blockers and reservations

Legacy blockers/reservations are preserved as adoption evidence.

They do not widen permissions.

A blocker may remain a current package blocker when it maps to a current unresolved external dependency or explicit operator constraint.

A legacy reservation does not create parallel-writer authority. RC4 remains single product writer by default.

## 17. Session adoption CLI

P6 exposes:

```bash
claude-auto start --resume-session <session-id-or-name>
```

The flag is explicit top-level operator intent.

P6 does not expose arbitrary transcript-path resume through this wrapper in RC4 v1, even though current Claude Code supports it.

The supplied selector is treated as a conversation locator, not repository/task authority.

## 18. Current Claude resume behavior

As of the P6 design pass, current Claude Code supports:

- `claude --resume <id-or-name>`;
- `--fork-session` to resume context under a new session ID;
- current settings/permission/profile flags on the new Claude launch;
- background-session attach behavior when resuming a session that is already running.

P6 must not accidentally attach to an already-running legacy/background process whose original guard/settings boundary is unknown.

P6 feature-detects the required native resume/fork flags. If the installed Claude Code cannot provide a fresh forked resume, adoption blocks rather than falling back to in-place attach.

## 19. External-session adoption uses a fresh RC4-owned process

For migration/adoption of a session not already owned by the current RC4 supervisor, P6 uses native resume **with a fresh session identity**:

```bash
claude --resume <selector> --fork-session ...
```

This preserves conversation context while ensuring:

- current RC4 generated settings are loaded;
- current profile/permission flags apply;
- current hooks/guards apply;
- a new process/session is owned by the current supervisor;
- a running legacy background session is not merely attached as-is.

Existing RC4 profile hot-switch continuity may keep its already-owned exact session-ID reuse behavior.

P6 does not force all internal profile switches to fork.

## 20. Session adoption preflight

Before launching an adopted conversation P6:

1. acquires/reconciles repository state;
2. completes schema migration;
3. validates current repository identity;
4. validates current governance/TaskSourceSet;
5. reconciles active P4 task workspace or P5 planning repair;
6. determines the effective working directory;
7. generates current settings/hooks;
8. persists a `PREPARING` SessionAdoptionRecord.

Only then may Claude be launched with `--resume ... --fork-session`.

## 21. SessionAdoptionRecord

Canonical semantic fields include:

- schema version;
- source selector hash/type;
- source selector display value when safe;
- repository identity;
- effective working directory;
- source/current product SHA;
- governance snapshot digest;
- TaskSourceSet digest;
- active task/workspace/envelope digests when present;
- active planning RepairEnvelope when present;
- autonomy profile;
- settings digest;
- lifecycle state;
- adopted Claude session ID when known.

Finite states:

- `PREPARING`
- `LAUNCHED`
- `ADOPTED`
- `BLOCKED`
- `ENDED`

Conversation contents are not copied into package state.

## 22. Capturing the adopted session ID

P6 records the exact new Claude session ID from the current runtime/SessionStart evidence already used by the package.

If the forked launch cannot provide an exact session ID, P6 does not claim successful adoption.

The original selector remains recorded only as migration provenance.

Future exact resumes use the adopted session ID.

## 23. Conversation memory cannot bypass current authority

A resumed conversation may contain stale instructions such as:

- old file ownership;
- old plan path;
- old permission assumptions;
- old profile;
- old task ID;
- old verifier result.

Those are conversation context only.

Every tool call remains constrained by current RC4 hooks/ExecutionEnvelope/RepairEnvelope/package state.

The worker prompt/checkpoint must state that current package authority overrides remembered conversation instructions.

If the old conversation expects a different task than the currently active TaskSpec, P6 reports the mismatch and does not silently reactivate the old task.

## 24. Session working directory

When a current P4 task workspace is active, adopted product-worker conversation resumes in that exact package task worktree.

When a P5 planning repair Architect session is being explicitly adopted, it resumes only in the exact planning repair worktree and under Planning Repair restrictions.

Otherwise it resumes in the coordinator repository root.

P6 never chooses a worktree merely because it resembles the old conversation's remembered path.

## 25. Shadow mode purpose

Shadow mode lets RC4 compare its independent governance/task decision with an existing/bespoke harness before replacing that harness.

Shadow mode is **read-only** with respect to:

- repository files;
- Git refs;
- P4 task state;
- P5 repair state;
- accepted-task state;
- Claude sessions.

It may write only package-owned shadow audit records.

## 26. Normalised ShadowObservation v1

Both RC4 and the legacy harness are represented in the same neutral observation schema.

Conceptual fields:

- product SHA;
- AuthoritySet IDs/digests;
- planning member roles/mutability;
- TaskSourceSet/task graph digest;
- dependency-safe READY frontier;
- active task;
- next task;
- accepted task claims/IDs;
- owned/evidence/scratch paths for active/next task;
- blockers/reservations;
- planning-repair state;
- verification-through SHA/evidence identity when available.

Missing legacy fields remain `unknown`; they are not guessed.

## 27. RC4 shadow snapshot

P6 provides a pure package calculation of the current RC4 observation.

It may resolve current governance/TaskSources read-only but must not:

- activate a task;
- create a task workspace;
- start Planning Repair;
- run a worker;
- promote anything.

Persisted runtime state may be read as evidence, but shadow calculation distinguishes:

- computed current truth;
- currently persisted active state;
- imported/legacy claims.

## 28. Legacy shadow input

P6 baseline shadow comparison accepts a normalised JSON observation supplied by the top-level operator.

This avoids executing arbitrary legacy harness code merely to compare it.

The legacy observation input is bounded (P6 v1 target: <= 2 MiB), must be a regular operator-selected file and is parsed as data only.

An optional future adapter may be supported only through an existing bounded read-only helper/adapter boundary with explicitly supplied inputs.

P6 must not shell out to an arbitrary bespoke harness command with unrestricted host access by default.

## 29. Shadow comparison

Comparison classifies each field/dimension as:

- `MATCH`
- `MISSING_LEGACY_EVIDENCE`
- `MISSING_RC4_EVIDENCE`
- `MISMATCH`

P6 does not automatically classify a mismatch as "understood".

An operator may attach a documented disposition to a discrepancy after review, but the raw mismatch remains in the audit record.

A replacement/field-qualification gate may require:

- zero unresolved authority/task-frontier mismatches;
- or an explicit reviewed disposition for every remaining difference.

## 30. Shadow audit record

Shadow comparison persists a package-owned immutable-style audit record containing:

- repository identity;
- product SHA;
- RC4 observation digest;
- legacy observation digest;
- per-field results;
- discrepancy count;
- reviewed dispositions;
- timestamp.

Shadow audit records do not modify current task/planning authority.

## 31. Shadow qualification target

P6/P7 release qualification requires at least one complex multi-ledger repository shadow comparison.

The public test suite uses neutral synthetic repositories.

A real field repository may be used as external qualification evidence without embedding its project name, paths or schema into the runtime.

## 32. Migration/adoption CLI

P6 may add a unified top-level family such as:

```bash
claude-auto migrate status --repo .
claude-auto migrate state --repo .
claude-auto migrate planning-repair --repo .
claude-auto migrate adopt-state --from legacy-state.json --repo .
claude-auto migrate adopt-wip --repo .
claude-auto shadow snapshot --repo .
claude-auto shadow compare --legacy legacy-observation.json --repo .
claude-auto start --resume-session SESSION
```

Names may be adjusted during implementation to preserve existing CLI consistency.

Read-only status/snapshot/compare commands do not require mutation authority.

Migration/adoption actions require top-level operator/package authority.

## 33. P6 qualification matrix

P6 must test at least:

### State migration

- new repository creates schema 10 directly;
- schema 9 -> 10 migration;
- older known schema chain -> 10;
- unknown future schema blocks;
- migration PREPARING crash;
- migration APPLYING crash;
- migration VERIFYING crash;
- corrupt current state with valid previous generation;
- migration does not rewrite repository files/refs;
- legacy state-directory migration then schema migration;
- active older supervisor blocks migration;
- two plausible legacy state directories block.

### Legacy planning policy/repair

- legacy one-file policy normalises to synthetic default authority identity;
- policy branch/plan mismatch blocks;
- schema-1 active repair no candidate -> schema-2 active;
- schema-1 PREPARING recovery;
- legacy branch exists/worktree missing recovery;
- worktree exists/branch missing blocks;
- both missing blocks without destroying state;
- uncommitted canonical-plan repair WIP preserved;
- unexpected product WIP in repair worktree blocks migration;
- candidate exact base relationship validated;
- candidate with only legacy plan change migrates;
- legacy verified candidate loses trusted verification/attestation status;
- migrated candidate reruns P5 validation/verifier;
- current governance incompatible with legacy repair blocks/preserves.

### Legacy task-state adoption

- well-formed adoption document imports as claims only;
- malformed/oversized/non-UTF8 document blocks;
- product SHA mismatch classified;
- unknown task ID classified;
- TaskSpec digest mismatch classified;
- accepted SHA missing/not ancestor classified;
- imported accepted task does not immediately unlock dependency;
- re-attested imported accepted task becomes P4 acceptance;
- imported active task maps to current TaskSpec;
- imported active task with stale base blocks;
- stale-base primary WIP is preserved but never silently rebased into a P4 workspace;
- imported task WIP requires explicit adoption;
- admitted primary WIP copied to P4 task worktree byte/mode exactly;
- primary checkout unchanged by WIP adoption;
- out-of-envelope/protected WIP blocks adoption;
- legacy blockers/reservations preserved without widening authority.

### Session adoption

- CLI accepts session ID;
- CLI accepts session name;
- P6 invokes native resume with forked fresh session identity;
- current settings/profile/hooks included;
- exact adopted session ID recorded from runtime evidence;
- missing adopted session ID fails adoption claim;
- stale conversation task cannot widen current envelope;
- current P4 task resumes in exact task worktree;
- current P5 Architect adoption resumes only under repair guard;
- no active task/repair resumes in coordinator root;
- state/governance mismatch blocks before Claude launch;
- already-running legacy/background session is not attached in-place;
- missing upstream fork-resume capability blocks instead of attaching/reusing unsafe process state;
- RC4 internal profile switch exact-session continuity still works.

### Shadow mode

- RC4 snapshot is read-only;
- legacy observation JSON normalises deterministically;
- exact match;
- task-frontier mismatch;
- owned/evidence-path mismatch;
- AuthoritySet mismatch;
- missing evidence classifications;
- persisted active task differs from computed frontier;
- compare writes only shadow audit state;
- reviewed disposition never erases raw mismatch;
- complex synthetic multi-ledger repository;
- large observation remains bounded.

### Regression

- P1-P5 tests remain green;
- legacy one-file planning remains first-class;
- no P6 action promotes RC4 to main or creates release tag.

## 34. Implementation sequence

P6 proceeds in independently qualified slices:

1. explicit state-schema migration framework and schema 10;
2. legacy planning-policy normalisation;
3. schema-1 in-flight planning-repair migration;
4. normalised AdoptionRecord import;
5. accepted-task re-attestation/adopted active-task mapping;
6. explicit primary-WIP adoption into P4 workspace;
7. operator-facing session resume/adoption;
8. shadow snapshot + comparison;
9. docs/hostile/interruption qualification.

Do not combine the whole phase into one unqualified migration commit.

## 35. Implementation boundary

P6 may modify/extend:

- `lib/repo_runtime.py`;
- `lib/repo_identity.py`;
- `lib/state_store.py`;
- `lib/planning_repair.py`;
- P4 task adoption/acceptance helpers where current verification must re-attest legacy progress;
- `lib/claude_auto.py`;
- `lib/cli_schema.py`;
- settings/runtime-event hooks needed to record adopted session identity;
- a focused `lib/migration.py` / `lib/state_adoption.py` / `lib/shadow_validation.py` if that keeps boundaries testable;
- migration/session/shadow docs/tests.

P6 must not:

- weaken P1-P5 exact authority;
- import legacy accepted-task claims directly into trusted P4 acceptance;
- trust a legacy verifier result as a current attestation;
- attach an unknown running background Claude process and call that migration;
- execute arbitrary legacy harness code unsandboxed for shadow mode;
- destructively reset/stash/delete primary WIP;
- implement P7 release promotion/tagging.

## 36. Exit criterion

P6 is complete only when:

- all supported old state schemas migrate explicitly and crash-safely to current schema;
- legacy one-file policy remains semantically identical after migration;
- schema-1 in-flight planning repair can be preserved/recovered into P5 without fabricating verification;
- a normalised legacy harness state can be imported without discarding claims/progress or trusting them blindly;
- accepted legacy work can be re-attested without reimplementation;
- active legacy WIP can be explicitly preserved into a P4 workspace without modifying the primary checkout;
- an operator can resume an existing Claude conversation under a fresh RC4-owned guarded process;
- conversation memory cannot override current repository/task/planning authority;
- shadow mode independently computes RC4 governance/task state and compares it read-only to a neutral legacy observation;
- unresolved shadow mismatches are visible/auditable;
- all P1-P5 behavior remains green.

Only then may P7 begin adversarial/topology/release qualification.


## 37. Closure record

RC4-P6 is formally closed.

The qualified implementation now provides:

- explicit crash-safe durable-state migration to schema 10, including PREPARING/APPLYING/VERIFYING recovery and fail-closed refusal of unknown future schemas;
- preservation/normalisation of the legacy one-file planning-policy path as the synthetic default AuthoritySet;
- crash-recoverable migration of in-flight legacy Planning Repair into current P5 RepairEnvelope semantics without transferring legacy verifier/attestation trust;
- bounded legacy harness-state import as provenance-bearing claims rather than current authority;
- current-P4 re-attestation of eligible imported accepted-task claims before they can satisfy dependencies;
- current active-task mapping only when the imported/current TaskSpec and base are compatible;
- explicit byte/mode/deletion-preserving WIP adoption into the P4 task worktree while leaving the primary checkout unchanged and rejecting protected/out-of-envelope/stale progress;
- operator-facing migration/adoption surfaces through `claude-auto migrate status|state|planning-repair|adopt-state|reattest|adopt-active|adopt-wip`;
- side-effect-free migration status for absent adoption state and fail-closed reporting of corrupt adoption records;
- external Claude conversation adoption through native resume plus a fresh forked RC4-owned session identity under current generated settings/hooks/profile;
- exact adopted-session identity proof from current SessionStart runtime evidence, with missing/reused identity blocking;
- current P4 task-worktree / P5 RepairEnvelope working-directory authority taking precedence over remembered conversation state;
- read-only/data-only RC4 shadow observation and legacy comparison with explicit MATCH/MISMATCH/MISSING evidence classes and durable external audit records;
- reviewed shadow dispositions that annotate but never erase raw mismatches;
- final interruption/topology qualification for migration VERIFYING recovery, legacy repair branch/worktree loss and preservation behavior;
- complete P1-P5 regression continuity.

P6 deliberately does not perform RC4 release promotion/tagging and does not claim that imported legacy evidence, legacy verifier output, or resumed conversation memory can mint current authority.

The next and final RC4 implementation phase is **P7 — adversarial and topology qualification**. P7 may repair defects exposed by qualification, but it is not a feature-expansion phase. It must execute the original RC4 scenario/stress/security/release gates before any `main` promotion or `v1.0.0-rc4` tag.
