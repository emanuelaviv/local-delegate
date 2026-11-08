# Contributing

Thanks for helping. Bug reports, platform support and new tasks are all welcome.

## Ground rules

These keep the plugin safe; pull requests that break them can't be merged.

1. **Standard library only.** No third-party packages at run time. (The tests use the official MCP
   SDK when it's installed, and skip that test when it isn't.)
2. **No network beyond 127.0.0.1.** The connector talks only to Ollama on loopback.
3. **Every model run needs the user's approval,** through `local_run(..., user_approved=true)`.
   Nothing starts a model run on its own.
4. **Exact facts come from scripts,** not the model: counts, file lists, hashes, dates.
5. **No personal data** in code, tests, fixtures, issues or pasted logs. Use made-up names and
   generic examples. Build anything that looks like a real key at run time, as
   `tests/test_guard_text_verify.py` does, so secret scanners have nothing to flag.

## Getting started

```powershell
git clone https://github.com/emanuelaviv/local-delegate.git
cd local-delegate
python -m unittest discover -s tests
```

On Linux or macOS, use `python3` instead of `python`. The tests use a simulated Ollama
(`tests/fake_ollama.py`), so they need no model and take about a minute.
`python tests/scenarios.py` prints which option each preset recommends on three kinds of hardware.

To try your copy in Claude Code without installing it, run `claude --plugin-dir .` from the
repository folder. To check the plugin's files, run `claude plugin validate --strict .`.

## Where things are

| Path | What it is |
|---|---|
| `server/local_delegate/` | The connector; [docs/DESIGN.md](docs/DESIGN.md) describes each module |
| `skills/local-delegate/SKILL.md` | The instructions Claude follows |
| `tests/` | Unit and end-to-end tests with the simulated Ollama |
| `docs/` | Install, usage, privacy and design notes |
| `packs/example.md` | The example rule sheet copied to new installs |

## Adding a task

1. In `tasks.py`, write `prep_x(params, cfg, profile) -> Prepared`. Read files with `load_docs`,
   which enforces the folder rules and redaction. Put fixed instructions in the system message
   and the variable input last, so Ollama can reuse its prompt cache.
2. Optionally write `fin_x(prep, outs)` to assemble the result, and register a `Task` in `TASKS`.
3. Give it a JSON schema with citations if it extracts, and a test with the simulated Ollama.
4. Add it to the skill's table and to [docs/USAGE.md](docs/USAGE.md).

## Help wanted

- **Linux and macOS.** The plugin starts the connector with `python`, which Linux and macOS usually
  call `python3`, and some system checks are Windows-only. The tests already run on Linux.
- **Other models.** Reasoning-level mapping and calibration checks for models other than Qwen 3.8.
- **Windows CI.** A Windows job in `.github/workflows/tests.yml`; the current one runs on Ubuntu.
- **A faster second model** for the fast level (see "Extending" in [docs/DESIGN.md](docs/DESIGN.md)).

## Pull requests

Keep them focused, include tests, update the docs and [CHANGELOG.md](CHANGELOG.md), and fill in
the checklist in the pull-request template. By contributing, you agree that your contribution is
licensed under the [MIT License](LICENSE).
