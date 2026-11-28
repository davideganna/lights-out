# Releasing

Steps for a human. The Release workflow does the rest.

1. Choose the version using [semantic versioning](https://semver.org/).
2. Set it as `version` in `pyproject.toml`.
3. In `CHANGELOG.md`, move the `Unreleased` entries under `## [x.y.z] - YYYY-MM-DD` and leave an empty
   `## [Unreleased]`.
4. Merge that change through a pull request.
5. On an up-to-date `main`, run `git tag vX.Y.Z && git push origin vX.Y.Z`.
6. Check that the Release workflow passed, that the image is listed under Packages, and that the GitHub Release
   exists. The first time only, make the package public in the GitHub package settings.

The workflow fails if the tag doesn't match the version in `pyproject.toml`. The fix is to delete the tag
(`git tag -d vX.Y.Z && git push origin :refs/tags/vX.Y.Z`), correct the version, and tag again.

The workflow runs `make check`, pushes `ghcr.io/<owner>/lights-out` for `linux/amd64` and `linux/arm64` tagged
`<version>`, `<major>.<minor>` and `latest`, and creates a GitHub Release whose notes are that version's section
of `CHANGELOG.md`.
