# RC4 Universal Repository Governance Plan

Status: **PLANNING BASELINE — no RC4 implementation is accepted by this document alone**

Target: **1.0.0-rc4**

Base release: **1.0.0-rc3** at `d75f7c48dfe53d1a1759ee7828d9a42f5f744569`

## 1. Purpose

RC4 will generalise the remaining repository-specific governance concepts that were exposed by real long-running autonomous engineering work.

The target is not “support one large repository by special case”. The target is a repository-agnostic control model that can safely handle:

- one-file implementation plans;
- multi-file planning authorities;
- multiple task ledgers;
- manifest/index + ledger + traceability + generated-projection planning systems;
- repositories with no structured task ownership metadata;
- repositories with explicit task ownership metadata;
- ordinary single-package repositories;
- monorepos and multi-package repositories;
- linked Git worktrees;
- repositories with pre-existing WIP;
- large plans and large repositories;
- custom planning formats through bounded adapters;
- interrupted execution and interrupted planning repair;
- product branches that advance while a repair is in progress;
- generated files, scratch/build outputs and evidence files;
- malformed or hostile repository configuration without turning it into host authority.

“Universal” in RC4 means **adapt to supported repository structures without requiring the repository to be redesigned for Claude Auto, and fail closed with an actionable explanation when a safe interpretation cannot be established**. It does not mean silently guessing authority.

RC4 must remain Linux-first, preserve the existing RC3 execution/safety model, and keep simple repositories simple.

## 2. Problems RC4 must solve

### 2.1 RC3 canonical planning authority is single-file

RC3 currently models repository-owned canonical planning as one tracked regular file and requires a planning-repair candidate to change exactly that path.

That is a sound minimum-security implementation, but it cannot represent a planning authority whose truth is intentionally distributed across:

- an authority manifest;
- one or more task/microstep ledgers;
- dependency/traceability tables;
- contracts;
- generated projections;
- validation metadata.

RC4 must replace the single `canonical_plan` abstraction with a normalised **Authority Set** while keeping the one-file case as the simplest Authority Set.

### 2.2 RC3 has repository boundaries, but no generic task-scoped ownership

RC3 can stop writes outside the selected repository and can protect configured control paths. It does not generically understand:

- active task ID;
- task dependencies;
- task-owned paths;
- evidence paths;
- runtime scratch paths;
- promotion/commit path envelopes.

RC4 must add a **Task Authority** model so a repository can define the exact paths a selected task is allowed to contribute, without letting the model grant itself more authority.

### 2.3 Repository governance must remain data, not a repository escape hatch

A repository may describe its own structure, but repository text must never become arbitrary host authority.

RC4 therefore needs a strict separation between:

1. repository-declared structure;
2. package-owned parsing/normalisation;
3. package-owned external durable state;
4. package-owned enforcement;
5. independently verified promotion.

No repository config or adapter may grant writes outside the selected repository, bypass protected host paths, weaken sandbox requirements, or self-authorise an unsandboxed helper.

## 3. Core design rule

RC4 will normalise every supported project into the same internal objects:

```text
Repository Governance
├── AuthoritySet
│   ├── authority members
│   ├── roles
│   ├── mutability classes
│   ├── validators/reconcilers
│   └── exact snapshot identity
├── TaskSourceSet
│   ├── one or more source adapters
│   └── normalised TaskSpec records
├── TaskAuthority
│   ├── dependency graph
│   ├── active task
│   ├── accepted/completed task history
│   └── reservations/blockers
└── ExecutionEnvelope
    ├── readable repository scope
    ├── direct-edit scope
    ├── runtime scratch scope
    ├── promotable scope
    └── protected scope
```

A one-file plan is therefore:

```text
AuthoritySet = { PLAN.md }
```

A complex project can be:

```text
AuthoritySet =
{
  CURRENT_AUTHORITY.yaml,
  kernel_tasks.yaml,
  control_tasks.yaml,
  fabric_tasks.yaml,
  traceability.yaml,
  generated_projection.yaml
}
```

The supervisor and promotion logic operate on the normalised Authority Set in both cases.

## 4. Backward compatibility requirements

RC4 must not make RC3 users rewrite their repositories.

When no RC4 governance contract exists:

- current RC3 objective-driven execution continues to work;
- current supplied-plan execution continues to work;
- `planning-repair configure --plan <file>` continues to work;
- the existing single-file planning policy is internally normalised into a one-member Authority Set;
- current verification contracts continue to work;
- current profiles, model routing, permissions, promotion and recovery remain compatible.

Existing RC3 external state must be upgraded in place without discarding:

- objectives;
- checkpoints;
- provider/model state;
- permission grants;
- telemetry;
- verification state;
- trusted Git configuration;
- planning policy;
- planning-repair state.

If an in-flight RC3 planning repair cannot be migrated mechanically, activation must stop before mutation and preserve both the old state and the transactionally installed rollback copy.

## 5. Repository governance contract

Introduce an optional repository-owned regular file:

```text
.claude-auto/governance.json
```

The contract is declarative. It is not executable.

Requirements:

- versioned schema;
- UTF-8 JSON so the base package requires no third-party parser;
- regular tracked file for Git-backed governance;
- no symlinked contract or symlinked `.claude-auto` control directory;
- repository-relative paths only;
- no `..` traversal;
- no absolute paths;
- deterministic path normalisation;
- duplicate/collision detection;
- size and item-count limits;
- unknown security-sensitive fields fail closed;
- the product worker cannot edit the governance contract;
- any contract change invalidates the current normalised governance snapshot before further product mutation.

Initial shape:

```json
{
  "schema_version": 1,
  "planning_authority": {
    "members": [
      {
        "path": "PLAN.md",
        "role": "source",
        "repair": "repairable",
        "required": true
      }
    ]
  },
  "tasks": {
    "sources": [],
    "execution_mode": "single-writer"
  }
}
```

The schema must support exact paths and explicitly bounded tracked-file patterns, but patterns must resolve to a concrete deterministic set before a run starts.

## 6. Authority Set model

### 6.1 Authority member roles

RC4 should support generic role labels with package-defined behaviour for at least:

- `source` — human/authoritative planning source;
- `manifest` — current-authority/index file;
- `task_ledger` — structured task definitions;
- `traceability` — requirement/dependency mappings;
- `contract` — planning/acceptance/security contract;
- `projection` — derived/generated planning view;
- `other` — protected planning member with no special parser semantics.

Roles describe behaviour; they must not assume a particular project name or directory structure.

### 6.2 Mutability classes

Each member must have one of:

- `immutable` — automated planning repair cannot alter it;
- `repairable` — a bounded Planning Repair Architect may edit it;
- `generated` — the architect may not directly edit it; only a declared bounded reconciliation/generation step may update it.

The governance contract itself is always outside automated planning-repair authority.

### 6.3 Exact membership and bounded patterns

Support both:

- exact file entries;
- bounded patterns for repositories whose planning membership can legitimately grow.

Rules:

- pattern expansion is against tracked files, not arbitrary filesystem contents;
- expansion order is deterministic;
- symlink members are rejected by default;
- members resolving outside the worktree are rejected;
- case-fold and Unicode-normalisation collisions are rejected;
- a pattern that unexpectedly resolves to zero required files fails closed;
- membership is hashed and stored in external state;
- a new matching file changes Authority Set identity and forces revalidation.

### 6.4 Authority snapshot

Before implementation or repair, persist a snapshot containing:

- governance-contract hash;
- product HEAD;
- current branch;
- every resolved authority path;
- role and mutability class;
- Git blob SHA or content digest;
- task-source identity;
- validator/reconciler contract digest;
- normalised Authority Set digest.

Any material authority change invalidates stale task envelopes, cached planning verdicts and stale planning attestations.

## 7. Multi-file planning repair

The current exact-SHA Architect -> Verifier -> protected promotion design remains, but repair scope changes from one exact file to a **Repair Envelope**.

### 7.1 Repair Envelope

The supervisor creates a repair envelope before the architect starts:

```text
base SHA
authority snapshot digest
repair reason
repairable candidate paths/patterns
immutable paths
generated paths
allowed new authority paths, if any
required validators
required reconcilers
```

The model must not choose its own envelope.

### 7.2 Architect constraints

The Planning Repair Architect:

- works only in a dedicated repair worktree;
- may directly edit only `repairable` members allowed by the envelope;
- cannot edit immutable members;
- cannot directly edit generated members;
- cannot edit the governance contract;
- cannot alter product source merely to make planning validation pass;
- must analyse backward impact on accepted work;
- must analyse forward impact on remaining work;
- must return `SEMANTIC_DECISION` rather than invent product meaning when evidence does not determine the answer.

### 7.3 Derived planning files

If repairable source changes require generated projections:

- package-owned orchestration runs declared reconciliation commands in an isolated/read-only-host boundary;
- generated outputs may change only within paths classified `generated`;
- commands get no implicit host/network authority;
- output paths are verified after the command;
- deterministic generation should be checked by a second clean regeneration when practical;
- direct model edits to generated members are rejected.

### 7.4 Candidate acceptance

A planning candidate is valid only if:

- every changed path is within the Repair Envelope;
- no immutable member changed;
- no unexpected authority member appeared/disappeared;
- required generated outputs are current;
- all declared planning validators pass;
- generic authority invariants pass;
- an independent verifier reviews the exact candidate SHA;
- the verifier attestation is bound to the exact SHA + Authority Set digest + contract;
- product-base advancement is reconciled using the existing interrupted/advancing-base machinery;
- promotion remains remote-aware and idempotent.

## 8. TaskSpec normal form

RC4 must define one internal task protocol independent of source format.

Minimum TaskSpec:

```json
{
  "id": "T123",
  "depends_on": ["T120"],
  "owned_paths": ["src/component/**", "tests/component/**"],
  "evidence_paths": ["docs/evidence/T123/**"],
  "runtime_scratch_paths": [".build-cache/**"],
  "verification": ["test"],
  "commit_subject": null,
  "metadata": {}
}
```

Only `id`, dependency identity and authority-relevant path fields affect enforcement. Free-form metadata never grants authority.

Validation must reject:

- duplicate IDs;
- self-dependencies;
- dependency cycles;
- missing dependencies when strict dependency mode is enabled;
- malformed path expressions;
- absolute paths;
- path traversal;
- ownership that resolves outside repository/workspace boundaries;
- ownership intersecting protected planning/governance paths unless the operation is a planning repair;
- ambiguous duplicate definitions from multiple sources.

## 9. Task source adapters

A universal harness cannot require every repository to use the same task file format.

RC4 should therefore support a **bounded adapter protocol**.

### 9.1 Built-in sources

Implement directly where safe and dependency-free:

- Claude Auto native generated plan;
- JSON task ledgers;
- TOML task ledgers via Python stdlib `tomllib`;
- JSONL task records;
- explicit static TaskSpec declarations in the governance contract.

### 9.2 Custom read-only adapter

For YAML, Markdown, databases, generated task systems or proprietary planning formats, permit a repository-declared adapter that emits the normalised TaskSpec JSON protocol.

Security requirements:

- adapter runs read-only against repository content;
- no write access to product/planning files;
- no host-control sockets;
- no unsandboxed fallback;
- network off by default;
- bounded runtime/output;
- stdout must be schema-valid JSON;
- duplicate/unknown/malformed fields fail closed;
- output paths are re-normalised by Claude Auto;
- adapter output cannot override package protected paths;
- adapter identity/content hash is part of the governance snapshot.

An adapter interprets repository data; it does not become an authority broker.

### 9.3 No structured task source

Repositories without task metadata continue to use RC3 plan-wide autonomous execution. Task-scoped path enforcement is activated only when a trustworthy TaskSpec source exists.

This is required for universal backward compatibility.

## 10. Task selection and dependency safety

When Task Authority is configured, the supervisor owns selection.

The model may recommend a task but may not activate arbitrary authority.

The supervisor must:

1. reconcile current Git/authority state;
2. load all TaskSpecs;
3. validate the graph;
4. reconcile durable accepted-task history;
5. reject externally reserved or blocked tasks;
6. select only dependency-safe READY work;
7. persist the exact TaskSpec digest before mutation.

Do not assume a repository's textual `status` field is the execution truth. Some repositories intentionally keep source ledgers immutable or status-neutral.

RC4 external state therefore records accepted completion independently. A source status may be used only when the adapter contract explicitly declares its semantics.

## 11. Execution Envelope

For each active task, RC4 derives:

- `direct_edit_paths` — paths direct file tools may mutate;
- `promotion_paths` — paths that may exist in an accepted commit/diff;
- `runtime_scratch_paths` — paths tools/tests may create but never promote;
- `protected_paths` — governance/planning/security/control paths;
- `baseline_wip` — exact pre-task tracked/untracked state.

### 11.1 Direct file tools

Edit/Write/NotebookEdit must be denied outside the active task direct-edit envelope when task ownership is enabled.

No active task means no product direct edits in task-owned mode.

### 11.2 Bash/process writes

Command strings cannot reliably reveal every file a compiler, formatter, generator or test will mutate.

RC4 must therefore use layered enforcement:

1. static pre-command rejection for obvious outside-repository/protected/task-escape writes;
2. OS sandbox for host/repository boundary protection;
3. isolated task worktree where practical;
4. post-command Git-state comparison;
5. staged-diff/commit gate;
6. promotion gate.

An arbitrary subprocess must never be able to smuggle an out-of-envelope product change into an accepted commit.

### 11.3 Task worktrees

For Git-backed task-owned execution, prefer a dedicated package-owned task worktree.

Benefits:

- accidental formatter/generator edits do not damage the user's primary checkout;
- unauthorised tracked changes can be discarded safely;
- pre-existing user WIP remains untouched;
- exact task base SHA is stable;
- interrupted task state can be resumed deterministically;
- accepted task commits can be promoted only after envelope verification.

The task-worktree implementation must support restart recovery and must never `reset --hard` or `clean` the user's primary checkout.

### 11.4 Runtime scratch versus promotable source

A test/build may legitimately write files that a task is not allowed to commit.

Scratch paths are therefore distinct from promotion paths.

Scratch changes:

- may exist during execution if explicitly allowed or safely detected;
- cannot be staged/promoted;
- are cleaned only inside package-owned disposable task worktrees;
- never cause trusted Git excludes to hide real source changes.

Tracked changes outside promotion paths always reject acceptance.

## 12. Ownership repair and scope expansion

A model must never self-expand `owned_paths`.

When implementation proves the current ownership declaration incomplete:

- if the missing path is mechanically required by existing planning authority, trigger Planning Repair to update the authoritative task definition;
- independently verify and promote that repair;
- rebuild the TaskSpec;
- resume the same task under the new exact envelope.

If the required expansion changes product semantics or authority and is not mechanically determined, return an operator decision blocker.

This converts “please let me edit one more file” into a durable, reviewable planning correction instead of an implicit permission bypass.

## 13. Protected-path precedence

The following precedence must hold:

```text
host/platform hard boundary
    >
package protected paths
    >
repository governance protection
    >
planning-repair envelope
    >
task execution envelope
    >
model/tool request
```

A broader lower-level rule must never override a narrower higher-level deny.

Examples:

- task says `**` but governance contract is protected -> governance file remains denied;
- task owns `docs/**` but planning authority contains `docs/plan/**` -> plan members remain denied to product worker;
- adapter outputs `../outside/**` -> rejected before activation;
- repository config asks for host `/tmp` or `~/.ssh` -> rejected as product authority.

## 14. Repository topology handling

RC4 qualification must explicitly cover these topologies.

### 14.1 Standard Git repository

Full support.

### 14.2 Linked Git worktree

Full support. Work-unit identity must remain distinct while sharing the common Git directory safely.

### 14.3 Monorepo

Full support for one Git root containing many packages/languages.

Task ownership may target:

- one package;
- several packages;
- shared root files;
- cross-package integration tests.

Shared files may be owned by multiple tasks sequentially. Parallel writers are not implied.

### 14.4 Nested repositories / submodules

Default: treat each nested Git repository/submodule as a separate authority boundary.

The parent task cannot silently gain mutation authority inside a child repository.

RC4 may read submodule metadata, but child mutation requires an explicitly configured managed workspace/repository boundary.

If the child content required for validation is missing, report a typed environment/repository blocker.

### 14.5 Sparse checkout

Detect it.

If required authority/task files are not materialised, do not guess. Either materialise them through a bounded operator-approved Git path or block with exact missing paths.

### 14.6 Shallow/partial clones

Detect ancestry/object absence.

Fetch only through the existing trusted Git/network policy when permitted. Otherwise report a typed missing-history/object blocker.

Never treat “object unavailable” as “not an ancestor”.

### 14.7 Git LFS

Detect pointer-vs-materialised content when an authority or verification input depends on LFS content. Missing objects are an environment blocker, not permission to reinterpret the file.

### 14.8 Detached HEAD

Read-only inspection is allowed.

Autonomous mutation/promotion requires an explicit target branch or package-owned work branch. Never invent a durable branch destination.

### 14.9 Non-Git directory

Retain RC3 inspection/objective execution where possible.

Features requiring exact-SHA attestation, Git worktrees, ancestry or Git promotion are unavailable and must report that limitation explicitly.

Do not emulate Git guarantees with weaker hidden behaviour.

### 14.10 Multiple remotes

Never infer the promotion remote from arbitrary order.

Use explicit configuration or an unambiguous tracked upstream. Promotion identity records remote URL/name + branch + expected SHA.

## 15. Path edge cases

Tests must cover:

- spaces;
- tabs/newlines where Git permits them;
- Unicode filenames;
- Unicode normalisation collisions;
- case-fold collisions;
- very long paths;
- glob metacharacters;
- leading dash filenames;
- symlink files and symlink directories;
- rename source + destination;
- deletion;
- file-to-directory and directory-to-file transitions;
- executable-bit-only changes;
- binary files;
- submodule gitlinks.

Every promotion-path check must operate on NUL-delimited Git output or equivalent safe structured data.

## 16. Dirty worktrees and pre-existing WIP

Before a task starts, capture exact baseline state.

Rules:

- unrelated primary-checkout WIP must never be destroyed;
- task worktrees should isolate product work from that WIP;
- if the selected task logically depends on uncommitted WIP that is not in its task base, stop and require explicit adoption/checkpointing;
- promotion must revalidate remote/product base and WIP fingerprint;
- lost-response retries must remain idempotent;
- cleanup may delete only package-owned disposable state or files proven byte-identical to a known target, never arbitrary user WIP.

## 17. Concurrency

RC4 default remains **single product writer per repository/work unit**.

The schema should not prevent future parallel writers, but RC4 must not claim parallel safety it has not proven.

Required:

- one supervisor lease for state-mutating operations;
- one active product task by default;
- independent read-only verifier/research/security agents may run;
- planning writer and product writer remain separate;
- planning architect cannot verify/promote itself;
- stale supervisor fencing tokens are rejected;
- overlapping ownership is valid sequentially but blocks future parallel activation unless explicitly proven conflict-free.

## 18. Session continuity and migration

RC4 should close the migration gap exposed by replacing an existing harness.

Add an explicit operator-facing resume path using Claude Code's native session resume facility, for example:

```text
claude-auto start --resume-session <session-id-or-name>
```

Requirements:

- resume is operator-initiated;
- current RC4 settings/guards/profile are applied to the resumed process;
- repository identity is checked/reconciled before mutation;
- durable external state remains authoritative over conversation memory;
- a resumed conversation cannot bypass a changed governance/task envelope;
- session name/ID is recorded for subsequent exact resumes.

Also support a normalised state-adoption/import path so an existing repository-specific harness can supply:

- current product SHA;
- verified-through SHA;
- active task;
- accepted task IDs;
- blockers/reservations;
- authority snapshot identity.

Imported state is never trusted blindly; RC4 reconciles it against Git and current governance before continuing.

## 19. Governance observability

Add machine-readable and human-readable inspection:

```text
claude-auto governance validate --repo .
claude-auto governance status --repo .
claude-auto governance explain --repo .
claude-auto task status --repo .
claude-auto task show TASK_ID --repo .
```

Status should show:

- resolved Authority Set;
- Authority Set digest;
- planning-member roles/mutability;
- task-source adapters;
- active task;
- resolved edit/promotion/scratch envelopes;
- blockers;
- current base/verified SHA;
- whether configuration is RC3-compatible single-file mode or RC4 governance mode.

Every guard denial should explain which authority layer denied the operation.

## 20. Security model for repository-supplied commands

Planning validators, task adapters and reconciliation/generation commands are repository-controlled code.

They must use the same or stronger boundary as deterministic verification:

- sanitised environment;
- no inherited `GIT_CONFIG_*` injection;
- no host-control sockets;
- no secret reads;
- no network unless explicitly required and policy-approved;
- bounded timeout;
- bounded output;
- no unsandboxed retry in Strict;
- write scope appropriate to the command class;
- exact before/after repository snapshots.

A validator is never allowed to mutate source and then call the mutation “validation”.

A generator/reconciler may mutate only declared generated paths inside a dedicated worktree.

## 20.1 Governance is semantic authority, not a permission profile

Task/planning governance must remain enforced in every execution profile.

- Balanced may reduce prompts but cannot widen task or planning authority.
- Strict may further narrow runtime permissions.
- Unattended removes ordinary human approval friction, but it must **not** disable planning authority, task ownership, exact-SHA verification or promotion gates.
- Isolated Full may broaden runtime execution inside its outer isolation boundary, but accepted product history is still constrained by the same Authority Set and TaskSpec.

Profiles control *how operations execute*. Governance controls *which product/planning state is authorised*. The two concerns must not be conflated.

## 20.2 Named authority domains for large monorepos

Some monorepos contain several genuinely independent planning domains rather than one global plan.

RC4 schema should therefore allow a repository to define either:

- one default Authority Set; or
- multiple named Authority Sets/domains.

A task may reference one or more authority-domain IDs. Repair and attestation are scoped to the affected domain(s), while the repository-level governance contract remains common.

Example conceptual structure:

```json
{
  "planning_authority": {
    "sets": [
      {"id": "platform", "members": []},
      {"id": "service-a", "members": []},
      {"id": "service-b", "members": []}
    ]
  }
}
```

The one-file and ordinary multi-file cases remain shorthand for one default set.

Cross-domain dependencies are allowed only when they resolve unambiguously. A repair must not gain permission to change unrelated domains merely because they share the same Git root.

## 20.3 Repository control surfaces require stronger treatment

The following files can change the behaviour of the harness or verification environment and therefore must not be treated as ordinary product files merely because they live inside the repository:

- `.claude-auto/governance.json`;
- `.claude-auto/verification.json`;
- task-adapter source/configuration;
- planning validator/reconciler definitions;
- Claude project settings/hooks/agents/skills used by the selected session;
- Git control files such as `.gitattributes`, `.gitmodules` and repository-local config that changes checkout/filter/submodule semantics.

RC4 must classify these as **control surfaces**.

Rules:

- control-surface mutation cannot silently take effect inside an already-authorised task;
- a change invalidates the relevant governance/verification/session snapshot;
- weakening a verification contract cannot make the same candidate pass;
- repository hooks or filters are never trusted promotion/commit authority;
- package-owned commits should disable repository Git hooks and signing requirements where appropriate;
- trusted inspection should avoid external diff/text-conversion execution and use plumbing/structured Git output where possible;
- a task that legitimately needs to modify a control surface must go through an explicit control-surface repair/change path and fresh independent verification.

This prevents a task from first weakening its own guard/verification configuration and then using the weakened policy.

## 20.4 Out-of-band repository changes

The supervisor must treat unexpected repository changes as a first-class concurrency event.

If HEAD, index, tracked WIP, Authority Set, TaskSpec source or control-surface identity changes outside the current supervisor:

- pause mutation;
- classify whether the change is a known descendant produced by the supervisor;
- invalidate stale task/planning/verification snapshots when identity changed;
- recompute overlap with the active task;
- resume only after exact reconciliation.

Ancestry alone is insufficient: an out-of-band descendant commit may still change planning or task authority.

## 20.5 Adapter and validator capabilities

Repository-supplied adapters/validators are read-only and networkless by default.

If a legitimate source lives behind a network/API/database boundary, the adapter may **request** capabilities in its declaration, but the declaration cannot grant them.

The package/operator policy decides whether to allow:

- specific network domains;
- loopback services;
- a specific read-only external path;
- a specific credential broker.

Granted capability identity is included in the adapter snapshot. Changing requested capabilities invalidates the snapshot.

Adapters must never receive arbitrary write authority merely because their data source is external.

## 20.6 Claude Code platform boundary assumptions

RC4 must explicitly test the Claude Code version it qualifies against instead of assuming sandbox behaviour.

Current platform behaviour distinguishes:

- shell commands/processes, which are inside the OS sandbox when enabled;
- built-in file tools and hooks, which are outside that shell sandbox and therefore require package-level permission/hook enforcement;
- native session resume by session ID or name.

RC4 P0 must pin a minimum Claude Code version whose tested setting-source, strict-sandbox and resume semantics satisfy the governance model. If the platform behaviour changes, `claude-auto doctor` must detect the unsupported version rather than silently weakening the boundary.

## 21. Large-repository requirements

RC4 must avoid per-tool-call full-repository rescans.

Use:

- cached normalised governance keyed by governance hash + Git identity;
- concrete resolved Authority Set snapshots;
- compiled ownership matchers;
- incremental Git status/diff checks;
- bounded adapter outputs;
- indexed full-source evidence for large verifier inputs.

Qualification fixtures should include:

- at least 1,000 tasks across multiple ledgers;
- at least 100,000 tracked-file path records in synthetic path-resolution tests;
- large planning files beyond prompt-inline limits;
- duplicate/cycle/error cases at scale.

The test fixtures must use neutral synthetic project names.

## 22. Failure and interruption matrix

RC4 must be restart-safe if interrupted:

- before task selection is persisted;
- after task selection but before Claude starts;
- during product edits;
- after tests but before commit;
- after commit but before verifier;
- after verifier but before promotion;
- during remote push with lost response;
- after product branch advances;
- before planning repair worktree creation;
- during multi-file architect edits;
- during generated-projection reconciliation;
- after candidate commit;
- after verifier attestation;
- during planning base refresh;
- during planning promotion;
- after planning promotion but before task envelope rebuild;
- during state-schema migration;
- during session/profile switch.

Every state transition must be idempotent or have an explicit recovery state.

## 23. Qualification scenario matrix

RC4 is not qualified until automated tests cover at least:

1. one-file Markdown plan, no task ownership;
2. one-file structured plan with task ownership;
3. multi-file planning authority with two task ledgers;
4. manifest + ledgers + traceability;
5. generated planning projection;
6. immutable requirement file plus repairable ledger;
7. cross-ledger dependency;
8. duplicate task IDs across ledgers;
9. missing dependency;
10. dependency cycle;
11. task with no owned paths;
12. task owning one file;
13. task owning nested directories;
14. task owning paths with spaces/Unicode;
15. overlapping task ownership used sequentially;
16. attempted direct edit outside active task;
17. arbitrary Bash command modifies out-of-envelope tracked file;
18. formatter touches unrelated tracked file;
19. build creates allowed scratch files;
20. build creates unexpected untracked files;
21. attempted edit of planning authority by product worker;
22. attempted governance-contract edit;
23. mechanical ownership defect repaired through planning repair;
24. semantic ownership expansion rejected for operator decision;
25. multi-file repair changes only permitted repairable members;
26. architect attempts immutable-member edit;
27. architect attempts generated-member direct edit;
28. generator modifies undeclared path;
29. stale exact-SHA planning attestation;
30. product branch advances during repair;
31. lost-response remote promotion retry;
32. real pre-existing WIP outside task;
33. overlapping pre-existing WIP;
34. linked worktree;
35. monorepo cross-package task;
36. submodule mutation attempt;
37. sparse checkout missing authority file;
38. shallow clone missing ancestry;
39. missing LFS authority content;
40. detached HEAD;
41. non-Git directory graceful limitation;
42. multiple remotes;
43. symlink/path traversal attack;
44. malicious custom adapter output;
45. adapter timeout/invalid JSON/oversized output;
46. repository validator tries host escape;
47. corrupted external state recovery;
48. stale supervisor;
49. active RC3 single-file policy upgrade;
50. active/inactive planning-repair state upgrade;
51. exact named/session-ID resume under new RC4 policy;
52. imported legacy task state reconciles or fails closed;
53. 1,000-task multi-ledger stress fixture;
54. 100,000-path matcher/resolution stress fixture;
55. Unattended profile still enforces task/planning governance;
56. multiple named authority domains in one monorepo;
57. task spanning two declared authority domains;
58. unrelated authority domain cannot be modified by a scoped repair;
59. task attempts to weaken governance/verification contract before commit;
60. task changes Claude/Git control-surface files and forces revalidation;
61. out-of-band descendant commit changes task authority;
62. adapter requests undeclared network/credential capability;
63. ignored/untracked scratch cannot be promoted with `git add -f`;
64. repository Git hooks/filter/textconv cannot alter broker truth.

## 24. Shadow-validation requirement against a complex real-world structure

Before RC4 replaces an existing bespoke harness, run **read-only shadow mode**.

Shadow mode must:

- resolve the repository's planning authority;
- resolve task sources;
- compute the dependency-safe frontier;
- resolve the current/next TaskSpec;
- compute owned/evidence paths;
- compare the result with the existing harness;
- make no product/planning/control writes.

Migration is allowed only when the results are semantically equivalent or every difference is explicitly understood.

Public tests should use neutral synthetic fixtures; private/real-project shadow validation is an additional field qualification, not hard-coded product logic.

## 25. Proposed implementation components

New modules should remain small and testable. Tentative split:

```text
lib/governance_contract.py
lib/authority_set.py
lib/task_protocol.py
lib/task_sources.py
lib/task_authority.py
lib/execution_envelope.py
lib/task_worktree.py
lib/governance_migration.py

hooks/task_write_guard.py
hooks/task_post_command_guard.py
```

Existing modules to extend rather than duplicate:

```text
lib/planning_repair.py
lib/planning_support.py
lib/settings_policy.py
lib/workspace_recovery.py
lib/promotion_policy.py
lib/repo_profile.py
lib/control_plane.py
lib/claude_auto.py
lib/cli_schema.py
```

Do not create a second promotion system or a second unrelated state store.

## 26. Implementation sequence

### RC4-P0 — planning closure

- review this document against RC3 implementation;
- threat-model governance and adapter inputs;
- verify Claude Code current sandbox/resume semantics;
- freeze the governance schema v1;
- define exact state migration from RC3.

Exit: no unresolved architecture contradiction.

### RC4-P1 — Authority Set foundation

- implement governance contract loader/validator;
- implement path normalisation and deterministic membership;
- implement Authority Set snapshot/digest;
- map legacy single canonical plan to one-member Authority Set;
- protect governance + all authority members from product worker.

Exit: single-file RC3 behaviour passes unchanged and multi-file authority can be represented read-only.

### RC4-P2 — TaskSpec and adapters

- implement TaskSpec schema;
- implement built-in sources;
- implement bounded custom adapter protocol;
- implement multi-source merge, uniqueness and graph validation;
- persist task-source digest.

Exit: structured repositories produce deterministic TaskSpecs without model authority.

### RC4-P3 — Execution envelopes

- derive active task envelopes;
- extend direct file-write guard;
- add staged-diff gate;
- add post-command repository mutation detection;
- separate scratch from promotable paths;
- add explain/status commands.

Exit: no out-of-envelope product change can become accepted/pushed.

### RC4-P4 — task worktree isolation

- add package-owned resumable task worktree;
- preserve primary-checkout WIP;
- implement restart/interruption recovery;
- promote accepted task commit only after envelope + verifier checks.

Exit: arbitrary in-repository subprocess side effects are contained until acceptance.

### RC4-P5 — multi-file Planning Repair

- generalise one-file policy to Authority Set;
- implement Repair Envelope;
- direct-edit restrictions by mutability class;
- bounded generated-member reconciliation;
- whole-authority validation;
- exact-SHA + Authority Set attestation;
- advancing-base/interruption handling.

Exit: one-file and multi-file repair use the same core flow.

### RC4-P6 — migration/session adoption

- state schema migration;
- legacy single-file policy migration;
- in-flight repair migration/recovery;
- normalised legacy task-state import;
- `--resume-session` support;
- shadow mode.

Exit: an existing supervised project can be adopted without discarding durable progress.

### RC4-P7 — adversarial and topology qualification

- execute the full scenario matrix;
- performance/stress tests;
- security red-team;
- source and extracted-release tests;
- installer upgrade/rollback tests;
- documentation and examples.

Exit: no known HIGH/CRITICAL issue and all release gates green.

## 27. Release gates

Do not promote RC4 to `main` or tag `v1.0.0-rc4` until:

- all existing RC3 tests still pass;
- new governance/task tests pass from source;
- the extracted release archive passes the same tests;
- upgrade from RC3 preserves external state;
- single-file planning remains a first-class supported path;
- multi-file planning repair is independently exact-SHA verified;
- task ownership cannot be self-expanded by the model;
- out-of-envelope writes cannot reach accepted product history;
- governance/adapters cannot escape repository/host boundaries;
- interruption matrix is exercised;
- neutral large-repository fixtures pass;
- read-only shadow validation succeeds on at least one complex multi-ledger repository;
- release manifest/licence/NOTICE/executable metadata remain correct;
- final independent correctness and security review returns no unresolved release blocker.

## 28. Non-goals for RC4

To keep the security boundary precise, RC4 does not need to claim:

- native Windows sandbox parity; Linux remains the supported baseline;
- arbitrary simultaneous multi-repository transactional commits;
- multiple concurrent product writers by default;
- automatic semantic decisions when requirements conflict;
- blind execution of repository-provided code outside isolation;
- parsing every conceivable planning language natively.

Those cases must be handled by an explicit adapter/boundary or an actionable typed blocker, never by silent weakening.

## 29. Success criterion

RC4 succeeds when a user can point Claude Auto at either:

```text
PLAN.md
```

or a complex planning system containing many authority files and task ledgers, and the same package can:

1. determine the exact planning authority;
2. determine the exact task frontier when structured tasks exist;
3. activate a bounded task;
4. keep product changes inside that task's accepted envelope;
5. test and independently verify the exact implementation;
6. repair defective planning without letting the product worker rewrite authority;
7. promote only exact verified state;
8. survive restart, interruption and remote advancement;
9. preserve user WIP;
10. explain clearly when safe autonomous continuation is impossible.

No project-specific names, paths or hard-coded task schemas belong in the universal runtime.
