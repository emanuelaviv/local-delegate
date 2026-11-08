"""The stdio server speaks MCP well enough for the official client (which first probes
`server/discover`, then falls back to `initialize`), and starts the way Claude starts it: plain
`python ld.py serve`, with no interpreter options."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import Env

LD = str(Path(__file__).resolve().parents[1] / "server" / "ld.py")

# What the server needs from the environment. The rest of the test's environment isn't passed on.
KEEP = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "LOCALAPPDATA", "LANG", "LC_ALL")


def child_env(**extra: str) -> dict:
    e = {k: v for k, v in os.environ.items() if k in KEEP or k.startswith("LOCAL_DELEGATE_")}
    e.update(extra)
    return e


def start(env: dict, *args: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, *(args or (LD, "serve"))], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)


def ask(proc: subprocess.Popen, obj: dict) -> dict:
    proc.stdin.write((json.dumps(obj) + "\n").encode())
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


def initialize(proc: subprocess.Popen) -> dict:
    r = ask(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
    proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    return r


def finish(proc: subprocess.Popen) -> bytes:
    proc.stdin.close()
    proc.wait(timeout=10)
    err = proc.stderr.read()
    proc.stdout.close()
    proc.stderr.close()
    return err


class RawProtocol(unittest.TestCase):
    def test_handshake_and_errors(self):
        env = Env()
        try:
            proc = start(child_env())
            r = ask(proc, {"jsonrpc": "2.0", "id": 0, "method": "server/discover", "params": {}})
            self.assertEqual(r["error"]["code"], -32601)
            r = initialize(proc)
            self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
            self.assertIn("local-delegate", r["result"]["serverInfo"]["name"])
            r = ask(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            names = [t["name"] for t in r["result"]["tools"]]
            self.assertEqual(names, ["local_status", "local_estimate", "local_run", "local_result", "local_feedback", "local_admin"])
            r = ask(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nope", "arguments": {}}})
            self.assertTrue(r["result"]["isError"])
            r = ask(proc, {"jsonrpc": "2.0", "id": 4, "method": "ping"})
            self.assertEqual(r["result"], {})
            self.assertEqual(finish(proc), b"")  # nothing printed outside the protocol
        finally:
            env.close()


class Startup(unittest.TestCase):
    """Claude starts the server with plain `python`, so Python's own environment variables apply."""

    def test_a_module_on_pythonpath_cannot_replace_a_standard_one(self):
        env = Env()
        decoy = Path(tempfile.mkdtemp(prefix="ldpath-"))
        try:
            (decoy / "json.py").write_text("raise ImportError('the json module on PYTHONPATH was used')\n",
                                           encoding="utf-8")
            proc = start(child_env(PYTHONPATH=str(decoy)))
            self.assertIn("local-delegate", initialize(proc)["result"]["serverInfo"]["name"])
            self.assertEqual(finish(proc), b"")
        finally:
            env.close()
            shutil.rmtree(decoy, ignore_errors=True)

    def test_works_without_python_utf8_mode(self):
        # Windows' default: text files and pipes use the code page, not UTF-8 (on Linux, LC_ALL=C
        # does the same). The server must not depend on Python's UTF-8 mode.
        extra = {"PYTHONUTF8": "0"} if os.name == "nt" else {"PYTHONUTF8": "0", "LC_ALL": "C", "LANG": "C"}
        probe = subprocess.run([sys.executable, "-c", "import locale, sys; print(sys.flags.utf8_mode, "
                                "locale.getpreferredencoding(False))"], capture_output=True, encoding="utf-8",
                               env=child_env(**extra))
        mode, enc = probe.stdout.split()
        if mode != "0" or enc.lower().replace("-", "") == "utf8":
            self.skipTest(f"text here is UTF-8 anyway ({probe.stdout.strip()})")
        env = Env()
        try:
            letter = env.write("lettre.txt", "Objet : travaux de façade\n\nMadame, Monsieur,\n\n"
                                             + "Les travaux débuteront le lundi à 8 h. Merci de libérer "
                                               "les balcons et de fermer les fenêtres côté cour.\n" * 30)
            proc = start(child_env(**extra))
            initialize(proc)
            r = ask(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": "local_estimate",
                                      "arguments": {"task": "translate", "params": {"paths": [str(letter)], "target": "en"}}}})
            self.assertFalse(r["result"].get("isError"), r)
            est = json.loads(r["result"]["content"][0]["text"])
            self.assertIn("options", est)
            self.assertEqual(finish(proc), b"")
        finally:
            env.close()

    def test_an_old_python_gets_a_clear_message(self):
        proc = subprocess.run([sys.executable, "-c", "import runpy, sys; sys.version_info = (3, 9, 18, 'final', 0); "
                               "runpy.run_path(sys.argv[1], run_name='__main__')", LD, "serve"],
                              capture_output=True, encoding="utf-8", timeout=30, env=child_env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("needs Python 3.11 or newer, but this is Python 3.9.18", proc.stderr)
        self.assertEqual(proc.stdout, "")


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
                params = StdioServerParameters(command=sys.executable, args=[LD, "serve"], env=child_env())
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
