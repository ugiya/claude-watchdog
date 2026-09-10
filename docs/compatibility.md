# Compatibility and schema evidence

Agent session formats are not stable public APIs. `claude-watchdog` therefore
treats compatibility as evidence attached to exact fields and tests. It does
not infer support from a product name or a similar directory layout.

## Release evidence

The provider versions used during the development of `v0.1.0` were not recorded
in a public, reproducible compatibility matrix. Those version values remain
**unknown** unless later compatibility work recorded an exact version. The
repository contains synthetic regression fixtures for the observed formats
described below.

| Source | Provider version exercised | Current evidence | Confidence |
| --- | --- | --- | --- |
| Claude Code and configured profiles | 2.1.267 | Synthetic JSONL parsing, profile scoping, native child discovery, prompt/title metadata, session-registry process identity, and tree tests | Experimental |
| Codex CLI/App | Unknown | Synthetic rollout JSONL, optional state database metadata, inherited-identity, and tree tests | Experimental |
| OMX | 0.21.3 | Synthetic shared-log identity admission, timestamp attribution, and Codex subagent-tracking lineage tests | Experimental |
| OpenCode | Unknown | Synthetic SQLite root/descendant, activity, current-model, and tree tests | Experimental |

CI on Linux exercises portable unit behavior. CI on macOS additionally runs
isolated lifecycle and PTY checks. This does not establish support for every
macOS release, Mac model, terminal, or agent version, and it does not test a
real sleep request.

## Observed data contracts

These are the exact layouts the current parser recognizes. Extra fields are
ignored.

### Claude Code and configured profiles

The built-in Claude source looks under `~/.claude/projects` for parent JSONL
files and native children at:

```text
<project>/<parent-session-id>/subagents/agent-<child-id>.jsonl
```

Additional Claude-compatible projects directories can be declared in
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

The schema version must be `1` and the file can contain at most 32 profiles.
Each entry has a unique slug `id` and a `projects_dir`; `label` is optional and
defaults to `id`. Labels have a 64-terminal-cell limit and reject control
characters and line/paragraph separators. Entries allow no other keys. Roots
must be unique after canonicalization and cannot alias the built-in root.
Configured roots may be absent. The omitted default profile file means no
custom profiles; a path supplied with
`--profiles-file` must exist. Unreadable files, files larger than 64 KiB,
malformed JSON, unsupported versions, and invalid entries abort before the wake
assertion begins.

Configuration is frozen once per watchdog run. Live discovery continues to
find eligible sessions inside those frozen roots. `--source claude` and
`--source auto` include the built-in root and configured profiles. Other source
selections do not load the default registry and cannot be combined with an
explicit `--profiles-file`.

For a custom profile, the detected client has ` [<label>]` appended; an unknown
client becomes `Claude Code [<label>]`. A profile label is never interpreted as
a model. Custom profiles do not consult the built-in Claude session registry
for missing titles.

The parser recognizes top-level JSONL fields including:

- `timestamp`, `sessionId`, `agentId`, `entrypoint`, `cwd`, and `effort`;
- `type: "ai-title"` with `aiTitle`;
- `type: "custom-title"` with `customTitle`;
- `type: "last-prompt"` with `lastPrompt` when prompt fallback is enabled;
- `message.model` for a displayed model.

Native child ancestry comes from the directory structure. A session-registry
lookup may supply an exact name when an explicit title is absent.

Claude Code 2.1.267 was also observed writing one live-process registry record
per process at:

```text
~/.claude/sessions/<pid>.json
```

Process-confirmed OMX lineage recognizes a record only when `pid` is an
integer, `sessionId` exactly matches a loaded Claude row, `cwd` is absolute,
and `procStart` parses in `ps lstart` format. `procStart` is UTC even though
`/bin/ps` renders `lstart` in local time; both values are parsed with their
explicit zones and compared to the second. The observed `entrypoint` for
`claude -p` was `sdk-cli`, and its observed `kind` was `interactive`, but
neither field is used to admit or reject lineage. Registry enumeration stops
after 512 directory entries and reads at most 2 MiB across candidate files,
with no recursive search.

### Codex CLI and App

Discovery recursively scans `$CODEX_HOME/sessions`, or `~/.codex/sessions`, for
`rollout-*.jsonl`. Activity uses the top-level `timestamp` field.

The metadata parser recognizes:

- `type: "session_meta"` with a `payload` containing fields such as `id`,
  `originator`, `client`, `cwd`, `model`, `reasoning_effort`, `agent_nickname`,
  and `agent_role`;
- native parentage at
  `payload.source.subagent.thread_spawn.parent_thread_id`;
- `type: "turn_context"` with `payload.model`,
  `payload.reasoning_effort`, and `payload.cwd`;
- `custom-title`/`custom_title` metadata.

Forked rollout history may contain an inherited parent `session_meta`. The
parser keeps the first valid Codex identity as the child's own identity and
does not let a later inherited record replace it.

When present, `state_5.sqlite` can enrich display metadata from a `threads`
table. The required lookup key is `rollout_path`; recognized optional columns
are `title`, `name`, `model`, `reasoning_effort`, `created_at_ms`, `created_at`,
`source`, `thread_source`, `agent_nickname`, `agent_role`, and `cwd`. This
database does not determine activity.

### OMX

Discovery reads JSONL files directly under `~/.omx/logs`. Activity records need
a top-level `timestamp` and at least one recognized identity field:

- `session_id`
- `native_session_id`
- `thread_id`

Because logs are shared, the launch scan admits only identities with recent
content. Live discovery may add recently active identities; activity queries
remain attributable to the admitted identity set.

For Codex rollouts whose `session_meta.payload.cwd` is absolute, display lineage
may also come from:

```text
<cwd>/.omx/state/subagent-tracking.json
```

The accepted document has `schemaVersion: 1` and a `sessions` object. Each
accepted session entry has a string `session_id` matching its object key, a
non-empty string `leader_thread_id`, and a `threads` object. A rollout receives
that leader as its parent only when its exact session ID selects a thread entry
whose matching string `thread_id` has `kind: "subagent"`, and exact launch
evidence does not identify the rollout as that project's OMX launch root. Launch
evidence is an exact `native_session_id` match in `.omx/state/session.json` or an
`omx-`-prefixed `session_start_reconciled` record in the date-adjacent shared
logs. A rollout with an embedded Codex `thread_spawn.parent_thread_id` does not
use its own tracking fallback. After all visible rollouts are loaded, tracking
parents that participate in a cycle are discarded while embedded parents remain
intact. Leaders are never made their own parent. Conflicting parent declarations
yield no lineage. Lineage precedence is rollout-embedded parentage, then OMX
tracking, then the external registry, then process-confirmed lineage.

The file read is limited to 256 KiB, with one extra byte read to detect and
reject oversized documents. Documents with more than 256 session entries or a
considered session with more than 256 thread entries are ignored. Missing,
malformed, truncated, recursive, unsupported-schema, oversized, or invalid
nested data yields no lineage and does not affect activity or sleep decisions.

This fallback restores lineage only for sessions recorded by OMX's subagent
tracking machinery. Sessions launched outside that machinery do not receive
this fallback.

For a loaded Claude row whose exact live registry record names an absolute
`cwd`, display lineage may additionally consult:

```text
<cwd>/.omx/state/session.json
```

The bounded record must contain an integer `pid`, a non-empty string
`native_session_id`, and a parseable, timezone-aware `started_at`. The OMX
version that wrote the observed `session.json` record was not captured. The
file is rejected if it exceeds 64 KiB. The watchdog then runs one
`/bin/ps -axo pid=,ppid=,lstart=` command per dashboard poll, with no shell and
a one-second timeout, and confirms all of the following before emitting a
Claude-to-Codex display edge:

- the registry PID's `ps` start matches UTC `procStart` exactly to the second;
- the OMX PID's local `ps` start is within 120 seconds of UTC `started_at`;
- the OMX PID occurs within the bounded parent chain of the Claude PID.

Process ancestry is only a yes/no verifier for the two provider-written files;
it never supplies either session identity. The parent identity is the same
record's `native_session_id`, so the rendered edge may be shallower than the
actual OMX leader relationship, but it is not guessed. A confirmed link is
retained additively for the rest of the watchdog run and records the PIDs and
observation time in row details. Missing, ambiguous, oversized, malformed,
stale, recycled, over-depth, or unavailable evidence yields no edge. A parent
row must also be loaded and resolve unambiguously. This metadata remains
display-only and cannot change activity, holding, quietness, or sleep decisions.

This fallback can confirm only a Claude child observed while it is alive. A
child that exited before the watchdog saw its process registry record is not
recoverable from the local provider data currently available.

### OpenCode

The database path is `$OPENCODE_DB` when set. Otherwise it is
`$XDG_DATA_HOME/opencode/opencode.db`, with `~/.local/share` used as the XDG
default. The database is opened read-only.

Activity requires:

```text
session(id, parent_id)
message(session_id, time_updated)
part(session_id, time_updated)
```

The initial scan admits recently active session IDs as lineage roots. A
recursive query includes their descendants. Activity is the newest bounded,
sane `time_updated` value from `message` or `part` for those lineages.

Display enrichment requires these `session` columns:

```text
id, parent_id, title, time_created, time_updated
```

It optionally reads `directory`, `model`, and `agent`. A session-level `model`
JSON object may contain `id` and `variant`. If needed, recent `message.data`
objects can supply an assistant `modelID`, `variant`, and `agent`. The current
session selection wins over an older assistant message after a model switch.
Metadata reads are bounded to 32 recently updated sessions and time-limited;
failure falls back to a lineage-seed label without changing activity.

## Reporting a compatibility result

Open a provider compatibility issue and include:

- exact application name and version, or `unknown`;
- macOS and Python versions;
- source selection and display mode;
- expected and observed behavior;
- a minimal synthetic record reproducing the field layout, if possible.

Do not upload a real JSONL transcript or SQLite database. Redact prompts, paths,
session IDs, repository names, and credentials from logs and screenshots.
