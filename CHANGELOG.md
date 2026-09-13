# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- The Linux keep-awake hold passed `--no-ask-password` to `systemd-inhibit`,
  which does not accept it. On systemd 255 (Ubuntu 24.04 LTS, Pop!_OS 24.04)
  the inhibitor exited immediately and every run aborted with "keep-awake hold
  is no longer active", so the machine was never held awake.
- The Linux keep-awake hold ran `sleep infinity` and relied on `terminate()`.
  An unclean watchdog exit left the inhibitor running and the machine awake
  indefinitely. The hold now blocks on a pipe owned by the watchdog, so the
  kernel releases it even on `SIGKILL` — the guarantee `caffeinate -w <pid>`
  already gives macOS.
- `IdleHint=no` was read as "the user is present". wlroots desktops (COSMIC,
  sway, Hyprland) report idle only over the Wayland `ext-idle-notify-v1`
  protocol and leave the hint at `no` forever, so the watch held the machine
  awake indefinitely while logging that the user was active. Unmaintained
  hints are now reported as no answer.

### Added

- GNOME (`Mutter.IdleMonitor`), KDE/Xfce/Cinnamon
  (`org.freedesktop.ScreenSaver`), and `xprintidle` idle adapters, tried in
  that order ahead of logind, so Linux human-idle detection works on the
  desktops where logind's hint is not maintained.
- The isolated lifecycle and PTY suites run on Linux, parameterised by platform
  rather than duplicated. They skip cleanly on hosts with no usable logind.
- `PresenceCheckError` for an unavailable idle source now names
  `--user-idle-minutes 0`.

## [0.1.3] - 2026-09-13

Prerelease. Sleep guards still use persisted activity timestamps, not process
liveness. Keep-awake, human idle, and suspend now go through one power session
that selects private adapters.

### Added

- A platform-neutral `power` session. macOS keeps `caffeinate`, HID idle, and
  `pmset sleepnow`. systemd Linux uses `systemd-inhibit` and `systemctl
  --no-ask-password --check-inhibitors=yes suspend` without sudo. Human-idle
  detection on Linux uses session idle hints when available; otherwise that
  gate is unavailable unless `--user-idle-minutes 0`.

## [0.1.2] - 2026-09-12

Prerelease. Sleep guards still use persisted activity timestamps, not process
liveness. A killed session can keep holding until its JSONL ages out; `h` and
`--hide` are an explicit this-run dismiss, not a liveness detector.

### Added

- Dismiss a watch target for the current run with dashboard `h` or repeatable
  `--hide PATH`. The row leaves the watch set, live discovery will not re-admit
  it, and it no longer blocks sleep. `c` does not undo hide. Restart without
  `--hide` to watch it again if the file is still inside the 15-minute select
  window.

### Fixed

- Restore a display-only Claude child edge when OMX did not persist one by
  confirming Claude and OMX provider records against bounded live process
  ancestry, then retaining the confirmed relationship for the watchdog run.
- Keep dashboard trees connected through metadata-known quiet ancestors by
  rendering them as selectable, explicitly unwatched display-only rows. Recent
  sorting ranks those trees by their newest member without changing watch,
  holding, or sleep decisions.
- Ignore inverted OMX subagent-tracking claims for a project's launch root,
  including when the corresponding leader is outside the dashboard watch window.
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
