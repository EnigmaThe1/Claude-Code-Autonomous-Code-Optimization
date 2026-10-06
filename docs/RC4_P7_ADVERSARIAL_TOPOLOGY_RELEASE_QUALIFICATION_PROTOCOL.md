# RC4 P7 — Adversarial, Topology, Stress and Release Qualification Protocol

Status: **FROZEN FOR QUALIFICATION**

Date: 2026-10-06

Branch: `release/1.0.0-rc4`

Base accepted public release: `1.0.0-rc3` at `d75f7c48dfe53d1a1759ee7828d9a42f5f744569`

P6 formal closure: `bb79a2fe5bdfbd82622699e87e671803d7915f3f`

P6 closure qualification:

- 567/567 tests passed;
- 567/567 tests passed again under coverage;
- aggregate measured coverage: 66%;
- critical regression groups: 15/15, 19/19, 25/25, 19/19 and 18/18 passed;
- branch/version identity, public-baseline audit and package static checks passed;
- `main` remained unchanged at RC3.

## 1. Purpose

P7 is the final RC4 qualification phase.

P7 is **not a feature-expansion phase**. It may add tests, fixtures, qualification tooling, documentation and narrowly scoped defect repairs that are required to satisfy already-frozen RC4 requirements. It must not broaden RC4 architecture merely because a new feature would be convenient.

P7 proves that the combined P1-P6 system is release-ready across:

- the original 64-scenario RC4 qualification matrix;
- repository topology/pathological Git cases;
- large neutral stress fixtures;
- hostile governance/adapters/validators/reconcilers/workers;
- interruption/restart/recovery boundaries;
- migration/adoption/shadow paths;
- source-tree and extracted-release execution;
- transactional install/upgrade/rollback/uninstall;
- release-manifest/licence/NOTICE/executable metadata;
- final independent correctness and security review.

No RC4 promotion to `main` and no `v1.0.0-rc4` tag may occur until every P7 release gate is green on one exact finalised candidate SHA.

## 2. Qualification principle

P7 treats P1-P6 as frozen implementation authority.

A P7 failure must be classified as one of:

- `TEST_GAP` — frozen behavior exists but lacks required qualification;
- `DOCUMENTATION_GAP` — behavior is correct but release/user documentation is incomplete or contradictory;
- `RELEASE_PIPELINE_GAP` — source behavior is correct but packaging/install/finalisation cannot prove the same behavior;
- `LOW_MEDIUM_DEFECT` — bounded repair that does not alter frozen architecture;
- `HIGH_CRITICAL_DEFECT` — release blocker; P7 remains open until repaired and all affected qualification reruns;
- `OUT_OF_SCOPE_FEATURE` — not required by frozen RC4; do not implement in P7.

Every repair invalidates downstream qualification evidence that depends on the repaired code and must rerun those gates.

## 3. Exact release identity

The candidate branch is:

```text
release/1.0.0-rc4
```

and `VERSION` must remain exactly:

```text
1.0.0-rc4
```

`main` remains pinned to accepted RC3 until the final exact RC4 SHA has:

1. finalised manifest/qualification trigger;
2. green normal CI;
3. green release-candidate qualification;
4. green P7 evidence;
5. final independent correctness/security review with no unresolved HIGH/CRITICAL finding.

The release tag remains absent until after exact-SHA promotion to `main` and green post-promotion `main` CI.

## 4. P7 slices

P7 proceeds in independently attributable slices.

### P7-A — Scenario-matrix traceability and missing-case qualification

Build a machine-readable/inspectable traceability map from each original RC4 scenario 1-64 to one or more concrete tests/evidence.

For every scenario:

- map to existing test(s), or
- add a neutral synthetic test, or
- record why the scenario is release-pipeline/manual evidence rather than unit/integration test.

No scenario may be marked covered by prose alone.

Exit: all 64 scenarios have direct executable or release evidence, with no unexplained gap.

### P7-B — Large/topology/stress qualification

Execute neutral fixtures for at least:

- 1,000-task multi-ledger graph;
- 100,000-path selector/membership/resolution stress;
- multiple named AuthoritySets/domains;
- cross-domain task;
- linked worktrees;
- submodule/gitlink boundaries;
- sparse checkout;
- shallow/missing ancestry;
- detached HEAD;
- multiple remotes;
- Unicode/spaces/leading-dash paths;
- large WIP sets;
- restart/recovery around package worktrees, planning repair and migrations.

Stress gates should assert deterministic correctness and bounded execution without brittle hardware-specific microbenchmarks.

Exit: no correctness failure, unbounded memory/path explosion, or pathological timeout in supported Linux CI.

### P7-C — Security red-team

Exercise hostile cases including:

- worker self-expands task ownership;
- worker edits governance/verification/Claude/Git control surfaces;
- arbitrary Bash/formatter/generator writes outside envelope;
- package candidate ref manipulation;
- planning Architect immutable/generated/unselected edits;
- malicious custom adapter;
- adapter timeout/invalid JSON/oversized output/nondeterminism;
- reconciler undeclared output/network/external-read request;
- validator host escape/input mutation;
- repository Git hooks;
- filters;
- textconv;
- fsmonitor;
- inherited `GIT_CONFIG_*` injection;
- symlink/path traversal;
- stale/corrupt exact-SHA attestations;
- lost-response promotion retry;
- Unattended profile semantic-governance enforcement;
- corrupt external semantic records.

Exit: no known route from repository/model-controlled input to unauthorised host/product/planning/promotion authority.

### P7-D — Source, archive and installer qualification

Exercise the actual release pipeline behavior:

- source manifest currentness;
- source tests/static checks;
- deterministic archive build;
- archive executable modes;
- LICENSE and NOTICE inclusion;
- extracted archive manifest check;
- extracted archive full tests;
- fresh transactional install/uninstall;
- RC3 -> RC4 upgrade with preserved external state;
- failed RC4 upgrade rollback to prior known-good install;
- uninstall with state preservation;
- purge-state uninstall;
- user-layer merge/removal without clobbering unrelated user configuration;
- unsafe install/uninstall destination refusal.

Exit: source and extracted release are behaviorally equivalent for supported release gates, and upgrade failure cannot destroy the previous usable installation or durable state.

### P7-E — Complex shadow and migration field qualification

Run read-only RC4 shadow validation against at least one complex neutral multi-ledger repository containing:

- multiple AuthoritySets;
- multiple task ledgers;
- cross-ledger dependency;
- generated projection;
- active/accepted/blocker evidence;
- non-trivial owned/evidence paths.

Compare RC4 computed:

- authority;
- TaskSourceSet;
- dependency-safe frontier;
- active/next task;
- owned/evidence paths;

against an independently produced neutral legacy observation.

Require either semantic MATCH or explicit reviewed disposition for every mismatch.

Also exercise supported old-state migration/adoption against the same fixture without product mutation.

Exit: complex adoption/shadow behavior is independently auditable and read-only until explicit adoption.

### P7-F — Final release/security review and exact release qualification

Before finalisation:

- review the full RC4 diff against RC3;
- review all P1-P6 closure records;
- review P7 traceability;
- inspect release workflow/installer/manifests/licence/NOTICE;
- run independent correctness review;
- run independent security review.

No unresolved HIGH/CRITICAL finding is allowed.

Then use the existing guarded release flow only:

1. create/update `.release-finalize` with `version=1.0.0-rc4` and a monotonic sequence;
2. allow finaliser to rebuild `MANIFEST.sha256`;
3. finaliser commits only `MANIFEST.sha256` and `.release-qualify`;
4. finaliser dispatches normal CI and release-candidate qualification;
5. require both green on the exact finalised SHA;
6. only then advance `main` to that exact SHA;
7. require green `main` CI at the exact same SHA;
8. only then dispatch the existing accepted-release tag workflow for `v1.0.0-rc4`.

P7 does not bypass or manually approximate these release controls.

## 5. Original RC4 64-scenario matrix

P7 must provide direct evidence for every original scenario:

1. one-file Markdown plan, no task ownership;
2. one-file structured plan with task ownership;
3. multi-file planning authority with two task ledgers;
4. manifest + ledgers + traceability;
5. generated planning projection;
6. immutable requirement file plus repairable ledger;
7. cross-ledger dependency;
8. duplicate task IDs across ledgers;
9. missing dependency;
10. dependency cycle;
11. task with no owned paths;
12. task owning one file;
13. task owning nested directories;
14. task owning paths with spaces/Unicode;
15. overlapping task ownership used sequentially;
16. attempted direct edit outside active task;
17. arbitrary Bash command modifies out-of-envelope tracked file;
18. formatter touches unrelated tracked file;
19. build creates allowed scratch files;
20. build creates unexpected untracked files;
21. attempted edit of planning authority by product worker;
22. attempted governance-contract edit;
23. mechanical ownership defect repaired through planning repair;
24. semantic ownership expansion rejected for operator decision;
25. multi-file repair changes only permitted repairable members;
26. architect attempts immutable-member edit;
27. architect attempts generated-member direct edit;
28. generator modifies undeclared path;
29. stale exact-SHA planning attestation;
30. product branch advances during repair;
31. lost-response remote promotion retry;
32. real pre-existing WIP outside task;
33. overlapping pre-existing WIP;
34. linked worktree;
35. monorepo cross-package task;
36. submodule mutation attempt;
37. sparse checkout missing authority file;
38. shallow clone missing ancestry;
39. missing LFS authority content;
40. detached HEAD;
41. non-Git directory graceful limitation;
42. multiple remotes;
43. symlink/path traversal attack;
44. malicious custom adapter output;
45. adapter timeout/invalid JSON/oversized output;
46. repository validator tries host escape;
47. corrupted external state recovery;
48. stale supervisor;
49. active RC3 single-file policy upgrade;
50. active/inactive planning-repair state upgrade;
51. exact named/session-ID resume under new RC4 policy;
52. imported legacy task state reconciles or fails closed;
53. 1,000-task multi-ledger stress fixture;
54. 100,000-path matcher/resolution stress fixture;
55. Unattended profile still enforces task/planning governance;
56. multiple named authority domains in one monorepo;
57. task spanning two declared authority domains;
58. unrelated authority domain cannot be modified by scoped repair;
59. task attempts to weaken governance/verification contract before commit;
60. task changes Claude/Git control-surface files and forces revalidation;
61. out-of-band descendant commit changes task authority;
62. adapter requests undeclared network/credential capability;
63. ignored/untracked scratch cannot be promoted with `git add -f`;
64. repository Git hooks/filter/textconv cannot alter broker truth.

## 6. Interruption matrix

P7 must verify restart/idempotency around at least:

- before/after task workspace PREPARING;
- after task branch creation;
- after task worktree creation;
- after ExecutionEnvelope persistence;
- after candidate record before candidate ref;
- after candidate ref before verification;
- during deterministic verification;
- after verifier attestation;
- during task promotion;
- after task product promotion before AcceptedTaskRecord;
- during accepted-task cleanup;
- before planning-repair worktree creation;
- during multi-file Architect changes;
- during reconciler generation;
- after planning candidate commit;
- after planning verifier attestation;
- during planning base refresh;
- during planning promotion;
- after planning promotion before task-source rebuild;
- during state-schema migration PREPARING/APPLYING/VERIFYING;
- during session adoption/fork evidence capture;
- during release finalisation.

Each transition must either be idempotent or enter a typed recovery/blocking state that preserves valuable work.

## 7. Performance/stress rules

P7 stress tests must use neutral generated data.

Minimum scales:

- 1,000 normalised tasks distributed across multiple ledgers;
- 100,000 repository paths/selectors for membership/path-boundary stress;
- at least 10 named AuthoritySets/domains;
- at least 100 cross-ledger dependencies;
- large shadow observation sufficient to exercise output bounding.

Do not add hard microsecond/millisecond gates tied to GitHub runner speed.

Use generous bounded test timeouts and assert:

- deterministic digest/result equality across repeated runs;
- no recursion blow-up;
- bounded output/evidence size;
- no accidental quadratic behavior that causes CI-scale timeout;
- stable memory-friendly streaming/path processing where applicable.

## 8. Release archive invariants

The final archive must:

- be built solely from `MANIFEST.sha256` package entries plus the manifest itself;
- use deterministic timestamps/order/compression policy;
- preserve executable mode for `bin/claude-auto`, `install.sh`, `uninstall.sh`;
- include `LICENSE` and `NOTICE`;
- contain no `.git`, package state, caches, secrets or generated local credentials;
- pass `sha256sum -c MANIFEST.sha256` after extraction;
- pass the same package static/full regression suite from the extracted tree.

## 9. Installer upgrade/rollback invariants

RC3 -> RC4 qualification must prove:

- existing RC3 install marker is accepted;
- external repo state remains present;
- trusted Git state remains present;
- planning policy/state remains migratable;
- unrelated Claude user settings/rules/hooks survive;
- RC4 package files replace RC3 package files transactionally;
- RC4 smoke succeeds before old package removal becomes final;
- forced RC4 smoke/install failure restores the previous exact usable package;
- launcher symlink remains correct after success and rollback;
- rollback does not roll back or delete durable user/repository state;
- uninstall without `--purge-state` preserves durable state;
- `--purge-state` removes package-owned state only after valid marker/path checks.

## 10. Security review checklist

Final security review must explicitly examine:

- authority-data parsing;
- symlink/path canonicalisation;
- shell parsing/pre-write guard;
- PostToolBatch fail-closed semantics;
- package state integrity digests;
- Git ref protection;
- trusted Git reconstruction;
- worktree cleanup path containment;
- adapter/reconciler/validator isolation;
- verification command authority;
- independent verifier exact-SHA binding;
- promotion attestations;
- remote lease/lost-response reconciliation;
- migration/adoption trust downgrade prevention;
- session-memory vs current authority precedence;
- shadow-mode read-only guarantee;
- release workflow token/write scopes;
- installer/uninstaller path ownership checks.

## 11. Defect-repair rule

P7 may repair a defect only when the repair:

- is necessary to satisfy an already-frozen RC4 requirement;
- does not introduce a new architecture or public capability;
- has a focused regression test;
- reruns all affected scenario/release gates.

A repair that would materially change P1-P6 semantics must reopen the relevant phase protocol rather than being hidden inside P7.

## 12. P7 evidence record

P7 will maintain a durable evidence document containing:

- exact qualified SHA for every slice;
- test counts and coverage;
- 64-scenario traceability map;
- stress fixture sizes/results;
- security-red-team findings/dispositions;
- archive hash and extracted qualification result;
- installer upgrade/rollback evidence;
- complex shadow result;
- independent correctness/security review findings;
- finalisation sequence;
- final exact release SHA;
- CI/release-qualification run IDs;
- post-promotion `main` CI run ID;
- tag creation evidence.

Evidence must distinguish:

- pre-finalisation development SHA;
- finalised release SHA;
- promoted `main` SHA;
- immutable tag SHA.

Those values must agree where the release process requires exact identity.

## 13. Exit criterion

P7 is complete only when:

- all 64 original RC4 scenarios have direct evidence;
- interruption/recovery qualification is green;
- neutral large stress fixtures pass;
- no known HIGH/CRITICAL security/correctness finding remains;
- source-tree full CI is green;
- final committed manifest is current;
- extracted archive passes manifest/static/full regression;
- RC3 -> RC4 upgrade preserves durable state;
- forced upgrade failure rolls back to a usable RC3 install without state loss;
- complex multi-ledger shadow qualification is MATCH or every mismatch has explicit reviewed disposition;
- final independent correctness review is release-accepting;
- final independent security review is release-accepting;
- existing finaliser produces one exact finalised SHA;
- normal CI is green on that exact SHA;
- release-candidate qualification is green on that exact SHA.

Only after those gates may P7 advance `main` to the exact finalised RC4 SHA.

P7 itself is not formally closed until:

- post-promotion `main` CI is green at the same SHA;
- the existing accepted-release tagging workflow creates `v1.0.0-rc4` at that SHA;
- the accepted release branch remains fixed.

