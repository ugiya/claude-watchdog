# Security policy

## Supported versions

Security fixes are provided for the latest published prerelease. Earlier
prereleases may not receive patches.

## Report a vulnerability

Use GitHub's **Report a vulnerability** feature in the repository's Security
tab to submit a private security advisory. Do not open a public issue for a
suspected vulnerability.

Include:

- the affected commit or release;
- a concise description of the impact;
- reproduction steps using synthetic data where possible;
- the macOS and Python versions;
- any proposed mitigation.

Do not include real agent transcripts, credentials, private prompts, local
session databases, or identifying filesystem paths. Replace sensitive values
with consistent placeholders so relationships remain understandable.

Maintainers will acknowledge the report through GitHub, investigate it, and
coordinate disclosure there. No response-time guarantee is offered for this
experimental prerelease.

## Security boundaries

`claude-watchdog` reads local agent state, starts `caffeinate`, queries macOS
user-idle state with `ioreg`, and can execute `pmset sleepnow`. It does not
provide sandboxing, authentication, remote access, or isolation from other
software running as the same user.

Dashboard metadata is sanitized before terminal display, and supported SQLite
sources are opened read-only. Session content remains sensitive even after
terminal sanitization. Use `--task-label metadata` to avoid Claude's last-prompt
fallback and `--no-color` when collecting a deliberately redacted text report.
