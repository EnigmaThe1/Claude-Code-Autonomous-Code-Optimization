# RC4 P7 — Final Independent Correctness and Security Review

Review verdict: **RELEASE-ACCEPTING**

Date: 2026-10-07

Release candidate: `1.0.0-rc4`

Review basis:

- accepted public base: `1.0.0-rc3` at `d75f7c48dfe53d1a1759ee7828d9a42f5f744569`;
- reviewed RC4 exact tree before this evidence-only document: `6bab34372faff5ab159943fbab579c8eeaec3471`;
- sequence-7 reviewed source attested by `.release-qualify`: `af64fe4f0daf147764e7fc3631a19e4b03018620`;
- sequence-7 exact finalisation commit changed only `MANIFEST.sha256` and `.release-qualify`;
- `main` remained at the accepted RC3 SHA throughout review.

This document records the independent P7-F correctness/security review required before RC4 may be promoted. It does not itself promote `main`, create a tag, or weaken any release gate. Because adding this evidence changes the source tree, the candidate must be finalised again under a new monotonic release-finalisation sequence and must pass fresh exact-SHA CI and release-candidate qualification before promotion.

## 1. Review scope

The review covered the full RC4 change set and concentrated on the release-critical surfaces identified by P7:

- AuthoritySet and governance parsing/binding;
- TaskSource/TaskSpec normalisation and adapter boundaries;
- ExecutionEnvelope path authority;
- task linked-worktree isolation and primary-checkout protection;
- package-owned candidate sealing, deterministic verification, independent Task Verifier and AcceptedTaskRecord semantics;
- multi-file Planning Repair / RepairEnvelope selection, validation, reconciliation, exact-SHA Planning Verifier and promotion;
- Git trust reconstruction, ref protection and promotion broker behavior;
- durable-state migration, legacy state/WIP adoption, session adoption and read-only shadow validation;
- pre-write and PostToolBatch fail-closed enforcement;
- installer/upgrade/rollback/uninstall ownership and path checks;
- release finaliser, release qualification and accepted-release tagging workflows;
- P1-P6 closure records;
- P7 64-scenario and interruption traceability records;
- exact sequence-7 CI/release-qualification evidence.

## 2. Qualification evidence reviewed

### P7 scenario traceability

`docs/RC4_P7_SCENARIO_TRACEABILITY.json` records exactly **64 scenarios** with IDs 1-64 and no scenario without executable evidence.

The mapped evidence covers the original RC4 cases including:

- single-file and multi-file planning authority;
- duplicate/missing/cyclic task graphs;
- owned/evidence/scratch path boundaries;
- out-of-envelope direct/Bash mutations;
- immutable/generated planning members;
- stale attestations and advancing bases;
- dirty primary WIP;
- linked worktrees, submodules/gitlinks, sparse/shallow/detached repositories;
- LFS pointer authority failure;
- malicious adapters/validators/reconcilers;
- corrupted external state;
- migration/adoption/session continuity;
- 1,000-task and 100,000-path stress fixtures;
- Unattended semantic-governance enforcement;
- multiple authority domains;
- Git hooks/filter/textconv/trusted-Git behavior.

### P7 interruption traceability

`docs/RC4_P7_INTERRUPTION_TRACEABILITY.json` records **25 interruption/recovery entries** with no entry lacking evidence.

The entries cover task-workspace creation, candidate sealing/ref creation, deterministic verification, independent verifier attestation, task promotion/AcceptedTaskRecord recovery, planning repair and promotion, migration PREPARING/APPLYING/VERIFYING, session adoption and release finalisation.

### Exact sequence-7 source/qualification evidence

On exact finalised SHA `6bab34372faff5ab159943fbab579c8eeaec3471`:

- normal CI run `37616297165`: **SUCCESS**;
- release-candidate qualification run `37616300118`: **SUCCESS**;
- full regression: **600/600 passed**;
- coverage rerun: **600/600 passed**;
- measured aggregate coverage: **66%**;
- critical regression groups: **15/15, 19/19, 25/25, 19/19, 18/18**;
- package static checks: passed;
- public-baseline reference audit: passed;
- source manifest check: passed;
- extracted archive manifest check: passed;
- extracted archive regression: **600/600 passed**;
- transactional installer smoke: passed;
- deterministic archive SHA-256: `bf598a6fe4395740d80825c816f77ad8817ee80656113c195cb48604ed0c1598`.

The pull-request-triggered CI copy for PR #2 is `action_required` with no jobs because the finalisation commit was produced by GitHub Actions. It is not used as release evidence; the exact same SHA has independent successful normal CI and release-candidate qualification runs listed above.

## 3. Correctness review

### Authority and task execution

No release-blocking path was found that allows model/worker prose or TaskSpec metadata to mint authority.

The implementation binds task execution to durable, digest-checked repository authority and separates:

- coordinator authority root;
- task execution root;
- coordinator Git-trust state;
- package-owned semantic state.

P4 candidate creation discards worker staging authority, stages only admitted product/evidence paths, excludes runtime scratch, uses exact Git tree/commit objects, and keeps task HEAD pinned to the product base. Candidate verification is exact-SHA and a verifier rejection returns the same task to repair without transferring prior verification/attestation.

Accepted-task readiness is not trusted from a compact claim alone; full acceptance evidence, TaskSpec digest and accepted-product ancestry are validated.

### Planning repair

Multi-file Planning Repair preserves the stronger planning trust boundary:

- package-selected RepairEnvelope;
- repairable/immutable/generated/shared precedence;
- bounded deterministic reconciler/validator execution;
- exact candidate authority/TaskSource validation;
- exact-SHA independent Planning Verifier;
- promotion attestation bound to current authority content and candidate evidence.

Ordinary product promotion cannot silently bypass the planning attestation when canonical planning authority changes.

### Migration/adoption/shadow

Migration/adoption logic preserves legacy evidence as claims, not current authority. Imported accepted work requires current re-attestation before satisfying dependencies. Active WIP adoption is path-bounded into a P4 worktree and does not destructively rewrite the primary checkout.

Session adoption uses a fresh RC4-owned process/session identity under current settings/hooks/profile rather than attaching an unknown running legacy process. Current repository/task/planning authority takes precedence over remembered conversation state.

Shadow comparison is data/read-only and persists audit evidence without activating tasks, starting repair, or promoting state.

### Crash/recovery behavior

P4/P5/P6 lifecycle records use typed states and exact identity checks rather than inferring success after interruption. Candidate, promotion, accepted-task, planning-repair and migration interruption paths preserve valuable work and either reconcile exact known truth or block.

No path was found that converts missing/corrupt evidence into VERIFIED/ACCEPTED/PROMOTED state.

## 4. Security review

### Command and code execution

Across the critical authority/task/planning/migration paths reviewed:

- no `shell=True`;
- no `os.system`;
- no Python `eval`/`exec`;
- no unsafe YAML loader;
- no pickle-based authority deserialisation.

Repository-controlled verification/adapter/validator/reconciler execution remains behind explicit bounded execution policy rather than treating free-form TaskSpec verification strings as shell authority.

### Write and Git boundaries

Direct file tools and statically identifiable Bash writes are checked against protected paths and active task authority. Authority-resolution errors on the mutation path fail closed.

PostToolBatch remains the authoritative after-batch backstop for opaque/parallel side effects and checks task workspace state, primary-checkout invariants and Git-ref bindings before the next model call.

Raw worker Git mutation in a P4 task workspace is package-owned authority; the package candidate/promotion paths use trusted Git helpers and exact-state admission.

### Promotion

The promotion broker verifies exact local commits, current branch/base relationships, durable exact attestations where required, planning authority when planning paths change, active task admission for task-governed product changes, and local WIP preservation.

Remote promotion uses expected-base/lease semantics and reconciles remote truth after a potentially lost response rather than assuming push failure/success.

### Installer and uninstaller

Install/uninstall paths are canonicalised and reject:

- symlink install/uninstall destinations;
- root/home/XDG/unsafe shallow destinations;
- marker paths outside the canonical install directory;
- symlink/non-regular/corrupt/foreign package markers;
- marker identity/path mismatch.

Upgrade is staged transactionally and rolls back the previous known-good package on failure. Uninstall preserves external state by default and requires the validated package marker before purge.

### Release workflows

The finaliser:

- has bounded Actions/contents write permission;
- requires a marker-only finalisation request;
- requires a strictly increasing sequence;
- rebuilds/checks the manifest;
- commits only `MANIFEST.sha256` and `.release-qualify`;
- uses a lease-protected push;
- verifies the remote release head before dispatching final CI and release qualification.

The tag workflow:

- runs only after successful `main` CI or explicit dispatch;
- requires current `main` and `release/<version>` to be the same exact SHA;
- requires matching finalisation/qualification markers;
- requires successful release qualification for that exact SHA;
- requires successful `main` CI for that exact SHA;
- re-reads remote `main` and release-branch truth immediately before the tag write;
- refuses to move an existing conflicting release tag.

## 5. Findings

### HIGH / CRITICAL

**None found.**

### Medium

**None unresolved.**

### Low / informational

1. The PR-triggered CI copy for the GitHub-Actions-authored finalisation commit can appear as `action_required` with no jobs. This is not treated as qualification evidence. Exact-SHA push/manual CI and release-candidate qualification remain the release gates.
2. The write-boundary guard contains compatibility fallbacks for repositories without configured task governance. The actual mutation-authority resolver and PostToolBatch path remain fail-closed for task-governed execution. No acceptance/promotion bypass was found through this compatibility behavior.
3. Aggregate line coverage is 66%, so coverage percentage alone is not proof of correctness. Release acceptance instead relies on the direct 64-scenario/25-interruption mapping, hostile/topology/stress tests, exact-SHA verification and the independent review recorded here.

These observations are not release blockers.

## 6. Independent review verdict

**Correctness: PASS**

**Security: PASS**

**Release verdict: RELEASE-ACCEPTING**, subject to the remaining mechanical release gates:

1. commit this review evidence;
2. require green normal CI for the resulting development head;
3. request a new monotonic finalisation sequence;
4. require the finaliser to create one new exact release SHA;
5. require normal CI and release-candidate qualification to be green on that exact finalised SHA;
6. advance `main` to that exact SHA without introducing a merge commit;
7. require green `main` CI on the same SHA;
8. allow the accepted-release workflow to create immutable tag `v1.0.0-rc4` at that same SHA.

No manual bypass of those gates is authorised by this review.
