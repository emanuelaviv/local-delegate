"""One-time setup that makes Ollama local-only and sized for this computer.

Changes (each shown to the user before it is applied):
  * ~/.ollama/server.json: "disable_ollama_cloud": true      (Ollama's own documented switch)
  * user environment OLLAMA_NO_CLOUD=1                        (same switch, belt and braces)
  * user environment OLLAMA_MAX_LOADED_MODELS=1               (never two big models in memory at once)
  * OLLAMA_HOST reset to 127.0.0.1:11434 only if it currently exposes Ollama to the network
  * optional OLLAMA_CONTEXT_LENGTH, so other apps calling Ollama directly (Cursor, Codex)
    don't get their input silently cut at the 4k default
  * restart Ollama so the running server picks the settings up

Setting $env:OLLAMA_NO_CLOUD in a PowerShell window only affects programs started from that
window; the tray app never sees it. That is why this writes the user-level settings and
restarts Ollama.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from typing import Any

from . import config, guard, sysinfo
from .ollama import Ollama, OllamaError

LOOPBACK = re.compile(r"^(127\.\d+\.\d+\.\d+|localhost|\[?::1\]?)(:\d+)?$")


def plan(cfg: dict[str, Any], context_length: int | None = None) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    sj, _, sj_path = guard.read_server_json()
    if not (sj and sj.get("disable_ollama_cloud") is True):
        changes.append({"what": f"set disable_ollama_cloud=true in {sj_path}", "kind": "server_json"})
    if sysinfo.IS_WINDOWS:
        if (sysinfo.user_env("OLLAMA_NO_CLOUD") or "") != "1":
            changes.append({"what": "set user variable OLLAMA_NO_CLOUD=1", "kind": "env", "name": "OLLAMA_NO_CLOUD", "value": "1"})
        if (sysinfo.user_env("OLLAMA_MAX_LOADED_MODELS") or "") != "1":
            changes.append({"what": "set user variable OLLAMA_MAX_LOADED_MODELS=1 (one model in RAM at a time)",
                            "kind": "env", "name": "OLLAMA_MAX_LOADED_MODELS", "value": "1"})
        host = sysinfo.user_env("OLLAMA_HOST")
        if host and not LOOPBACK.match(host.strip()):
            changes.append({"what": f"change OLLAMA_HOST from {host} to 127.0.0.1:11434 (loopback only)",
                            "kind": "env", "name": "OLLAMA_HOST", "value": "127.0.0.1:11434"})
        if context_length:
            changes.append({"what": f"set user variable OLLAMA_CONTEXT_LENGTH={int(context_length)} (default for other apps)",
                            "kind": "env", "name": "OLLAMA_CONTEXT_LENGTH", "value": str(int(context_length))})
    if changes:
        changes.append({"what": "restart Ollama (quits anything currently using it)", "kind": "restart"})
    return changes


def _write_server_json() -> str:
    sj, _, path = guard.read_server_json()
    data = sj if isinstance(sj, dict) and not sj.get("_unreadable") else {}
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(f"server.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copyfile(path, backup)
    data["disable_ollama_cloud"] = True
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return str(path)


def restart_ollama(cfg: dict[str, Any], env_updates: dict[str, str]) -> str:
    """Quit Ollama and start it again with the new settings."""
    if not sysinfo.IS_WINDOWS:
        return "not Windows: restart Ollama yourself (systemctl restart ollama, or quit and reopen the app)"
    for image in ("ollama app.exe", "ollama.exe"):
        sysinfo.run(["taskkill", "/IM", image, "/F"], timeout=15)
    time.sleep(2)
    app = config.ollama_app_path()
    try:
        if app:
            # Start it through Explorer: it then runs outside this connector's process tree (so closing
            # Claude doesn't close Ollama) and with the user environment Explorer just refreshed.
            subprocess.Popen(["explorer.exe", str(app)], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            exe = shutil.which("ollama")
            if not exe:
                return "could not find Ollama to start it; start it from the Start menu"
            env = dict(os.environ)
            for k in ("OLLAMA_NO_CLOUD", "OLLAMA_MAX_LOADED_MODELS", "OLLAMA_HOST", "OLLAMA_CONTEXT_LENGTH"):
                v = sysinfo.user_env(k)
                if v is not None:
                    env[k] = v
            env.update(env_updates)
            base = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            try:
                subprocess.Popen([exe, "serve"], env=env, creationflags=base | 0x01000000,  # break away from job
                                 close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except OSError:
                subprocess.Popen([exe, "serve"], env=env, creationflags=base, close_fds=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        return f"could not start Ollama ({exc}); start it from the Start menu"
    client = Ollama(cfg["ollama_url"])
    for _ in range(40):
        time.sleep(1)
        try:
            client.version()
            return "Ollama restarted"
        except OllamaError:
            continue
    return "Ollama did not answer within 40 s after restart; start it from the Start menu"


def apply(cfg: dict[str, Any], context_length: int | None = None, restart: bool = True) -> dict[str, Any]:
    done: list[str] = []
    env_updates: dict[str, str] = {}
    for ch in plan(cfg, context_length):
        if ch["kind"] == "server_json":
            done.append(f"wrote {_write_server_json()}")
        elif ch["kind"] == "env":
            if sysinfo.set_user_env(ch["name"], ch["value"]):
                env_updates[ch["name"]] = ch["value"]
                done.append(ch["what"])
            else:
                done.append(f"FAILED: {ch['what']}")
        elif ch["kind"] == "restart" and restart:
            done.append(restart_ollama(cfg, env_updates))
    guard.clear_cache()
    time.sleep(1)
    rep = guard.report(cfg, refresh=True)
    return {"applied": done or ["nothing to change"], "local_only_checks": rep}
