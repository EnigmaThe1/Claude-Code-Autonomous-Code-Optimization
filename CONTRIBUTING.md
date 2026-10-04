# Contributing

Contributions should preserve long-running autonomous engineering, truthful completion, controlled plan repair, minimal unnecessary human interruption and explicit authority boundaries.

- work from latest accepted `main`;
- keep changes focused;
- add or update regression tests for behavioural changes;
- run the full test and static-check suite;
- do not weaken correctness/security/verification gates merely to keep a run moving;
- document current behaviour rather than private development history.

Release-candidate integration uses `release/<version>` branches. See [docs/RELEASE_PROCESS.md](docs/RELEASE_PROCESS.md).

Permission, sandbox, profile-switching, secret-access, write-boundary, provider-routing, recovery and completion-gate changes should include adversarial negative-path tests.

## Licence of contributions

This project is licensed under the Apache License, Version 2.0. Unless you explicitly state otherwise, any contribution intentionally submitted for inclusion in this project is provided under the Apache License, Version 2.0, consistent with section 5 of that licence.

Copyright and attribution notices that apply to existing project code must be preserved. Contributors may add appropriate copyright notices for their own contributions.
