# Contributor guidance

`claude-watchdog` is a Python 3.10+ macOS utility for observing persisted local
agent activity and controlling an owned `caffeinate` process before an optional
sleep request.

## Working agreements

- Keep the runtime standard-library-only.
- Preserve content timestamps as the activity signal; never substitute
  filesystem modification time or process liveness.
- Keep launch-time JSONL path/size, OMX identity, and OpenCode lineage-root
  freezing unless the requirements explicitly change.
- Shared OMX logs remain scoped to identities admitted at launch or during live
  discovery. OpenCode remains scoped to admitted roots and their descendants.
- Do not equate recent persisted activity with a running process in code, tests,
  documentation, or messages.
- Filters, sorting, colors, details, and tree layout are display-only.
- Never invoke real `pmset sleepnow` during automated tests, smoke checks, or
  demos.
- Do not use private transcripts, session IDs, usernames, project names, or
  absolute home-directory paths in fixtures, documentation, screenshots, or
  artifacts.
- Add a regression test before changing parsing, selection, quietness, power,
  or status behavior.

## Verification

Run:

```bash
python3 scripts/check.py
```

On macOS, also run:

```bash
python3 scripts/check.py --integration
```

Update `docs/compatibility.md` when provider parsing changes. Record the exact
provider version when known and say `unknown` when it was not captured.
