"""A small MCP server over stdio (JSON-RPC 2.0, one JSON message per line).

Implements the handshake-era protocol (initialize, tools/list, tools/call, ping,
notifications/cancelled). Newer clients first probe `server/discover`; answering
"method not found" makes them fall back to `initialize`, which is the documented
behaviour of the official SDKs. Standard library only.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

HANDSHAKE_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
LATEST = HANDSHAKE_VERSIONS[-1]

log = logging.getLogger("local_delegate.mcp")


class StdioServer:
    def __init__(self, name: str, version: str, instructions: str, tools: list[dict[str, Any]],
                 handlers: dict[str, Callable[..., Any]], stdin: Any = None, stdout: Any = None):
        self.name = name
        self.version = version
        self.instructions = instructions
        self.tools = tools
        self.handlers = handlers
        self.stdin = stdin or sys.stdin.buffer
        self.stdout = stdout or sys.stdout.buffer
        self.write_lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="mcp-call")
        self.cancels: dict[Any, threading.Event] = {}

    # ------------------------------------------------------------ io
    def send(self, obj: dict[str, Any]) -> None:
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8") + b"\n"
        with self.write_lock:
            self.stdout.write(data)
            self.stdout.flush()

    def result(self, rid: Any, result: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "id": rid, "result": result})

    def error(self, rid: Any, code: int, message: str) -> None:
        self.send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})

    # ------------------------------------------------------------ loop
    def serve(self) -> None:
        for raw in iter(self.stdin.readline, b""):
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.error(None, -32700, "parse error")
                continue
            if isinstance(msg, list):  # batches (2025-03-26)
                for m in msg:
                    self.handle(m)
            else:
                self.handle(msg)
        self.pool.shutdown(wait=False)

    def handle(self, msg: dict[str, Any]) -> None:
        if not isinstance(msg, dict):
            return
        method = msg.get("method")
        rid = msg.get("id")
        params = msg.get("params") or {}
        if method is None:
            return  # a response to something we never send
        if rid is None:  # notification
            if method == "notifications/cancelled":
                ev = self.cancels.get(params.get("requestId"))
                if ev:
                    ev.set()
            return
        try:
            if method == "initialize":
                requested = params.get("protocolVersion")
                version = requested if requested in HANDSHAKE_VERSIONS else LATEST
                self.result(rid, {"protocolVersion": version,
                                  "capabilities": {"tools": {"listChanged": False}},
                                  "serverInfo": {"name": self.name, "version": self.version},
                                  "instructions": self.instructions})
            elif method == "ping":
                self.result(rid, {})
            elif method == "tools/list":
                self.result(rid, {"tools": self.tools})
            elif method == "tools/call":
                ev = threading.Event()
                self.cancels[rid] = ev
                self.pool.submit(self._call, rid, params, ev)
            elif method in ("resources/list", "resources/templates/list"):
                self.result(rid, {"resources": []} if method == "resources/list" else {"resourceTemplates": []})
            elif method == "prompts/list":
                self.result(rid, {"prompts": []})
            elif method == "logging/setLevel":
                self.result(rid, {})
            else:
                self.error(rid, -32601, f"Method not found: {method}")
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("request failed")
            self.error(rid, -32603, f"internal error: {exc}")

    def _call(self, rid: Any, params: dict[str, Any], cancel: threading.Event) -> None:
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = self.handlers.get(name)
        try:
            if handler is None:
                self.result(rid, {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True})
                return
            out = handler(args, cancel=cancel)
            is_error = isinstance(out, dict) and "error" in out and len(out) <= 3
            # Compact JSON: everything returned here lands in Claude's context.
            text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, separators=(",", ":"), default=str)
            if not cancel.is_set():
                self.result(rid, {"content": [{"type": "text", "text": text}], "isError": bool(is_error)})
        except Exception as exc:
            log.error("tool %s failed: %s", name, traceback.format_exc())
            msg = str(exc) if exc.__class__.__name__ in ("TaskInputError", "OllamaError", "ReadError") \
                else f"{type(exc).__name__}: {exc}"
            self.result(rid, {"content": [{"type": "text", "text": msg}], "isError": True})
        finally:
            self.cancels.pop(rid, None)
