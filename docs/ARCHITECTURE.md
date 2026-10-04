# Architecture

## Principle

Install once at Claude Code user scope, benefit in every local repository, and keep repository-specific runtime state outside target repositories.

The system has an always-on low-risk user layer and an explicit autonomous supervisor for long-running mutating work.

## Main modules

- `claude_auto.py`: orchestration and autonomous run loop.
- `planning_support.py` / `control_plane.py`: planning, simulation and review control.
- `execution.py` / `process_runner.py`: isolated repository-command execution and process supervision.
- `state_store.py` / `repo_identity.py`: durable state, recovery, identity and writer leasing.
- `settings_policy.py` / `permission_escalation.py` / `profile_switch.py`: execution policy, human authority and runtime profile transitions.
- `provider_config.py` / `model_qualification.py`: routing and model capability qualification.
- `verification.py` / `review_gates.py`: deterministic verification and completion reviews.
- `environment_policy.py` / `toolchain_preflight.py`: resumable environment and dependency diagnostics.
- `workspace_recovery.py`: narrow cleanup/promotion recovery helpers.
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
  -> plan generation/reconciliation
  -> simulation + red team
  -> selected profile
  -> implement / verify / diagnose / repair
  -> plan repair when materially necessary
  -> final whole-system review
  -> deterministic verification
  -> correctness/security review
  -> COMPLETE or reopen
```

Claude Code owns model/tool execution and native turn continuation. Claude Auto owns durable objective state, planning, policy, environment continuity, verification evidence, recovery semantics and completion acceptance.

## Plan control

Planning, simulation and red-team roles run in fresh read-only contexts. External state stores versioned plans and evidence. The original objective and acceptance criteria remain above the plan in the authority chain. See [PLANNING_AND_REPAIR.md](PLANNING_AND_REPAIR.md).

## Profiles

Balanced is compatibility-first autonomy for trusted repositories. Strict tightens containment. Unattended is explicit host authority while retaining quality gates. Isolated Full assumes an explicitly attested disposable outer VM/container.

Control/review roles remain read-only regardless of main-worker profile.

## Profile switching

Interactive switching occurs at the user-prompt boundary and resumes the exact Claude session under rebuilt policy. Headless switching uses a durable request bound to the live supervisor identity and applies only at safe supervisor boundaries.

## Durable state and evidence

Active writers share a lease. Durable state uses atomic replacement and recovery metadata. Completion evidence is repository-generation/fingerprint bound so stale PASS evidence cannot be reused after relevant changes.

## Supervisor verification boundary

Repository tests/build tools are repository-controlled code. Supervisor-owned verification therefore executes with a scrubbed environment and an isolation boundary; unsafe fallback requires explicit operator authority.

## Failure model

The supervisor distinguishes repairable implementation failure, material plan defects, recoverable checkpoints, transient provider failure, permission waits, model/provider unavailability, stagnation, genuine external blockers and verified completion.

## Process resurrection

On Linux, an optional user-level systemd unit can resume a repository from durable configuration while restoring only restricted non-secret development environment state.
