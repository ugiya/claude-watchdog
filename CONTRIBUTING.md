# Contributing

Thank you for helping make `claude-watchdog` safer and more compatible.

## Before opening an issue

Search existing issues first. For provider compatibility reports, include the
exact agent application and version when available, macOS version, Python
version, watchdog command, display mode, and whether `--dry-run` was enabled.

Never post raw transcripts. Redact prompts, task titles, usernames, local paths,
session identifiers, repository names, access tokens, and proprietary provider
details from logs and screenshots.

Use GitHub's private vulnerability reporting for security issues. See
[SECURITY.md](SECURITY.md).

## Development setup

Fork and clone the repository. The project requires Python 3.10 or later and
has no third-party runtime or test dependencies.

Run the canonical validation command from the repository root:

```bash
python3 scripts/check.py
```

The default check runs the portable unit, syntax, indentation, command-help,
and installer validations, including detached execution of the installed ZIP. On macOS, also run the isolated lifecycle and PTY
dashboard checks:

```bash
python3 scripts/check.py --integration
```

With uv, the equivalent commands select Python explicitly without installing
an enclosing project or its dependencies:

```bash
uv run --no-project --python 3.14 python scripts/check.py
uv run --no-project --python 3.14 python scripts/check.py --integration
uv run --no-project --python 3.14 python scripts/demo.py --no-delay
```

The integration command runs the same lifecycle and terminal suites against both
the source launcher and a ZIP installed into a temporary prefix. Evidence is
written under ignored `.omx/artifacts/dashboard-pty/source/` and `installed/`;
never publish raw captures. The integration command requires macOS. uv downloads the requested interpreter
if needed; no third-party Python packages are required.

The integration checks use isolated command shims and synthetic session data;
automated tests must never invoke a real `pmset sleepnow`.

To inspect a synthetic dashboard without reading your agent sessions:

```bash
python3 scripts/demo.py
```

## Repository layout

- `claude-watchdog`: thin source-checkout entrypoint.
- `claude_watchdog/`: internal runtime modules, imported normally by tests.
- `tests/test_*.py`: portable unit and artifact regression tests.
- `tests/integration/`: isolated macOS lifecycle and terminal runners; not part
  of portable unit discovery.
- `scripts/`: maintainer commands for checking, installing, releases, demos,
  and benchmarks, plus the shared runtime-bundle helper.
- `docs/`: architecture, compatibility, release guidance, and synthetic demo assets.

Run unit tests directly with `python3 -m unittest discover -s tests -p 'test_*.py'`.
Patch the module where a dependency is looked up; do not recreate the former
monolithic import surface as a test facade. New runtime modules must be included
in the explicit bundle manifest and source release, and checked for import cycles.
The canonical checker compiles and indentation-checks runtime, test, and script
Python files. Keep publication allowlists explicit rather than recursively
including arbitrary local files.

## Change guidelines

- Keep the runtime compatible with the Python standard library.
- Preserve persisted content timestamps as the activity signal. Do not replace
  them with filesystem modification times or process presence.
- Keep dashboard metadata, filtering, sorting, and hierarchy observational.
  They must not add or remove sleep guards.
- Preserve launch-time JSONL path/size snapshots, OMX identity scoping, and
  OpenCode lineage-root scoping unless a reviewed requirement changes them.
- Add a regression test before changing parsing, session selection, quietness,
  power commands, or status behavior.
- Use synthetic, anonymized fixtures. Do not commit local session transcripts,
  database copies, terminal captures, home-directory paths, or session IDs.
- Do not add a dependency without explaining why the standard library cannot
  meet the requirement.

## Pull requests

Keep changes focused and explain the user-visible behavior, safety implications,
and validation performed. `python3 scripts/check.py` must pass before review.
macOS contributors should also run `python3 scripts/check.py --integration`.
If a check is not available on your platform, say which check was skipped and
why.

Changes to provider parsing should update [docs/compatibility.md](docs/compatibility.md)
with the schema evidence and tested provider version. Write “unknown” when the
version was not recorded; do not infer a version from file layout alone.

By contributing, you agree that your contribution is licensed under the MIT
License in this repository.
