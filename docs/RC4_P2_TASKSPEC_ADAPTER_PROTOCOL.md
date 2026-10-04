# RC4 P2 — TaskSpec and Adapter Protocol

Status: **FROZEN FOR IMPLEMENTATION**

Date: 2026-10-04

Branch: `release/1.0.0-rc4`

P1 closure: `0d9d9c1984efa7c502191b81395c33d625eaacb6`

## 1. Purpose

P2 turns repository-declared structured task sources into deterministic package-owned TaskSpecs **without giving a model, repository script, adapter, or source file permission to enlarge its own authority**.

P2 is a normalisation/read-only authority phase. It does not select a task for execution, create a task worktree, or grant product mutation rights.

The RC3 validated model-produced plan graph remains intact. Later RC4 phases may reconcile/adopt TaskSpecs into execution, but P2 does not create a second autonomous supervisor.

## 2. Source declaration v1

Every repository task source has a stable source ID and an explicit AuthoritySet ceiling.

Built-in file source:

```json
{
  "id": "component-a",
  "kind": "json",
  "authority_sets": ["default"],
  "paths": ["planning/tasks/*.json"]
}
```

The same shape supports `jsonl` and `toml`.

Static source:

```json
{
  "id": "bootstrap",
  "kind": "static",
  "authority_sets": ["default"],
  "tasks": []
}
```

Custom adapter source:

```json
{
  "id": "legacy-export",
  "kind": "adapter",
  "authority_sets": ["default"],
  "argv": ["python3", "tools/export_tasks.py"],
  "cwd": ".",
  "inputs": ["tools/export_tasks.py", "planning/legacy.db"],
  "timeout_seconds": 60,
  "capabilities": {
    "network": [],
    "read_external": []
  }
}
```

P2 rules:

- source IDs use the same compact stable-ID family as AuthoritySet IDs;
- source IDs are unique;
- `authority_sets` is non-empty and references existing AuthoritySets;
- file-source selectors are repository-relative, bounded package globs;
- each file selector must resolve at least one exact committed regular blob;
- duplicate file resolution inside one source is rejected rather than silently deduplicated;
- file/adapter inputs become protected control surfaces;
- source ordering does not define merge precedence.

P2 supports no source override/last-writer-wins semantics. Duplicate Task IDs across any sources fail closed.

## 3. TaskSpec v1

Canonical TaskSpec:

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

Exact v1 keys are required. Unknown fields fail closed.

### 3.1 Stable IDs

Task IDs:

- are non-empty strings up to 128 characters;
- use `[A-Za-z0-9][A-Za-z0-9._:-]*`;
- are globally unique in the merged TaskSourceSet.

This admits existing IDs such as `PV2-CORE-I01.01` without making path syntax part of the ID language.

### 3.2 Authority ceiling

A task's `authority_sets` must be a non-empty subset of its source declaration's `authority_sets`.

An adapter can therefore emit **less** authority than its source ceiling, never more.

Every referenced AuthoritySet must exist in the active AuthoritySet snapshot.

### 3.3 Path selectors

`owned_paths`, `evidence_paths` and `runtime_scratch_paths` use the same package-owned path-selector grammar as AuthoritySet members.

They are normalised to NFC/POSIX form and reject:

- absolute paths;
- traversal;
- empty segments;
- control characters;
- duplicate selectors.

P2 does not require an owned/evidence/scratch selector to match an existing blob because tasks may legitimately create new paths.

However, no selector may claim any currently resolved planning-authority member or control surface. Mandatory governance prefixes also retain precedence even when the task uses a broad selector such as `**`.

P3's envelope/diff gate remains the final enforcement boundary.

### 3.4 Verification and metadata

`verification` is a non-empty list of non-empty stable check IDs/names. P2 records requirements; it does not execute them.

`commit_subject` is optional human-facing text and is excluded from the authority digest.

`metadata` must be a bounded JSON object and is excluded from the authority digest.

All other TaskSpec fields are authority-relevant.

Each normalised task records:

- full record digest;
- authority-only `task_spec_sha256`;
- source provenance outside the canonical TaskSpec.

## 4. Built-in source formats

### JSON

Accepted exact committed content:

```json
{"schema_version": 1, "tasks": [ ...TaskSpec... ]}
```

or a top-level TaskSpec array.

JSON duplicate object keys are rejected.

### JSONL

Each non-empty line is exactly one JSON TaskSpec object. Duplicate keys are rejected per line.

### TOML

Uses Python's standard `tomllib`.

Expected shape:

```toml
schema_version = 1

[[tasks]]
schema_version = 1
id = "T1"
...
```

### Static

The `tasks` array is embedded directly in the committed governance contract.

No built-in parser consults the mutable working-tree copy.

## 5. Exact committed input rule

Built-in file sources and adapter inputs resolve from the exact `product_head` in the active AuthoritySet snapshot.

P2 uses Git tree/blob identity, not:

- mutable index contents;
- uncommitted files;
- textconv;
- checkout filters;
- generated working-tree state;
- external diff drivers.

Git LFS pointer content is not silently substituted with untracked/materialised LFS payload. If the committed pointer is not valid TaskSpec source content, parsing blocks with an actionable error.

## 6. Custom adapter protocol

Adapters are repository code, therefore hostile by default.

### 6.1 No live-checkout execution

The package creates an ephemeral input view outside the repository containing only exact-commit regular blobs matched by the adapter's declared `inputs`.

No `.git` metadata is copied.

The live repository path is not used as the adapter working directory and is not automatically exposed to the adapter.

### 6.2 Execution boundary

Adapter execution:

- uses argv directly, never a shell string;
- uses a scrubbed environment and fresh temporary HOME/TMP;
- receives only the package-materialised exact-commit input view;
- has network disabled;
- has no provider/cloud credentials;
- has no writable Git metadata;
- receives no automatic fallback to trusted/unrestricted host execution;
- times out at the declared bounded timeout;
- has bounded stdout/stderr accepted by the protocol.

The materialised source view is mounted/readable as a read-only root by the adapter execution primitive. If the package cannot establish the required isolation boundary, adapter resolution is BLOCKED. P2 does not ask a model whether it is safe.

### 6.3 Capabilities

In P2 the only executable adapter capability set is:

```json
{"network": [], "read_external": []}
```

A repository may declare non-empty requested capabilities for future evolution, but P2 treats them as an unsatisfied authority request and blocks adapter execution.

A declaration is never a permission grant.

### 6.4 Output

Adapter stdout must be a single JSON value:

```json
{"schema_version": 1, "tasks": [ ...TaskSpec... ]}
```

or a top-level TaskSpec array.

Non-protocol stdout, malformed JSON, oversized output or non-zero exit blocks the source.

stderr is diagnostic only and is size bounded/redacted when reported.

### 6.5 Determinism challenge

A custom adapter is executed twice in fresh isolated contexts over the same exact input snapshot.

The two **normalised TaskSpec result digests** must match.

This catches ordinary time/random/PID/temp-state/output-order nondeterminism before adapter output becomes task authority.

The package supplies stable protocol context such as exact product commit and source ID through non-secret environment values.

## 7. Multi-source merge

Resolution order is deterministic but has no precedence semantics.

The package:

1. resolves all source declarations;
2. normalises every TaskSpec;
3. rejects duplicate Task IDs globally, even if payloads are identical;
4. sorts the merged set by Task ID;
5. validates dependencies against the complete merged ID set;
6. validates cycles iteratively rather than recursively;
7. records external/missing dependencies only when `strict_dependencies=false`;
8. fails on missing dependencies when `strict_dependencies=true`.

A dependency cycle always fails.

P2 does not infer dependencies from file paths or model reasoning.

## 8. Digests and durable state

The package persists a TaskSourceSet record outside the repository under the repository state directory.

The record binds at least:

- AuthoritySet snapshot digest;
- exact product commit;
- task-source contract digest;
- exact source input blob identities;
- adapter normalised output digest/evidence;
- merged canonical TaskSpecs;
- per-task authority digest;
- global TaskSourceSet digest.

After a successful resolve:

`state.json.task_source_sha256` is the effective TaskSourceSet digest.

If the AuthoritySet/governance snapshot changes, activation invalidates the resolved task-source digest rather than pretending the old resolution still applies.

P2 does not set `active_task_id` or an execution-envelope digest.

## 9. Operator surfaces

P2 adds:

```bash
claude-auto tasks status --repo .
claude-auto tasks resolve --repo .
```

`status` never executes repository code. It reports persisted state and whether it is current for the active AuthoritySet snapshot.

`resolve` may run declared custom adapters, but only under the bounded protocol above.

## 10. Relationship with RC3 planning

RC3 currently validates model-produced executable tasks containing fields such as:

- ID;
- title;
- dependencies;
- verification;
- risk.

P2 does not delete or silently reinterpret that plan state.

TaskSpec is repository-owned task **authority/ownership**. The later RC4 adoption/execution phases will define the reconciliation between a TaskSpec and the model-produced implementation plan.

Until then, TaskSpec resolution alone cannot authorise a worker to mutate product files.

## 11. P2 threat/qualification matrix

P2 must test at least:

- deterministic JSON source;
- deterministic JSONL source;
- deterministic TOML source;
- static source;
- several sources merging in source-order-independent form;
- duplicate source ID;
- duplicate Task ID inside one source;
- duplicate Task ID across two sources;
- malformed/duplicate-key JSON;
- malformed JSONL line;
- malformed TOML;
- missing required source selector;
- one source selector resolving the same file twice;
- source file symlink;
- source file gitlink/submodule boundary;
- source AuthoritySet reference missing;
- TaskSpec AuthoritySet exceeds source ceiling;
- missing TaskSpec AuthoritySet;
- malformed Task ID;
- duplicate dependency;
- self-dependency;
- missing dependency in strict mode;
- missing dependency in non-strict mode;
- deep acyclic graph;
- deep cycle without recursion failure;
- duplicate path selectors;
- absolute/traversal/control-character task path;
- task selector directly naming authority member;
- broad `**` task selector overlapping an authority/control surface;
- task selector attempting to own `.claude-auto/governance.json`;
- metadata/commit-subject change leaves authority digest stable while full digest changes;
- authority-relevant TaskSpec change changes task digest;
- task-source contract change invalidates TaskSourceSet digest;
- committed source blob change invalidates TaskSourceSet digest;
- uncommitted source WIP cannot alter the resolved task set;
- malicious adapter tries to modify its materialised inputs;
- malicious adapter tries to find/use live repository Git metadata;
- malicious adapter attempts network access;
- malicious adapter declares non-empty network capability;
- malicious adapter declares external-read capability;
- adapter timeout;
- adapter non-zero exit;
- adapter malformed/oversized output;
- adapter noisy stdout around JSON;
- adapter output attempts authority escalation;
- adapter output duplicate Task IDs;
- nondeterministic adapter output;
- adapter input file changed in the working tree but not committed;
- source-order permutation produces the same merged digest;
- `tasks status` never executes an adapter;
- `tasks resolve` cannot mutate target repository state;
- no task becomes active merely because resolution succeeded;
- all existing P1/RC3 regression tests remain green.

## 12. P2 implementation boundary

P2 may implement:

1. strict task-source declaration validation in `governance_contract.py`;
2. `lib/task_spec.py`;
3. `lib/task_sources.py`;
4. a reusable read-only isolated repository-code execution primitive;
5. exact-Git-blob source parsing;
6. adapter input materialisation and two-run determinism challenge;
7. deterministic merge/graph validation;
8. package-owned TaskSourceSet persistence;
9. `tasks status` and `tasks resolve`;
10. activation invalidation/persistence of `task_source_sha256`;
11. P2 tests/documentation.

P2 must **not** implement:

- active task selection;
- task execution envelopes;
- task worktrees;
- worker write fences derived from TaskSpec;
- staged-diff/promotion gates;
- multi-file planning repair;
- adapter-requested network/external-read grants;
- conversion of TaskSpecs into completed/accepted task state;
- promotion to `main` or an RC4 tag.

## 13. Exit criterion

P2 is complete only when:

- structured repositories deterministically resolve TaskSpecs without model participation;
- adapters cannot grant themselves authority or execute against the live repository;
- multi-source graph validation is deterministic and fail-closed;
- effective TaskSourceSet state is bound to the active AuthoritySet snapshot and exact committed inputs;
- RC3/P1 regression behaviour remains green.

Only after that may RC4-P3 derive execution envelopes from TaskSpec authority.
