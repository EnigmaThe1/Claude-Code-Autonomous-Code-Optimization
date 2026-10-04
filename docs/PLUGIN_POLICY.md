# Plugin policy

The core package installs its own rules, agents, skills and hooks at Claude Code user scope. Third-party plugins are optional and are not runtime dependencies.

```bash
./install.sh --with-plugins
# or
claude-auto plugins --install
```

Recommended optional candidates include `claude-code-setup`, `context7` and `serena`.

Plugins with known upstream packaging/discovery defects remain deferred rather than being installed for every session.

Language-server plugins are stack-specific opt-ins. The repository profiler can report relevant candidates, but installation remains explicit.

Because user-scope plugins may add context, hooks or MCP processes to every session, the project favours a small measured set over large overlapping collections.
