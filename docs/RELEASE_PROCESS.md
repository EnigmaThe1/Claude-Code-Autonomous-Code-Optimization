# Release process

`main` is the latest accepted and qualified public state.

Each candidate is developed on `release/<version>`, for example `release/1.0.0-rc2`.

1. Create the next release branch from `main`.
2. Develop and test only on that branch.
3. Keep `VERSION` aligned with the branch suffix.
4. Run normal CI and release qualification.
5. Do not advance `main` until qualification is green.
6. Advance `main` to the exact accepted candidate.
7. Create immutable tag `v<version>`.
8. Keep the accepted release branch fixed for recovery/comparison.
9. Start the next candidate from the new `main`.

This repository starts public version history at `1.0.0-rc1`.

Generated archives and qualification outputs belong in GitHub Releases or Actions artefacts rather than binary release folders committed into source.
