# Sources and provenance

**Canonical repository:** https://github.com/EnigmaThe1/Claude-Code-Autonomous-Code-Optimization

This repository is the authoritative source for Claude Code Autonomous Code Optimization.

The pack prefers first-party Claude Code primitives over reimplementing them.

## Current Claude Code references

- CLI reference: https://code.claude.com/docs/en/cli-reference
  - `--restricted`
  - `--permission-prompts none`
  - `--no-session-persistence`
  - `--fallback-model`
  - `--exclude-dynamic-system-prompt-sections`
  - `--autocompact`
- `/goal` automation and Stop-hook continuation cap (`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`): https://code.claude.com/docs/en/goal
- Built-in commands, including the native read-only `/security-review` workflow: https://code.claude.com/docs/en/commands
- Environment variables/settings, including `CLAUDECODE` nested-session detection: https://code.claude.com/docs/en/env-vars
- LLM gateway guidance: https://code.claude.com/docs/en/llm-gateway
- Claude Code changelog: https://code.claude.com/docs/en/changelog
- Settings scopes/precedence: https://code.claude.com/docs/en/settings
- Permission modes/Auto/bypass boundaries and headless non-interactive behaviour: https://code.claude.com/docs/en/permission-modes
- Auto environment classifier configuration: https://code.claude.com/docs/en/auto-mode-config
- Bash sandbox, `permissions.blockReadsOutsideWorkingDirectories`, unsandboxed retry, `sandbox.credentials`, workspace filesystem behaviour and Docker compatibility: https://code.claude.com/docs/en/sandboxing
- User/project memory and rules: https://code.claude.com/docs/en/memory
- Personal skills: https://code.claude.com/docs/en/skills
- User-scope subagents: https://code.claude.com/docs/en/sub-agents
- Hooks/SessionStart context injection, `PermissionDenied`, and `StopFailure`: https://code.claude.com/docs/en/hooks
- Plugins and user install scope: https://code.claude.com/docs/en/plugins
- Anthropic loop engineering guidance: https://claude.com/blog/getting-started-with-loops

## Gateway/router references

- OpenRouter Claude Code integration: https://openrouter.ai/docs/guides/coding-agents/claude-code-integration
- LiteLLM Claude Code client setup: https://docs.litellm.ai/docs/proxy/client_setup/claude_code
- Claude Code Router: https://github.com/musistudio/claude-code-router
- CCR routing: https://github.com/musistudio/claude-code-router/blob/main/docs/src/content/docs/en/configuration/routing.md

### Important support boundary

Anthropic's current LLM gateway documentation supports gateway/proxy deployment for Claude Code but states that routing Claude Code to non-Claude models is not supported by Anthropic. Third-party translation can still work; it is treated as experimental by this package and requires empirical qualification.

OpenRouter's integration guidance requires `ANTHROPIC_API_KEY` to be explicitly empty when using its Claude Code route; Claude Auto applies that separation for non-native gateways.

## Plugin references

- Marketplace: https://claude.com/marketplace
- Official plugin repository: https://github.com/anthropics/claude-plugins-official
- Claude Code Setup: https://claude.com/plugins/claude-code-setup
- session-report: https://claude.com/plugins/session-report (deferred from default install while upstream packaging issue remains open)
- Context7: https://claude.com/plugins/context7
- Serena: https://claude.com/plugins/serena
- CLAUDE.md Management: https://claude.com/plugins/claude-md-management (deferred from default install while upstream skill-discovery issue remains open)
- session-report packaging issue: https://github.com/anthropics/claude-plugins-official/issues/2022
- CLAUDE.md Management skill-discovery issue: https://github.com/anthropics/claude-plugins-official/issues/1751

## LSP caveat

Official LSP plugins remain explicit opt-in compatibility candidates pending local verification. Relevant upstream reports include:

- https://github.com/anthropics/claude-plugins-official/issues/379
- https://github.com/anthropics/claude-plugins-official/issues/4492

## Future optimisation candidates deliberately not enabled by default

- `CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT=1`: potentially lower prompt overhead, but needs repository-level A/B quality measurement.
- explicit auto-compaction thresholds: useful for some workloads, but model/context-size specific.
- lower `MAX_MCP_OUTPUT_TOKENS`: can save context on noisy MCPs but can also cause extra follow-up reads.
- aggressive tool concurrency: latency/cost trade-off rather than universally better.
- automatic plugin promotion: Claude Code now provides plugin evaluation tooling; candidates should be measured rather than assumed beneficial.
