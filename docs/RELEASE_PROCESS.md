# Release process

`main` is the latest accepted and qualified public state.

Each candidate is developed on `release/<version>`, for example `release/1.0.0-rc3`.

1. Prepare the candidate identity (`VERSION`) and release-workflow changes before creating the branch so the first branch SHA is internally consistent.
2. Create the next release branch from the latest accepted `main` ancestry.
3. Develop and test only on that branch. Normal CI runs on every candidate push and regenerates the package manifest only inside the CI workspace when development changes make the committed manifest stale.
4. Keep `VERSION` aligned with the branch suffix throughout development.
5. When the candidate is ready, create or update `.release-finalize` with `version=<version>` and an incremented `sequence=<n>`.
6. The guarded finalisation workflow validates the branch/version identity, rebuilds `MANIFEST.sha256`, records the exact source SHA in `.release-qualify`, and lease-protected pushes only those finalisation files. Because GitHub suppresses recursive push workflows created by `GITHUB_TOKEN`, the finaliser then explicitly dispatches normal CI and release qualification on the final branch head.
7. Release qualification requires `.release-qualify` to attest its immediate parent, requires a current committed manifest, and runs complete source/archive/installer qualification.
8. Do not advance `main` unless that exact finalised candidate SHA has a green release qualification.
9. Advance `main` to the exact accepted candidate, then require normal `main` CI to pass at that same SHA.
10. Create immutable tag `v<version>` only for the accepted SHA.
11. Keep accepted release branches fixed for recovery/comparison, and start the next candidate from the new `main`.

This repository starts public version history at `1.0.0-rc1`.

Generated archives and qualification outputs belong in GitHub Releases or Actions artefacts rather than binary release folders committed into source.
