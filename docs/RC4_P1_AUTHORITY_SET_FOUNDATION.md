# RC4 P1 — AuthoritySet Foundation Closure

Status: **CLOSED subject to final closure-SHA CI; any failure automatically reopens P1**

Date: 2026-10-04

Branch: `release/1.0.0-rc4`

P0 architecture closure: `3e1376b88798fab1698e059c06d69790734159ae`

RC3 compatibility base: `d75f7c48dfe53d1a1759ee7828d9a42f5f744569`

## 1. P1 outcome

RC4 now has a read-only, Git-tree-derived repository governance foundation without introducing TaskSpec execution or multi-file mutation prematurely.

The key invariant is implemented:

> one-file planning and multi-file/multi-domain planning are represented through the same AuthoritySet model, while runtime autonomy profiles remain independent from semantic repository authority.

A legacy RC3 `canonical_plan` therefore becomes a synthetic one-member `default` AuthoritySet. A repository that opts into `.claude-auto/governance.json` can define one or more named AuthoritySets with deterministic multi-file membership.

P1 does **not** yet make those multi-file sets executable. It can resolve, explain and protect them; autonomous execution remains fail-closed until the later TaskSource / TaskSpec / multi-file repair phases exist.

## 2. Implemented files and integration seams

New foundation modules:

- `lib/governance_contract.py`
- `lib/authority_set.py`

Integrated seams:

- `lib/repo_runtime.py`
- `lib/settings_policy.py`
- `hooks/write_boundary_guard.py`
- `lib/cli_schema.py`
- `lib/claude_auto.py`

Qualification:

- `tests/test_governance_authority.py`

Operator documentation:

- `docs/SAFETY.md`
- `COMMANDS.md`

The RC4 release branch identity was also corrected to `VERSION=1.0.0-rc4`; this was required by the repository's existing release-branch CI policy and does not alter `main`.

## 3. Governance contract v1 implemented

The optional repository contract is:

```text
.claude-auto/governance.json
```

Activation reads the contract from the exact committed Git object rather than trusting mutable working-tree text.

Implemented validation includes:

- UTF-8 JSON;
- maximum contract size;
- duplicate-key rejection;
- exact top-level v1 keys;
- schema version 1;
- bounded authority-set/member/helper/task-source/control-surface counts;
- stable AuthoritySet IDs;
- role and repair-mode enums;
- POSIX repository-relative selectors;
- rejection of absolute paths, `..`, empty path segments and control characters;
- Unicode NFC normalisation;
- deterministic package-owned `*`, `?` and `**` selector handling;
- inert recording of declared task sources without executing them in P1;
- structured argv-based helper declarations without shell execution.

Unknown execution semantics are not granted authority by P1. Declared adapters, validators and reconcilers remain inert data until later phases add their bounded runners.

## 4. Exact Git-tree AuthoritySet resolution

Authority membership is derived from:

```bash
git ls-tree -r -z --full-tree <exact-commit>
```

under the existing trusted Git environment.

P1 does not derive committed authority identity from the mutable index, checkout filters, textconv output or filesystem discovery.

Resolved members record:

- exact normalised path;
- role;
- repair mode;
- required flag;
- Git mode;
- blob object ID.

P1 rejects:

- required selectors matching no committed file;
- symlink authority members;
- gitlinks/submodules treated as ordinary files;
- overlapping selectors that resolve the same member ambiguously inside one AuthoritySet;
- case-fold collisions;
- Unicode-normalisation collisions;
- excessive resolved membership.

Multiple named AuthoritySets are supported in one repository.

## 5. Deterministic snapshot

The package-owned snapshot records:

- repository identity;
- exact product commit;
- branch when applicable;
- governance blob identity;
- source mode: contract, legacy, or contract+legacy;
- resolved named AuthoritySets;
- member Git identities;
- validator/reconciler declaration digests;
- task-source declaration digest;
- resolved control surfaces and their Git identities;
- protected-path union;
- final SHA-256 snapshot digest.

Canonical hashing is deterministic JSON with sorted keys and no timestamp inside the hashed object.

The snapshot changes when authority blobs, role/mutability semantics, helper contracts, control-surface objects or other authority-relevant contract content changes.

## 6. Legacy RC3 compatibility

When no repository governance contract exists but an RC3 planning-repair policy exists:

```text
canonical_plan = PLAN.md
```

is normalised to:

```text
AuthoritySet "default"
  PLAN.md
    role   = source
    repair = repairable
    required = true
```

The existing RC3 planning-repair policy is not deleted or rewritten.

When both systems exist, the legacy canonical path must be represented by the repository governance contract as a repairable source member. Divergent dual authority fails closed.

An important regression found during qualification was also fixed: a normal freshly initialised/unborn Git repository has no committed governance authority, so it must preserve RC3 behaviour rather than fail simply because `HEAD` does not exist. A legacy policy that explicitly claims tracked authority still requires a resolvable commit.

## 7. State schema 9

External durable state migrates additively from schema 8 to schema 9.

Added fields:

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

Existing state fields and unknown fields are preserved.

The AuthoritySet snapshot is stored in package-owned external state at:

```text
<repo-state>/governance/snapshot.json
```

Generation increments only when the effective snapshot digest changes.

Task activation fields exist for migration continuity but remain unused/null in P1.

## 8. Profile-independent semantic governance

The RC3 implementation protected a configured canonical plan only when the worker was not unrestricted.

P1 removes that coupling.

AuthoritySet members and active control surfaces are protected in:

- Balanced;
- Strict;
- Isolated Full;
- Unattended.

Unattended still has its intended host/runtime permission posture. It simply cannot use that profile choice as a semantic permission to rewrite planning authority or governance/control surfaces.

Protection is layered through:

- Claude direct-tool deny rules;
- `CLAUDE_AUTO_PROTECTED_REPO_PATHS`;
- the package PreToolUse write/Bash guard;
- package-owned snapshot re-reading by the guard.

Malformed durable governance snapshot state causes repository mutation to fail closed.

## 9. Control surfaces implemented in P1

When a repository opts into the RC4 governance contract, mandatory control-surface discovery includes committed forms of:

- `.claude-auto/governance.json`;
- `.claude-auto/verification.json`;
- `.mcp.json`;
- `.gitattributes`;
- `.gitmodules`;
- `CLAUDE.md`;
- `.claude/**`;
- declared helper inputs/implementation paths;
- declared adapter paths/config/inputs that P1 can identify safely;
- repository-added control-surface selectors.

Legacy one-plan compatibility deliberately retains RC3's narrower behaviour until the repository opts into the new contract.

Uncommitted divergence in a resolved authority member or control surface invalidates the snapshot and blocks activation.

## 10. Planning-source admission

P1 updates the supervisor's repository-plan admission check to consume AuthoritySet truth.

If the effective authority contains exactly one source member and one total authority member, RC3's existing `--plan <canonical>` semantics remain valid.

If the effective authority is genuinely multi-file or multi-domain, P1 returns a typed/fail-closed message explaining that the authority is currently inspectable/protected but not yet executable.

This is intentional. P1 does not pretend that the old one-file planner is a multi-file planner.

## 11. Read-only explain surface

P1 adds:

```bash
claude-auto governance status --repo /path/to/repo
```

The command reports:

- READY + exact snapshot;
- UNCONFIGURED;
- BLOCKED + authority error.

The status path is read-only with respect to the target repository.

## 12. Qualification matrix implemented

The P1 suite explicitly exercises:

- no-governance compatibility;
- one-file governance;
- legacy one-file synthesis;
- equivalent legacy + governance authority;
- incompatible dual authority;
- multi-file one-domain authority;
- multiple named authority domains;
- deterministic repeated snapshots;
- authority blob invalidation;
- role/mutability/helper-contract invalidation;
- control-surface object invalidation;
- authority-member WIP divergence;
- control-surface WIP divergence;
- governance-contract WIP divergence;
- malformed/duplicate-key contract;
- oversized contract;
- absolute selector rejection;
- traversal rejection;
- control-character rejection;
- required zero-match rejection;
- symlink rejection;
- gitlink boundary rejection;
- case-fold collision rejection;
- Unicode-normalisation collision rejection;
- all-profile direct protection including Unattended;
- preservation of Unattended's non-governance host posture;
- read-only governance status parsing/output;
- RC3 full-regression compatibility.

## 13. Qualification evidence

The first meaningful RC4 CI run exposed two repository/process issues before product qualification:

1. the RC4 branch still declared `VERSION=1.0.0-rc3`, so CI correctly stopped at release branch/version identity;
2. the first P1 test filename used a historical-style `test_rcN_` prefix, which the repository's public-baseline audit correctly rejects.

Neither guard was weakened. The branch version was aligned to RC4 and the test was renamed to a capability-neutral filename.

The first full P1 regression then exposed the unborn-`HEAD` discovery-order bug described above. That defect was fixed rather than changing the tests.

Pre-closure implementation SHA:

```text
14984bfe6b77fa019d9f6f0a351442002bf7187f
```

GitHub Actions run:

```text
37234386440
```

passed:

- release branch/version identity;
- public-baseline reference audit;
- development manifest preparation;
- package static checks;
- full pytest: **357 passed**;
- full pytest under coverage: **357 passed**, 64% aggregate measured coverage;
- critical regression groups:
  - 15 passed;
  - 19 passed;
  - 25 passed;
  - 16 passed;
  - 18 passed.

Additional closure-matrix tests and documentation were then added. The final closure SHA must pass the same CI workflow. A failure reopens P1 automatically.

## 14. Deliberately deferred beyond P1

P1 does **not** implement:

- TaskSpec normalisation/execution;
- dependency-safe task frontier selection;
- active TaskSpec envelopes;
- task-owned worktrees;
- post-command task diff enforcement;
- custom adapter execution;
- validator execution;
- reconciler execution;
- multi-file Planning Architect mutation;
- RepairEnvelope execution;
- bound multi-attestation promotion requirements;
- control-surface change authority cycles;
- resume-session CLI exposure;
- Pandora shadow migration;
- main promotion or RC4 tag.

Those remain later RC4 phases and must build on this foundation rather than bypassing it.

## 15. Exit decision

Subject to the final closure-SHA CI rule above, RC4-P1 is complete.

The next implementation phase must consume this AuthoritySet truth rather than reintroducing a parallel planning-authority model.
