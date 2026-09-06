# Release process

This document is for repository maintainers. `v0.1.0` is the first public
prerelease.

## Prepare

1. Start from a clean release branch.
2. Confirm `CHANGELOG.md` describes the intended version and date.
3. Update `docs/compatibility.md` with exact provider versions actually tested.
   Preserve `unknown` for versions that were not captured.
4. Inspect the repository and Git history for prompts, usernames, local paths,
   session IDs, terminal captures, databases, credentials, and private project
   names.
5. Generate the synthetic demo assets:

   ```bash
   python3 scripts/demo.py --write-assets
   ```

6. Run the public-content guard:

   ```bash
   python3 scripts/check_public.py
   ```

7. Run the canonical validation:

   ```bash
   python3 scripts/check.py
   ```

## Build and inspect

Build the release archive and checksum manifest:

```bash
python3 scripts/build_release.py
```

Inspect the generated `dist/` contents. The archive must contain only public
source, tests, documentation, and release support files. It must not contain
local caches, `.git`, `.omx`, session data, databases, logs, or generated test
captures.

Verify the checksum manifest on macOS:

```bash
cd dist
shasum -a 256 -c SHA256SUMS
```

Extract the archive into a temporary directory and verify installation from the
archive:

```bash
mkdir -p /tmp/claude-watchdog-release-check
tar -xzf dist/claude-watchdog-0.1.0.tar.gz -C /tmp/claude-watchdog-release-check
cd /tmp/claude-watchdog-release-check/claude-watchdog-0.1.0
python3 scripts/install.py --prefix /tmp/claude-watchdog-prefix
/tmp/claude-watchdog-prefix/bin/claude-watchdog --help
python3 scripts/install.py --prefix /tmp/claude-watchdog-prefix --uninstall
```

Use a new, version-specific temporary directory when repeating this check.

## Publish

1. Review the final commit and required CI result.
2. Create a signed or annotated `v0.1.0` tag at that commit.
3. Push the branch and tag to `ugiya/claude-watchdog`.
4. Confirm the tag-triggered Release workflow passes its reused CI job, public
   content scan, version/tag check, and deterministic build.
5. Confirm the workflow creates a GitHub **prerelease** and attaches the
   `.tar.gz` archive and `SHA256SUMS`.
6. Download the published assets and verify the checksum again.

Do not mark the release stable until compatibility has been recorded across
named agent versions and the real-world sleep lifecycle has broader macOS
coverage.
