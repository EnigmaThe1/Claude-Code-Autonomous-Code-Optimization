# Claude Code Autonomous Code Optimization

**Version 1.0.0-rc3**

Claude Code Autonomous Code Optimization is a user-level control and optimisation layer for Claude Code. It is designed for long-running software-engineering work where Claude should keep implementing, testing, diagnosing and repairing until the requested objective is complete or a genuine external blocker is reached.

It does not replace Claude Code and it does not require package files to be installed into every target repository. The optimisation layer is installed once for the user, while repository-specific runtime state is kept outside the repositories it operates on.

Repository: https://github.com/EnigmaThe1/Claude-Code-Autonomous-Code-Optimization

## What it adds

- durable autonomous execution across multiple Claude turns and process restarts;
- objective-driven implementation with a validated, versioned implementation plan;
- plan simulation and red-team review before coding and at material repair boundaries;
- automatic diagnosis and repair of implementation failures;
- controlled plan repair when repository reality proves the current plan wrong or incomplete;
- deterministic test/build/lint/type-check verification;
- independent correctness and security review gates;
- durable permission escalation for operations that require human authority;
- explicit execution profiles for different trust and autonomy levels;
- human-controlled hot switching between normal execution profiles;
- provider/model qualification, fallback and retry handling;
- repository-state, environment and toolchain continuity across resumes;
- compact telemetry and durable checkpoints;
- narrow recovery helpers for Git/workspace failure cases;
- deterministic package-owned Git WIP/promotion context, including trusted excludes reconstruction;
- exact-SHA verifier attestations for protected promotion;
- optional remote-aware, idempotent promotion with exact expected-base protection;
- optional repository-owned canonical-plan authority with dedicated repair worktrees and independent verification;
- deterministic repository-owned TaskSpecs with package-owned active ExecutionEnvelopes, post-batch mutation detection and path-gated staging/promotion;
- explicit crash-safe RC4 state migration, legacy progress adoption/re-attestation, forked Claude session adoption and read-only shadow comparison.

The governing principle is: **the user's objective is authoritative; the implementation plan is a repairable route to that objective.**

## Objective and plan behaviour

Claude Auto is intentionally neither locked to a defective plan nor free to invent a different product.

Before implementation begins, a supplied plan is reconciled with the objective, repository state and current instructions. If only an objective is supplied, Claude Auto generates the smallest complete plan needed to satisfy it. The candidate is independently simulated and red-teamed before mutation begins.

During implementation:

- contained coding defects can be repaired directly;
- non-trivial local repairs are checked for backward and forward impact;
- material architecture, API, schema, security, dependency or requirement conflicts trigger a versioned plan-repair cycle;
- the repaired plan must still satisfy the original objective and acceptance criteria;
- new technical work may be added when necessary to implement an existing requirement;
- unrelated product features or scope expansion are not authorised merely because they might be useful.

Completion is accepted only after the whole plan and implementation survive deterministic verification plus independent review gates.

### Repository-owned planning authority

Repositories may keep planning authority inside Git as either a legacy single canonical file or a structured multi-file governance contract.

Legacy one-file setup remains supported:

```bash
claude-auto planning-repair configure --repo . --plan IMPLEMENTATION_PLAN.md
claude-auto run --repo . --plan IMPLEMENTATION_PLAN.md
```

RC4-P5 treats that legacy file as a synthetic one-member `default` AuthoritySet. Repositories that need multiple planning ledgers/domains can instead declare named AuthoritySets in `.claude-auto/governance.json`. Each selected set may contain:

- **repairable** source members the Planning Repair Architect may edit directly;
- **immutable** requirements/contracts that may be read but not changed;
- **generated** projections that only declared package-run reconcilers may update;
- bounded deterministic validators and reconcilers;
- TaskSources whose exact candidate graph is rebuilt before verification.

When more than one AuthoritySet could be repaired, selection is explicit:

```bash
claude-auto planning-repair begin --repo . --authority-set domain-a --reason "..."
claude-auto planning-repair architect --repo . --authority-set domain-a --reason "..."
```

A durable RepairEnvelope binds the exact product base, selected AuthoritySets, member mutability, growth selectors, helper contracts, TaskSource contract and protected control surfaces. The Architect cannot widen that envelope. Generated members are reconciled package-side, validators run against exact candidate inputs, candidate AuthoritySets and TaskSources are rebuilt from the candidate SHA, and a separate read-only Planning Verifier must attest that same SHA and RepairEnvelope before promotion.

The normal P5 lifecycle is:

```text
material planning defect
  -> dedicated repair worktree + RepairEnvelope
  -> Planning Repair Architect
  -> deterministic reconciliation (when declared)
  -> exact candidate AuthoritySet/TaskSource validation
  -> declared deterministic validators
  -> independent exact-SHA Planning Verifier
  -> enriched planning attestation
  -> protected fast-forward promotion
  -> re-resolve planning/task authority
```

A generic `promote-ff` cannot bypass this gate when a target changes an existing or selector-matching AuthoritySet planning member. Base refresh is crash-recoverable but refuses automatic reuse if governance, executable helper/verification control bytes, selected authority shape or TaskSource contract changed.

If repair requires a genuinely unresolved product/semantic decision, automation stops instead of redefining the objective.

See [Planning and repair](docs/PLANNING_AND_REPAIR.md).

### Repository-owned tasks and ExecutionEnvelopes

RC4 repositories may declare deterministic TaskSources in `.claude-auto/governance.json`. Claude Auto resolves those sources from exact committed Git objects into a package-owned TaskSourceSet. Built-in JSON/JSONL/TOML/static sources require no repository code; custom adapters run twice against only their declared exact-commit inputs inside the bounded read-only/no-network adapter boundary.

For task-governed repositories, `claude-auto run` resolves the dependency-safe READY task and performs mutating work in a package-owned resumable linked worktree rather than in the user's primary checkout. The coordinator checkout remains the authority root and is fingerprinted independently, while the active ExecutionEnvelope applies owned/evidence/scratch path authority to the task worktree.

Useful inspection/control commands include:

```bash
claude-auto tasks status --repo .
claude-auto tasks show TASK_ID --repo .
claude-auto tasks explain TASK_ID --repo .
claude-auto tasks workspace-status --repo .
claude-auto tasks begin [TASK_ID] --repo .
claude-auto tasks candidate --repo .
claude-auto tasks verify --repo .
claude-auto tasks accept --repo .
claude-auto tasks cleanup --repo .
claude-auto tasks abort --repo . --reason "..."
```

Direct edits outside the task envelope are denied. Opaque command effects are checked after each Claude tool batch against actual task-worktree, primary-checkout and relevant Git-ref state. Scratch may be writable but cannot enter a candidate. Protected planning/control state wins over broad task selectors in every execution profile, including Unattended and Isolated Full.

Candidate sealing is package-owned and keeps task-worktree HEAD pinned to the exact product base. Deterministic verification runs in detached exact-SHA worktrees using the existing verification-command authority; TaskSpec verification strings remain acceptance claims, not executable shell. A separate read-only Task Verifier must attest the exact candidate SHA before promotion. Successful acceptance persists a full integrity-checked AcceptedTaskRecord, supports no-op tasks without fake commits, cleans the task workspace and recomputes the next dependency-safe READY frontier. Rejected candidates return to the same task for repair; stale/drifted or unsafe cleanup states preserve evidence rather than silently resetting or rebasing work.

### Migration, session adoption and shadow validation

RC4-P6 preserves engineering progress without treating legacy state or conversation memory as authority.

Known older durable-state schemas migrate through explicit versioned steps before activation writes current state. Unknown future schemas block with zero semantic migration write. In-flight legacy one-file Planning Repair can be normalised into the P5 RepairEnvelope lifecycle while preserving candidate/WIP evidence and invalidating legacy verifier trust.

Legacy harness state can be imported as bounded claims:

```bash
claude-auto migrate adopt-state --repo . --from legacy-state.json
claude-auto migrate reattest TASK_ID --repo .
claude-auto migrate adopt-active --repo .
claude-auto migrate adopt-wip --repo .
```

Imported accepted-task claims do not immediately satisfy dependencies. Eligible work must pass the ordinary current P4 deterministic verification, independent Task Verifier and exact acceptance transaction. Active-task WIP adoption copies only current admitted owned/evidence paths into the package task worktree and never rewrites/stashes/resets the primary checkout.

Existing Claude conversations can be brought forward with:

```bash
claude-auto start --repo . --resume-session SESSION_ID_OR_NAME
```

Claude Auto uses native fork/resume to create a fresh RC4-owned guarded session. The current repository authority boundary is rebuilt first: product sessions run in the active P4 task worktree when applicable; planning-architect sessions require the active P5 repair worktree. Resumed memory cannot widen current authority.

For field qualification against an existing/bespoke harness:

```bash
claude-auto shadow snapshot --repo .
claude-auto shadow compare --repo . --legacy legacy-observation.json
```

Shadow mode computes RC4 truth independently and compares ordinary JSON observations. It does not execute the legacy harness or mutate repository/product/planning state; raw mismatches and evidence gaps remain auditable even after reviewed dispositions.

See [RC4 P6 migration/session/shadow protocol](docs/RC4_P6_MIGRATION_SESSION_SHADOW_PROTOCOL.md).

## Operating profiles

**Balanced** is the default for normal trusted development repositories. It keeps a sandboxed execution boundary where practical, lets routine reversible engineering proceed without unnecessary prompts, and supports scoped human escalation.

**Strict** is for unknown or less-trusted repositories. It tightens sandbox/read boundaries, disables unsandboxed retry and fails closed when required isolation is unavailable.

**Unattended** is explicit normal-host maximum-authority mode. The human must select it. It uses Claude Code bypass-permissions execution and removes the package's normal operational restrictions for the active run. Repository planning/task governance, exact task envelopes, verification, correctness, security and completion gates remain active.

**Isolated Full** is maximum execution freedom inside a separately established disposable VM/container boundary. It cannot be hot-switched into on a normal workstation.

See [Safety model](docs/SAFETY.md).

## Hot profile switching

For an interactive session started with `claude-auto start`:

```text
/profile status
/profile strict
/profile balanced
/profile unattended
```

A real switch occurs at the user-prompt boundary. Claude Auto stops only the current Claude child, rebuilds the target execution policy and resumes the exact same Claude session.

For a headless run, use a separate top-level operator terminal:

```bash
claude-auto profile status --repo /path/to/repo
claude-auto profile request strict --repo /path/to/repo
claude-auto profile request balanced --repo /path/to/repo
claude-auto profile request unattended --repo /path/to/repo
```

Headless switches apply only at a safe supervisor boundary. The control path is human-only and rejects requests originating inside the active Claude Auto process tree.

## Installation

Requirements: Linux, Claude Code, Python 3 and Git.

```bash
git clone https://github.com/EnigmaThe1/Claude-Code-Autonomous-Code-Optimization.git
cd Claude-Code-Autonomous-Code-Optimization
./install.sh

claude-auto --version
claude-auto doctor
claude-auto global status
claude-auto profiles
```

The executable/control layer is installed under `~/.local/share/claude-autonomy/` with `~/.local/bin/claude-auto`. Package-owned Claude Code customisations are merged into namespaced user paths under `~/.claude/`. Unrelated user settings and hooks are preserved.

## Ordinary Claude Code versus autonomous execution

After installation, ordinary `claude` sessions receive a small universal optimisation layer. They are not forced into autonomous or bypass-permission mode.

For long-running work:

```bash
claude-auto run --objective "Implement the requested software completely and verify every acceptance criterion"
```

or:

```bash
claude-auto run --plan IMPLEMENTATION_PLAN.md
```

For an interactive supervised session:

```bash
claude-auto start --objective "Continue the approved objective"
```

## Permission escalation

Balanced and Strict can pause on a genuinely required operation without converting the whole run to Unattended.

```bash
claude-auto permissions status
claude-auto permissions approve --id REQUEST_ID --scope once
claude-auto permissions approve --id REQUEST_ID --scope run
claude-auto permissions approve --id REQUEST_ID --scope repository
claude-auto permissions deny --id REQUEST_ID --reason "..."
claude-auto permissions revoke --id REQUEST_ID
claude-auto permissions history
```

Constrained grants remain resource-bound and decisions are audited.

## Trusted Git and protected promotion

Promotion/WIP decisions use a package-owned Git view. Inherited inline `GIT_CONFIG_*` injection is removed; a trusted excludes file can be registered by the operator and is copied into private package state so later edits cannot change broker truth. Git hooks and fsmonitor are disabled for broker-owned Git operations, while ordinary authentication/transport configuration remains available.

```bash
claude-auto git-trust status --repo .
claude-auto git-trust set-excludes --repo . --path ~/.config/claude-auto/git-excludes
claude-auto git-trust clear-excludes --repo .
```

Local promotion remains available, and remote promotion adds exact expected-base checking plus lost-response reconciliation:

```bash
claude-auto promote-ff --repo . --sha TARGET_SHA
claude-auto promote-ff --repo . --sha TARGET_SHA \
  --remote origin --remote-branch main --expected-remote-sha BASE_SHA
```

When the target changes a configured canonical plan, the broker automatically requires the exact repository-planning attestation even if the caller omits an attestation option.

## Durable state

Runtime state lives outside target repositories:

```text
~/.local/share/claude-autonomy/repos/<repo-id>/
```

It includes objective/plan state, checkpoints, profile information, verification evidence, permission history and compact telemetry.

## Model routing

Native Claude is the compatibility-first default. Optional gateway/provider routes can be used for experimentation, fallback or independent challenge, but non-native routes are qualification-gated before autonomous mutation.

See [Model routing](docs/MODEL_ROUTING.md).

## Documentation

- [Quick start](QUICKSTART.md)
- [Command reference](COMMANDS.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Planning and repair](docs/PLANNING_AND_REPAIR.md)
- [Safety model](docs/SAFETY.md)
- [Model routing](docs/MODEL_ROUTING.md)
- [Plugin policy](docs/PLUGIN_POLICY.md)
- [Release process](docs/RELEASE_PROCESS.md)
- [Sources and upstream references](SOURCES.md)
- [Contributing](CONTRIBUTING.md)

## Release model

`main` represents the latest accepted and qualified public state. Each candidate is developed on `release/<version>`, qualified there, then merged or fast-forwarded into `main`. Accepted release branches remain fixed as recovery/comparison references and version tags are immutable.

The first public baseline is **1.0.0-rc1**. **1.0.0-rc2** adds trusted Git reconstruction, protected/idempotent promotion and repository-owned planning repair. **1.0.0-rc3** adds explicit Apache-2.0 licensing, release-workflow hardening and deterministic release finalisation.

## License

Licensed under the Apache License, Version 2.0.

Copyright 2026 Bogdan Carp (@EnigmaThe1).

See [LICENSE](LICENSE) and [NOTICE](NOTICE) for the licence text and attribution notice.

## Project status

This is a release candidate. Autonomous execution—especially Unattended mode—can make substantial changes to a workstation, repository or external systems. Review the safety documentation and use an isolated environment for untrusted code.
