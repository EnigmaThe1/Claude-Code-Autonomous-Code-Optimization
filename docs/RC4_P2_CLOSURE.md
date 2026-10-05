# RC4 P2 Closure — TaskSpec and Adapter Protocol

Status: **CLOSURE CANDIDATE — CLOSED ONLY WHEN THIS RECORD'S COMMIT PASSES CI**

Date: 2026-10-05

Branch: `release/1.0.0-rc4`

P1 closure SHA: `0d9d9c1984efa7c502191b81395c33d625eaacb6`

Qualified P2 implementation SHA: `3342f289c4f9903e96e5bd21575af86998c73451`

Frozen P2 protocol baseline: `docs/RC4_P2_TASKSPEC_ADAPTER_PROTOCOL.md`

## 1. Closure rule

P2 is closed only if the commit adding this closure record passes the repository's complete CI workflow.

The implementation SHA above already passed the full workflow. This closure record adds documentation only. If its own commit is not green, P2 remains open.

No P2 commit, including this closure record, is promoted to `main` or tagged as RC4.

## 2. Qualified CI evidence

GitHub Actions run for implementation SHA `3342f289c4f9903e96e5bd21575af86998c73451`:

- release branch/version identity: PASS;
- public-baseline reference audit: PASS;
- package static checks: PASS;
- full regression suite: **408 passed**;
- coverage run: **408 passed**;
- aggregate coverage: **66%** (`9799` statements, `3323` missed);
- critical regression groups: **15 + 19 + 25 + 16 + 18 passed**;
- overall workflow conclusion: **success**.

The four red commits immediately before the qualified SHA were not accepted evidence. They all inherited one test-fixture defect: a test attempted to execute `hooks/write_boundary_guard.py` directly on a checkout where the hook was not executable. The product hook is launched through Python by generated settings. The fixture was corrected to invoke it through Python; the resulting implementation SHA above passed the entire workflow.

## 3. Branch integrity

At closure:

- RC4 work remains on `release/1.0.0-rc4`;
- `main` remains exactly the accepted RC3 SHA:
  `d75f7c48dfe53d1a1759ee7828d9a42f5f744569`;
- P2 implementation is 60 commits ahead of the P1 closure;
- no RC4 tag exists as part of P2;
- no P2 promotion to `main` has occurred.

## 4. What P2 now provides

### 4.1 Repository-owned TaskSource declarations

The governance contract now validates deterministic, kind-specific task sources with:

- stable unique source IDs;
- explicit AuthoritySet ceilings;
- built-in `json`, `jsonl`, `toml` and `static` sources;
- bounded custom `adapter` declarations;
- exact keys and bounded fields;
- duplicate selector/source rejection;
- referenced AuthoritySet existence checks;
- source files and adapter inputs treated as protected control surfaces.

There are no source-priority or last-writer-wins semantics. Duplicate Task IDs fail closed.

### 4.2 Canonical TaskSpec v1

`lib/task_spec.py` now provides package-owned TaskSpec normalisation with:

- stable Task IDs compatible with existing IDs such as `PV2-CORE-I01.01`;
- exact v1 fields;
- non-empty AuthoritySet membership;
- source AuthoritySet ceiling enforcement;
- deterministic dependency normalisation;
- iterative dependency-cycle validation for deep graphs;
- strict and non-strict missing-dependency modes;
- package-owned repository path-selector normalisation;
- protected planning/control-path precedence;
- explicit protection for `.git/**`, `.claude-auto/**`, `.claude/**` and resolved control paths;
- full-record and authority-only SHA-256 digests;
- metadata and commit-subject exclusion from the authority digest;
- finite canonical JSON and NFC-normalised authority strings.

Broad selectors such as `**` cannot swallow semantic authority/control surfaces.

### 4.3 Exact committed built-in sources

Built-in TaskSources are resolved from exact Git tree/blob identity at the current `product_head`.

P2 does not use:

- mutable working-tree content;
- index-only changes;
- filters/textconv;
- materialised Git LFS content in place of committed pointer bytes;
- symlink targets;
- gitlink/submodule traversal.

Input blob size is checked before materialisation.

Uncommitted changes to a task source/control surface fail closed.

### 4.4 Custom adapter isolation

Custom adapters are repository code and remain hostile by default.

P2 now:

1. resolves only declared adapter inputs from the exact committed tree;
2. materialises only those regular blobs into an ephemeral view;
3. runs the view read-only;
4. hides the live repository;
5. scrubs credentials/environment;
6. grants no network capability;
7. grants no external-read capability;
8. has no trusted-host fallback for read-only adapter execution;
9. bounds runtime and output;
10. runs each adapter twice in fresh contexts;
11. accepts only identical normalised TaskSpec results.

The Linux bubblewrap adapter mode is host-layout-independent: non-runtime top-level host paths are masked by default rather than relying on a fixed list of conventional mount names.

The Anthropic Sandbox Runtime configuration uses deny-by-default reads with explicit re-opening of the materialised view, fresh temp home and essential runtime paths.

If the host exposes a sandbox binary but cannot establish the required isolation primitive, adapter resolution is **BLOCKED**. GitHub's hosted Linux runner demonstrated this case by denying the required network namespace loopback operation; P2 correctly failed closed and did not fall back to host execution.

`claude-auto doctor` now probes the real supervisor isolation recipe instead of reporting success merely because `srt` or `bwrap` is installed.

### 4.5 Deterministic multi-source merge

P2 merges all normalised source results without source precedence and:

- rejects duplicate Task IDs globally;
- sorts the effective set deterministically;
- validates dependencies against the complete merged ID set;
- uses iterative cycle detection;
- records external dependencies only in non-strict mode;
- produces source-order-independent merged authority digests.

Adapter runtime-boundary evidence is retained for audit but excluded from semantic TaskSourceSet authority so the same exact repository authority does not hash differently merely because one host used `srt` and another used bubblewrap.

### 4.6 Durable TaskSourceSet authority

The resolved TaskSourceSet is stored outside the target repository and binds:

- exact AuthoritySet snapshot SHA-256;
- exact product commit;
- task-source contract digest;
- exact source input blob identities;
- normalised adapter outputs;
- merged canonical TaskSpecs;
- per-task authority/full digests;
- global TaskSourceSet digest.

`state.json.task_source_sha256` is the effective resolved TaskSourceSet digest.

A governance/AuthoritySet generation change invalidates the active task-source binding.

P2 added a verified TaskSourceSet loader that recomputes:

- the complete semantic TaskSourceSet digest;
- merged-task digest;
- durable-state binding;
- current AuthoritySet snapshot binding when current authority is required.

Malformed or semantically tampered persisted authority is BLOCKED.

A cryptographically valid old set may be shown to an operator as `STALE`, but it cannot be consumed as current execution authority.

Concurrent repository/authority movement during source resolution or persistence invalidates the resolution and discards the durable result.

### 4.7 Operator surfaces

P2 adds:

```bash
claude-auto tasks status --repo .
claude-auto tasks resolve --repo .
```

`tasks status` never executes repository adapters.

`tasks resolve` may execute adapters only through the bounded P2 protocol.

Large TaskSourceSets are persisted in full but rendered as compact count/digest summaries on the CLI.

Successful resolution does **not** set:

- `active_task_id`;
- `active_task_spec_sha256`;
- `active_execution_envelope_sha256`;
- accepted/completed task state.

P2 therefore resolves repository-owned task authority without granting task execution authority.

## 5. Semantic control-surface hardening discovered during P2

The P2 threat review found several authority surfaces that had to be protected before P3 could safely consume TaskSpecs.

Governed repositories now protect:

- nested `CLAUDE.md` instruction files;
- repository CI workflow definitions;
- `.claude-auto/verification.json`;
- exact repository-local files directly referenced by declared verification commands;
- task-source files and adapter inputs;
- helper code/inputs;
- declared repository control surfaces.

Opaque/dynamic verification dependencies remain declarable through explicit `control_surfaces`.

The ordinary worker cannot mutate a protected file by instead replacing/deleting one of its ancestor directories. Protected descendants and protected ancestors are both mutation-denied.

## 6. Runtime-profile semantic authority

P2 closes an important profile-boundary gap:

- Balanced and Strict retain ordinary host/repository write boundaries **and** semantic authority protection.
- Unattended and Isolated Full may remove ordinary host/runtime write restrictions as designed, but they still install the semantic `write_boundary_guard.py`.
- In those broad-authority profiles the guard runs in semantic-only mode: outside-host writes are not blocked merely for being outside the repository, but planning/task/control authority remains protected.
- `bypassPermissions` therefore does not mean “bypass repository semantic governance”.

This distinction is required before P3 introduces active task envelopes.

## 7. Threat/qualification coverage

The P2 qualification matrix includes, at minimum:

- deterministic JSON, JSONL, TOML and static sources;
- multi-source order independence;
- duplicate source IDs;
- duplicate Task IDs within/across sources;
- malformed and duplicate-key JSON;
- malformed JSONL/TOML;
- required selector resolving zero files;
- overlapping source selectors resolving one file twice;
- symlink and clean gitlink source rejection;
- unknown AuthoritySet references;
- TaskSpec AuthoritySet escalation;
- empty/malformed IDs and AuthoritySets;
- duplicate/self/missing dependencies;
- deep 5,000-task acyclic and cyclic graphs;
- duplicate/absolute/traversal/control-character task paths;
- direct and wildcard control-surface ownership attempts;
- Git metadata ownership attempts;
- metadata/commit-subject authority-digest invariance;
- authority-field digest sensitivity;
- canonical numeric/Unicode behaviour;
- committed-source and task-contract digest invalidation;
- uncommitted source WIP rejection;
- adapter input mutation;
- adapter live-repository masking;
- denied network/external-read capabilities;
- timeout/non-zero/noisy/malformed/oversized adapter output;
- adapter AuthoritySet escalation;
- duplicate adapter Task IDs;
- adapter nondeterminism;
- normalised output-order determinism;
- runtime-boundary portability;
- source-resolution TOCTOU invalidation;
- persistent TaskSourceSet corruption/malformed-state rejection;
- stale-versus-current durable-state semantics;
- real sandbox success-or-fail-closed behaviour;
- profile-independent semantic write guard;
- protected ancestor mutation denial;
- `tasks status` not executing adapters;
- `tasks resolve` leaving the target repository clean;
- no task activation as a side effect of resolution;
- RC3/P1 regression preservation.

## 8. Explicitly not implemented in P2

P2 does **not** implement:

- active TaskSpec selection;
- model-plan/TaskSpec reconciliation into an executable task;
- execution envelopes;
- task worktrees;
- per-task worker write fencing;
- post-tool mutation rollback;
- staged-diff ownership admission;
- task acceptance/promotion;
- multi-file RepairEnvelope orchestration;
- adapter network/external-read grants;
- RC4 release packaging or promotion.

Those remain later RC4 phases.

## 9. P3 entry contract

P3 may start only after this closure-record commit is itself green.

P3 must consume the verified current TaskSourceSet through the package-owned integrity loader. It must not trust raw persisted JSON, reconstruct TaskSpec authority from model output, or create a parallel task-authority system.

P3's responsibility is to derive a fail-closed **ExecutionEnvelope** for one selected TaskSpec, bind it to the exact:

- TaskSourceSet digest;
- TaskSpec authority digest;
- AuthoritySet snapshot;
- base/product commit;
- permitted product/evidence/scratch selectors;
- protected control surfaces;
- verification requirements.

P3 must preserve the RC3 single-writer execution model while introducing task-specific write/admission authority.

## 10. Closure decision

If the commit containing this record passes the full repository CI workflow, RC4-P2 is **CLOSED and FROZEN**.

After that point, any newly discovered TaskSpec/TaskSource/adapter defect must be treated as either:

- a P2 regression requiring an explicit reopen; or
- a later-phase integration issue,

rather than silently changing the frozen P2 contract during P3.
