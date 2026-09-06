## Behavior

Describe the user-visible problem and the resulting behavior.

## Safety and compatibility

- Activity/sleep behavior changed: yes / no
- Display-only behavior changed: yes / no
- Provider schema changed: yes / no
- Compatibility documentation updated: yes / no / not applicable

Explain any effect on persisted timestamps, admission, lineage scoping,
quietness, wake-assertion cleanup, terminal restoration, or sleep requests.

## Validation

- [ ] `python3 scripts/check.py`
- [ ] Tests use synthetic data and cannot execute real `pmset sleepnow`
- [ ] Fixtures, logs, screenshots, and artifacts contain no private transcripts,
      prompts, paths, session IDs, credentials, or project names

List skipped checks and the reason:
