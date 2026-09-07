# Release process

This document is for repository maintainers. `v0.1.0` is the first public
prerelease. Its published tag is immutable; these instructions apply to a new
version, not to rebuilding or moving an existing release.

## Prepare

1. Start from a clean release branch.
2. Confirm `CHANGELOG.md` describes the intended version and date.
3. Update `VERSION` and the literal version in `claude_watchdog/__init__.py`
   together. The runtime bundle and source-release builder reject mismatches.
   Update the README's project status and any changed installation or usage
   instructions for the new release.
4. Update `docs/compatibility.md` with exact provider versions actually tested.
   Preserve `unknown` for versions that were not captured.
5. Inspect the repository and Git history for prompts, usernames, local paths,
   session IDs, terminal captures, databases, credentials, and private project
   names.
6. Generate the synthetic demo assets:

   ```bash
   python3 scripts/demo.py --write-assets
   ```

7. Run the public-content guard:

   ```bash
   python3 scripts/check_public.py
   ```

8. Run the canonical validation:

   ```bash
   python3 scripts/check.py
   ```

## Build and inspect

Build the release archive and checksum manifest:

```bash
python3 scripts/build_release.py
```

Maintainers using uv can run the same standard-library tools with an explicit
interpreter, from the repository root:

```bash
uv run --no-project --python 3.14 python scripts/check_public.py
uv run --no-project --python 3.14 python scripts/check.py
uv run --no-project --python 3.14 python scripts/build_release.py
```

Inspect the generated `dist/` contents. The archive must contain only public
source, tests, documentation, and release support files. It must not contain
local caches, `.git`, `.omx`, session data, databases, logs, or generated test
captures. The source archive includes the thin launcher, all explicitly required
`claude_watchdog/` modules, the runtime-bundle helper, and tests under `tests/`.
Missing runtime files must fail the build rather than producing an incomplete
archive. The installer builds the single executable ZIP from that source archive;
no generated runtime binary is committed to the repository.

Verify the checksum manifest on macOS:

```bash
cd dist
shasum -a 256 -c SHA256SUMS
```

Extract the archive into a temporary directory and verify installation from the
archive:

```bash
version=$(cat VERSION)
work=$(mktemp -d)
tar -xzf "dist/claude-watchdog-$version.tar.gz" -C "$work"
cd "$work/claude-watchdog-$version"
python3 scripts/install.py --prefix "$work/prefix"
"$work/prefix/bin/claude-watchdog" --help
"$work/prefix/bin/claude-watchdog" --version
python3 scripts/install.py --prefix "$work/prefix" --uninstall
```

Return to the source checkout afterward and remove only the temporary directory
you created. Use a fresh temporary directory when repeating this check.

## Publish

1. Merge the release pull request only after reviewing the final diff and
   required CI and CodeQL results. Inspect open code-scanning alerts separately
   as described below, including the final merged commit before tagging.
2. Create a signed or annotated tag for the **new version** at that commit.
   Never move `v0.1.0` or any other published tag.
3. Push the branch and tag to `ugiya/claude-watchdog`.
4. Confirm the tag-triggered Release workflow passes its reused CI job, public
   content scan, version/tag check, and deterministic build.
5. Confirm the workflow creates a GitHub **prerelease** and attaches the
   `.tar.gz` archive and `SHA256SUMS`.
6. Download the published assets and verify the checksum again.

Do not mark the release stable until compatibility has been recorded across
named agent versions and the real-world sleep lifecycle has broader macOS
coverage.

## Code-scanning review

A successful CodeQL workflow means the analysis completed; it does not mean
there are no open alerts. Before tagging, check the Security tab's code-scanning
results for `main` and the release candidate. With the GitHub CLI:

```bash
gh api 'repos/{owner}/{repo}/code-scanning/alerts?state=open&ref=refs/heads/main' \
  --paginate --jq '.[] | {number, rule: .rule.id, location: .most_recent_instance.location, html_url}'
gh api 'repos/{owner}/{repo}/code-scanning/analyses?ref=refs/heads/main' \
  --jq '.[] | {commit_sha, tool: .tool.name, error, created_at}'
```

Also inspect the release PR's code-scanning results. Confirm that the latest
successful analyses cover the intended final commit; do not treat an empty
alert list, failed API request, missing analysis, or stale scan as a clean bill
of health. Do not publish while actionable findings remain unresolved.

For a suspected false positive, trace the exact value to the reported sink and
check the relevant test or runtime path. Record the evidence and disposition
on that alert. Synthetic credential-shaped fixtures may intentionally exercise
the publication guard; their test location alone is not proof of safety.
Renaming a file can produce new alert instances even when an earlier instance
was dismissed, so revalidate the new instances rather than assuming the old
disposition carried over. Keep queries and regression coverage enabled.
