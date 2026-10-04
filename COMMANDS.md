# Claude Auto command reference — 1.0.0-rc1

## Installation and health

```bash
./install.sh
./install.sh --with-plugins
claude-auto --version
claude-auto doctor
claude-auto global status
claude-auto profiles
```

## Repository inspection

```bash
claude-auto inspect --repo /path/to/repo
claude-auto status --repo /path/to/repo
claude-auto metrics --repo /path/to/repo
claude-auto setup --repo /path/to/repo
```

## Autonomous run

```bash
claude-auto run --objective "..."
claude-auto run --plan IMPLEMENTATION_PLAN.md
claude-auto run --profile balanced --objective "..."
claude-auto run --profile strict --objective "..."
claude-auto run --profile unattended --objective "..."
claude-auto run --resume-config --repo /path/to/repo
```

## Interactive supervised Claude

```bash
claude-auto start --objective "..."
claude-auto start --profile strict --objective "..."
```

```text
/profile status
/profile strict
/profile balanced
/profile unattended
```

## Headless profile control

```bash
claude-auto profile status --repo /path/to/repo
claude-auto profile request strict --repo /path/to/repo
claude-auto profile request balanced --repo /path/to/repo
claude-auto profile request unattended --repo /path/to/repo
```

## Permission escalation

```bash
claude-auto permissions status
claude-auto permissions approve --id REQUEST_ID --scope once
claude-auto permissions approve --id REQUEST_ID --scope run
claude-auto permissions approve --id REQUEST_ID --scope repository
claude-auto permissions deny --id REQUEST_ID --reason "..."
claude-auto permissions revoke --id REQUEST_ID
claude-auto permissions revoke --capability CAPABILITY
claude-auto permissions history
```

## Model qualification

```bash
claude-auto models qualify --provider native --model MODEL
claude-auto models qualify --provider openrouter --model MODEL
```

## Plugins

```bash
claude-auto plugins --install
claude-auto plugins --install --include-deferred
claude-auto plugins --install --include-lsp
```

## Resume service

```bash
claude-auto service install --repo /path/to/repo
claude-auto service status --repo /path/to/repo
claude-auto service start --repo /path/to/repo
claude-auto service stop --repo /path/to/repo
```

## Narrow recovery

```bash
claude-auto cleanup-untracked --repo /path/to/repo --path relative/file --match-commit REPAIR_SHA
claude-auto promote-ff --repo /path/to/repo --sha DESCENDANT_SHA
```
