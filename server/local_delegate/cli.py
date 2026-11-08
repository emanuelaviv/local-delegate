"""Command line: `python ld.py <command>`.

  serve                      run the MCP server on stdio (what Claude, Cursor or Codex start)
  status                     local-only checks, RAM, calibration
  setup [--yes] [--context N] make Ollama local-only (shows the changes first)
  calibrate [--minutes N]    measure this computer
  tasks                      list tasks and their parameters
  estimate TASK JSON         e.g. estimate test_log_triage "{\"paths\": [\"C:/projects/app/build.log\"]}"
  run TASK JSON --approve [--mode fast|careful|deep] [--when now|idle|tonight|at:HH:MM]
                             run on the local model (the --approve flag is the user's go-ahead)
  jobs [--cancel ID]         recent jobs and tonight's queue
  priority [NAME] [--until HH:MM]  show or set the priority preset (rush, balanced, saver, night)
  worker-check               start the background worker (no job, no model) and say whether it
                             will outlive Claude's app
  worker                     the background job worker (started automatically)
  log [--days N]             savings and accuracy summary
  unload                     free the model's RAM now
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from typing import Any

from . import config


def _print(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n")


def _setup_logging(to_stderr: bool, name: str = "server.log") -> None:
    handlers: list[logging.Handler] = [logging.FileHandler(config.data_dir() / name, encoding="utf-8")]
    if to_stderr:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="ld", description="local-delegate")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve")
    sub.add_parser("worker", help="run the background job worker (started automatically)")
    sub.add_parser("worker-check", help="start the worker without a job and report on it")
    sub.add_parser("launch-worker", help=argparse.SUPPRESS)
    sub.add_parser("status")
    s = sub.add_parser("setup")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--context", type=int, default=None)
    c = sub.add_parser("calibrate")
    c.add_argument("--minutes", type=float, default=12)
    c.add_argument("--model", default=None)
    sub.add_parser("tasks")
    e = sub.add_parser("estimate")
    e.add_argument("task")
    e.add_argument("params", nargs="?", default="{}")
    e.add_argument("--priority", default=None)
    r = sub.add_parser("run")
    r.add_argument("task")
    r.add_argument("params", nargs="?", default="{}")
    r.add_argument("--approve", action="store_true", help="your go-ahead to run the local model")
    r.add_argument("--mode", default=None, choices=["fast", "careful", "deep"])
    r.add_argument("--when", default=None, help="now | idle | tonight | at:HH:MM")
    r.add_argument("--max-minutes", type=float, default=None)
    r.add_argument("--file-only", action="store_true")
    r.add_argument("--script", action="store_true", help="script result only, no model")
    q = sub.add_parser("jobs")
    q.add_argument("--cancel", default=None)
    pr = sub.add_parser("priority")
    pr.add_argument("name", nargs="?", choices=["rush", "balanced", "saver", "night"])
    pr.add_argument("--until", default=None)
    lg = sub.add_parser("log")
    lg.add_argument("--days", type=float, default=30)
    sub.add_parser("unload")
    args = ap.parse_args(argv)

    if args.cmd in (None, "serve"):
        _setup_logging(to_stderr=False)
        from . import mcp_stdio, server

        svc = server.Service()
        mcp_stdio.StdioServer("local-delegate", config.VERSION, server.INSTRUCTIONS, server.TOOLS,
                              server.make_handlers(svc)).serve()
        return 0
    if args.cmd == "launch-worker":
        from . import jobs

        jobs.launch_worker()
        return 0
    if args.cmd == "worker":
        _setup_logging(to_stderr=False, name="worker.log")
        from . import jobs

        logging.getLogger("local_delegate").info("worker: %s", jobs.Worker().run_forever())
        return 0

    _setup_logging(to_stderr=False)
    from . import server

    svc = server.Service()
    if args.cmd == "status":
        _print(svc.local_status(refresh=True))
    elif args.cmd == "setup":
        plan = svc.local_admin("setup_local_only", user_approved=False, context_length=args.context)
        if plan.get("nothing_to_change"):
            _print(plan)
            return 0
        print("These changes will be made:")
        for ch in plan.get("changes", []):
            print("  -", ch)
        if not args.yes and input("Apply them? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing changed.")
            return 1
        _print(svc.local_admin("setup_local_only", user_approved=True, context_length=args.context))
    elif args.cmd == "calibrate":
        out = svc.local_admin("calibrate", user_approved=True, budget_minutes=args.minutes, model=args.model)
        if "job_id" not in out:
            _print(out)
            return 1
        res = svc.local_result(out["job_id"], wait_s=5)
        while res.get("status") in ("queued", "running", "waiting"):
            print(f"[{time.strftime('%H:%M:%S')}] {res.get('phase')}", flush=True)
            res = svc.local_result(out["job_id"], wait_s=20)
        _print(res)
    elif args.cmd == "tasks":
        _print(svc.local_admin("tasks"))
    elif args.cmd == "estimate":
        out = svc.local_estimate(args.task, json.loads(args.params), priority=args.priority)
        print(out.get("ask_text") or "", file=sys.stderr)
        _print(out)
    elif args.cmd == "run":
        params = json.loads(args.params)
        mode = "file_only" if args.file_only else None
        est = svc.local_estimate(args.task, params, return_mode=mode)
        if "estimate_id" not in est:
            _print(est)
            return 1
        if args.script or est["action"] == "script":
            _print(svc.local_run(est["estimate_id"], option="script"))
            return 0
        print(est.get("ask_text") or est.get("next"), file=sys.stderr)
        out = svc.local_run(est["estimate_id"], mode=args.mode, when=args.when, user_approved=args.approve,
                            max_minutes=args.max_minutes, wait_s=0, return_mode=mode)
        if out.get("job_id") and (args.when in (None, "now")):
            while out.get("status") in ("queued", "running", "waiting"):
                print(f"  {out.get('phase')} ... ready about {out.get('ready_about')}", file=sys.stderr)
                out = svc.local_result(out["job_id"], wait_s=15)
        _print(out)
    elif args.cmd == "worker-check":
        _print(svc.local_admin("worker_check"))
    elif args.cmd == "jobs":
        if args.cancel:
            _print(svc.local_admin("cancel", job_id=args.cancel))
        else:
            _print(svc.local_admin("jobs"))
    elif args.cmd == "priority":
        _print(svc.local_admin("set_priority", priority=args.name, until=args.until) if args.name
               else svc.local_status().get("priority"))
    elif args.cmd == "log":
        _print(svc.local_admin("log_summary", days=args.days))
    elif args.cmd == "unload":
        _print(svc.local_admin("unload"))
    return 0
