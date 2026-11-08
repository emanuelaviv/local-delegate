# Privacy: what stays local, what reaches Claude

This is Local Delegate's privacy policy. Last updated: 7 November 2026.

**In short:** the author collects nothing. There is no account, telemetry, analytics or crash
reporting, and nothing is sent anywhere except to Ollama on your own computer and, as tool results,
to your Claude conversation.

The local model's work happens on your computer. What a tool returns to Claude becomes part of your
conversation, like anything else you share with Claude. This page lists exactly what goes where.

## What stays on your computer

- **The model's work.** Prompts go only to Ollama at `127.0.0.1`. Before every run the connector
  checks that Ollama listens on loopback only, that its cloud features are off (proven by Ollama's
  own log line `Ollama cloud disabled: true`), and that the model is downloaded rather than a
  `:cloud` model. If any check fails, nothing runs.
- **The files you point at.** They are read on your computer. Only the parts a job's result quotes
  or summarizes come back to Claude.
- **File-only results.** With *"keep this on my computer"*, the result is written to
  `%LOCALAPPDATA%\local-delegate\outbox` and Claude sees only its path. Translations work this way
  by default.

## What reaches Claude

Whatever the tools return, which includes:

- **Estimates:** file paths, their sizes in characters, the task, and the options with times and
  token figures.
- **Results:** the digest, checklist, summary, draft or answer, with the quotes it cites, plus the
  script-computed facts (counts, file lists, dates, hashes).
- **Status:** your computer's memory, graphics card name, Ollama version, calibration figures, and
  the local-only check results.
- **Errors**, which can include file paths.

For private material, use file-only mode, and don't ask Claude to open the result unless you want
it in the conversation.

## What the connector never reads

- **Outside the allowed folders.** By default the allowed folder is your user folder; add more with
  *"allow the folder ..."* (Claude asks you first).
- **Secret stores, wherever they are**, including `.ssh`, `.gnupg`, `.aws`, `.azure`, `.kube`,
  `.docker`, `.ollama`, the app folders of Claude, Cursor and Codex, Windows credential folders,
  browser profiles, `.git`, `.env` files, key and certificate files (`*.pem`, `*.key`, `*.p12`,
  `*.pfx`, `*.jks`, `*.keystore`), `id_rsa`/`id_ed25519`, password databases (`*.kdbx`), and files
  named like `credentials*`, `secrets*` or `service-account*.json`. The full list is `deny_globs`
  in `server/local_delegate/config.py`.
- **Known key formats are blanked** before any text reaches the model: private-key blocks, API
  keys in the formats used by OpenRouter, Anthropic, OpenAI, AWS, Google, GitHub, Slack and
  Supabase, JSON Web Tokens, bearer tokens, and values assigned to names such as `password`,
  `api_key` or `token`.

Blanking is a safety net, not a guarantee: don't point the connector at secrets.

## What it stores, and for how long

Everything lives in `%LOCALAPPDATA%\local-delegate`:

| Item | Contents | Kept |
|---|---|---|
| `config.json`, `profile.json` | Settings and calibration figures | Until you delete them |
| `packs\` | Your rule sheets | Until you delete them |
| `jobs\` | Each job's settings, progress and result. Text you typed into a job (notes, pasted text) is removed when the job completes; a job that stopped early keeps it, so it can continue. | 14 days |
| `outbox\` | File-only results | 14 days |
| `log.jsonl` | One line of metadata per job: task, sizes, times, outcome. Never text. | Until you delete it |
| `server.log`, `worker.log` | Events and errors | Until you delete them |

Change the 14 days with the `retention_days` setting.

## What it runs on your computer

- **The connector**, a Python program that Claude starts and talks to over standard input and
  output. It opens no network port.
- **A background worker**, a second Python program that runs the queued jobs. It's started through
  a short-lived launcher so that it keeps running if the app closes. If Windows won't let it start
  that way, it's started through Windows' own process service (WMI, via PowerShell's
  `Invoke-CimMethod`). It exits a few minutes after the queue is empty.
- **Read-only system queries** for the checks and estimates: PowerShell's `Get-NetTCPConnection`
  and `Get-Process` (what listens on Ollama's port), `Get-CimInstance` (the graphics card's name),
  `nvidia-smi`, `netstat`, `tasklist`, and registry reads (the processor's name and your user
  environment variables). PowerShell runs these as inline commands with `-ExecutionPolicy Bypass`.
- **`git diff` and `git status`**, which only read, for the diff and file-list tasks.
- **`pdftotext`**, if you have it installed, to read PDFs.

## What it changes on your computer

- **Only during setup, and only after you approve the list Claude shows you:** Ollama's
  `server.json` (an existing file is backed up first), the user environment variables
  `OLLAMA_NO_CLOUD` and `OLLAMA_MAX_LOADED_MODELS`, `OLLAMA_HOST` if it exposed Ollama to the
  network, `OLLAMA_CONTEXT_LENGTH` if you ask for a default context size, and a restart of Ollama.
- **While jobs are pending, on mains power only:** it asks Windows not to go to sleep. The request
  ends when the queue is empty.

[INSTALL.md](INSTALL.md#uninstalling) shows how to remove the plugin, delete its data and undo the
Ollama settings.

## Network

The only network connection is HTTP to Ollama on `127.0.0.1`. Any other address is refused, proxy
settings are ignored, and there is no telemetry, no update check and no download.

## Sharing with third parties

Nothing is sent to the author or to any other service. Prompts go only to Ollama on your computer,
which you install and control; setup turns its cloud features off. What a tool returns becomes part
of your Claude conversation, which Anthropic handles under its own privacy policy.

## Contact

Questions about this policy: open an issue at
[github.com/emanuelaviv/local-delegate/issues](https://github.com/emanuelaviv/local-delegate/issues). Security or
privacy problems: report them privately, as [SECURITY.md](../SECURITY.md) describes.

Changes to this policy are listed in [CHANGELOG.md](../CHANGELOG.md).
