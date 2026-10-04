# Quick start — 1.0.0-rc2

## Install

```bash
git clone https://github.com/EnigmaThe1/Claude-Code-Autonomous-Code-Optimization.git
cd Claude-Code-Autonomous-Code-Optimization
./install.sh

claude-auto --version
claude-auto doctor
claude-auto global status
claude-auto profiles
```

## Use Claude normally

```bash
cd /path/to/repository
claude
```

The user-level optimisation layer is available automatically; no per-repository package installation is required.

## Autonomous objective

```bash
claude-auto run --objective "Implement the requested application completely, run verification, repair failures and finish only when the acceptance criteria are satisfied"
```

Balanced is the default.

## Start from a plan

```bash
claude-auto run --plan IMPLEMENTATION_PLAN.md
```

A supplied plan is reconciled, simulated and red-teamed before mutation, and may later be repaired when implementation evidence requires a better route to the same objective.

If the repository's tracked plan itself is authoritative, configure it once:

```bash
claude-auto planning-repair configure --repo . --plan IMPLEMENTATION_PLAN.md
claude-auto run --repo . --plan IMPLEMENTATION_PLAN.md
```

Material defects in that plan are then repaired through a dedicated worktree, independently verified at the exact candidate SHA and promoted before implementation continues.

## Interactive supervised session

```bash
claude-auto start --objective "Complete the approved repository objective"
```

Inside that session:

```text
/profile status
/profile strict
/profile balanced
/profile unattended
```

## Headless profile switching

From a separate human terminal:

```bash
claude-auto profile status --repo /path/to/repository
claude-auto profile request balanced --repo /path/to/repository
claude-auto profile request strict --repo /path/to/repository
claude-auto profile request unattended --repo /path/to/repository
```

## Strict

```bash
claude-auto run --profile strict --objective "..."
```

## Unattended

```bash
claude-auto run --profile unattended --objective "..."
```

Unattended removes normal package execution restrictions but retains quality and completion gates.

## Isolated Full

```bash
export CLAUDE_AUTO_ISOLATED_FULL=1
export CLAUDE_AUTO_ISOLATION_ATTESTATION=container
claude-auto run --profile isolated-full --objective "..."
```

Use only inside an already disposable/isolated VM or container.

## Permission request

```bash
claude-auto permissions status
claude-auto permissions approve --id REQUEST_ID --scope once
claude-auto permissions approve --id REQUEST_ID --scope run
claude-auto permissions approve --id REQUEST_ID --scope repository
claude-auto permissions deny --id REQUEST_ID --reason "not approved"
```

## Trusted Git compatibility exclusions

If your sandbox/tooling creates compatibility files that must be ignored consistently by the worker and promotion broker, register an operator-owned excludes source. Claude Auto copies it into private package state.

```bash
claude-auto git-trust set-excludes --repo . --path ~/.config/claude-auto/git-excludes
claude-auto git-trust status --repo .
```

## Inspect/status

```bash
claude-auto inspect --repo /path/to/repository
claude-auto status --repo /path/to/repository
claude-auto metrics --repo /path/to/repository
```

## Optional resume service

```bash
claude-auto service install --repo /path/to/repository
claude-auto service status --repo /path/to/repository
```

## Optional plugins

```bash
./install.sh --with-plugins
# or
claude-auto plugins --install
```

## Uninstall

```bash
~/.local/share/claude-autonomy/uninstall.sh
```
