# Issue tracker: GitHub

Issues and PRDs live in this repository's GitHub Issues.
Use the `gh` CLI, resolving the repository from the Git remote.

## Operations

- Create: `gh issue create --title "..." --body-file <file>`
- Read: `gh issue view <number> --comments`
- List: `gh issue list --state open --json number,title,body,labels`
- Comment: `gh issue comment <number> --body-file <file>`
- Label: `gh issue edit <number> --add-label "..."`
- Close: `gh issue close <number> --reason completed --comment "..."`

When a skill says "publish to the issue tracker", create a GitHub issue.
When it says "fetch the relevant ticket", read that issue and its comments.

## Work records

Write issues around independently verifiable outcomes, with acceptance
criteria and relevant dependencies. Link implementation PRs and verification
evidence.

For retrospective issues, explicitly state that they were created after
implementation. Mark only verified acceptance criteria complete, link the
merged PRs, and close as completed. Do not imply the issue existed before
the work or manufacture a historical timeline.

Use the vocabulary in `docs/agents/triage-labels.md` for incoming work.
Do not mark already completed retrospective issues as ready for implementation.

## Pull requests as a triage surface

PRs as a request surface: no.

Implementation changes still go through pull requests as required by
AGENTS.md.

## Sensitive information

Follow SECURITY.md for suspected vulnerabilities; report them privately.
Never put private configuration, transcripts, credentials, or identifying
local paths into public issues.
