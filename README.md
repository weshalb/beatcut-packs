# Beatcut scene packs

This repository hosts the public store index. Creators publish their video ZIP,
cover, and previews as GitHub Release assets in repositories they own. The editor
reads the raw URL of this repository's `packs.json` and checks the ZIP's SHA-256
before installing it.

## Set up the store

1. Copy the contents of `central_repo/` into the root of a public
   `beatcut-packs` repository, including the hidden `.github` directory.
2. Enable Issues and Actions. Allow the workflow's GitHub token to write issues
   and contents, and configure the default branch to allow its direct pushes.
3. Create an issue label named `approved`. The workflow creates `valid` and
   `invalid` as it processes issues.
4. Give the editor the raw URL of `packs.json` on the default branch. Configure
   publishing with your central repository's `OWNER/beatcut-packs` name.

## Approve a pack

- The publishing tool opens a **Pack submission:** issue. The workflow validates
  the entry and release assets, then labels it **valid** and comments
  **Waiting for approval**. Invalid submissions receive an explanation and an
  **invalid** label.
- Review the submission. As the personal repository owner, apply **approved**.
  The workflow revalidates the current issue and assets, commits the entry to
  `packs.json`, and closes the issue. Approval is tied to the repository owner's
  GitHub login. Use a personal repository for this workflow.
- If a creator edits a failed submission, review the corrected issue and apply
  **approved**. If that label is already present, remove it before applying again.
- Use the **Report a pack** issue form for problems. To delist an entry, remove it
  from `packs.json` and commit the change.
