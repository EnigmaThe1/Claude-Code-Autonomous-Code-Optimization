# Claude Auto universal user rules

These are personal, repository-neutral defaults. Repository instructions and current repository evidence remain authoritative for repository-specific behaviour.

- Reconcile the current repository before acting: inspect relevant Git state, project instructions, manifests, tests, and only the documentation needed for the task.
- Treat the detected Git/project root as the engineering workspace boundary; sibling packages/modules inside that root are normal project resources even when a command executes from a nested subdirectory.
- Prefer targeted semantic/symbol lookup when available, then targeted search/ranges, then broader reads. Avoid loading large trees or long logs into context without need.
- Reuse the repository's existing frameworks, conventions, commands, and architecture. Do not invent replacement infrastructure merely for convenience.
- Resolve routine engineering choices from code, tests, docs, established patterns, and reversible experiments instead of asking the user unnecessarily.
- Treat ordinary local and reversible engineering actions as normal work: project edits, dependency/tool setup (including non-destructive missing OS development packages when reasonably necessary), local services, tests/builds, Docker when the repo uses it, disposable development data, normal task-branch commits, and bounded repair attempts do not inherently require human confirmation.
- Existing configured credentials may be used through normal authorised development tools when required by the task. Credential discovery/disclosure/export/rotation/revocation or privilege administration still requires explicit authority.
- Escalate destructive unrelated-data deletion, destructive accepted-history rewrites/force pushes, production/shared infrastructure changes, production deployment/public release, protected-branch merges not explicitly authorised, purchases/material paid-resource changes, credential/security administration, and other irreversible external effects.
- Run relevant locally executable checks for changes and never claim a command or test passed unless it was actually executed or verified from durable evidence.
- Keep responses and progress narration concise; spend context on the work itself.
- Use specialist agents only when isolation, independent verification, or focused research materially improves quality.
- If a project has its own CLAUDE.md, AGENTS.md, rules, skills, or local settings, respect them. Do not silently overwrite project configuration.
- The globally installed `claude-auto` supervisor is an operator-shell entry point for substantial autonomous work. **Never launch `claude-auto run`, `claude-auto start`, or another `claude` process from inside an active Claude Code session.** Nested Claude sessions share runtime resources and are guarded by Claude Code. If the operator wants the supervisor, tell them the exact command to run from a fresh top-level terminal; continue the current session normally unless they choose to switch.
