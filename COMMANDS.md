# Claude Auto command reference — 1.0.0-rc4

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
claude-auto start --resume-session SESSION_ID_OR_NAME --repo /path/to/repo
claude-auto start --resume-session SESSION_ID_OR_NAME --resume-role planning-architect --repo /path/to/repo
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

## Repository-owned TaskSpecs and execution envelopes

```bash
claude-auto tasks status --repo /path/to/repo
claude-auto tasks resolve --repo /path/to/repo
claude-auto tasks show TASK_ID --repo /path/to/repo
claude-auto tasks explain TASK_ID --repo /path/to/repo
claude-auto tasks activate TASK_ID --repo /path/to/repo
claude-auto tasks validate-stage --repo /path/to/repo
claude-auto tasks reconcile --repo /path/to/repo
claude-auto tasks deactivate --repo /path/to/repo

# P4 task-worktree / acceptance lifecycle
claude-auto tasks workspace-status --repo /path/to/repo
claude-auto tasks begin [TASK_ID] --repo /path/to/repo
claude-auto tasks candidate --repo /path/to/repo
claude-auto tasks verify --repo /path/to/repo
claude-auto tasks accept --repo /path/to/repo
claude-auto tasks cleanup --repo /path/to/repo
claude-auto tasks abort --repo /path/to/repo --reason "..."
```

`tasks status`, `show`, `explain`, `validate-stage` and `workspace-status` are read-only inspection/admission commands. `tasks resolve` reads built-in JSON/JSONL/TOML/static task sources from the exact committed Git tree. A custom adapter receives only its declared exact-commit inputs in an ephemeral read-only snapshot, with the live repository hidden, network disabled and no host fallback; the adapter must produce the same normalised TaskSpecs in two independent runs.

When TaskSources are configured, `claude-auto run` automatically resolves the dependency-safe READY task and executes it in a package-owned linked worktree stored under external coordinator state. The user's primary checkout remains a separately fingerprinted semantic boundary. P3's ExecutionEnvelope still binds the exact TaskSourceSet, TaskSpec digest, AuthoritySet snapshot, product base, direct-edit/promotion selectors and scratch selectors, but P4 applies that envelope to the task worktree while authority currentness remains anchored to the coordinator checkout.

Direct file/Bash writes remain envelope-gated and every tool batch is checked against both task-worktree state and coordinator semantic boundaries. Candidate creation is package-owned: the worker-controlled index is discarded, only admitted non-scratch product/evidence paths are staged, and Git plumbing seals an exact candidate snapshot without moving task-worktree HEAD away from the envelope base.

`tasks verify` first runs deterministic verification against exact base/candidate worktrees using existing verification-command authority; TaskSpec verification prose is never shell authority. Only after deterministic PASS does an independent read-only Task Verifier inspect the exact candidate SHA and issue the task-acceptance attestation. `tasks accept` can then promote only that exact verified SHA and persist an integrity-checked AcceptedTaskRecord; `tasks cleanup` retires the accepted workspace and recomputes the dependency-safe READY frontier. Verified no-op tasks are accepted at the unchanged product SHA without manufacturing an empty commit.

Authority-changing P3/P4 lifecycle commands are top-level-operator only when invoked through the CLI. The autonomous supervisor calls the same package functions while holding the coordinator lease. `tasks abort` performs bounded safe cleanup from ACTIVE workspaces and preserves admitted product work when required rather than destructively resetting the user's checkout.

## Repository-owned planning repair

Legacy one-file planning remains available:

```bash
claude-auto planning-repair configure --repo /path/to/repo --plan IMPLEMENTATION_PLAN.md
claude-auto planning-repair status --repo /path/to/repo
```

For repositories with `.claude-auto/governance.json`, P5 derives the same RepairEnvelope abstraction from one or more named AuthoritySets. If selection is ambiguous, choose the exact set(s) explicitly; repeat `--authority-set` for a deliberate multi-set repair:

```bash
claude-auto planning-repair begin --repo /path/to/repo \
  --authority-set domain-a \
  --reason "repair dependency ordering"

claude-auto planning-repair architect --repo /path/to/repo \
  --authority-set domain-a \
  --reason "repair dependency ordering"

claude-auto planning-repair reconcile --repo /path/to/repo
claude-auto planning-repair validate --repo /path/to/repo
claude-auto planning-repair verify --repo /path/to/repo
claude-auto planning-repair refresh-base --repo /path/to/repo
claude-auto planning-repair promote --repo /path/to/repo
claude-auto planning-repair abort --repo /path/to/repo
```

`begin` creates or recovers the dedicated repair worktree and binds the exact RepairEnvelope. `architect` may directly edit only repairable members; immutable members and governance/control state remain read-only. Generated members can be changed only by declared deterministic reconcilers via `reconcile`.

`validate` rebuilds the exact candidate AuthoritySets and candidate TaskSources/graph without overwriting live runtime TaskSource state, then runs declared validators. `verify` preserves the legacy one-command workflow by automatically running any missing mandatory validation gates before invoking the independent exact-SHA Planning Verifier.

The resulting enriched attestation binds the candidate SHA, RepairEnvelope, exact base, authority-content evidence and helper receipt digests. `promote`/generic protected `promote-ff` accepts only the exact attested planning candidate. `refresh-base` invalidates stale evidence and refuses automatic refresh when planning governance/control executables/verification control, selected authority contracts or TaskSource authority changed.

Planning-repair authority-changing commands are top-level-operator/package operations and are not worker-self-service commands. Runtime profiles, including Unattended, do not widen RepairEnvelope authority.

## RC4 migration, adoption and shadow validation

State schemas migrate explicitly and crash-safely before normal activation writes current semantic state. Unknown future schemas fail closed.

```bash
claude-auto migrate status --repo /path/to/repo
claude-auto migrate state --repo /path/to/repo
claude-auto migrate planning-repair --repo /path/to/repo

claude-auto migrate adopt-state --repo /path/to/repo --from legacy-state.json
claude-auto migrate reattest TASK_ID --repo /path/to/repo
claude-auto migrate adopt-active --repo /path/to/repo
claude-auto migrate adopt-wip --repo /path/to/repo
```

`adopt-state` imports a bounded legacy observation as **claims only**. It does not write P4 AcceptedTaskRecords, trust a legacy verifier, or unlock dependencies. `reattest` maps one eligible accepted-task claim into the ordinary P4 no-op candidate/verification/independent-verifier/acceptance path. `adopt-active` maps a currently valid active-task claim into the normal P4 workspace. `adopt-wip` is explicit: it copies only current owned/evidence WIP byte-for-byte into that workspace and leaves the primary checkout unchanged; stale-base, protected or out-of-envelope WIP is refused.

Existing Claude conversation context can be adopted only by launching a **fresh forked RC4-owned session** with current settings/hooks/authority:

```bash
claude-auto start --repo /path/to/repo --resume-session SESSION_ID_OR_NAME
claude-auto start --repo /path/to/repo --resume-session SESSION_ID_OR_NAME --resume-role planning-architect
```

The product role resumes inside the exact active P4 task worktree when one exists. The planning-architect role requires the exact active P5 repair worktree/RepairEnvelope. Conversation memory never overrides current TaskSpec, ExecutionEnvelope or RepairEnvelope authority.

Shadow validation is read-only with respect to repository/product/planning authority:

```bash
claude-auto shadow snapshot --repo /path/to/repo
claude-auto shadow compare --repo /path/to/repo --legacy legacy-observation.json
claude-auto shadow compare --repo /path/to/repo --legacy legacy-observation.json --dispositions reviewed.json
```

Shadow comparison independently computes RC4 AuthoritySets, TaskSources, dependency-safe frontier, active/next task, accepted-task evidence, blockers, planning-repair state and verification-through evidence. Mismatches and missing evidence remain explicit; reviewed dispositions annotate but never erase the raw result.

## Narrow recovery and promotion

```bash
claude-auto cleanup-untracked --repo /path/to/repo --path relative/file --match-commit REPAIR_SHA
claude-auto promote-ff --repo /path/to/repo --sha DESCENDANT_SHA
claude-auto promote-ff --repo /path/to/repo --sha TARGET_SHA \
  --remote origin --remote-branch main --expected-remote-sha BASE_SHA
```

Protected promotion supports `--attestation-contract CONTRACT_ID`. A target that changes a configured canonical plan automatically requires the repository planning-repair contract.
