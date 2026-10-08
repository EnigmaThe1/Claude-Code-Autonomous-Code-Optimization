# Architecture

## Principle

Install once at Claude Code user scope, benefit in every local repository, and keep repository-specific runtime state outside target repositories.

The system has an always-on low-risk user layer and an explicit autonomous supervisor for long-running mutating work.

## Main modules

- `claude_auto.py`: orchestration and autonomous run loop.
- `planning_support.py` / `planning_complexity.py` / `planning_completeness.py` / `control_plane.py`: independent scope analysis, complexity-aware plan generation, deterministic completeness/traceability validation, simulation and review control.
- `governance_contract.py` / `authority_set.py`: exact-Git repository authority and control-surface snapshots.
- `task_spec.py` / `task_sources.py`: deterministic repository-owned TaskSpec normalisation, graph validation and bounded adapter resolution.
- `task_authority.py` / `execution_envelope.py`: dependency-safe task activation, exact WIP-bound ExecutionEnvelopes, staged/promotion admission and violation recovery.
- `execution.py` / `process_runner.py`: isolated repository-command execution and process supervision.
- `state_store.py` / `repo_identity.py`: durable state, recovery, identity and writer leasing.
- `settings_policy.py` / `permission_escalation.py` / `profile_switch.py`: execution policy, human authority and runtime profile transitions.
- `provider_config.py` / `model_qualification.py`: routing and model capability qualification.
- `verification.py` / `review_gates.py`: deterministic verification and completion reviews.
- `environment_policy.py` / `toolchain_preflight.py`: resumable environment and dependency diagnostics.
- `workspace_recovery.py`: narrow local/remote promotion and workspace recovery broker.
- `git_trust.py`: deterministic package-owned Git execution context.
- `promotion_policy.py`: durable exact-SHA promotion-attestation policy.
- `repair_envelope.py` / `planning_repair.py`: universal one-file/multi-file Planning Repair authority, lifecycle, refresh and promotion.
- `planning_helpers.py` / `planning_validation.py`: bounded deterministic reconcilers/validators and exact candidate AuthoritySet/TaskSource validation.
- `state_migration.py` / `migration.py` / `state_adoption.py`: explicit schema migration, legacy Planning Repair migration, claims import, re-attestation and WIP adoption.
- `session_adoption.py` / `shadow_validation.py`: fresh forked Claude-session adoption and independent read-only RC4/legacy observation comparison.
- `user_layer.py`: package-owned Claude user-layer installation.
- `telemetry.py` / `protocols.py`: structured outcomes, redaction and budgets.

## Runtime layout

```text
~/.claude/
  settings.json
  rules/claude-auto/
  agents/claude-auto/
  skills/claude-auto/
  skills/profile/

~/.local/share/claude-autonomy/
  bin/lib/hooks/templates/...
  repos/<repo-id>/
  model-registry.json
```

## Autonomous flow

```text
objective or plan
  -> repository discovery
  -> independent scope + complexity baseline
  -> plan generation/reconciliation
  -> deterministic completeness + requirement traceability
  -> simulation + red team + independent Plan Verifier
  -> selected profile
  -> resolve/reuse repository TaskSpec + ExecutionEnvelope when configured
  -> implement / post-batch envelope check / verify / diagnose / repair
  -> external-plan repair, or repository-owned canonical-plan repair when configured
  -> final whole-system review
  -> deterministic verification
  -> correctness/security review
  -> COMPLETE or reopen
```

Claude Code owns model/tool execution and native turn continuation. Claude Auto owns durable objective state, planning, policy, environment continuity, verification evidence, recovery semantics and completion acceptance.

## Plan control

Scope analysis, planning, simulation, red-team and Plan Verifier roles run in fresh read-only contexts. RC5 first establishes an independent scope/complexity baseline, then requires the planner to cover every baseline requirement and pass deterministic requirement-to-task-to-verification-to-acceptance traceability. Complex plans persist both a machine-checkable graph and substantive versioned section files. External state stores versioned executable plans and evidence. The original objective and acceptance criteria remain above the plan in the authority chain, and product mutation authority is withheld until all plan gates pass.

RC4 represents one-file and multi-file planning through named AuthoritySets derived from exact committed Git objects. Legacy one-file planning remains a one-member `default` AuthoritySet. Authority members, governance and resolved control surfaces are fenced from ordinary worker mutation in every runtime profile, including Unattended.

Structured repositories may additionally declare TaskSources. Built-in JSON/JSONL/TOML/static sources are parsed from exact blobs. Custom adapters receive only declared exact-commit inputs in an ephemeral read-only/no-network sandbox, run twice for normalised determinism, and cannot expand the source's AuthoritySet ceiling. The resulting package-owned TaskSourceSet is durable P2 authority data.

P3 derives one active ExecutionEnvelope from a verified dependency-safe TaskSpec. The envelope binds exact task/source/governance/base identities, direct-edit and promotion selectors, runtime scratch selectors and accepted-dependency state. `PreToolUse` rejects deterministic path escapes and `PostToolBatch` compares actual repository state after each parallel tool batch so opaque subprocess effects cannot silently enter accepted history.

P4 separates the **coordinator authority root** from the **task execution root**. One coordinator lease/state remains authoritative while the worker runs in a package-owned linked task worktree. The primary checkout and relevant Git refs are independent semantic boundaries, so opaque commands cannot escape from the task worktree into user WIP or package refs. Candidate sealing is package-owned and uses exact Git plumbing while keeping task HEAD pinned to its base. Deterministic base/candidate verification runs in detached worktrees; a separate read-only Task Verifier binds one immutable candidate SHA; the existing promotion broker then admits only that exact attested candidate. Acceptance is crash-recoverable, persists full AcceptedTaskRecord evidence, supports verified no-op tasks and re-resolves TaskSources before the next task is selected.

P5 generalises repository-owned Planning Repair onto the same AuthoritySet model. A legacy one-file canonical plan is a synthetic one-member `default` RepairEnvelope; structured repositories may select one or more named AuthoritySets explicitly. The RepairEnvelope distinguishes repairable, immutable and generated members, binds allowed growth/helper contracts and protects planning-control surfaces independently from runtime profile authority.

The Planning Repair Architect can mutate only repairable members. Generated members are package-owned deterministic reconciler outputs. Candidate validation rebuilds the entire exact candidate AuthoritySet topology and TaskSource graph, executes declared validators, and stores integrity-bound evidence without replacing live TaskSource state. The independent Planning Verifier then reviews the exact candidate SHA against the same RepairEnvelope and helper receipts. Promotion requires an enriched exact-SHA planning attestation even when a generic fast-forward target reaches planning authority indirectly through a selector. Base refresh preserves work across interruption but invalidates prior evidence and refuses automatic reuse when planning control executables, governance/contracts or selected authority semantics changed.

P6 adds a migration/adoption layer **below current authority** rather than trusting older state. Durable schemas migrate through explicit crash-safe transactions. Legacy Planning Repair is mapped onto P5 RepairEnvelope semantics. Legacy accepted/active task state is imported as claims; only current P4 gates can turn eligible claims into accepted work, and explicit WIP adoption copies admitted bytes/modes into the task worktree without modifying the primary checkout.

External Claude conversation adoption never attaches an old process in place. Claude Auto rebuilds the current product-task or Planning-Repair boundary, launches native resume with fork semantics, records the new SessionStart identity and treats resumed conversation memory as lower authority than current Git/governance/task state.

Shadow validation is data-only: RC4 independently computes AuthoritySet, TaskSource/frontier, active/next task, acceptance, blocker, planning-repair and verification-through observations, then compares them with an operator-supplied bounded JSON observation. Snapshot is repository/package-state read-only; compare may persist only external shadow audit evidence.

See [PLANNING_AND_REPAIR.md](PLANNING_AND_REPAIR.md).

## Profiles

Balanced is compatibility-first autonomy for trusted repositories. Strict tightens containment. Unattended is explicit host authority while retaining quality gates. Isolated Full assumes an explicitly attested disposable outer VM/container.

Control/review roles remain read-only regardless of main-worker profile.

## Profile switching

Interactive switching occurs at the user-prompt boundary and resumes the exact Claude session under rebuilt policy. Headless switching uses a durable request bound to the live supervisor identity and applies only at safe supervisor boundaries.

## Git trust and promotion

Broker-owned Git operations reconstruct a deterministic inline Git policy after scrubbing inherited inline injection. The broker always supplies its own private excludes file, disables hooks/fsmonitor and recursive submodule behaviour, and preserves ordinary authentication/transport configuration.

Promotion can remain local or operate against a remote branch. Remote mode refreshes remote truth, requires an exact expected base, verifies ancestry, performs a lease-protected exact push and reconciles the remote after a transport/result failure before deciding whether the operation failed. Attestations bind protected promotion to an exact target SHA and contract.

## Durable state and evidence

Active writers share a lease. Durable state uses atomic replacement and recovery metadata. Completion evidence is repository-generation/fingerprint bound so stale PASS evidence cannot be reused after relevant changes.

## Supervisor verification boundary

Repository tests/build tools are repository-controlled code. Supervisor-owned verification therefore executes with a scrubbed environment and an isolation boundary; unsafe fallback requires explicit operator authority.

## Failure model

The supervisor distinguishes repairable implementation failure, material plan defects, recoverable checkpoints, transient provider failure, permission waits, model/provider unavailability, stagnation, genuine external blockers and verified completion.

## Process resurrection

On Linux, an optional user-level systemd unit can resume a repository from durable configuration while restoring only restricted non-secret development environment state.
