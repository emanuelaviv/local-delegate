# Local Delegate: design notes

For whoever tunes or extends this. It started from a map of which routine tasks a local model
could take over; the design decisions, and the reasons for them, come first.

## 1. Design decisions, and why

1. **Three routes, not two: script, local model, Claude.** Several "delegable" items need no model
   at all and are now exact, instant scripts: file-list comparison, hash checks, test counts,
   cached-versus-executed Gradle tasks, dates in letters, exact-term rule checks, keyword search.
   Hybrid tasks return the script result in the estimate itself, so Claude often has what it needs
   before any model runs (`script` / `script_only` recommendations).
2. **Scripts trim before the model reads.** On CPU, reading (prefill) runs at roughly 30 to 40
   tokens a second for a 27B model, so a 50,000-token log would take about 25 minutes just to read.
   Parsers cut it to the excerpts around the first failures; the packer ranks chunks with BM25 and
   sends only a budget sized to the measured reading speed (`_prefill_tps(profile) * 120 s`).
3. **The model picks; scripts copy and check.** The context packer's model only chooses line
   ranges; the text returned is copied verbatim from the file. Every citation is checked against the
   file (`verified`, `relocated`, `close`, `unverified`), so Claude no longer spot-checks by
   re-reading; it looks only at `unverified_quotes`, plus the keyword lines and script-found dates
   the model did not cite (where omissions hide).
4. **The real cost of delegating includes orchestration.** Every tool call is a step in which
   Claude re-reads the whole conversation (from cache, at about a tenth of the price). Estimating,
   asking, running and fetching cost 3 to 4 steps; reading a file here costs steps too, and the text
   then stays in the conversation for every later step. The router counts all of it
   (`estimate.py`). Consequences: single small inputs (one posting, one short letter, a short draft
   from notes Claude already has) are cheaper here; big reads, multi-file reads and batches win.
   Savings on small per-document tasks only appear when they are batched, so
   `posting_checklist` and `doc_summary` now take several files per job (one call each, with the
   instructions reused from Ollama's prompt cache).
5. **Jobs run in a separate worker process, not inside the tool call.** Claude Desktop cuts MCP
   tool calls off after about one to four minutes (claude-code issues 22542 and 65643), its app
   restarts on updates, and each poll is a full Claude step. So the connector only writes job files;
   a detached worker process (`ld.py worker`, started outside Claude's process tree) runs them, and
   a call waits at most ~45 s. The skill tells Claude to keep working and check once, or schedule a
   single wake-up. The connector itself usually sits in job objects that end it with the app: Node-based
   apps put the processes they start in a kill-on-close job (libuv), and the `py` launcher, where a
   configuration uses it, does the same to python.exe (with silent breakaway). So the worker is started through a short-lived
   launcher that asks to leave the job (ignored when there is none; refused when the job forbids it,
   then a plain start when the job lets children go silently, then WMI, then an ordinary child as a
   last resort).
   The worker reports the job object it ended up in (`worker.json` -> `job`), from which
   `outlives_claude` is derived; `local_admin(action="worker_check")` shows it without a job. v0.2.1
   fixed the check itself: undeclared ctypes signatures passed the current-process handle as a
   32-bit int on 64-bit Windows, so the check always said "unknown". After an update, a worker left
   by the older version hands over to the newer code (`server.json`) once nothing is in progress.
6. **Reasoning is a choice with a budget, not switched off** (v0.2: quality
   can be worth hours when the computer is idle anyway). Qwen3.8-27B defaults to its `xhigh`
   reasoning level, reported to produce up to ~60k reasoning tokens per answer: about 4 hours at
   the reference machine's ~4 tokens/s. That is the "an hour to define hardware" problem. Each job therefore
   runs at an explicit level -- fast (`none`), careful (`low`, 3k-token budget), deep (`medium`, 8k,
   16k overnight, plus a fix-up pass) -- read from `/api/show` `thinking.values` and mapped to the
   nearest offered level, never the default. Budgets are enforced while streaming; a reasoning
   loop (the same text recurring) or an exhausted budget falls back to a direct answer on the
   cached prompt. Sampling follows the model card per mode (thinking: temperature 1.0, top_p 0.95,
   top_k 20; fast extraction: temperature 0 with a JSON schema). Ollama 0.34.4+ applies the JSON
   schema after the reasoning in one pass.
7. **Machine memory is measured, not assumed.** A first estimate assumed about 0.25 MB per
   token of context. Qwen3.8-27B is a hybrid: of its 64 layers only 16 use full attention (4 KV
   heads, head size 256); the other 48 are Gated DeltaNet layers with a fixed-size state. That is
   about 64 KB per token at f16, a quarter of the assumption, so a 32k context costs about 2 GB, not
   8 GB. The Ollama build is about 18 GB, not 16 to 17 (it includes a 0.9 GB vision projector, so
   the model *can* read images). On a 32 GB machine without
   a big GPU, reading speed, not memory, is the binding limit, so usable context is the smaller of
   a memory limit and a time limit (`limits` in the profile). Calibration measures RAM per token from
   Ollama's own `/api/ps` report at two context sizes, and the share on a GPU, if any.
8. **Few fixed context sizes.** Changing `num_ctx` makes Ollama reload the model. Jobs reuse the
   loaded size when it is big enough, otherwise pick from fixed buckets (default 16k).
9. **RAM checked before every job.** With the Android emulator and Studio open, 32 GB cannot also
   hold an 18 GB model; Windows would page to disk and a minute would become an hour. Such jobs come
   back `low_memory` with the numbers, and `unload` frees the model on demand.
10. **Strictly local is verified, not configured and hoped for** (section 5). Setting
    `$env:OLLAMA_NO_CLOUD` in PowerShell does not reach the tray app; setup writes Ollama's
    documented `server.json` switch and the user variable, restarts Ollama, and every job checks the
    running server's own log line.
11. **Privacy route.** `prefer_local=true` with `return_mode="file_only"`: offered even with no token
    saving; the result stays in a file and Claude sees only its path.
12. **Self-correcting.** Each job logs predicted versus actual reading and writing time; the
    estimator scales by the median ratio of recent jobs (bounded 0.5 to 3). `log_summary` reports
    real savings and flags tasks redone in more than 30% of rated runs.
13. **No dependencies.** Standard-library Python with a small MCP implementation (tested against
    the official SDK client, including its `server/discover` probe and fallback). Nothing is fetched
    from the internet to run it, startup is instant, and there is no SDK version churn (mcp 2.x
    renamed FastMCP and changed the protocol era in 2026).

14. **Options instead of a yes/no, priced by a preset.** Each estimate prices Claude-now, the
    script result, and every level now and tonight: ready time (clock), Claude tokens saved, and the
    expected saving after likely redos. A preset (rush / balanced / saver / night) puts a price on a
    minute of waiting, on a result arriving tomorrow, and a bonus per reasoning level; the best total
    is recommended, and up to four options are shown (the recommendation, Claude, the quickest
    local, and the deepest overnight one). Presets can be set for a while, as a default, or by time
    of day.
15. **Redo risk is learned, not assumed.** Starting guesses per level (accepted / edited / redone:
    fast .60/.25/.15, careful .70/.22/.08, deep .75/.20/.05, worth 6 observations) are updated with
    the user's feedback in two layers: first all tasks at that level, then the task itself. Expected
    saving = (P(accept)+P(edit)) x net - P(edit) x 0.3 x Claude's own cost - P(redo) x delegation cost.
16. **Overnight is a first-class schedule.** Night window, start-at-a-time and start-when-idle
    (`GetLastInputInfo`); the worker keeps Windows awake with `SetThreadExecutionState`
    (ES_SYSTEM_REQUIRED, display may sleep) only while jobs are pending and only on mains power; a
    job wanted now overtakes an overnight job at the next part boundary; deadlines keep finished
    parts (per-call checkpoints keyed by a hash of the call) so a job can continue another night.
17. **Deep answers are checked and fixed by the same scripts that check them for Claude.** The
    fix-up pass is spent only when a check fails: unverified quotes, keyword lines the digest
    skipped, dates a summary missed, rule breaks in a draft, numbers dropped by a translation,
    "covered" claims without a CV line. The better of the two answers (fewer problems) is kept.

## 2. Architecture

```
Claude (chat, cloud) --tool calls--> Claude desktop app --stdio--> ld.py serve (the connector)
                                                                       |  reads files, runs scripts, estimates
                                                                       |  writes jobs\<id>.json, reads them back
                                                                       v
                                                     ld.py worker (detached; survives Claude's app)
                                                                       |  one model call at a time, schedules,
                                                                       |  deadlines, keep-awake, checkpoints
                                                                       v
                                                     Ollama on 127.0.0.1:11434 (cloud off, loopback only)
```

Modules (`server/local_delegate/`): `mcp_stdio` (protocol), `server` (tools), `tasks` (contracts),
`parsers` (exact preprocessing), `verify` (citations, schema), `estimate` (time, tokens, decision),
`calibrate`, `jobs` (queue, worker), `schedule` (night window, presets), `logbook`, `guard` (local-only, paths, redaction), `ollama` (loopback client, reasoning levels, loop guard),
`sysinfo` (RAM, GPU, battery, listening sockets), `setup_local`, `packs`, `textio`, `config`, `cli`.

## 3. The estimate

Per option (level x now/tonight), from the calibrated rates of this computer:

- load: 0 if the model is loaded at a big enough context; else a reload (context change) or a cold
  load (always assumed for tonight);
- reading (prefill) of `n` uncached tokens after `p` cached ones: `a*n + b*((p+n)^2 - p^2)`;
- writing (decode) `k` tokens (reasoning + answer) at context `c`: `k * (g + h*(c + k/2))`;
- reasoning tokens per answer: a per-level starting guess (careful 900, deep 3,500) x the task's
  factor x (1 + input tokens / 20,000) x the learned ratio for that task and level, capped by the
  budget; the slow case is 2.5x, capped;
- deep: + P(fix-up needed) x (reading the previous answer and the problems, mostly cached, + a
  careful-level answer); P starts at 0.5 and follows the log;
- p90 adds 15% slack; plus jobs queued ahead (tonight: everything booked earlier tonight); x1.8 on
  battery if calibration was plugged in.

Claude's side: reading `file chars / chars-per-token` plus one step per file (and per 2,000 lines),
writing the answer, and the read text carried into `future_steps` (default 5) later steps at the
cache weight (0.1). Delegating: the result Claude reads back, 3 steps (4 if the job outlasts one
call or runs tonight), and the result carried forward. Each step costs
`claude_context_tokens * 0.1 + 250`.

Not offered: options that need more context than any bucket, more RAM than is free now (tonight
only checks total RAM; the worker re-checks free RAM at start), more than `now_max_minutes` now,
past the night window's end, or past `need_by`.

Decision: `blocked` (local-only checks failing) -> `script` (no model needed, or the exact script
result is enough and saves tokens) -> `too_large` -> otherwise the preset's best option among those
whose expected saving clears `min_saving` (or any local option with `prefer_local`), against
Claude-now: Claude wins -> `do_here`, a local option wins -> `ask`. All prices are in
`config.json` -> `priorities`.

## 4. Calibration (`calibrate.py`)

Budgeted (12 min default) and adaptive: it skips the long reading tests when the short ones show
they would not fit. Steps: model info and its reasoning switch; unload and cold load at 4k context;
characters per token for English prose, code, French and Hebrew (each also a reading-speed point);
reload at 16k (8k if RAM is short) for reload time and RAM per context token; reading speed at
about 1.5k, 4k and 8k tokens; prefix reuse (the same long prompt with a new ending, immediately,
since Ollama caches only the last prompt) together with writing speed at long context; writing
speed at short context; the cost of reasoning on "In one sentence, what is computer hardware?"
with the default and with reasoning off. Results go to `profile.json` with limits:
`max_ctx_memory_now`, `max_prompt_tokens_interactive`, `default_ctx`.

Recalibrate after an Ollama or model update, after adding a GPU, or when `log_summary` says the
real times drift from the estimates.

## 5. Local-only guarantees (`guard.py`)

| Check | How |
|---|---|
| loopback URL | `ollama_url` must resolve to loopback; `http.client` and raw sockets ignore proxy variables |
| listening only on loopback | `Get-NetTCPConnection -LocalPort 11434 -State Listen` (language-independent), else `netstat` |
| cloud off in the running server | Ollama's log line `Ollama cloud disabled: true` (`%LOCALAPPDATA%\Ollama\server.log`); or `server.json` / `OLLAMA_NO_CLOUD` set before the server process started |
| cloud off at the next start | `server.json` `disable_ollama_cloud: true` or user variable `OLLAMA_NO_CLOUD` |
| model is local | not a `:cloud` / `-cloud` name; present in `/api/tags` with a size and no `remote_host` |

Strict mode (default) refuses to run unless the running server is proven cloud-disabled.

## 6. Task contracts

| Task | Kind | Script facts | Model returns | Checks |
|---|---|---|---|---|
| digest | model | paths mentioned, keyword lines (approval, PASS, TODO...) | status, files, tests, open items, approval lines, decisions, notes (all cited) | citations; uncited keyword lines |
| test_log_triage | hybrid | counts per runner, failing tests, Gradle tasks from cache, first-error excerpts | likely cause, next step (cited) | citations |
| diff_summary | hybrid | file table, areas, protected hits, line counts | per-area summary, risks, questions | - |
| filelist_compare | script | differences between lists | - | - |
| hash_check | script | match / mismatch / missing | - | - |
| context_pack | hybrid | BM25 passages | chosen line ranges | text copied verbatim |
| posting_checklist | model, batch | - | must-haves, nice-to-haves, keywords (cited) | citations |
| cv_match | model | keyword presence (exact) | requirement-by-requirement status (cited) | citations; keywords re-checked |
| doc_summary | model, batch | dates, amounts, emails, phones | summary, actions, dates, contacts, terms explained | citations; script dates not used |
| translate | model | - | text, in chunks | saved to a file by default |
| draft | model | pack rules on the output, [[TODO]] gaps | text | exact-term rules |
| rules_check | hybrid | exact-term rules | written-rule violations (cited) | citations |
| freeform | model | - | short text or JSON | output cap |

Extraction runs at temperature 0, seed 7, with Ollama's JSON-schema `format`, so output is always
parseable unless it hits the length cap (reported as `truncated_parts`). The conversation's own
decisions never go to the model; the skill allows at most a two-line summary.

## 7. Extending

- **A new task:** write `prep_x(params, cfg, profile) -> Prepared` (read with `load_docs`, which
  enforces the path rules and redaction; put fixed instructions in the system message, variable
  input last), an optional `fin_x(prep, outs)`, and register a `Task` in `TASKS`. Give it a JSON
  schema with citations if it extracts; add a test with the fake Ollama (`tests/fake_ollama.py`).
- **A faster second model:** set `fast_model` and calibrate it with `ld.py calibrate --model ...`.
  Routing by tier (`fast_ok` tasks) is prepared in the task registry; wiring the choice into
  `estimate` is the next step. On CPU, a mixture-of-experts model with about 3B active parameters
  writes several times faster than a dense 27B; only one fits in 32 GB at a time
  (`OLLAMA_MAX_LOADED_MODELS=1`), so switching costs a load.
- **Images:** the Q4 build ships a vision projector; an early Ollama bug routed Q4 vision input to
  the wrong parser, so test image handling on your Ollama version before adding a figure-check task.

## 8. Open items

- Field-tested on one Windows 11 computer (2026-10-22, v0.2.1): the worker starts with `how: breakaway` and
  reports no job object (`outlives_claude: true`); the connector sits in the `py` launcher's job
  (kill on close, silent breakaway, no explicit breakaway flag, which Windows accepted anyway).
  Since 0.4.1 the plugin starts the connector with `python` directly (Claude's plugin directory
  wants `node`, `python`, `python3`, `bash` or `sh` there), so it sits in the app's job instead;
  that start still needs the same field check (*"check the worker"*).
  Still to see on a real night: that keep-awake holds, and the real reasoning lengths per level
  (the log shows them).
- A faster second model for the fast level: on this machine (8 GB GPU, 32 GB RAM) a mixture-of-experts
  model with ~3B active parameters writes several times faster than the dense 27B. Only one model
  fits in memory at a time (`OLLAMA_MAX_LOADED_MODELS=1`), so switching costs a load.

- Local transcription: Whisper for speech to text, then `doc_summary` on the transcript.
- Measure against Claude on about ten real samples per task before relying on a task; the log's
  feedback outcomes are the start of that.
