# Claude Code Autonomous Code Optimization

**Version 1.0.0-rc2**

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
- optional repository-owned canonical-plan authority with dedicated repair worktrees and independent verification.

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

### Repository-owned canonical plans

Projects that keep their authoritative implementation plan inside Git can opt into stronger plan authority:

```bash
claude-auto planning-repair configure --repo . --plan IMPLEMENTATION_PLAN.md
claude-auto run --repo . --plan IMPLEMENTATION_PLAN.md
```

Once configured, that exact tracked plan is the single planning authority. Normal Balanced/Strict workers cannot edit it directly. If implementation evidence exposes a material plan defect, the supervisor automatically uses a dedicated planning worktree, a one-plan Planning Repair Architect, an independent exact-SHA Planning Verifier, protected fast-forward promotion, and then rebuilds/revalidates the executable external plan from the promoted canonical file.

If the repair requires a genuine unresolved product/semantic decision, automation stops rather than changing the objective.

See [Planning and repair](docs/PLANNING_AND_REPAIR.md).

## Operating profiles

**Balanced** is the default for normal trusted development repositories. It keeps a sandboxed execution boundary where practical, lets routine reversible engineering proceed without unnecessary prompts, and supports scoped human escalation.

**Strict** is for unknown or less-trusted repositories. It tightens sandbox/read boundaries, disables unsandboxed retry and fails closed when required isolation is unavailable.

**Unattended** is explicit normal-host maximum-authority mode. The human must select it. It uses Claude Code bypass-permissions execution and removes the package's normal operational restrictions for the active run. Planning, verification, correctness, security and completion gates remain active.

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

The first public baseline is **1.0.0-rc1**. **1.0.0-rc2** is the next candidate and adds trusted Git reconstruction, protected/idempotent promotion and repository-owned planning repair.

## Project status

This is a release candidate. Autonomous execution—especially Unattended mode—can make substantial changes to a workstation, repository or external systems. Review the safety documentation and use an isolated environment for untrusted code.
