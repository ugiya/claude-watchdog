# claude-watchdog

[![CI](https://github.com/ugiya/claude-watchdog/actions/workflows/ci.yml/badge.svg)](https://github.com/ugiya/claude-watchdog/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Keep your Mac awake while local coding-agent sessions remain active, then let
it sleep after the sessions and the user have both been quiet.

`claude-watchdog` is an experimental, standard-library-only Python utility for
Claude Code, configured Claude profiles, Codex CLI/App sessions, OMX logs, and
OpenCode. Its terminal dashboard shows the sessions responsible for the wake
assertion, including recorded parent/child relationships, current models, and
quiet-time countdowns. When the dashboard closes, it leaves a plain terminal
report explaining the final decision.

![Synthetic claude-watchdog terminal demo](docs/demo.svg)

The demo uses synthetic data. The accompanying [terminal recording](docs/demo.cast)
can be played with an asciinema-compatible player.

> [!WARNING]
> A normal run executes `pmset sleepnow` after all configured guards pass.
> Start with `--dry-run`. The watchdog reads persisted activity timestamps; it
> does not determine whether a process is alive, whether a task completed, or
> whether a model is still generating output. It cannot guarantee sleep or wake
> behavior while a Mac notebook lid is closed.

## Requirements

- macOS
- Python 3.10 or later
- Local agent session data from at least one [supported source](#supported-sources)

The runtime has no third-party Python dependencies.

## Install

Clone the repository and install the executable under your own prefix:

```bash
git clone https://github.com/ugiya/claude-watchdog.git
cd claude-watchdog
python3 scripts/install.py --prefix ~/.local
```

Ensure `~/.local/bin` is on `PATH`, then verify the command:

```bash
claude-watchdog --help
```

The installer refuses to replace a modified destination unless you explicitly
pass `--force`. To remove the installed executable:

```bash
python3 scripts/install.py --prefix ~/.local --uninstall
```

The source checkout contains a small `claude-watchdog` launcher and the internal
`claude_watchdog/` Python package. Run the launcher from the checkout, or use the
installer above. **Copying only the source launcher is not an installation.**

The installer builds a deterministic, self-contained
[Python ZIP application](https://docs.python.org/3.10/library/zipapp.html)
from an explicit list of runtime modules. The installed command is still one
executable file, requires Python 3.10+ on `PATH`, and works without the source
checkout. Its ownership manifest still protects modified or unrelated files;
an owned older single-file installation can be upgraded with the same installer.
No third-party packages, virtual environment, or `pip` installation are needed.
The internal source package is not a published pip distribution.

### Using uv

If you use [uv](https://docs.astral.sh/uv/getting-started/installation/),
it can select or download Python and run this dependency-free source directly.
After cloning the repository:

```bash
uv python install 3.14
uv run --no-project --python 3.14 python claude-watchdog --help
uv run --no-project --python 3.14 python claude-watchdog --dry-run
```

`--no-project` avoids loading an enclosing Python project's dependencies.
To install or uninstall through the same interpreter:

```bash
uv run --no-project --python 3.14 python scripts/install.py --prefix ~/.local
uv run --no-project --python 3.14 python ~/.local/bin/claude-watchdog --help
uv run --no-project --python 3.14 python scripts/install.py --prefix ~/.local --uninstall
```

The installed executable still uses `python3` from `PATH` when invoked directly.
Use the explicit `uv run ... python` form above if you rely on uv-managed Python.
There is no package to install with `uv tool install`; use the source and installer.
See [uv's script guide](https://docs.astral.sh/uv/guides/scripts/) for interpreter
selection and script execution details.

## First run

Start an agent session, then run the watchdog in dry-run mode:

```bash
claude-watchdog --dry-run
```

By default it:

1. admits sessions with persisted activity from the previous 15 minutes;
2. discovers additional recently active sessions every 60 seconds;
3. keeps the Mac awake with an owned `caffeinate` process;
4. waits until all watched sources have had no persisted activity for 30
   minutes;
5. checks that macOS reports at least five minutes of user inactivity;
6. releases `caffeinate`, restores the terminal, and prints a final report;
7. logs the sleep request without executing it because `--dry-run` is active.

After you have inspected that behavior, omit `--dry-run` to allow the final
`pmset sleepnow` request.

Useful examples:

```bash
# Watch only Codex and OMX, with a 20-minute session quiet period.
claude-watchdog 20 --source codex-omx --dry-run

# Disable the separate macOS user-idle requirement.
claude-watchdog --user-idle-minutes 0 --dry-run

# Keep the launch-time watch set fixed and use plain log output.
claude-watchdog --session-discovery frozen --display log --dry-run

# Avoid using Claude's last prompt as a fallback dashboard label.
claude-watchdog --task-label metadata --dry-run

# Dismiss a transcript from this run so it cannot block sleep.
claude-watchdog --hide /path/to/session.jsonl --dry-run
```

Run `claude-watchdog --help` for every option.

## Dashboard controls

| Key | Action |
| --- | --- |
| `j`, `k`, arrows | Move the selected row |
| `/` | Edit the text filter; `Enter` accepts and `Esc` cancels |
| `p`, `f` | Cycle the provider filter |
| `s` | Cycle recent, title, and source sorting |
| `t` | Toggle tree and flat presentation |
| `Enter` | Toggle details for the selected row |
| `h` | Dismiss the selected target from this run |
| `c` | Clear text and provider filters |
| `q`, `Ctrl-C` | Exit safely without requesting sleep |

Filters, sorting, and tree presentation change only the display. Filtered-out
rows remain in the watch set and continue to affect the sleep decision.
`h` and `--hide PATH` dismiss a target from this run: the row is removed, live
discovery will not re-admit it, and it no longer blocks sleep. Restart without
`--hide` to watch it again if its JSONL is still inside the select window.
Conversely, tree-only ancestor rows are descriptive: they never join the watch
set, increase holding counts, or delay the sleep decision.

## Supported sources

| Source | Activity data | Scope |
| --- | --- | --- |
| Claude Code and profiles | JSONL under `~/.claude/projects` and configured profile directories | Parent transcripts and native `subagents/agent-*.jsonl` children |
| Codex CLI/App | Rollout JSONL under `$CODEX_HOME/sessions` or `~/.codex/sessions` | Recursively discovered rollout files |
| OMX | JSONL under `~/.omx/logs` | Only identities admitted from shared logs |
| OpenCode | SQLite at `$OPENCODE_DB`, or the XDG OpenCode data directory | Admitted root sessions and their recursive descendants |

Provider formats are private implementation details that may change without
notice. See [compatibility and schema evidence](docs/compatibility.md) before
assuming a particular agent release is supported.

### Claude profiles

Use profiles when more than one Claude-compatible session tree exists on the
same Mac. The default configuration path is
`~/.config/claude-watchdog/profiles.json`:

```json
{
  "version": 1,
  "claude_profiles": [
    {
      "id": "work",
      "label": "Work",
      "projects_dir": "~/.work-claude/projects"
    }
  ]
}
```

The file can define at most 32 profiles. Each `id` is a unique slug and
`projects_dir` points directly to a Claude-compatible projects directory. The
optional `label` controls the terminal label and defaults to the profile `id`.
Labels are limited to 64 terminal cells and cannot contain control characters
or line/paragraph separators. Profile roots must be unique after path
resolution and cannot alias the built-in `~/.claude/projects` root. A
configured directory may be absent until the corresponding tool creates it.

Selecting `--source claude` or `--source auto` covers the built-in Claude tree
and every configured profile. To read a different configuration file, use:

```bash
claude-watchdog --profiles-file /path/to/profiles.json --dry-run
```

The omitted default file may be absent, which means there are no custom
profiles. An explicitly selected file must exist. An unreadable file, a file
larger than 64 KiB, malformed JSON, an unsupported schema version, or an invalid
profile aborts before `caffeinate` starts so an intended guard cannot be
silently lost. Profile configuration is loaded only when the selected source
is `auto` or `claude`; using `--profiles-file` with a non-Claude-only source is
a command-line error.

The watchdog freezes the validated profile configuration when it starts. With
live session discovery, it can still admit newly active sessions that appear
inside those frozen directories. Changes to the profile file take effect on
the next watchdog run.

A custom session's client label includes the profile label. The label identifies
where the session was discovered; it never supplies or infers the session's
model. Model information must come from transcript metadata and otherwise
remains `unknown`.

## What “active” means

The safety-critical signal is the newest valid timestamp stored in attributable
JSONL content or OpenCode database records. Filesystem modification time and
process presence do not drive the sleep decision. A row marked `holding` means
that its persisted activity is recent enough to prevent sleep; it does not mean
the process is running.

Live discovery is additive. Once a target is admitted, it remains watched for
the run. OpenCode keeps its launch-time lineage roots and includes newly created
descendants of those roots. OMX keeps admitted identities within shared logs.

The dashboard reads extra metadata for labels and hierarchy, but metadata
failures do not change the activity calculation. The watchdog exits without
requesting sleep when safety-critical activity or macOS presence cannot be read
reliably.

## Agent trees and external lineage

Native Claude, Codex, and OpenCode parent identifiers appear as a `pstree`-style
hierarchy. When a watched child has a known but unwatched parent and that
parent's metadata is available, tree view adds the parent as a display-only row
so the hierarchy remains connected. It does not invent rows for unavailable
metadata. Filters select watched rows first and retain any structural ancestors
they require; sorting keeps each subtree together, and `recent` ranks a
timestamp-less structural ancestor by its most recent subtree member. Tree-only
ancestors are labeled as unwatched and remain selectable, while flat view
continues to show only the filtered watched rows. Codex parsing preserves the
child's own first `session_meta` identity when forked transcripts contain an
inherited parent record. Codex sessions launched as OMX subagents can also use
exact lineage recorded in the rollout project's
`.omx/state/subagent-tracking.json`. Rollout-embedded Codex parentage remains
authoritative: if tracking fallbacks would create a cycle, only the tracking
parents in that cycle are discarded and embedded parents remain intact. Only
sessions recorded by OMX's tracking machinery receive this fallback, and a
session identified by exact OMX launch evidence as the project's launch root is
excluded even if the tracking file labels it as a subagent.

When OMX launches a Claude session without persisting a native parent ID, the
dashboard can also confirm a shallower Claude-to-Codex edge from Claude's exact
live process registry record, the project's OMX `session.json`, and matching
process ancestry. The process table verifies those provider-written identities;
it never invents an identity or uses cwd/time proximity alone. The macOS `ps`
probe preserves the watchdog environment but forces `LC_ALL=C` so localized
hosts produce the stable clock format the parser expects. Confirmation is
bounded, failure-safe, retained for the rest of the run, and display-only: it
does not add watch targets, extend the `caffeinate` hold, or affect sleep.

Shell-launched cross-provider children do not necessarily record a native
parent. An optional `~/.config/claude-watchdog/lineage.json` can declare an
exact relationship for Claude and Codex sessions:

```json
{
  "version": 1,
  "links": [
    {
      "child": {"source": "codex", "session_id": "child-session-id"},
      "parent": {"source": "claude", "session_id": "parent-session-id"},
      "evidence": "review launcher recorded both session IDs"
    }
  ]
}
```

The registry affects presentation only. Entries must resolve unambiguously
among metadata-known candidates, and only ancestry required by a watched row is
rendered; conflicts, ambiguity, unsupported sources, and malformed files are
ignored. The file is limited to 256 KiB and 256 links. Lineage precedence is
rollout-embedded parentage, then OMX tracking, then the external registry, then
process-confirmed lineage.

## Logs and privacy

The watchdog writes plain operational logs to `~/claude-watchdog.log`. Session
prompts, task titles, project paths, model names, and session identifiers can
appear in the dashboard, final report, or logs. Treat all of these as sensitive.

Before sharing a screenshot or issue report, redact:

- prompts and task titles;
- usernames and local paths;
- session and thread identifiers;
- repository names and proprietary model/provider details.

Do not attach raw agent transcripts. Metadata displayed in the terminal is
sanitized to remove control characters, but sanitization is not anonymization.

## Project status

`v0.1.2` is an experimental public prerelease. Dashboard `h` and `--hide PATH`
dismiss a watch target for the current run only; recency is still not process
liveness. The repository has regression
coverage for parsers, watch-set scoping, power-command ordering, dashboard
rendering, and isolated lifecycle behavior. That coverage does not establish
compatibility with every agent release or prove real-world sleep behavior on
every Mac.

Changes are reviewed through pull requests. Each release gets a new immutable
version tag, matching runtime version, and updated changelog and README.
Maintainers review open code-scanning alerts separately from successful CI and
CodeQL runs; a passing scan job is not a finding-free verdict. See the
[release checklist](docs/releasing.md) for the publication gates.

- [Compatibility evidence](docs/compatibility.md)
- [Architecture and safety boundaries](docs/architecture.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)

Agent-assisted engineering uses GitHub Issues, the triage vocabulary, and
domain-documentation conventions under [`docs/agents/`](docs/agents/); see the
[Agent skills section in AGENTS.md](AGENTS.md#agent-skills).

## License

MIT. See [LICENSE](LICENSE).
