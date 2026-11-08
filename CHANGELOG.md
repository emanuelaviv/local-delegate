# Changelog

## 0.4.0 (2026-11-07): ready for Claude's plugin directory

- `local_admin` is now marked as able to make destructive changes: it can change Ollama's settings,
  restart Ollama and cancel jobs. Claude's apps may ask you before using it.
- README: a privacy policy section, example prompts, and fictional sample files to try it with
  (`examples/`).
- Privacy page: sharing with third parties (none) and how to reach the author.
- The skill names where the connector runs: Claude Code or a Cowork session on your Windows
  computer, not chat on the web or in the mobile apps.
- Python 3.11 or newer, since 3.9 and 3.10 are past end of life. Tests run on 3.11 to 3.14.
- Three tests no longer depend on the time of day. They failed in the hour and a half before the
  night window closed, or just after it opened.

## 0.3.0 (2026-10-24): first public release

- Instructions and tool texts written for any user, with neutral examples and test fixtures.
- A plugin marketplace file, so the repository installs directly in the Claude app and Claude Code.
- Documentation: an install guide with a check after every step and troubleshooting, a usage
  guide, a privacy page, a security policy and a contributing guide.
- An example rule sheet, `packs/example.md`.
- Tests run on Python 3.9 to 3.14, with continuous integration on GitHub Actions.

## 0.2.1 (October 2026)

- Fixed the check of whether the background worker outlives the app. On 64-bit Windows it always
  reported "unknown", because undeclared ctypes signatures cut the process handle to 32 bits.
- The worker now always starts outside the app's job object, with fallbacks.
- `worker_check` starts the worker without a job or the model and reports whether it outlives
  the app.
- After an update, a worker left running by the older version hands its queue to the new version.

## 0.2.0 (October 2026)

- Every run is offered as options: Claude now, or the local model at fast, careful or deep
  reasoning, now or tonight, each with ready time, tokens saved and redo risk.
- Presets (rush, balanced, saver, night), set for a while, as a default, or by time of day.
- A background worker with a file queue: scheduled and idle starts, overnight runs, deadlines that
  keep finished parts, and keep-awake on mains power.
- Explicit reasoning levels with hard budgets, loop detection and a direct-answer fallback; deep
  runs get a fix-up pass driven by script checks.
- Redo risk learned from feedback.

## 0.1.0 (October 2026)

- First version: local-only checks, calibration, estimates with the user's approval before every
  run, and 13 tasks with script-computed facts and checked citations.
