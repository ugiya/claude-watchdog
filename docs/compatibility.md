# Compatibility and schema evidence

Agent session formats are not stable public APIs. `claude-watchdog` therefore
treats compatibility as evidence attached to exact fields and tests. It does
not infer support from a product name or a similar directory layout.

## Release evidence

The provider versions used during the development of `v0.1.0` were not recorded
in a public, reproducible compatibility matrix. The honest version value for
each provider is therefore **unknown**. The repository contains synthetic
regression fixtures for the observed formats described below.

| Source | Provider version exercised | Evidence in `v0.1.0` | Confidence |
| --- | --- | --- | --- |
| Claude Code | Unknown | Synthetic JSONL parsing, native child discovery, prompt/title metadata, and tree tests | Experimental |
| Claudex | Unknown | Synthetic/path-scoping tests for a compatible Claude JSONL tree | Experimental |
| Marjory | Unknown | Synthetic/path-scoping tests for a compatible Claude JSONL tree | Experimental |
| Codex CLI/App | Unknown | Synthetic rollout JSONL, optional state database metadata, inherited-identity, and tree tests | Experimental |
| OMX | Unknown | Synthetic shared-log identity admission and timestamp attribution tests | Experimental |
| OpenCode | Unknown | Synthetic SQLite root/descendant, activity, current-model, and tree tests | Experimental |

CI on Linux exercises portable unit behavior. CI on macOS additionally runs
isolated lifecycle and PTY checks. This does not establish support for every
macOS release, Mac model, terminal, or agent version, and it does not test a
real sleep request.

## Observed data contracts

These are the exact layouts the current parser recognizes. Extra fields are
ignored.

### Claude Code and compatible wrappers

Discovery looks under `~/.claude/projects` for parent JSONL files and native
children at:

```text
<project>/<parent-session-id>/subagents/agent-<child-id>.jsonl
```

Claudex and Marjory resolve their own compatible data roots beside their
executables. The parser recognizes top-level JSONL fields including:

- `timestamp`, `sessionId`, `agentId`, `entrypoint`, `cwd`, and `effort`;
- `type: "ai-title"` with `aiTitle`;
- `type: "custom-title"` with `customTitle`;
- `type: "last-prompt"` with `lastPrompt` when prompt fallback is enabled;
- `message.model` for a displayed model.

Native child ancestry comes from the directory structure. A session-registry
lookup may supply an exact name when an explicit title is absent.

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
