---
name: local-delegate
description: Offload routine reading, extraction and first drafts to the user's local model (Ollama on their own computer) through the local-delegate tools. Every model run is offered with options (Claude now, or the local model at fast / careful / deep reasoning, now or overnight), each with ready time, Claude tokens saved and redo risk, and runs only after the user picks one. Use whenever a task means reading large files on the user's computer (reports, logs, test output, diffs, folders of documents, job postings, CVs, letters) or producing a digest, extraction, translation or draft that a script or a spot check can verify, even if they don't mention the local model; whenever they want something kept on their computer, queued for tonight, or say how much time or how many tokens they want to spend. Not for verdicts, approvals, security reviews, clinical, legal or financial judgment, or anything sent in the user's name without their review.
---

# Local delegation

The local-delegate tools run on the user's own computer. Files are read there, exact facts come
from scripts there, and the local model reasons and drafts there; only what a tool returns reaches
this conversation. The aim is to save Claude tokens and keep text local, on the user's terms: when
they are in a hurry Claude does the work; when time is cheap (tonight, a lunch break) the local
model can reason long and carefully.

Find the tools with ToolSearch (query `local_estimate`) if they are not loaded; their names may
carry a prefix. If they are missing, work as usual and say once that the local connector isn't
connected (it runs only in Claude Code or a Cowork session on the user's Windows computer, not in
chat on the web or in the mobile apps).

## When to reach for it

| Situation | Task |
|---|---|
| Catch up on a long report or a project log (an AI agent's report, a running log) | `digest` (template `agent_report`, `coordinator_log` or `generic`; `since` for dated logs) |
| A build or test run failed (Gradle, pytest, Deno, Jest/Vitest, Node, JUnit XML) | `test_log_triage` |
| What changed in a commit, a range, the staged files | `diff_summary` |
| Does a commit match a report or a list of files | `filelist_compare` |
| Do files still match a hash record | `hash_check` |
| Answer a question from a big folder without reading all of it | `context_pack` |
| Several job postings to sort through | `posting_checklist` (batch them) |
| A CV against a posting | `cv_match` |
| A long letter or form: what to do and by when | `doc_summary` |
| Translate something for the user's own reading | `translate` |
| A first draft from the user's notes, with their rules | `draft` (+ `packs`) |
| Check a draft against the user's rules | `rules_check` (+ `packs`, `target`) |
| A small conversion or explanation over a file | `freeform` |

`local_admin(action="tasks")` lists every parameter and the user's rule sheets ("packs"). Pass packs
by name; never paste personal details into them.

## Never delegate

Verdicts (PASS or Not PASS), approvals, commit checks and plan or security reviews; clinical,
safety, legal or financial judgment; protocol interpretation; pay or salary questions; anything
sent in the user's name without their review; anything that needs this conversation's decisions
(pass a two-line summary at most); live accounts (email, calendar, websites and code hosting stay
with Claude).

## Reasoning levels, in plain words

- **fast**: answers directly, no reasoning. Quickest; fine for clear-cut extraction.
- **careful**: reasons briefly first (the model's low reasoning level, capped at a few thousand
  reasoning tokens). Better on judgment calls such as CV matching or explaining a failure.
- **deep**: reasons longer (medium level, with a larger cap overnight), then scripts check the
  answer (quotes against the files, missed dates, rule breaks, numbers dropped in translation) and
  the model gets one fix-up pass on whatever they flag.

On a typical desktop, writing is the slow part (a 27B model writes about 4 tokens a second on an
8 GB GPU with 32 GB of RAM), and reasoning is writing, so deeper levels take minutes to an hour.
That is what "tonight" is for. Every level has a hard reasoning budget, and a reasoning loop or an
exhausted budget falls back to a direct answer, so a job can't run away.

## The flow

1. **Status, once per session**: `local_status`. If `setup_needed` is not empty, offer setup
   (below). If `queue.recent` shows overnight jobs that finished, offer their results.
2. **Estimate**: `local_estimate(task, params, claude_context_tokens=<rough size of this chat>)`.
   Pass file paths, never contents. Add `need_by` when the user has a deadline ("15:00",
   "20 min") and `priority` when they state one for this job.
3. **Act on `action`.**
   - `script`: use `script_result` (exact, instant). No question.
   - `do_here`: do it yourself; mention the best local alternative in one line (it's in `next`).
   - `blocked` / `too_large`: do it yourself and say why in one line.
   - `ask`: ask the user (below), then run what they pick.
4. **Run** only after the user picks a local option:
   `local_run(estimate_id, option=<id>, user_approved=true)`. They may also ask for a different
   start: "when I step away" (`when="idle"`), "at 14:00" (`when="at:14:00"`), "tonight"
   (`when="tonight"`), or a level by name (`mode`). "Claude now" means you do it here.
5. **Collect.** Jobs run in a background worker outside the app. Quick ones return inline;
   otherwise you get a `job_id` and a `ready_about` time.
   - Keep working on what doesn't need the result, then call `local_result(job_id)` once (it waits
     up to ~45 s). Never poll in a loop: each check re-reads this conversation.
   - Nothing else to do: if a scheduling tool is available, schedule one check-in at
     `ready_about` and end the turn with one status line; otherwise tell the user when it will be
     ready and to say "check".
   - Overnight jobs: say when they start and finish. Next time, `local_admin(action="jobs")` lists
     what finished; offer the results then.
   - A job stopped at its deadline returns its finished parts and `resumable`; offer to continue it
     (`local_run(job_id=..., when=..., user_approved=true)`).
6. **Check before using.** Counts, file lists, hashes and dates come from `facts` /
   `script_result`: trust those. Drop or verify `citation_check.unverified_quotes`. Glance at
   `keyword_lines_not_cited`, `paths_mentioned_not_in_files` and
   `dates_found_by_script_but_not_used`, where omissions hide. `quality` shows fix-ups, truncation
   and direct-answer fallbacks.
7. **Present** digests as digests and drafts as drafts (`DRAFT - review before use`), with how long
   the job took against the estimate in a few words. Verdicts and final wording stay with Claude
   and the user.
8. **Record** `local_feedback(job_id, "accepted" | "edited" | "redone" | "rejected")`. The redo-risk
   figures shown with every option learn from this, per task and level.

## Asking the user

Use AskUserQuestion when available (otherwise one short message): header `Local model`, question =
the estimate's `question`, options = the estimate's `options` in order. Use each option's `label`
(add "(Recommended)" to the recommended one, which comes first) and its `description` (ready time,
tokens saved and after likely redos, reasoning level, redo risk). More choices are in
`more_options`; the user can type one ("deep, tonight", "fast at 12:30").

Frame it in terms of the whole task when that helps: "The review takes ~15 min locally instead of
~1 min here, but saves ~24k tokens; I'll draft the rest meanwhile."

Several similar jobs (five reports, eight postings): one question for the batch, with summed times.
After "do this kind locally from now on", later runs of that task in this chat skip the question
but state the choice in one line. A standing yes never carries over to other chats unless the user
asks.

If the estimate's `notes` say "not calibrated", mention that the figures are rough.

## Priorities

A preset decides which option is recommended. Each one prices waiting in Claude tokens:

| Preset | Recommends | Use when the user says |
|---|---|---|
| `rush` | Claude, unless a local result is nearly instant | "I'm short on time", "need this now" |
| `balanced` (default) | local when it saves tokens and is ready soon; tonight for long jobs | normal days |
| `saver` | the deepest local level that's ready in time | "save tokens today", "I don't mind waiting" |
| `night` | tonight's queue at the deep level | "batch it", "do these overnight" |

Set it when the user states a standing preference or a mood for the day:
`local_admin(action="set_priority", priority=..., until="12:00")` for a while, without `until` as
the new default, or `schedule_rules=[{"from": "07:00", "to": "11:00", "priority": "rush"}]` for a
daily pattern. For a single job, pass `priority` to `local_estimate` instead. The night window
defaults to 23:00-07:00; change it with `local_admin(action="night_window", start=..., end=...)`.

## Keeping things on the user's computer

When the user wants something kept off the cloud (family, legal or medical letters, anything they
mark private), call `local_estimate(..., prefer_local=true, return_mode="file_only")`. Local
options are offered even without token savings, "Claude now" comes last and says it sends the text
to Claude, and the result is written to a file on the user's computer: you only see its path. Don't
open it unless they ask. Translations default to file_only.

Never pass secrets, other people's personal data (clients, patients, students) or sensitive
personal details as parameters. The connector refuses key stores and `.env` files and redacts
known key formats, but don't rely on that.

## Setup and calibration

First run, or after a new model or an Ollama update:

1. `local_admin(action="setup_local_only")` returns the planned changes (Ollama cloud features
   off, one model in memory at a time, Ollama reachable only from this computer). Show them,
   mention that Ollama restarts, ask, then call again with `user_approved=true`.
2. `local_admin(action="calibrate")`: ask, then call again with `user_approved=true`; it runs as a
   job. Share the reading speed (prefill), writing speed (decode) and what reasoning cost on a
   one-line question, in plain words.

Other actions: `unload` frees the model's memory now (before starting a memory-hungry app), `jobs`
shows recent jobs and tonight's queue, `cancel` stops one, `log_summary` shows real savings,
accuracy and reasoning length per task and level, `allow_root` and `set_model` need the user's
approval. `worker_check` starts the background worker without a job or the model (no approval
needed) and says whether it will outlive the app: use it after installing or updating the
connector, or when the user asks whether overnight jobs are safe. Never book a dummy model job to
test the worker.

Overnight runs keep the computer awake until the queue is done (the screen can still turn off) and
need it to stay on and plugged in. If `worker.outlives_claude` is false, tell the user to leave the
app open while tonight's jobs run. Windows Update restarts during the night stop them; the finished
parts are kept and the job can continue. After a connector update, a worker left running by the
older version hands its queue to the new version once nothing is in progress.
