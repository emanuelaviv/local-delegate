"""The stdio server speaks MCP well enough for the official client (which first probes
`server/discover`, then falls back to `initialize`)."""

import asyncio
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from helpers import Env

LD = str(Path(__file__).resolve().parents[1] / "server" / "ld.py")


def child_env(env: Env) -> dict:
    e = dict(os.environ)
    for k in ("LOCAL_DELEGATE_HOME", "LOCAL_DELEGATE_OLLAMA_HOME", "LOCAL_DELEGATE_OLLAMA_LOG"):
        e[k] = os.environ[k]
    return e


class RawProtocol(unittest.TestCase):
    def test_handshake_and_errors(self):
        env = Env()
        try:
            proc = subprocess.Popen([sys.executable, "-I", "-X", "utf8", LD, "serve"], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=child_env(env))

            def ask(obj):
                proc.stdin.write((json.dumps(obj) + "\n").encode())
                proc.stdin.flush()
                return json.loads(proc.stdout.readline())

            r = ask({"jsonrpc": "2.0", "id": 0, "method": "server/discover", "params": {}})
            self.assertEqual(r["error"]["code"], -32601)
            r = ask({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
            self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
            self.assertIn("local-delegate", r["result"]["serverInfo"]["name"])
            proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            r = ask({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            names = [t["name"] for t in r["result"]["tools"]]
            self.assertEqual(names, ["local_status", "local_estimate", "local_run", "local_result", "local_feedback", "local_admin"])
            r = ask({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nope", "arguments": {}}})
            self.assertTrue(r["result"]["isError"])
            r = ask({"jsonrpc": "2.0", "id": 4, "method": "ping"})
            self.assertEqual(r["result"], {})
            proc.stdin.close()
            proc.wait(timeout=10)
            self.assertEqual(proc.stderr.read(), b"")  # nothing printed outside the protocol
        finally:
            env.close()


class OfficialClient(unittest.TestCase):
    def test_sdk_client_end_to_end(self):
        try:
            from mcp import StdioServerParameters
            from mcp.client.client import Client
        except ImportError:
            self.skipTest("mcp SDK not installed")
        env = Env()
        try:
            report = env.write("r.md", "Status: done\n" * 5)

            async def go():
                params = StdioServerParameters(command=sys.executable, args=["-I", "-X", "utf8", LD, "serve"],
                                               env=child_env(env))
                async with Client(params, mode="auto") as client:
                    tools = await client.list_tools()
                    status = await client.call_tool("local_status", {})
                    est = await client.call_tool("local_estimate", {"task": "digest", "params": {"paths": [str(report)]}})
                    return client.protocol_version, tools, status, est

            version, tools, status, est = asyncio.run(go())
            self.assertIn(version, ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"))
            self.assertEqual(len(tools.tools), 6)
            st = json.loads(status.content[0].text)
            self.assertTrue(st["local_only"]["ok"], st)
            e = json.loads(est.content[0].text)
            self.assertEqual(e["action"], "do_here")   # a five-line report is quicker to read here
        finally:
            env.close()


if __name__ == "__main__":
    unittest.main()
