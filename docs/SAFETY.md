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

RC4-P1 deliberately does not execute multi-file/multi-domain planning authority yet. Such authority can be inspected and protected, but autonomous execution is blocked until the later TaskSource and multi-file repair phases provide a matching execution model.

Authority-changing `git-trust`, promotion-policy and planning-repair CLI actions continue to refuse invocation from inside an active Claude worker. Legacy one-file repair candidates remain independently verified at their exact SHA before promotion. Bound multi-requirement attestations for the general RC4 promotion model are introduced in a later RC4 phase.

Unattended remains explicit unrestricted **host** authority; for untrusted repositories use an outer disposable VM/container.

## Untrusted repositories

For genuinely untrusted code, use a disposable VM/container. A policy layer on the same host is not a substitute for outer isolation.
