# Claude Auto command reference — 1.0.0-rc2

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

## Trusted Git policy

```bash
claude-auto git-trust status --repo /path/to/repo
claude-auto git-trust set-excludes --repo /path/to/repo --path /path/to/operator-owned-excludes
claude-auto git-trust clear-excludes --repo /path/to/repo
```

`set-excludes` copies the validated source into private Claude Auto state. Authority-changing operations are refused when invoked from inside an active Claude worker.

## Promotion policy and exact-SHA attestations

```bash
claude-auto promotion status --repo /path/to/repo
claude-auto promotion status --repo /path/to/repo --sha TARGET_SHA
claude-auto promotion require-contract --repo /path/to/repo --contract CONTRACT_ID
claude-auto promotion clear-contract --repo /path/to/repo
```

## Repository governance status

```bash
claude-auto governance status --repo /path/to/repo
```

This is read-only. It reports the exact committed AuthoritySet snapshot, protected control surfaces and snapshot digest, or a fail-closed blocker. A repository may opt into multi-file/multi-domain planning with `.claude-auto/governance.json`.

## Repository-owned TaskSpecs

```bash
claude-auto tasks status --repo /path/to/repo
claude-auto tasks resolve --repo /path/to/repo
```

`tasks status` is read-only and never executes repository adapters. `tasks resolve` reads built-in JSON/JSONL/TOML/static task sources from the exact committed Git tree. A custom adapter receives only its declared exact-commit inputs in an ephemeral read-only snapshot, with the live repository hidden, network disabled and no host fallback; the adapter must produce the same normalised TaskSpecs in two independent runs.

Successful P2 resolution persists a deterministic TaskSourceSet digest but does **not** select or activate a task. Task execution/enforcement begins in later RC4 phases.

## Repository-owned planning repair

```bash
claude-auto planning-repair configure --repo /path/to/repo --plan IMPLEMENTATION_PLAN.md
claude-auto planning-repair status --repo /path/to/repo
```

Optional remote-backed canonical planning:

```bash
claude-auto planning-repair configure \
  --repo /path/to/repo \
  --plan IMPLEMENTATION_PLAN.md \
  --remote origin \
  --remote-branch main
```

The normal autonomous path is then:

```bash
claude-auto run --repo /path/to/repo --plan IMPLEMENTATION_PLAN.md
```

When a material plan defect is detected, the supervisor automatically performs the configured repair/verify/promote/revalidate sequence. The following commands are available for operator diagnostics/recovery and are top-level-operator only when they change authority:

```bash
claude-auto planning-repair begin --repo /path/to/repo --reason "..."
claude-auto planning-repair architect --repo /path/to/repo --reason "..."
claude-auto planning-repair verify --repo /path/to/repo
claude-auto planning-repair refresh-base --repo /path/to/repo
claude-auto planning-repair promote --repo /path/to/repo
claude-auto planning-repair abort --repo /path/to/repo
```

## Narrow recovery and promotion

```bash
claude-auto cleanup-untracked --repo /path/to/repo --path relative/file --match-commit REPAIR_SHA
claude-auto promote-ff --repo /path/to/repo --sha DESCENDANT_SHA
claude-auto promote-ff --repo /path/to/repo --sha TARGET_SHA \
  --remote origin --remote-branch main --expected-remote-sha BASE_SHA
```

Protected promotion supports `--attestation-contract CONTRACT_ID`. A target that changes a configured canonical plan automatically requires the repository planning-repair contract.
