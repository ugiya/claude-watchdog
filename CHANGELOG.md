# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-09-07

Initial public prerelease.

### Added

- Persisted-activity guards for Claude Code, Claudex, Marjory, Codex, OMX, and
  OpenCode.
- Additive live session discovery and launch-time frozen discovery.
- macOS `caffeinate`, user-idle, and explicit sleep-request lifecycle.
- Responsive terminal dashboard with filtering, sorting, details, provider and
  model colors, tree/flat views, and a retained exit report.
- Native Claude, Codex, and OpenCode parent/child presentation.
- Optional, bounded external Claude-family/Codex lineage registry.
- Current OpenCode model selection and scoped descendant metadata.
- Regression suites, isolated lifecycle tests, PTY dashboard tests, CI, release
  archives, checksums, and a synthetic terminal demo.

### Safety

- Activity is based on persisted timestamps rather than filesystem mtimes or
  process liveness.
- Dashboard-only state cannot change the sleep guards.
- Uncertain activity, presence, terminal cleanup, or wake-assertion cleanup
  prevents a sleep request.
- Automated lifecycle tests replace `caffeinate`, `ioreg`, and `pmset` with
  isolated shims and never request real sleep.
