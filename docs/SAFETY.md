# Safety model

Claude Auto separates **autonomy** from **authority**.

## Installation

The installer writes package-owned namespaced Claude Code customisations and a user-level executable/control layer while preserving unrelated settings, rules, agents, skills and hooks.

Ordinary `claude` sessions do not automatically become unattended.

## Balanced

Balanced is the default for trusted development repositories. It retains sandboxing where practical, establishes the repository workspace, protects high-confidence credential locations and supports reviewed unsandboxed retry for legitimate commands that cannot run inside the sandbox.

## Strict

Strict blocks reads outside working directories, disables unsandboxed retry, strengthens secret-read restrictions and fails closed when the required sandbox is unavailable.

## Unattended

Unattended is explicit maximum host-authority mode for the current run. It uses Claude Code bypass-permissions execution and disables the package's normal main-worker operational restrictions.

It must be selected by the human. It does not disable planning, deterministic verification, correctness/security review or circuit breakers.

## Isolated Full

Isolated Full assumes a disposable VM/container is already the outer security boundary and requires explicit isolation attestation.

## Human-only profile switching

Interactive profile control is bound to the user-prompt event and is non-model-invokable. The headless path requires an operator TTY, rejects callers inside the active supervisor process tree and binds requests to the live supervisor identity.

## Permission escalation

Balanced and Strict can create durable permission requests with capability/resource, rationale, risk, safer alternative and objective context. Approvals can be once-only, run-scoped or repository-scoped. Constrained capabilities remain resource-bound and decisions are audited.

## Repository-controlled code

Tests, builders, linters and scanners may be hostile or broken. Supervisor-owned execution therefore uses a scrubbed environment and isolation boundary rather than blindly inheriting host credentials.

## Credentials and containers

Existing credentials may be used indirectly through normal authenticated tools when required by the task. Credential disclosure/export/rotation or privilege changes remain separate authority decisions in normal profiles.

A token-aware container guard rejects dangerous host-control forms while allowing ordinary repository container workflows through the normal policy path.

## Read-only control roles

Planning, simulation, red-team, correctness, security and model-qualification roles are more restricted than the mutating worker and use repository mutation evidence checks.

## Git trust and recovery

Cleanup and promotion helpers are narrow and prove locality, Git ancestry, content identity and work-in-progress preservation before mutation.

Broker-owned Git operations do not trust inherited process-scoped `GIT_CONFIG_*` values for WIP/promotion semantics. Claude Auto rebuilds a private Git view with a package-owned excludes file, hooks disabled, fsmonitor disabled and recursive submodule behaviour disabled. An operator may register an external excludes source, but its contents are copied into private package state so later source-file edits cannot silently change broker truth.

Remote promotion requires an exact expected remote base and verifies the final remote SHA. A lost client response after a successful server-side push is reconciled against durable remote truth rather than replayed blindly.

## Planning and repository authority

Repository semantic authority is independent from runtime profile authority. When repository-owned planning is configured, its AuthoritySet members are protected from ordinary worker writes in **Balanced, Strict, Isolated Full and Unattended**. Unattended may widen host/runtime permission authority, but it does not grant a worker permission to rewrite planning authority or governance/control surfaces.

RC4 repositories may declare `.claude-auto/governance.json`. The v1 contract is read from the exact committed Git tree, resolves one or more named AuthoritySets and records a deterministic package-owned snapshot. A legacy RC3 `planning-repair` policy is normalised into the same one-member `default` AuthoritySet representation when no repository contract exists.

Authority and control-surface working-tree divergence fails closed. Symlink authority members and ordinary traversal through gitlinks/submodules are rejected. The read-only `claude-auto governance status --repo ...` command explains the currently resolved snapshot or blocker.

RC4-P2 can additionally resolve repository-owned structured TaskSpecs from exact committed JSON, JSONL, TOML, static declarations or bounded custom adapters. Task-source files and adapter inputs are control surfaces. Uncommitted divergence fails closed rather than changing task authority.

Custom adapters do not run against the live checkout. Claude Auto materialises only declared exact-commit blobs into an ephemeral view, hides the real repository, requires a read-only/no-network sandbox with no host fallback, and runs the adapter twice. Only identical normalised output is accepted. Adapter declarations may request capabilities, but P2 grants no network or external-read capability.

P2 TaskSpec resolution by itself is **not task execution authority**. P3 adds the separate package-owned activation boundary: a current dependency-safe TaskSpec is bound into an ExecutionEnvelope before a mutating worker runs. Direct file writes are limited to owned/evidence paths; Bash writes are rejected pre-execution when their target can be determined; and an authoritative `PostToolBatch` check compares actual repository state after each tool batch so opaque subprocess writes are detected before the next model turn.

Runtime scratch remains distinct from promotable source. Scratch may be used by builds/tests when declared, but candidate/promotion admission rejects it. Protected planning/governance/control paths override broad task selectors.

P4 moves mutating repository-task work into a package-owned linked worktree while keeping one coordinator state/lease. The user's primary checkout—including pre-existing WIP—is a protected semantic boundary rather than worker input. Every task batch is checked against task-worktree state, the primary baseline and relevant Git-ref bindings. Unexpected primary drift or ref mutation blocks/marks the workspace rather than deleting, stashing or rebasing user work.

Candidate creation is package-owned: worker-controlled staging is discarded, only admitted product/evidence paths are staged, runtime scratch is excluded, and the exact candidate commit is anchored under package refs without moving task-worktree HEAD. TaskSpec `verification` strings are acceptance claims only and are never executed as shell commands. Executable verification comes from the existing verification contract/discovery/operator-authority mechanisms and runs against detached exact-SHA base/candidate worktrees. A separate read-only Task Verifier must return a matching task ID and candidate SHA before a task-acceptance attestation is minted.

Only the exact independently VERIFIED candidate may be promoted. Promotion and AcceptedTaskRecord persistence are crash-recoverable and idempotent; verified no-op tasks can be accepted at the unchanged product SHA without fake commits. Rejected candidates reopen the same task for repair, while stale-base, blocked or unsafe cleanup states preserve candidate/workspace evidence. Safe abort may remove disposable scratch-only work or preserve admitted product changes under package-owned recovery refs rather than destructively cleaning unknown changes.

RC4-P5 applies the same semantic/runtime separation to **Planning Repair**. Legacy one-file repair and multi-file planning use one RepairEnvelope model. The package/operator selects the AuthoritySet domain; the model cannot self-select another domain or widen the envelope. Repairable planning members may be directly edited by the dedicated Architect. Immutable requirements/contracts, governance and executable control state are read-only. Generated projections are writable only by explicitly declared deterministic reconcilers; the Architect cannot fake them.

Declared reconcilers and validators run through bounded repository-command execution with declared inputs/outputs, no implicit network/external-read capability and deterministic repeat checks where required. Candidate validation reconstructs exact candidate AuthoritySets and TaskSources rather than trusting the live coordinator state. Task-ledger cycles, required-member deletion, case/Unicode collisions, unsafe symlinks/gitlinks and out-of-envelope growth fail closed.

P1 deliberately uses a broad control-surface set for normal product-worker protection, including TaskSource/helper data inputs. P5 uses a narrower immutable **planning-control/executable** digest for repair validation so a selected repairable ledger may legitimately change without allowing governance, helper executable code or verification-control bytes to change silently.

The independent Planning Verifier is bound to the exact candidate SHA and RepairEnvelope. Promotion attestations additionally bind the exact product base, authority-content evidence and helper receipt digests. Generic `promote-ff` cannot bypass the planning gate for an existing or selector-matching planning member. Refresh-base invalidates prior verification and refuses automatic reuse if protected planning-control bytes or contract shape changed. These rules remain active in Unattended.

These semantic task controls remain active in **Balanced, Strict, Isolated Full and Unattended**. Runtime profiles may change how an operation executes, but they do not widen the TaskSpec, disable the ExecutionEnvelope, bypass coordinator boundaries or grant authority to package acceptance refs/state.

Authority-changing `git-trust`, promotion-policy, planning-repair and P4 task-lifecycle CLI actions refuse invocation from inside an active Claude worker. Planning-repair candidates and repository-task candidates are independently verified at their exact SHA under their respective versioned attestation contracts before protected promotion.

Unattended remains explicit unrestricted **host** authority; for untrusted repositories use an outer disposable VM/container.

## Untrusted repositories

For genuinely untrusted code, use a disposable VM/container. A policy layer on the same host is not a substitute for outer isolation.
