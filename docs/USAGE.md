# Using Local Delegate

You don't need special commands: Claude offers the local model when a task fits and asks before
every run. This page explains what you'll see and what you can ask for.

## The 13 tasks

"Script" means the result is computed exactly and instantly, with no model involved.

| Task | Use it for | Exact by script |
|---|---|---|
| `digest` | Catching up on a long report or log: a fixed template with line citations | Paths mentioned, keyword lines (approval, PASS, TODO), every quote checked |
| `test_log_triage` | A failed build or test run (Gradle, pytest, Deno, Jest, Vitest, Node, JUnit XML) | Counts per runner, failing tests, first-error excerpts |
| `diff_summary` | What changed in a commit, a range or staged files | The file table, areas, protected-path flags, line counts |
| `filelist_compare` | Does a report's list of files match a commit or a staging list? | Everything (script only) |
| `hash_check` | Do files still match a checksum manifest? | Everything (script only) |
| `context_pack` | Answering a question from a big folder without reading all of it | Passages copied verbatim, with line ranges |
| `posting_checklist` | Sorting job postings: must-haves, nice-to-haves, keywords, deadline | Every quote checked; batch several postings in one job |
| `cv_match` | A CV against a posting: covered, partial or missing, with evidence lines | Keyword presence |
| `doc_summary` | A long letter or form: what to do, by when, who to contact | Dates, amounts, emails and phone numbers |
| `translate` | Translating a document for your own reading | Saved to a file by default; deep runs check that numbers carried over |
| `draft` | A first draft from your notes, following your rule sheets | Exact-term rules; gaps marked `[[TODO]]` |
| `rules_check` | Checking a draft against your rule sheets | Exact-term rules |
| `freeform` | A small conversion or explanation over a file | |

Some things are never delegated: verdicts and approvals, security reviews, clinical, legal or
financial judgment, and anything sent in your name without your review.

## The options

For each job, Claude gets an estimate and shows you up to four options, the recommended one first:

- **Ready time**: when the result would be ready, as a clock time and a range.
- **Tokens saved**: how many Claude tokens the local run saves, and how many after allowing for
  results that need redoing.
- **Redo risk**: how often results at that level needed redoing. It starts as a guess (fast 15%,
  careful 8%, deep 5%) and becomes your own record as you tell Claude how results turned out.

More options are available on request; you can type one, such as *"deep, tonight"* or *"fast at
12:30"*.

## Reasoning levels

Many local models can reason before answering. Left at their default, some write tens of thousands
of hidden reasoning tokens per answer, which can take hours. The connector never uses that default:

| Level | What the model does | Typical time on the reference computer |
|---|---|---|
| fast | Answers directly, no reasoning | Minutes |
| careful | Reasons briefly first (up to 3,000 reasoning tokens) | 5 to 15 more minutes per answer |
| deep | Reasons longer (up to 8,000 tokens, 16,000 overnight); then scripts check the answer and the model gets one fix-up pass on what they flag | Up to an hour per answer |

If the reasoning goes in circles or its budget runs out, the job falls back to a direct answer
instead of running on.

## When jobs run

- **Now.**
- **When you step away**: starts after 10 minutes without keyboard or mouse input, and pauses
  between parts when you come back.
- **At a time**: *"at 14:00"*.
- **Tonight**: in the night window, 23:00 to 07:00 by default.

Jobs run in a background program outside the app, so closing Claude doesn't stop them. While jobs
wait or run, the computer is kept from sleeping (the screen can still turn off), on mains power
only. A job wanted now overtakes an overnight job between two of its parts. A job stopped by its
deadline keeps its finished parts and can continue another night.

## Presets

A preset decides which option Claude recommends; you can always pick another.

| Preset | Recommends | Say |
|---|---|---|
| rush | Claude, unless a local result is nearly instant | *"I'm short on time"* |
| balanced (default) | Local when it saves tokens and is ready soon; long jobs tonight | |
| saver | The deepest local level that's ready in time | *"save tokens today"* |
| night | Tonight's queue at the deep level | *"batch everything tonight"* |

Presets can last a while (*"rush until noon"*), become the default, or follow a daily pattern
(*"mornings are rush"*). Under the hood, each preset puts a price in tokens on a minute of
waiting and on getting a result tomorrow instead of today, plus a bonus for deeper reasoning. The
option with the best total of tokens saved, plus that bonus, minus the waiting cost is recommended.

Small jobs usually go to Claude under every preset. That isn't a limitation: estimating, asking and
collecting a local result cost Claude a few steps too (each re-reads the conversation), so small
jobs are cheaper done directly.

## Keeping things private

Say *"keep this on my computer"*. Claude then offers local options even when they save no tokens,
lists "Claude now" last (it would send the text to Claude), and asks for a file-only result: it's
written to `%LOCALAPPDATA%\local-delegate\outbox` and Claude sees only its path. Translations are
file-only by default.

Results in `outbox` and finished jobs are deleted after 14 days (`retention_days`). Move anything
you want to keep.

## Rule sheets

Rule sheets ("packs") hold your standing rules for drafts and rules checks: terms to avoid,
required phrasing, tone. They are Markdown files in `%LOCALAPPDATA%\local-delegate\packs`; start
from [`example.md`](../packs/example.md), which is copied there on first use. Updates never
overwrite your packs.

## Teaching it

After you use a result, tell Claude whether you accepted it, edited it or had to redo it. Redo
risk per task and level, and the reasoning time estimates, learn from this. Say *"local log
summary"* to see real savings and accuracy per task and level.

## Settings

Most settings are easier to change by asking Claude. They live in
`%LOCALAPPDATA%\local-delegate\config.json`; anything not listed there uses the defaults in
`server/local_delegate/config.py`.

| Key | Default | Meaning |
|---|---|---|
| `model` | `qwen3.8:27b-q4_K_M` | The local model |
| `priority` | `balanced` | Default preset; `priority_schedule` sets daily patterns |
| `night_window` | 23:00 to 07:00 | When "tonight" jobs may run |
| `idle_minutes` | 10 | How long you must be away before "when idle" jobs start |
| `keep_awake` | `true` | Keep the computer from sleeping while jobs wait or run (mains power only) |
| `modes` | | Reasoning level, budget and sampling per level |
| `priorities` | | The prices behind each preset |
| `allowed_roots` | your user folder | Folders the connector may read |
| `deny_globs` | secrets and key stores | Never read, whatever the folder |
| `protected_paths`, `areas` | empty | Labels for diff summaries |
| `ram_headroom_gb` | 4 | Memory left free for Windows and your apps |
| `retention_days` | 14 | How long finished jobs and file-only results are kept |
| `strict_local` | `true` | Refuse to run without proof that cloud features are off |

## From a terminal

From the `server` folder of a copy of this repository (the data folder is shared with the plugin):

```powershell
py -3 ld.py status
py -3 ld.py estimate test_log_triage "{\"paths\": [\"C:/projects/app/build.log\"]}"
py -3 ld.py run digest "{\"paths\": [\"C:/projects/report.md\"]}" --approve --mode deep --when tonight
py -3 ld.py jobs
py -3 ld.py priority saver --until 18:00
py -3 ld.py worker-check
py -3 ld.py log
```

`--approve` is the yes that Claude otherwise asks you for.

## From Codex, Cursor or another MCP client

The same server works in any tool that can start a local MCP server over stdio. All tools share
one queue and one worker, so two tools never run the model at the same time. Copy the `server`
folder somewhere stable, such as `C:\tools\local-delegate\server`, then:

- **Codex** (its desktop app, CLI and IDE extension share these settings), in
  `%USERPROFILE%\.codex\config.toml`:

  ```toml
  [mcp_servers.local-delegate]
  command = "py"
  args = ["-3", "-E", "-X", "utf8", "C:\\tools\\local-delegate\\server\\ld.py", "serve"]
  tool_timeout_sec = 90
  default_tools_approval_mode = "writes"
  ```

  `writes` makes the app ask before every tool that isn't read-only, including `local_run`.

- **Cursor**, in `%USERPROFILE%\.cursor\mcp.json`:

  ```json
  {"mcpServers": {"local-delegate": {"command": "py",
    "args": ["-3", "-E", "-X", "utf8", "C:\\tools\\local-delegate\\server\\ld.py", "serve"]}}}
  ```

Other tools don't get the skill, so ask for local runs explicitly there.

## The data folder

`%LOCALAPPDATA%\local-delegate` holds:

| Item | What it is |
|---|---|
| `config.json` | Your settings |
| `profile.json` | Calibration results |
| `packs\` | Your rule sheets |
| `jobs\` | The queue and job results |
| `outbox\` | File-only results |
| `log.jsonl` | One line of metadata per job (sizes, times, outcome), never text |
| `server.log`, `worker.log` | Errors and events |
| `worker.json`, `server.json` | The running worker, and the newest installed connector |
