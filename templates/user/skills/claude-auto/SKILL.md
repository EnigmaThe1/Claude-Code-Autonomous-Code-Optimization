---
name: claude-auto
description: Start the globally installed Claude Auto supervisor when the user explicitly requests long-running autonomous repository work.
disable-model-invocation: true
---

# Claude Auto universal autonomy capability

This machine has the Claude Autonomous Optimisation Pack installed at user scope. It applies to every local repository without adding package files to the repository.

When the user wants long-running autonomous implementation, the external `claude-auto` supervisor is the preferred operator-shell entry point rather than a repository-specific loop. This skill must **not** launch it from the current Claude Code process. Claude Code guards nested sessions because they share runtime resources; provide the command for a fresh top-level terminal instead. It provides repository discovery, external checkpoints, candidate-plan validation or automatic plan generation, read-only simulation/red-team gates, native `/goal` continuation, bounded turns/cost, optional model routing, independent challenge, metrics, and restart-safe state.

Useful commands:

- `claude-auto inspect` — profile the current repository without modifying it.
- `claude-auto run --objective "..."` — autonomous bounded execution using the Balanced profile by default.
- `claude-auto run --profile strict --objective "..."` — stricter sandbox posture for unknown/untrusted repositories.
- `claude-auto run --profile isolated-full --objective "..."` — maximum autonomy only inside an explicitly attested disposable container/VM.
- `claude-auto run --plan PATH` — treat a supplied plan as a candidate, reconcile it with repository reality, simulate/red-team it, then execute the validated external working plan.
- `claude-auto status` — show compact durable state.
- `claude-auto metrics` — show token/cache/cost telemetry.
- `claude-auto models registry` — inspect recorded alternate-model qualifications.

Do not install or rewrite project `.claude` configuration merely to use this capability. Project-specific configuration remains the project's own concern.
