"""Loopback-only Ollama client.

* Refuses any URL that is not 127.0.0.1 / ::1 / localhost (resolving to loopback).
* Uses http.client and raw sockets, which never consult proxy settings, so requests
  cannot be routed off the machine by an HTTP(S)_PROXY variable.
* Streams /api/chat so a job can be watched, cut off at its approved time budget,
  or cancelled; closing the connection makes Ollama stop generating.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

STAT_KEYS = ("total_duration", "load_duration", "prompt_eval_count", "prompt_eval_cached_count",
             "prompt_eval_duration", "eval_count", "eval_duration", "done_reason")


class OllamaError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def parse_loopback_url(url: str) -> tuple[bool, str, int, str]:
    """(ok, host, port, detail). Only plain-HTTP loopback URLs pass."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "", 0, f"unparseable URL {url!r}"
    if parsed.scheme != "http":
        return False, "", 0, f"{url}: only http://127.0.0.1 (loopback) is allowed"
    host = parsed.hostname or ""
    port = parsed.port or 11434
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        addrs = {info[4][0] for info in infos}
    except socket.gaierror:
        return False, host, port, f"cannot resolve {host!r}"
    bad = [a for a in addrs if not ipaddress.ip_address(a.split("%")[0]).is_loopback]
    if bad or not addrs:
        return False, host, port, f"{host} resolves to non-loopback address(es) {sorted(bad)}; refusing"
    connect_host = "127.0.0.1" if host == "localhost" and "127.0.0.1" in addrs else host
    return True, connect_host, port, f"http://{host}:{port} (loopback)"


@dataclass
class ChatResult:
    content: str = ""
    thinking: str = ""
    stats: dict[str, Any] = field(default_factory=dict)
    aborted: bool = False
    abort_reason: str | None = None
    wall_s: float = 0.0
    first_token_s: float | None = None
    tokens_streamed: int = 0
    thinking_tokens: int = 0
    answer_tokens: int = 0

    @property
    def done_reason(self) -> str | None:
        return self.stats.get("done_reason")


class _Abort(Exception):
    pass


class _Reader:
    """Minimal HTTP/1.1 response reader over a socket with deadline + cancel checks."""

    def __init__(self, sock: socket.socket, cancel: threading.Event | None, deadline: Callable[[], float]):
        self.sock = sock
        self.cancel = cancel
        self.deadline = deadline
        self.buf = b""
        self.eof = False

    def _fill(self) -> None:
        while True:
            if self.cancel is not None and self.cancel.is_set():
                raise _Abort("cancelled")
            remaining = self.deadline() - time.monotonic()
            if remaining <= 0:
                raise _Abort("deadline")
            self.sock.settimeout(max(0.05, min(0.5, remaining)))
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                self.eof = True
                return
            self.buf += chunk
            return

    def line(self, sep: bytes = b"\r\n") -> bytes:
        while sep not in self.buf:
            if self.eof:
                out, self.buf = self.buf, b""
                return out
            self._fill()
        out, self.buf = self.buf.split(sep, 1)
        return out

    def exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            if self.eof:
                raise OllamaError("connection closed mid-response")
            self._fill()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def rest(self) -> bytes:
        while not self.eof:
            self._fill()
        out, self.buf = self.buf, b""
        return out


class Ollama:
    def __init__(self, url: str = "http://127.0.0.1:11434"):
        ok, host, port, detail = parse_loopback_url(url)
        if not ok:
            raise OllamaError(detail)
        self.url = url
        self.host = host
        self.port = port

    # ------------------------------------------------------------ simple JSON calls
    def _json(self, method: str, path: str, body: Any = None, timeout: float = 15.0) -> Any:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            data = json.dumps(body).encode("utf-8") if body is not None else None
            headers = {"Content-Type": "application/json"} if data is not None else {}
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status >= 400:
                msg = raw.decode("utf-8", "replace")[:400]
                try:
                    msg = json.loads(raw).get("error", msg)
                except Exception:
                    pass
                raise OllamaError(f"{method} {path}: HTTP {resp.status}: {msg}", resp.status)
            return json.loads(raw.decode("utf-8") or "{}")
        except OllamaError:
            raise
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
            raise OllamaError(f"Ollama is not reachable at {self.host}:{self.port} ({exc})") from exc
        finally:
            conn.close()

    def version(self) -> str | None:
        return self._json("GET", "/api/version", timeout=5).get("version")

    def tags(self) -> list[dict[str, Any]]:
        return self._json("GET", "/api/tags").get("models", []) or []

    def ps(self) -> list[dict[str, Any]]:
        return self._json("GET", "/api/ps", timeout=5).get("models", []) or []

    def show(self, model: str) -> dict[str, Any]:
        return self._json("POST", "/api/show", {"model": model}, timeout=30)

    def unload(self, model: str) -> None:
        self._json("POST", "/api/generate", {"model": model, "keep_alive": 0}, timeout=60)

    def loaded(self, model: str) -> dict[str, Any] | None:
        for m in self.ps():
            if m.get("name") == model or m.get("model") == model:
                return m
        return None

    # ------------------------------------------------------------ streaming chat
    def chat(self, model: str, messages: list[dict[str, Any]], *, options: dict[str, Any] | None = None,
             fmt: Any = None, think: Any = None, keep_alive: Any = None, budget_s: float | None = None,
             first_token_timeout_s: float | None = None, stall_timeout_s: float = 180.0,
             cancel: threading.Event | None = None, loop_guard: bool = True,
             max_thinking_tokens: int | None = None,
             on_progress: Callable[[dict[str, Any]], None] | None = None) -> ChatResult:
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        if options:
            payload["options"] = options
        if fmt is not None:
            payload["format"] = fmt
        if think is not None:
            payload["think"] = think
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive
        body = json.dumps(payload).encode("utf-8")

        start = time.monotonic()
        hard = start + budget_s if budget_s else float("inf")
        first_deadline = start + first_token_timeout_s if first_token_timeout_s else float("inf")
        state = {"first": None, "last": start}

        def deadline() -> float:
            if state["first"] is None:
                return min(hard, first_deadline)
            return min(hard, state["last"] + stall_timeout_s)

        result = ChatResult()
        content: list[str] = []
        thinking: list[str] = []
        tail_c = tail_t = ""
        last_report = 0.0
        try:
            sock = socket.create_connection((self.host, self.port), timeout=10)
        except OSError as exc:
            raise OllamaError(f"Ollama is not reachable at {self.host}:{self.port} ({exc})") from exc
        try:
            head = (f"POST /api/chat HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
                    "Content-Type: application/json\r\nAccept: application/x-ndjson\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode("ascii")
            sock.sendall(head + body)
            rd = _Reader(sock, cancel, deadline)
            status_line = rd.line().decode("latin-1")
            parts = status_line.split(" ", 2)
            status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            headers: dict[str, str] = {}
            while True:
                h = rd.line().decode("latin-1")
                if not h:
                    break
                k, _, v = h.partition(":")
                headers[k.strip().lower()] = v.strip()
            chunked = "chunked" in headers.get("transfer-encoding", "").lower()
            length = int(headers["content-length"]) if "content-length" in headers else None

            if status >= 400:
                raw = rd.exact(length) if length is not None else rd.rest()
                msg = raw.decode("utf-8", "replace")
                try:
                    msg = json.loads(msg).get("error", msg)
                except Exception:
                    pass
                raise OllamaError(f"/api/chat: HTTP {status}: {msg[:400]}", status)

            for raw_line in self._body_lines(rd, chunked, length):
                if not raw_line.strip():
                    continue
                obj = json.loads(raw_line.decode("utf-8"))
                if obj.get("error"):
                    raise OllamaError(f"Ollama error: {obj['error']}")
                msg = obj.get("message") or {}
                piece_c = msg.get("content") or ""
                piece_t = msg.get("thinking") or ""
                now = time.monotonic()
                if piece_c or piece_t:
                    if state["first"] is None:
                        state["first"] = now
                        result.first_token_s = now - start
                    state["last"] = now
                    result.tokens_streamed += 1
                    if piece_c:
                        content.append(piece_c)
                        result.answer_tokens += 1
                        tail_c = (tail_c + piece_c)[-4000:]
                    if piece_t:
                        thinking.append(piece_t)
                        result.thinking_tokens += 1
                        tail_t = (tail_t + piece_t)[-4000:]
                        if max_thinking_tokens and result.thinking_tokens > max_thinking_tokens:
                            raise _Abort("think_budget")
                    if loop_guard and result.tokens_streamed % 48 == 0:
                        if _looping(tail_t) or _looping(tail_c):
                            raise _Abort("loop")
                    if on_progress and now - last_report > 0.5:
                        last_report = now
                        on_progress({"phase": "thinking" if piece_t else "writing", "tokens_out": result.tokens_streamed,
                                     "thinking_tokens": result.thinking_tokens, "answer_tokens": result.answer_tokens,
                                     "elapsed_s": round(now - start, 1)})
                if obj.get("done"):
                    result.stats = {k: obj.get(k) for k in STAT_KEYS if obj.get(k) is not None}
                    break
        except _Abort as ab:
            result.aborted = True
            if str(ab) in ("cancelled", "loop", "think_budget"):
                result.abort_reason = str(ab)
            elif time.monotonic() >= hard:
                result.abort_reason = "time_budget"
            elif state["first"] is None:
                result.abort_reason = "no_output_before_deadline"
            else:
                result.abort_reason = "stalled"
        finally:
            try:
                sock.close()
            except OSError:
                pass
        if not result.aborted and not result.stats:
            result.aborted = True
            result.abort_reason = "incomplete_response"
        result.content = "".join(content)
        result.thinking = "".join(thinking)
        result.wall_s = time.monotonic() - start
        return result

    @staticmethod
    def _body_lines(rd: _Reader, chunked: bool, length: int | None):
        pending = b""
        if chunked:
            while True:
                size_line = rd.line()
                if not size_line and rd.eof:
                    break
                try:
                    size = int(size_line.split(b";")[0].strip() or b"0", 16)
                except ValueError:
                    raise OllamaError(f"bad chunk header {size_line[:40]!r}")
                if size == 0:
                    break
                pending += rd.exact(size)
                rd.exact(2)
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    yield line
        else:
            data = rd.exact(length) if length is not None else rd.rest()
            pending = data
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                yield line
        if pending.strip():
            yield pending


def _looping(tail: str, probe: int = 120, repeats: int = 4) -> bool:
    """True when the last `probe` characters already occurred `repeats` times in the recent text:
    the model is going round in circles (a known failure of long reasoning)."""
    if len(tail) < probe * repeats:
        return False
    needle = tail[-probe:]
    if len(set(needle)) < 4:      # long runs of one character (e.g. spaces) are not a loop
        return False
    return tail.count(needle) >= repeats


LEVEL_ORDER = ["minimal", "low", "medium", "high", "xhigh", "max"]


def think_levels(show: dict[str, Any]) -> list[Any]:
    th = show.get("thinking") if isinstance(show, dict) else None
    if isinstance(th, dict):
        return list(th.get("values") or [])
    return [False, True] if "thinking" in ((show or {}).get("capabilities") or []) else []


def think_value(level: str, show: dict[str, Any]) -> Any:
    """Map one of our levels ('off', 'low', 'medium', 'high', ...) onto a value this model accepts
    for `think`. Named levels are matched exactly or to the nearest one; models with only an on/off
    switch get True. None means: leave the field out (the model has no reasoning switch)."""
    if level in (None, "off", "none", False):
        return think_off_value(show)
    values = think_levels(show)
    named = [v for v in values if isinstance(v, str)]
    if named:
        if level in named:
            return level
        known = [v for v in LEVEL_ORDER if v in named]
        if known:
            want = LEVEL_ORDER.index(level) if level in LEVEL_ORDER else 2
            return min(known, key=lambda v: (abs(LEVEL_ORDER.index(v) - want), LEVEL_ORDER.index(v)))
        others = [v for v in named if v not in ("none", "off")]
        return others[0] if others else None
    if True in values:
        return True
    return None


def think_off_value(show: dict[str, Any]) -> Any:
    """The `think` value that turns reasoning off for this model, or None to omit the field.

    Uses /api/show's `thinking.values` when present (false, or the lowest named level)."""
    th = show.get("thinking") if isinstance(show, dict) else None
    if isinstance(th, dict):
        values = th.get("values") or []
        if False in values:
            return False
        for name in ("none", "off", "minimal", "low"):
            if name in values:
                return name
        return None
    caps = (show or {}).get("capabilities") or []
    return False if "thinking" in caps else None


def model_max_context(show: dict[str, Any]) -> int | None:
    info = (show or {}).get("model_info") or {}
    for k, v in info.items():
        if k.endswith(".context_length") and isinstance(v, (int, float)) and not k.startswith(("vision", "audio")):
            return int(v)
    return None
