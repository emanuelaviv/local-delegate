# Installing Local Delegate

This guide takes you from nothing to a working setup on a Windows 11 computer, with a check after
every step. Plan for 30 to 60 minutes; most of it is the model download.

**Before you install:** a plugin's connector runs a program on your computer. Read what this one
runs, reads and sends in [the README](../README.md#what-it-runs-reads-and-sends) and in
[PRIVACY.md](PRIVACY.md). The code is plain Python you can read.

## What you need

| | Needed |
|---|---|
| Windows | Windows 11 (Windows 10 is untested) |
| Memory | 32 GB for the default model, which takes about 18 GB while it runs. With 16 GB, use a smaller model (step 3). |
| Disk | About 20 GB free for the default model |
| Claude | Claude Code, or the Claude desktop app with Cowork, on this same computer |

A terminal is any PowerShell or Command Prompt window: press **Win**, type `PowerShell`, press
**Enter**.

## Step 1: Python

The connector is written in Python, using only its standard library, so there is nothing else to
install. Claude starts it with the `python` command, so that command has to work in a terminal.

1. Install Python 3.11 or newer from [python.org/downloads](https://www.python.org/downloads/):
   - **Python install manager** (what python.org recommends for Windows): install it. The first
     `python` command, in the next step, then installs the latest Python. If it asks whether to
     add a folder to PATH, either answer works for Local Delegate.
   - **Traditional installer** (`python-3.x.y-amd64.exe`): on its first screen, tick
     **Add python.exe to PATH**, then choose **Install Now**.
2. Open a **new** terminal and run:

   ```powershell
   python --version
   ```

**Check:** it prints `Python 3.11` or higher. If it opens the Microsoft Store or says
*Python was not found*, Windows' shortcut to the Store answered instead of a real Python: see
[Troubleshooting](#troubleshooting).

## Step 2: Ollama

[Ollama](https://ollama.com) runs the model on your computer.

1. Download and install it from [ollama.com/download](https://ollama.com/download). It starts in
   the system tray.
2. In a new terminal:

   ```powershell
   ollama --version
   ```

**Check:** version 0.34.4 or newer. The setup in step 5 also needs a version that can turn cloud
features off; if yours can't, the status check will say so, and updating Ollama fixes it.

## Step 3: A model

```powershell
ollama pull qwen3.8:27b-q4_K_M
```

This downloads about 17 GB. **Check:** `ollama list` shows the model.

**Less memory?** Any Ollama chat model works. Pick a smaller one from
[ollama.com/library](https://ollama.com/library) that fits in your memory with a few GB to spare,
pull it, and after step 5 tell Claude *"use the local model <name>"* (it asks for your OK), then
calibrate again. Models without adjustable reasoning work too: the careful and deep levels then
answer without a separate reasoning phase, and deep still gets its checks and fix-up pass.

## Step 4: The plugin

Choose one of the three ways.

### A. Claude desktop app (Cowork)

1. Open **Customize > Plugins** (in the desktop app or on claude.ai).
2. Select **Add > Add marketplace** and enter `emanuelaviv/local-delegate` (or the repository's URL).
3. Find **Local Delegate** in the list and select **Add**.
4. Use it in a **Cowork** session in the desktop app on this computer. The connector runs on your
   computer, so plain chat can't start it.

On Team and Enterprise plans, an organization Owner may limit adding plugins; ask yours if
**Add marketplace** is missing.

### B. Claude Code

In a terminal:

```powershell
claude plugin marketplace add emanuelaviv/local-delegate
claude plugin install local-delegate@local-delegate
```

Or, inside a Claude Code session (version 2.1.275 or later), in one step:

```text
/plugin install local-delegate --marketplace emanuelaviv/local-delegate
```

Start a new session (or run `/reload-plugins`). **Check:** `claude plugin list` shows
`local-delegate@local-delegate` as enabled.

### C. Without a marketplace (no automatic updates)

1. On the repository's GitHub page, select **Code > Download ZIP**.
2. Either upload it in the Claude app under **Customize > Plugins > Add > Upload plugin**, or
   unzip it and start Claude Code with `claude --plugin-dir <path to the unzipped folder>`.

## Step 5: Set up Ollama for local-only use

In Claude Code or a Cowork session on this computer, say:

> set up the local model

Claude shows the planned changes and asks before making any. Depending on what's already set,
they are:

- set `disable_ollama_cloud` to `true` in `%USERPROFILE%\.ollama\server.json` (an existing file is
  backed up first);
- set the user environment variable `OLLAMA_NO_CLOUD=1`;
- set `OLLAMA_MAX_LOADED_MODELS=1`, so only one model sits in memory at a time;
- change `OLLAMA_HOST` to `127.0.0.1:11434`, only if it currently exposes Ollama to your network;
- restart Ollama, which stops anything else using it at that moment.

Why the restart and the user-level settings? Setting `$env:OLLAMA_NO_CLOUD` in a PowerShell window
only affects programs started from that window; the Ollama tray app never sees it.

**Check:** Claude reports the local-only checks after applying. All should pass.

## Step 6: Calibrate

Say:

> calibrate the local model

For up to 12 minutes, the connector measures your computer: load time, reading speed, writing
speed, memory per token of context, and what reasoning costs on a one-line question. The model
runs at full load meanwhile, so avoid heavy apps. Estimates are rough until this is done.

## Step 7: Verify

Say *"local status"*. You should see:

- `ready: true` and an empty `setup_needed`;
- under `local_only`, these checks passing: `loopback_url`, `ollama_running`,
  `listen_loopback_only`, `cloud_disabled_now`, `cloud_disabled_config`, `model_is_local`.

Then say *"check the worker"*. It starts the background worker without a job or the model and
should report `outlives_claude: true`, meaning overnight jobs keep running if you close the app.

## Step 8: Your first job

Point Claude at a real file, for example:

> digest C:\Users\you\Documents\project\weekly-report.md

Claude shows the options (see the README for an example). Pick one, keep working, and Claude
collects the result when it's ready. Plain text, Markdown, Word (`.docx`) and logs are read
directly; PDFs need `pypdf` or `pdftotext` installed, and scanned PDFs need text recognition (OCR)
first.

## Troubleshooting

| What you see | What to do |
|---|---|
| Claude doesn't see the local tools | Use Claude Code or a Cowork session on this computer, not plain chat. Check that the plugin is enabled and that `python --version` shows 3.11 or newer in a new terminal, then restart the app. |
| `python` opens the Microsoft Store, says *Python was not found*, or isn't recognized | Python isn't on PATH, so Windows' shortcut to the Store answers. Install the Python install manager (step 1), or run the traditional installer again, choose **Modify**, and tick **Add Python to environment variables** under **Advanced Options**. Then open a new terminal. |
| `python --version` shows a version older than 3.11 | Another Python comes first on PATH. Uninstall it in **Settings > Apps**, or put the newer one first in PATH, then open a new terminal. |
| `ready: false` with a `cloud_disabled_now` problem | Run the setup again, or quit Ollama from the tray and start it from the Start menu. The check reads `%LOCALAPPDATA%\Ollama\server.log` for the line `Ollama cloud disabled: true`. An older Ollama version may not support this; update it. |
| `listen_loopback_only` fails | Something set `OLLAMA_HOST` to expose Ollama to the network. The setup resets it; remove any manual setting that puts it back. |
| `model_is_local` fails | The model isn't downloaded or the name differs: run `ollama list`. Models ending in `:cloud` are refused. |
| A job comes back `low_memory` | Close heavy apps (an Android emulator, games, virtual machines; `wsl --shutdown` frees what WSL holds), ask Claude to `unload`, pick the tonight option, or use a smaller model. |
| Slower than estimated | Estimates correct themselves after a few jobs. Recalibrate after updating Ollama or the model, or after changing the graphics card. |
| Overnight jobs didn't run | The computer slept, shut down, or restarted for Windows Update. Keep it plugged in and set Windows Update's active hours to cover the night. Stopped jobs keep their finished parts; ask Claude to continue them. |
| Jobs stop when you close Claude | Say *"check the worker"*: `outlives_claude` should be `true`. If it isn't, keep the app open overnight and open an issue with that output. |

Logs: `%LOCALAPPDATA%\local-delegate\server.log` (the connector) and `worker.log` (the worker).
They contain metadata and errors, not your files' text, but check before sharing them.

## Updating

- **Claude app:** in **Customize > Plugins**, select **Check for updates**, or turn on **Sync
  automatically** for this marketplace.
- **Claude Code:** `claude plugin update local-delegate@local-delegate`.

Your settings, calibration, rule sheets and job history in `%LOCALAPPDATA%\local-delegate` are
kept. A worker still running from the older version hands its queue to the new version once
nothing is in progress.

## Uninstalling

1. Remove the plugin: in **Customize > Plugins**, open Local Delegate, open its menu and select
   **Remove**; or run `claude plugin uninstall local-delegate@local-delegate`.
2. Delete its data folder, including job results and file-only outputs (this can't be undone):

   ```powershell
   Remove-Item -Recurse -Force "$env:LOCALAPPDATA\local-delegate"
   ```

3. Optional: undo the Ollama settings from step 5. Keeping them is harmless, and keeps Ollama
   local-only.

   ```powershell
   [Environment]::SetEnvironmentVariable("OLLAMA_NO_CLOUD", $null, "User")
   [Environment]::SetEnvironmentVariable("OLLAMA_MAX_LOADED_MODELS", $null, "User")
   [Environment]::SetEnvironmentVariable("OLLAMA_CONTEXT_LENGTH", $null, "User")
   ```

   Then remove the line `"disable_ollama_cloud": true` from `%USERPROFILE%\.ollama\server.json`
   (or restore the `server.json.bak-...` backup next to it), and quit and restart Ollama.
