# Model routing and alternate-model policy

Native Claude is the compatibility-first default. Gateway routes can expose other providers or model families, but non-native routing is experimental and qualification-required.

## Provider profiles

| Profile | Intended use |
|---|---|
| `native` | Existing Claude Code auth/provider path. |
| `openrouter` | Hosted multi-provider experiment/challenger route. |
| `ccr` | Advanced routing/control plane for local or hosted providers. |
| `litellm` | General self-hosted/team gateway. |
| `custom` | Operator-supplied Anthropic Messages-compatible gateway. |

Provider secrets remain environment variables and are not persisted in durable repository state.

## Qualification

Reachability is not enough. Qualification can test read-only compatibility, Auto-mode commands, safe mutation, goal continuation, subagents and completion-review capability in a disposable repository.

```bash
claude-auto models qualify --provider openrouter --model 'provider/model'
```

Qualification proves harness compatibility, not reasoning quality or production fitness.

## Recommended pattern

For important work, a conservative pattern is a native Claude implementer plus an optional independent different-family challenger.

## Fallback

Use Claude Code model fallback for models on the same route. Use cross-provider fallback only for a genuinely failed route/process; the fallback reconciles repository state before continuing.

## Support boundary

Anthropic supports gateway/proxy deployment for Claude Code but does not guarantee non-Claude model compatibility. Third-party translation layers are therefore treated as empirical integrations.
