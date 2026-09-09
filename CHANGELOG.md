# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Restore dashboard tree indentation for Codex subagents recorded by OMX while
  preserving rollout-embedded parentage and discarding only tracking-added
  parents from any resulting lineage cycle.

## [0.1.1] - 2026-09-07

Maintenance prerelease; persisted-activity and sleep-guard behavior is unchanged.

### Changed

- Split the runtime into cohesive, normally importable modules while preserving
  persisted-activity guards, observational display behavior, and power cleanup.
- Group portable tests under `tests/` and macOS runners under `tests/integration/`.
- Build a deterministic, explicitly allowlisted Python ZIP for single-file
  installation; preserve ownership checks and legacy single-file upgrades.
- Exercise both source and installed commands with the isolated macOS suites,
  and analyze runtime Python modules directly with CodeQL.

## [0.1.0] - 2026-09-07

Initial public prerelease.

### Added

- Persisted-activity guards for Claude Code, configured Claude profiles, Codex,
  OMX, and OpenCode.
- Versioned generic Claude-profile configuration with per-run frozen directory
  scope and live discovery of sessions within that scope.
- Additive live session discovery and launch-time frozen discovery.
- macOS `caffeinate`, user-idle, and explicit sleep-request lifecycle.
- Responsive terminal dashboard with filtering, sorting, details, provider and
  model colors, tree/flat views, and a retained exit report.
- Native Claude, Codex, and OpenCode parent/child presentation.
- Optional, bounded external Claude/Codex lineage registry.
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
