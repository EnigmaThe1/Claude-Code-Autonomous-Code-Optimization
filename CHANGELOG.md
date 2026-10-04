# Changelog

## 1.0.0-rc2

Second public release candidate.

- deterministic trusted Git reconstruction after inherited inline Git-config sanitisation;
- package-owned immutable compatibility excludes, with hooks/fsmonitor/submodule recursion disabled for broker operations;
- exact-SHA durable promotion attestations and configurable promotion contracts;
- local promotion hardening plus remote expected-base, lease-protected, idempotent promotion/reconciliation;
- automatic attestation enforcement for promotions that change a configured canonical plan;
- repository-owned canonical-plan authority with normal-worker write protection;
- dedicated one-plan Planning Repair Architect worktree/branch;
- independent read-only Planning Verifier bound to the exact candidate SHA;
- interrupted planning-repair and advancing-base reconciliation;
- automatic supervisor integration for material plan-impact, phase and final-review revalidation paths;
- semantic/product ambiguities fail closed instead of being converted into implementation-plan scope;
- package-owned planning commits independent of user Git identity, signing and hooks;
- authority-changing Git/planning policy commands reject invocation from inside an active Claude worker;
- version-generic release-candidate qualification and corrected public-history audit.

## 1.0.0-rc1

Initial public release candidate.

- user-level, repository-agnostic Claude Code optimisation;
- durable objective- or plan-driven autonomous supervision;
- validated/versioned planning with simulation, red-team and controlled plan repair;
- implementation-error diagnosis and bounded recovery;
- deterministic verification and independent correctness/security completion gates;
- Balanced, Strict, Unattended and Isolated Full profiles;
- human-controlled hot profile switching;
- durable resource-scoped permission escalation;
- provider/model qualification and fallback;
- external durable state, environment continuity and optional Linux user service;
- isolated supervisor verification of repository-controlled commands;
- narrow safe workspace cleanup and fast-forward promotion helpers;
- optional user-scope optimisation plugins;
- source/archive/installer release qualification.
