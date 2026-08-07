# Release Process

1. Create a focused release branch from an up-to-date `main`.
2. Update the package version, changelog, and customer documentation.
3. Run lint, unit tests, documentation-link validation, and a tracked-file secret scan.
4. Open a pull request and wait for required checks and review.
5. Merge the pull request into `main`.
6. Pull the merged commit locally and create an annotated semantic-version tag.
7. Push the tag and publish a GitHub release whose notes match the changelog.
8. Verify the release points to the expected commit and that `main` remains clean.

Never tag an unreviewed branch or rewrite a published release tag. If a release is defective, fix it through a new patch release.
