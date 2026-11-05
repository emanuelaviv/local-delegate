import io
import json
import unittest
import zipfile

from helpers import Env
from local_delegate import config, guard, textio, verify
from local_delegate.ollama import Ollama, OllamaError, parse_loopback_url


class TestLoopback(unittest.TestCase):
    def test_urls(self):
        self.assertTrue(parse_loopback_url("http://127.0.0.1:11434")[0])
        self.assertTrue(parse_loopback_url("http://localhost:11434")[0])
        self.assertTrue(parse_loopback_url("http://[::1]:11434")[0])
        self.assertFalse(parse_loopback_url("http://0.0.0.0:11434")[0])
        self.assertFalse(parse_loopback_url("https://127.0.0.1:11434")[0])
        self.assertFalse(parse_loopback_url("http://ollama.com")[0])
        with self.assertRaises(OllamaError):
            Ollama("http://192.168.1.20:11434")

    def test_cloud_names(self):
        self.assertTrue(guard.model_is_cloud_name("gpt-oss:120b-cloud"))
        self.assertTrue(guard.model_is_cloud_name("gemma4:cloud"))
        self.assertFalse(guard.model_is_cloud_name("qwen3.8:27b-q4_K_M"))


class TestGuardReport(unittest.TestCase):
    def test_ok_with_log_proof(self):
        env = Env()
        try:
            rep = guard.report(config.load(), refresh=True)
            by = {c["name"]: c["ok"] for c in rep["checks"]}
            self.assertTrue(rep["ok"], rep["problems"])
            self.assertTrue(by["cloud_disabled_now"])
            self.assertTrue(by["listen_loopback_only"])  # the fake listens on 127.0.0.1
            self.assertTrue(by["model_is_local"])
        finally:
            env.close()

    def test_blocks_when_cloud_on_or_unproven(self):
        for log_value in ("false", None):
            env = Env(cloud_log=log_value)
            try:
                rep = guard.report(config.load(), refresh=True)
                self.assertFalse(rep["ok"], log_value)
                self.assertTrue(any("cloud_disabled_now" in p for p in rep["problems"]))
            finally:
                env.close()

    def test_server_json_counts_only_if_set_before_start(self):
        env = Env(cloud_log=None)
        try:
            (env.tmp / "ollama" / "server.json").write_text(json.dumps({"disable_ollama_cloud": True}))
            rep = guard.report(config.load(), refresh=True)
            by = {c["name"]: c["ok"] for c in rep["checks"]}
            self.assertTrue(by["cloud_disabled_config"])
            # the fake "server" (this test process) started before server.json was written: not proven
            self.assertIsNot(by["cloud_disabled_now"], True)
        finally:
            env.close()

    def test_cloud_model_refused(self):
        env = Env(config_extra={"model": "gpt-oss:120b-cloud"})
        try:
            rep = guard.report(config.load(), refresh=True)
            self.assertFalse(rep["ok"])
            self.assertFalse({c["name"]: c["ok"] for c in rep["checks"]}["model_is_local"])
        finally:
            env.close()


class TestPaths(unittest.TestCase):
    def test_policy(self):
        env = Env()
        try:
            cfg = config.load()
            ok_file = env.write("notes/report.md", "hi")
            secret = env.write("app/.env", "KEY=1")
            ssh = env.write(".ssh/id_ed25519", "x")
            self.assertTrue(guard.check_path(str(ok_file), cfg)[0])
            self.assertFalse(guard.check_path(str(secret), cfg)[0])
            self.assertFalse(guard.check_path(str(ssh), cfg)[0])
            self.assertFalse(guard.check_path("/etc/hostname", cfg)[0])
            self.assertFalse(guard.check_path(str(env.work / "missing.txt"), cfg)[0])
        finally:
            env.close()

    def test_redaction(self):
        text = ("OPENROUTER_API_KEY=sk-or-v1-" + "a1" * 20 + "\n"
                "anon: " + ".".join(["eyJhbGciOiJIUzI1NiJ9", "eyJyb2xlIjoiYW5vbiJ9", "abcdefghijklmnop"]) + "\n"
                "password = hunter2hunter2X9\n"
                "-----BEGIN " + "PRIVATE KEY-----\nMIIE\n-----END " + "PRIVATE KEY-----\n"
                "token_count = 12345678\n")
        out, counts = guard.redact(text)
        self.assertNotIn("a1a1a1", out)
        self.assertNotIn("hunter2", out)
        self.assertNotIn("MIIE", out)
        self.assertIn("token_count = 12345678", out)  # numbers alone are not secrets
        self.assertEqual(counts.get("private_key"), 1)
        self.assertEqual(counts.get("jwt"), 1)


class TestText(unittest.TestCase):
    def test_decoding(self):
        self.assertEqual(textio.decode_bytes("héllo".encode("utf-16")), "héllo")
        self.assertEqual(textio.decode_bytes(b"\xef\xbb\xbfabc"), "abc")
        self.assertEqual(textio.decode_bytes("abc".encode("utf-16-le")), "abc")
        self.assertEqual(textio.decode_bytes("café".encode("cp1252")), "café")

    def test_docx(self):
        env = Env()
        try:
            buf = io.BytesIO()
            xml = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                   '<w:p><w:r><w:t>Dear Sam,</w:t></w:r></w:p><w:p><w:r><w:t>Please reply by</w:t></w:r>'
                   '<w:r><w:t xml:space="preserve"> Friday.</w:t></w:r></w:p></w:body></w:document>')
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("word/document.xml", xml)
            p = env.work / "letter.docx"
            p.write_bytes(buf.getvalue())
            self.assertEqual(textio.read_text(p), "Dear Sam,\nPlease reply by Friday.")
        finally:
            env.close()

    def test_kinds_and_tokens(self):
        self.assertEqual(textio.content_kind("שלום לכולם, זה מכתב קצר בעברית"), "he")
        self.assertEqual(textio.content_kind("def f(x):\n    return {x: [1, 2]}  # ok", ".py"), "code")
        self.assertEqual(textio.content_kind("Le rapport de la semaine est prêt pour la réunion avec vous."), "fr")
        self.assertGreater(textio.estimate_tokens("a" * 380), 90)


class TestVerify(unittest.TestCase):
    def test_cites(self):
        docs = {"D1": ["first line", "Status: DONE, waiting for approval", "third", "4", "5", "6", "seventh line here"]}
        obj = {"a": {"doc": "D1", "line": 3, "quote": "waiting for approval"},
               "b": {"doc": "D1", "line": 1, "quote": "seventh line"},
               "c": {"doc": "D1", "line": 2, "quote": "Status: DONE, waitng for aproval"},
               "d": {"doc": "D1", "line": 1, "quote": "never said this"}}
        res = verify.verify_cites(obj, docs)
        self.assertNotIn("check", obj["a"])  # verified quotes carry no mark
        self.assertEqual(obj["a"]["line"], 2)  # corrected to the exact line
        self.assertEqual(obj["b"]["check"], "relocated")
        self.assertEqual(obj["b"]["line"], 7)
        self.assertEqual(obj["c"]["check"], "close")
        self.assertEqual(obj["d"]["check"], "unverified")
        self.assertEqual(res["confirmed"], 3)

    def test_schema(self):
        schema = {"type": "object", "required": ["x"], "properties": {"x": {"type": "array", "items": {"type": "integer"}},
                                                                     "y": {"type": "string", "enum": ["a"]}}}
        self.assertEqual(verify.validate({"x": [1, 2]}, schema), [])
        self.assertTrue(verify.validate({"x": ["1"], "y": "b"}, schema))
        self.assertTrue(verify.validate({}, schema))


if __name__ == "__main__":
    unittest.main()
