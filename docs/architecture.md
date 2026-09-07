# Architecture and safety boundaries

`claude-watchdog` is one foreground Python process with two deliberately
separate data paths: a safety-critical activity path and an observational
metadata path.

```text
profile configuration + agent JSONL / OpenCode SQLite
             |
             v
  discover + admit watch targets -----> display metadata + lineage
             |                                  |
             v                                  v
  newest persisted timestamps             terminal dashboard
             |
             v
  session quiet gate -> macOS user-idle gate
             |
             v
  restore terminal -> release caffeinate -> final report -> pmset sleepnow
```

## Source and distribution boundaries

The root `claude-watchdog` launcher calls the internal package entrypoint.
`python3 -m claude_watchdog` is an equivalent source-checkout entrypoint.
Runtime responsibilities are separated into ordinary Python modules:

| Module | Responsibility |
| --- | --- |
| `models` | Shared records, configuration data, errors, and constants |
| `text` | Pure terminal normalization, width, and display-time formatting |
| `presentation` | Shared color settings below dashboard and reporting |
| `config` | CLI parsing and launch-frozen profile validation |
| `activity` | Discovery, persisted timestamps, admission, and scoped guards |
| `metadata` | Observational provider metadata and lineage |
| `dashboard` | Terminal rendering, navigation, and restoration |
| `reporting` | Bounded poll history and final reports |
| `power` | User presence and owned power-command operations |
| `app` | Polling and lifecycle orchestration |

Imports are acyclic. Configuration, activity, and power do not depend on metadata,
reporting, or the dashboard. Metadata never imports dashboard or reporting;
reporting never imports dashboard. Pure text/time and shared palette helpers sit
below their consumers. The application composes these paths while retaining the
cleanup order below. Structural regression tests enforce these boundaries.

Installation does not scatter these modules across the destination prefix. The
installer builds an executable ZIP from an explicit, required runtime-file list,
using fixed archive metadata and a Python shebang. Only that single executable
and its ownership manifest are installed, retaining the previous one-file upgrade
and uninstall model. Missing or symlinked runtime inputs abort before installation
is changed. The source-release builder includes the same runtime files and the
explicit test/tool allowlists; local configurations and captures are never bundled.

## Activity path

At launch, a run using source `auto` or `claude` loads the built-in Claude
projects directory and any profiles declared in
`~/.config/claude-watchdog/profiles.json`, or in the path selected with
`--profiles-file`. It validates and freezes this set of directories and labels
before starting `caffeinate`. The omitted default file means zero custom
profiles; an explicitly selected missing file or invalid configuration aborts
before the wake assertion starts. Non-Claude-only source selections do not load
the default registry and reject an explicit `--profiles-file`.

Discovery then snapshots candidate JSONL paths and sizes. A candidate is
admitted only when an attributable content timestamp falls within
`--select-window`. Filesystem modification time is never the activity signal.

With `--session-discovery live`, each poll performs another complete discovery
inside the frozen directories and additively admits recent targets. Existing
targets are not removed. JSONL activity reads current content after admission,
while the launch snapshot prevents content appended during the initial
selection scan from changing that selection retroactively. Editing the profile
file does not change a running watchdog; newly active sessions inside an
already configured directory remain discoverable.

Shared sources receive tighter scoping:

- OMX stores the exact identity set admitted from a shared JSONL file. Later
  activity must match one of those identities. Live discovery can add an
  identity that independently satisfies the admission window.
- OpenCode stores admitted session IDs as lineage roots. Recursive SQLite
  queries include those roots and their descendants, including descendants
  created later, without adopting unrelated roots.

Timestamps more than five minutes in the future are ignored. Missing individual
activity records are treated as unavailable or quiet according to their source;
discovery, database, presence, and cleanup failures that make the decision
uncertain abort the run without requesting sleep.

## Metadata path

The dashboard loads bounded labels, clients, models, effort levels, start times,
and lineage. These values do not participate in admission, quietness, or sleep
decisions. Metadata failures produce `unknown` or a group fallback.

Terminal strings are normalized, stripped of control/format characters,
collapsed to a single line, and clipped by terminal cell width. This protects
the display from embedded terminal control sequences; it does not make private
content safe to publish.

Native lineage uses exact session and parent IDs within a provider namespace.
OpenCode expands display-only child rows beneath one database activity guard.
Profile labels identify a display source but never provide model metadata. An
optional external registry can join exact visible Claude and Codex
sessions across providers. Registry relationships change presentation only.

## Quietness and power sequence

Every watched source must first reach `idle_minutes` without persisted
activity. Only then does the watchdog query `ioreg -c IOHIDSystem` and compare
`HIDIdleTime` with `--user-idle-minutes`. User activity delays the decision; a
new persisted agent event restarts the session quiet period.

The wake assertion is an owned child process:

```text
caffeinate -is -w <watchdog-pid>
```

On a successful decision, the watchdog restores the terminal, releases the
owned `caffeinate`, emits a final report, and runs `pmset sleepnow`. `--dry-run`
logs the last step without executing it. Interrupts and failures release the
wake assertion when possible and skip sleep. A failure to restore the terminal,
release `caffeinate`, or emit the final report also skips sleep.

This sequence does not override macOS lid-closed behavior and does not prove
that macOS entered sleep after accepting the command.

## Display model

The dashboard continuously repaints between authoritative polls so countdowns
remain readable. Only an authoritative poll records history. The final report
uses that bounded history to describe recent guard transitions and includes all
final watch targets even if the interactive view was filtered.

Tree/flat mode, text/provider filters, sorting, selection, details, color, and
responsive column allocation are presentation state. The watch set and
`holding_count` come from the independent activity path.

## Trust boundaries

The utility trusts local files readable by the current user as input but treats
their contents as malformed or hostile for parsing and terminal rendering.
JSONL reads, metadata sampling, registry size, registry link count, and OpenCode
metadata queries are bounded. SQLite is opened in read-only, query-only mode.

The utility has the same filesystem and command privileges as the invoking
user. It is not a daemon, privileged service, sandbox, or remote supervisor.
