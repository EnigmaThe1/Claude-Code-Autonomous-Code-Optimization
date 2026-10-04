# RC4 P0 Architecture Closure

Status: **CLOSED — implementation may proceed to RC4-P1**

Date: 2026-10-04

Base implementation reviewed: **1.0.0-rc3** at `d75f7c48dfe53d1a1759ee7828d9a42f5f744569`

Planning branch: `release/1.0.0-rc4`

This document freezes the RC4-P0 decisions required by `RC4_UNIVERSAL_REPOSITORY_GOVERNANCE_PLAN.md`. It is deliberately implementation-facing: where the planning baseline was conceptual, this document fixes the v1 schema, migration invariants, attestation binding, Claude Code platform floor, and the RC3 seams that must be changed without weakening existing behaviour.

## 1. P0 conclusion

The AuthoritySet + TaskSpec + isolated-worktree direction is sound, but RC3 contains four concrete single-authority assumptions that must not leak into RC4:

1. the planning policy stores one `canonical_plan`;
2. normal-worker protection is currently skipped by the `unattended` / unrestricted settings path;
3. promotion policy currently requires at most one attestation contract and validates only target SHA + contract;
4. the supervisor's repository-plan source check assumes one supplied plan path is the sole repository planning authority.

RC4 therefore must not implement multi-file support by merely changing `canonical_plan: str` into `canonical_plans: list[str]`. The internal boundary must move to AuthoritySet snapshots, bound promotion requirements, and task/control-surface envelopes.

No architecture contradiction remains after this review. RC4-P1 may begin subject to the frozen rules below.

## 2. RC3 implementation seams reviewed

The P0 review checked the RC3 paths that currently carry planning, mutation and promotion authority:

- `lib/planning_repair.py`
- `lib/settings_policy.py`
- `hooks/write_boundary_guard.py`
- `hooks/planning_repair_guard.py`
- `lib/workspace_recovery.py`
- `lib/promotion_policy.py`
- `lib/repo_runtime.py`
- `lib/repo_profile.py`
- `lib/claude_auto.py`
- `lib/supervisor_support.py`
- `lib/state_store.py`
- `lib/git_trust.py`
- `lib/cli_schema.py`

Important compatibility facts:

- RC3 durable `state.json` is schema 8.
- RC3 planning-repair policy is external package-owned state and is not stored in the repository.
- RC3 planning repair already has the correct high-level security flow: dedicated worktree -> exact candidate SHA -> independent verifier -> promotion.
- RC3 trusted Git execution already disables repository hooks, fsmonitor and submodule recursion for broker operations.
- RC3 state writes are crash-safer and retain a checksummed previous generation.
- RC3 repair worktree recovery and advancing-base refresh are valuable primitives and must be generalised rather than replaced.

## 3. Security corrections discovered during P0

### 3.1 Unattended must not disable semantic governance

RC3 `settings_policy.py` protects the configured canonical plan only when `not unrestricted`. That is valid for RC3's host-permission interpretation but is incompatible with RC4 governance.

RC4 freezes this rule:

> Runtime profile authority and repository semantic authority are independent dimensions.

Therefore:

- Unattended may bypass ordinary user prompts and broad host-path restrictions that the user explicitly delegated;
- Unattended may **not** bypass AuthoritySet protection;
- Unattended may **not** bypass TaskSpec ownership;
- Unattended may **not** bypass control-surface protection;
- Unattended may **not** bypass exact-SHA/bound-attestation promotion requirements.

Implementation consequence: governance/task protected paths must be applied outside the `unrestricted` conditional. Only host/runtime permission restrictions may be relaxed by Unattended.

### 3.2 One attestation contract is not enough

RC3 promotion state supports one `required_attestation_contract`. This cannot safely represent:

- a candidate touching two authority domains;
- an authority repair plus a control-surface change;
- a task candidate whose verifier result must be bound to a TaskSpec/envelope snapshot;
- a planning attestation that becomes stale because the AuthoritySet changed while the candidate SHA remained locally reachable.

RC4 must add **bound promotion requirements**. A promotion can require zero, one, or several independently satisfied requirements.

Conceptual normal form:

```json
{
  "contract": "repository-planning-repair",
  "scope": "authority:default",
  "binding_sha256": "<64-hex>"
}
```

An RC4 attestation is valid only when all three match:

- exact target Git commit SHA;
- exact contract + scope;
- exact binding digest.

For planning repair, the binding digest must include the AuthoritySet snapshot and Repair Envelope. For task implementation, it must include the TaskSpec, ExecutionEnvelope, verification/control-surface snapshot and base identity relevant to acceptance.

Legacy RC3 unbound attestations remain readable only for legacy RC3-compatible flows where no RC4 AuthoritySet/TaskSpec governance is active. They must never satisfy a bound RC4 requirement.

### 3.3 Git-tree truth must be separated from working-tree materialisation

Authority membership and committed identities must be derived from trusted Git object/plumbing operations at an exact commit, not from a mutable index or filtered working-tree view.

RC4 freezes these rules:

- resolve committed membership from the exact Git tree;
- identify gitlinks by tree mode and never descend into them as ordinary parent-repository files;
- use blob/object identities for committed authority;
- compare the index/worktree separately for WIP/control-surface divergence;
- never let a checkout filter, textconv, external diff, fsmonitor or hook define broker truth;
- use `--no-ext-diff` and `--no-textconv` where porcelain diff output is unavoidable;
- prefer `ls-tree`, `cat-file`, raw/tree diffs and other plumbing for security-sensitive identity.

A repository may legitimately use filters for its own developer workflow. That does not make those filters a trusted authority source.

### 3.4 Repository code that interprets authority is itself authority

A custom adapter, validator or reconciler can change TaskSpec/planning meaning even if `.claude-auto/governance.json` itself is unchanged.

Therefore its implementation/configuration is a control surface. Repository-declared helpers must run under a package-owned capability runner with explicit read/write/network boundaries. A declaration may request capabilities; it cannot grant them.

## 4. Frozen governance contract v1

Repository contract path:

```text
.claude-auto/governance.json
```

The contract is optional. When absent, RC3-compatible behaviour remains available.

The contract must be:

- UTF-8 JSON;
- a tracked regular non-symlink file in a Git-backed repository;
- read from the exact committed Git object for activation/snapshot purposes;
- schema version 1;
- bounded in size and item count;
- rejected on duplicate JSON keys;
- rejected on unknown security-sensitive fields;
- rejected if the working tree/index contains a divergent unaccepted copy when governance activation would otherwise proceed.

### 4.1 Canonical v1 shape

```json
{
  "schema_version": 1,
  "planning_authority": {
    "sets": [
      {
        "id": "default",
        "members": [
          {
            "path": "PLAN.md",
            "role": "source",
            "repair": "repairable",
            "required": true
          }
        ],
        "validators": [],
        "reconcilers": []
      }
    ]
  },
  "tasks": {
    "sources": [],
    "execution_mode": "single-writer",
    "strict_dependencies": true
  },
  "control_surfaces": []
}
```

Top-level v1 keys are exactly:

- `schema_version`
- `planning_authority`
- `tasks`
- `control_surfaces`

Unknown top-level fields fail closed in v1.

### 4.2 Authority-set IDs

Authority-set IDs:

- are 1-64 characters;
- use `[A-Za-z0-9][A-Za-z0-9._-]*`;
- are unique after exact string comparison;
- reserve `default` for the ordinary one-domain shorthand;
- are never interpreted as paths.

One repository may define multiple sets. A one-file or ordinary multi-file repository uses one set named `default`.

### 4.3 Authority members

Each member has exactly:

```json
{
  "path": "relative/path",
  "role": "source|manifest|task_ledger|traceability|contract|projection|other",
  "repair": "immutable|repairable|generated",
  "required": true
}
```

`path` may be an exact path or a package-defined bounded glob.

Path rules:

- POSIX `/` separators in the contract regardless of host spelling;
- no absolute paths;
- no empty path or `.`;
- no `..` segment;
- no NUL/control characters;
- normalise Unicode to NFC for comparison;
- reject duplicate/case-fold-colliding authority members;
- reject a resolved symlink authority member;
- reject a gitlink as an ordinary file;
- reject expansion outside the repository Git tree;
- deterministic sorted expansion;
- required selector resolving to zero files fails closed;
- expansion is performed against the exact committed tree, not arbitrary untracked filesystem content.

Bounded patterns are frozen to a package-owned glob dialect. They are not shell globs and are never passed through a shell. `*` and `?` match inside one path segment; `**` may span path segments. Pattern expansion has explicit maximum match counts and path-length limits.

### 4.4 Validators and reconcilers

A v1 validator/reconciler declaration is data, never shell text:

```json
{
  "id": "validate-plan",
  "argv": ["python3", "tools/validate_plan.py"],
  "cwd": ".",
  "inputs": ["PLAN.md", "tools/validate_plan.py"],
  "outputs": [],
  "timeout_seconds": 120,
  "capabilities": {
    "network": [],
    "read_external": []
  }
}
```

Additional reconciler rule:

- `outputs` must resolve only to AuthoritySet members marked `generated`.

Rules:

- `argv` is an argument vector; RC4 does not execute it through a shell;
- `cwd` is repository-relative and bounded;
- repository implementation/config paths used by the helper are control surfaces;
- declared inputs are a read ceiling, not a grant to read the host;
- network is empty by default;
- external reads are denied by default and require operator/package policy;
- no validator may mutate repository state;
- reconciler writes are restricted to declared generated outputs in a dedicated package-owned worktree;
- stdout/stderr/time are bounded;
- environment is scrubbed and package-owned.

### 4.5 Task sources

Task source entries use the same non-shell principle.

Built-in v1 kinds:

- `json`
- `jsonl`
- `toml`
- `static`
- `adapter`

An adapter declaration includes an `argv`, bounded `cwd`, explicit input selectors and requested capabilities. Adapter output is JSON TaskSpec protocol only. The adapter cannot directly grant paths/capabilities.

### 4.6 Control surfaces

RC4 has package-owned mandatory control surfaces that repository configuration cannot remove. The repository `control_surfaces` list may only add more.

Mandatory classes include:

- `.claude-auto/governance.json`;
- `.claude-auto/verification.json`;
- declared adapter/validator/reconciler implementation/configuration;
- Claude project configuration that can affect the selected worker if that source is loaded;
- `.mcp.json` when relevant to the selected worker;
- `.gitattributes`;
- `.gitmodules`;
- repository Git configuration/hook/filter identity relevant to the work unit;
- package-owned external governance/promotion/task state.

A product task cannot include mandatory control surfaces in ordinary `owned_paths`. A legitimate control-surface change uses a separate explicit authority cycle and fresh verification.

## 5. Frozen AuthoritySet snapshot v1

The normalised snapshot is package-owned external state.

Canonical data includes:

```json
{
  "schema_version": 1,
  "repository_id": "<stable repo/worktree identity>",
  "product_head": "<exact commit>",
  "branch": "<branch or null>",
  "governance_blob": "<blob sha or null>",
  "sets": [
    {
      "id": "default",
      "members": [
        {
          "path": "PLAN.md",
          "role": "source",
          "repair": "repairable",
          "required": true,
          "git_mode": "100644",
          "blob": "<git object id>"
        }
      ],
      "validator_digest": "<sha256>",
      "reconciler_digest": "<sha256>"
    }
  ],
  "task_source_contract_digest": "<sha256>",
  "control_surface_digest": "<sha256>"
}
```

The snapshot digest is SHA-256 over canonical JSON with:

- UTF-8;
- sorted object keys;
- deterministic array ordering where order is not semantically meaningful;
- separators `,` and `:`;
- no timestamps inside the hashed object.

Timestamps and diagnostics are stored alongside the snapshot but outside the hashed canonical object.

A change to any security/authority-relevant field creates a new digest and invalidates dependent TaskSpec, RepairEnvelope, ExecutionEnvelope and attestation state.

## 6. Frozen TaskSpec v1

Normal form:

```json
{
  "schema_version": 1,
  "id": "T123",
  "authority_sets": ["default"],
  "depends_on": ["T120"],
  "owned_paths": ["src/component/**", "tests/component/**"],
  "evidence_paths": ["docs/evidence/T123/**"],
  "runtime_scratch_paths": [".build-cache/**"],
  "verification": ["test"],
  "commit_subject": null,
  "metadata": {}
}
```

Authority-relevant fields are all fields except free-form `metadata` and optional human-facing `commit_subject`.

Rules:

- IDs are globally unique within the merged TaskSourceSet;
- dependencies are IDs, never source-file positions;
- duplicate IDs across sources fail closed;
- cycles fail closed;
- missing dependencies fail closed when `strict_dependencies=true`;
- no task may own a mandatory control surface;
- no task may own planning authority except through the separate Planning Repair flow;
- an empty `owned_paths` is valid only for an explicitly non-mutating/evidence-only task representation defined by the adapter contract; otherwise it blocks mutation;
- task-source textual status is not completion truth unless the source contract explicitly declares compatible semantics;
- accepted completion is durable package-owned state.

## 7. Frozen ExecutionEnvelope v1

An active task is represented by an exact envelope:

```json
{
  "schema_version": 1,
  "task_id": "T123",
  "base_sha": "<exact commit>",
  "task_spec_sha256": "<sha256>",
  "authority_snapshot_sha256": "<sha256>",
  "control_surface_sha256": "<sha256>",
  "direct_edit_paths": [],
  "promotion_paths": [],
  "runtime_scratch_paths": [],
  "protected_paths": [],
  "baseline_wip_sha256": "<sha256>"
}
```

Security rules:

- `direct_edit_paths` may be narrower than `promotion_paths`;
- `runtime_scratch_paths` never imply promotion authority;
- tracked changes outside `promotion_paths` reject acceptance;
- protected-path precedence always wins over a task selector, including `**`;
- no active task means no product direct mutation when task-owned governance is enabled;
- post-command state comparison is mandatory because command strings cannot fully predict subprocess writes;
- the final staged/tree diff and promotion gate are authoritative even when a pre-command guard missed an indirect write.

## 8. Frozen RepairEnvelope v1

Planning repair is scoped by:

```json
{
  "schema_version": 1,
  "base_sha": "<exact commit>",
  "authority_set_ids": ["default"],
  "authority_snapshot_sha256": "<sha256>",
  "repair_reason": "...",
  "repairable_paths": [],
  "immutable_paths": [],
  "generated_paths": [],
  "allowed_new_members": [],
  "validator_ids": [],
  "reconciler_ids": []
}
```

The architect may directly modify only `repairable_paths`. It receives no Bash authority.

Generated updates are run by package orchestration after architect edits, under the declared reconciler boundary.

A repair touching multiple authority sets must name those sets explicitly. Unrelated sets remain immutable for that repair.

## 9. Bound promotion/attestation model v2

RC4 will retain RC3's promotion broker and extend it rather than creating a second broker.

### 9.1 Promotion requirement set

Before promotion, the broker computes the complete requirement set from:

- explicit operator promotion policy;
- changed AuthoritySet members;
- changed control surfaces;
- active TaskSpec/ExecutionEnvelope;
- any other package-owned security contract.

All requirements must be satisfied. The caller cannot suppress an implicit stronger requirement by passing a weaker explicit contract.

### 9.2 Binding digest

For planning repair, bind at least:

- target candidate SHA;
- repair base SHA;
- AuthoritySet snapshot digest;
- RepairEnvelope digest;
- affected authority-set IDs;
- validator/reconciler contract digest.

For task implementation, bind at least:

- target candidate SHA;
- task base SHA;
- TaskSpec digest;
- ExecutionEnvelope digest;
- AuthoritySet snapshot digest;
- control-surface digest;
- verification contract digest.

The exact target SHA remains a first-class field even though it is also included in the canonical binding object.

### 9.3 Storage identity

Attestation storage identity must distinguish two attestations with the same target and contract but different scope/binding. File naming therefore derives from target SHA plus a digest of the canonical requirement, not target + contract alone.

## 10. State migration: RC3 -> RC4

### 10.1 Main durable state

RC4 moves `state.json` from schema 8 to schema 9.

Migration is additive and crash-safe. Existing fields remain unchanged. New schema-9 fields are initialised without discarding progress:

```json
{
  "governance_snapshot_sha256": null,
  "governance_snapshot_generation": 0,
  "task_source_sha256": null,
  "active_task_id": null,
  "active_task_spec_sha256": null,
  "active_execution_envelope_sha256": null,
  "accepted_tasks": {},
  "governance_blocker": null
}
```

The existing `state.prev.json` / checksum recovery mechanism remains authoritative for crash recovery.

Migration rules:

1. read and validate current state;
2. compute migration result in memory;
3. preserve all unknown existing fields;
4. write one crash-safe new generation;
5. never reset objective, plan versions, provider/model state, permissions, telemetry, verification evidence or session state;
6. if governance discovery is ambiguous, write no destructive repository mutation and surface a typed blocker.

### 10.2 Legacy single-plan policy

An existing RC3 `planning-repair/policy.json` is **not deleted**.

When no repository governance contract exists, RC4 synthesises:

```text
AuthoritySet "default"
  member = legacy canonical_plan
  role = source
  repair = repairable
  required = true
```

This preserves `planning-repair configure --plan <file>` and single-file behaviour.

If both a repository governance contract and a legacy planning policy exist, RC4 requires them to be semantically compatible for the legacy canonical path. It must not silently choose one authority. Incompatible dual authority is a typed blocker requiring top-level operator reconciliation.

### 10.3 In-flight RC3 planning repair

A schema-1 RC3 active repair may migrate mechanically only when all of the following hold:

- legacy policy is valid and unchanged;
- repair branch/worktree identity is valid;
- candidate ancestry is valid;
- candidate diff is exactly the legacy canonical plan;
- no unexpected authority/control-surface change exists;
- if marked verified, the stored exact-SHA attestation matches the candidate.

It becomes a one-member `default` RepairEnvelope.

If any condition cannot be proven, RC4 preserves the existing active state/worktree/branch and blocks mutation. It must not auto-abort, reset or delete the repair.

A legacy unbound VERIFIED attestation may preserve historical evidence, but after migration/rebase/authority-snapshot change a new bound RC4 verification is required before promotion.

### 10.4 Promotion-policy migration

Legacy `required_attestation_contract` becomes a one-element unbound compatibility requirement only when RC4 governance/task binding is not required.

RC4 bound requirements are never downgraded to the legacy field.

## 11. Claude Code platform floor

P0 re-checked the current official Claude Code CLI and sandbox documentation.

RC4 freezes **Claude Code 2.1.285 as the minimum qualified version**.

Reasons:

- current CLI supports resume by session ID or name;
- current CLI supports restricted mode for evaluation/control contexts;
- current sandbox documentation explicitly separates shell-process sandboxing from built-in file tools/hooks/MCP;
- strict-sandbox behaviour and setting precedence changed before 2.1.285, and RC4 must not depend on the older weaker/ambiguous behaviour.

`claude-auto doctor` must:

- detect Claude Code version;
- refuse RC4 governed mutation below 2.1.285;
- verify Linux sandbox prerequisites when a profile requires them;
- verify strict/fail-closed sandbox settings used by control runners;
- report a typed actionable blocker rather than silently falling back unsandboxed.

Reference documentation:

- https://code.claude.com/docs/en/sandboxing
- https://code.claude.com/docs/en/cli-reference

### 11.1 Restricted mode

Where compatible with the required tool set, RC4 control-plane agents/adapters/verifiers should prefer Claude Code `--restricted` in addition to explicit `--settings`, `--setting-sources` and tool allow/deny lists.

Restricted mode is defence in depth, not the sole security boundary. Package-level path guards, OS sandbox/capability runners, Git-tree validation and promotion gates remain required.

## 12. Topology/failure decisions frozen in P0

### Linked worktrees

Supported. Repository identity and work-unit identity remain distinct. Security-sensitive Git operations use the common Git directory only through trusted package-owned operations.

### Monorepos

Supported with multiple named authority sets. Task ownership may span sets/packages explicitly; unrelated sets are not inherited.

### Nested repositories/submodules

A gitlink/nested repository is a separate authority boundary by default. Parent TaskSpecs cannot claim descendant paths through a broad glob.

### Sparse checkout

Committed AuthoritySet membership can be resolved from Git objects even when a file is not materialised. Mutation/repair that requires the content must use a package-owned worktree where the required member is safely materialised, or fail with a typed content-unavailable blocker.

### Shallow/partial clone

Missing ancestry/object data is never guessed. RC4 may fetch only through an already-authorised remote/network path; otherwise it blocks with the exact missing object/ancestry requirement.

### Git LFS

An LFS pointer is not silently treated as the planning document's semantic content. If required authority content is not materialised/available, RC4 blocks. Automated repair of LFS-backed authority additionally requires qualified Git LFS tooling and deterministic pointer/content verification.

### Detached HEAD

Read-only inspection is supported. Mutation/promotion requires an explicit safe target branch/ref contract.

### Non-Git directory

RC3-compatible objective execution may continue where safe, but repository-owned AuthoritySet snapshots, exact-Git promotion and task-worktree admission are unavailable. RC4 must say so explicitly rather than simulate Git authority.

### Dirty/WIP repository

Primary-checkout WIP is recorded and preserved. Task-owned mutation occurs in package-owned worktrees. Overlap between WIP and a task/control surface blocks promotion/reconciliation until proven safe.

### Renames/deletions/mode changes/binary files

Promotion scope is derived from Git tree changes, not text patches. Rename/copy heuristics are not authority. Deletes, mode changes, symlinks and gitlinks are first-class tree-entry changes.

## 13. RC4-P1 exact scope

P1 may now implement only the foundation:

1. `lib/governance_contract.py`
2. `lib/authority_set.py`
3. schema-9 additive durable state fields/migration
4. legacy single-plan -> synthetic one-member AuthoritySet
5. governance/AuthoritySet snapshot + digest
6. fail-closed control-surface and authority-member protection for normal workers in **all profiles, including Unattended**
7. repository-plan source admission updated to understand one or more AuthoritySets without creating TaskSpec execution yet
8. read-only status/explain surfaces
9. regression tests proving RC3 single-file behaviour is unchanged

P1 must **not** yet:

- execute custom adapters;
- select/activate tasks;
- create task worktrees;
- generalise the planning architect to multi-file mutation;
- run repository reconcilers;
- change main or tag RC4.

Those belong to later phases after the read-only authority foundation is green.

## 14. P1 acceptance tests required by this closure

At minimum:

- no governance contract + no legacy plan -> RC3 behaviour unchanged;
- legacy one-file policy -> one-member default AuthoritySet;
- one-file governance contract -> same effective AuthoritySet semantics;
- multi-file one-domain contract resolves deterministically;
- multiple named authority sets resolve deterministically;
- malformed/oversized/duplicate-key contract fails closed;
- absolute/traversal/control-character path rejected;
- authority symlink rejected;
- gitlink/nested-repo boundary rejected as ordinary member;
- missing required member rejected;
- zero-match required pattern rejected;
- case-fold/Unicode-normalisation collision rejected;
- governance contract WIP divergence blocks activation;
- authority member WIP/control-surface divergence invalidates snapshot;
- snapshot digest stable across repeated loads;
- snapshot changes when membership/blob/role/mutability/helper contract changes;
- legacy policy + equivalent governance contract accepted;
- legacy policy + incompatible governance contract blocked;
- all authority members protected from direct file mutation in Balanced, Strict, Isolated Full and Unattended;
- Unattended still retains its intended host-permission posture outside governance protection;
- RC3 tests remain green.

## 15. Exit decision

RC4-P0 is complete.

The implementation should now advance to **RC4-P1 — Authority Set foundation**. The first code should establish the read-only governance/AuthoritySet truth and compatibility layer before any TaskSpec or multi-file mutation code is introduced.
