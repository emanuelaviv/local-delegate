"""A stand-in for Ollama's HTTP API, for tests.

It simulates load time, prompt reading (prefill) and token-by-token writing (decode),
prompt-prefix caching, context-size reloads, the memory report of /api/ps, and the
thinking switch. Durations are reported in simulated nanoseconds; real sleeping is
scaled down by `time_scale` so tests stay fast.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable


def minimal(schema: dict[str, Any]) -> Any:
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "null")
    if "enum" in schema:
        return schema["enum"][0]
    if t == "object":
        props = schema.get("properties") or {}
        return {k: minimal(props[k]) for k in schema.get("required", []) if k in props}
    if t == "array":
        return []
    if t == "string":
        return "x"
    if t in ("integer", "number"):
        return 1
    if t == "boolean":
        return False
    return None


class FakeOllama:
    def __init__(self, model: str = "qwen3.8:27b-q4_K_M", load_s: float = 20.0, reload_s: float = 8.0,
                 prefill_tps: float = 40.0, decode_tps: float = 4.0, time_scale: float = 0.001,
                 base_bytes: float = 18.6e9, kv_per_token: float = 65536.0, vram_fraction: float = 0.0,
                 thinking_tokens: int = 40, chars_per_token: float = 3.7,
                 think_values: list | None = None, version: str = "0.40.8"):
        self.model = model
        self.load_s, self.reload_s = load_s, reload_s
        self.prefill_tps, self.decode_tps = prefill_tps, decode_tps
        self.time_scale = time_scale
        self.base_bytes, self.kv = base_bytes, kv_per_token
        self.vram_fraction = vram_fraction
        self.thinking_tokens = thinking_tokens
        self.cpt = chars_per_token
        self.think_values = think_values if think_values is not None else ["none", "low", "medium", "xhigh"]
        self.level_tokens = {"low": thinking_tokens, "medium": thinking_tokens * 2, "xhigh": thinking_tokens * 5,
                             True: thinking_tokens * 5}
        self.loop_thinking = False
        self.version = version
        self.loaded_ctx: int | None = None
        self.last_prompt = ""
        self.requests: list[dict[str, Any]] = []
        self.responder: Callable[[dict[str, Any]], Any] | None = None
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a: Any) -> None:
                pass

            def _send(self, code: int, obj: Any) -> None:
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path == "/api/version":
                    self._send(200, {"version": fake.version})
                elif self.path == "/api/tags":
                    self._send(200, {"models": [
                        {"name": fake.model, "model": fake.model, "size": 17_900_000_000, "digest": "d1g3st"},
                        {"name": "gpt-oss:120b-cloud", "model": "gpt-oss:120b-cloud", "size": 384,
                         "remote_host": "https://ollama.com:443", "remote_model": "gpt-oss:120b"}]})
                elif self.path == "/api/ps":
                    models = []
                    if fake.loaded_ctx:
                        size = int(fake.base_bytes + fake.kv * fake.loaded_ctx)
                        models.append({"name": fake.model, "model": fake.model, "size": size,
                                       "size_vram": int(size * fake.vram_fraction), "context_length": fake.loaded_ctx,
                                       "expires_at": "2026-10-22T12:00:00Z"})
                    self._send(200, {"models": models})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                fake.requests.append({"path": self.path, **req})
                if req.get("model") and req["model"] != fake.model:
                    self._send(404, {"error": f"model '{req['model']}' not found"})
                    return
                if self.path == "/api/show":
                    self._send(200, {
                        "capabilities": ["completion", "thinking", "vision", "tools"],
                        "thinking": {"values": fake.think_values, "default": "xhigh"},
                        "details": {"family": "qwen35", "parameter_size": "27.3B", "quantization_level": "Q4_K_M"},
                        "model_info": {"general.architecture": "qwen35", "qwen35.context_length": 262144,
                                       "qwen35.block_count": 64, "qwen35.attention.head_count_kv": 4}})
                elif self.path == "/api/generate":
                    if req.get("keep_alive") in (0, "0"):
                        fake.loaded_ctx = None
                    self._send(200, {"model": fake.model, "done": True, "done_reason": "unload"})
                elif self.path == "/api/chat":
                    fake._chat(self, req)
                else:
                    self._send(404, {"error": "not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # ------------------------------------------------------------------ chat simulation
    def _chat(self, h: BaseHTTPRequestHandler, req: dict[str, Any]) -> None:
        opts = req.get("options") or {}
        ctx = int(opts.get("num_ctx") or 4096)
        prompt = "\n".join(m.get("content", "") for m in req.get("messages", []))
        with self.lock:
            load = 0.0
            if self.loaded_ctx is None:
                load = self.load_s
            elif self.loaded_ctx != ctx:
                load = self.reload_s
            cached_chars = 0
            if load == 0.0:
                for a, b in zip(self.last_prompt, prompt):
                    if a != b:
                        break
                    cached_chars += 1
            self.loaded_ctx = ctx
            self.last_prompt = prompt
        if req.get("keep_alive") in (0, "0"):
            self.loaded_ctx = None
        n_prompt = int(len(prompt) / self.cpt) + 20
        n_cached = min(n_prompt - 1, int(cached_chars / self.cpt)) if cached_chars else 0
        prefill = (n_prompt - n_cached) / self.prefill_tps
        # content
        if self.responder:
            out = self.responder(req)
        elif isinstance(req.get("format"), dict):
            out = minimal(req["format"])
        else:
            out = "This is a short local answer for testing purposes."
        text = out if isinstance(out, str) else json.dumps(out)
        pieces = [text[i:i + 4] for i in range(0, len(text), 4)] or [""]
        cap = int(opts.get("num_predict") or 100000)
        think = req.get("think")
        if think is None:   # field left out: the model's default level (xhigh for Qwen3.8)
            think = "xhigh" if self.think_values and "xhigh" in self.think_values else True
        n_think = 0 if think in (False, "none") else self.level_tokens.get(think, self.thinking_tokens)
        if self.loop_thinking and n_think:
            thinking = ["Let me check the dates again. "] * 100000
        else:
            thinking = [f"step {i} " for i in range(n_think)]
        stream = req.get("stream", True)
        done_reason = "stop"
        budget = cap
        th = thinking[:budget]
        budget -= len(th)
        content = pieces[:max(0, budget)]
        if len(th) + len(pieces) > cap:
            done_reason = "length"
        n_out = len(th) + len(content)
        decode = n_out / self.decode_tps
        stats = {"done": True, "done_reason": done_reason, "total_duration": int((load + prefill + decode) * 1e9),
                 "load_duration": int(load * 1e9), "prompt_eval_count": n_prompt, "prompt_eval_cached_count": n_cached,
                 "prompt_eval_duration": int(prefill * 1e9), "eval_count": n_out, "eval_duration": int(decode * 1e9)}
        if not stream:
            msg = {"role": "assistant", "content": "".join(content)}
            if th:
                msg["thinking"] = "".join(th)
            h._send(200, {"model": self.model, "message": msg, **stats})
            return
        h.send_response(200)
        h.send_header("Content-Type", "application/x-ndjson")
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()

        def chunk(obj: dict[str, Any]) -> None:
            data = (json.dumps(obj) + "\n").encode()
            h.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            h.wfile.flush()

        try:
            time.sleep((load + prefill) * self.time_scale)
            for t in th:
                time.sleep(self.time_scale / self.decode_tps)
                chunk({"model": self.model, "message": {"role": "assistant", "content": "", "thinking": t}, "done": False})
            for c in content:
                time.sleep(self.time_scale / self.decode_tps)
                chunk({"model": self.model, "message": {"role": "assistant", "content": c}, "done": False})
            chunk({"model": self.model, "message": {"role": "assistant", "content": ""}, **stats})
            h.wfile.write(b"0\r\n\r\n")
            h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
