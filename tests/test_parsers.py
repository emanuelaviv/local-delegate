import unittest

from helpers import GRADLE_LOG, PYTEST_LOG  # noqa: F401  (sets sys.path)
from local_delegate import parsers


def doc(text, name="log.txt", ext=".txt"):
    return {"text": text, "name": name, "ext": ext}


class TestLogs(unittest.TestCase):
    def test_pytest(self):
        f = parsers.parse_test_logs([doc(PYTEST_LOG)])
        self.assertEqual(f["totals"], {"passed": 10, "failed": 2, "errors": 0, "skipped": 1})
        names = [x["test"] for x in f["failing"]]
        self.assertIn("tests/test_engine.py::test_theme_calibration", names)
        self.assertEqual(f["verdict_from_counts"], "failures")
        self.assertTrue(f["first_errors"])
        first = f["first_errors"][0]
        self.assertTrue(any("AssertionError" in t for _, t in first["lines"]))

    def test_gradle_cached_vs_executed(self):
        f = parsers.parse_test_logs([doc(GRADLE_LOG)])
        s = f["suites"][0]
        self.assertEqual(s["runner"], "gradle")
        self.assertEqual((s["completed"], s["failed"], s["passed"]), (5, 1, 4))
        self.assertEqual(s["build"], "failed")
        self.assertEqual(s["tasks"]["executed"], 4)
        self.assertEqual(s["tasks"]["from_cache"], 2)
        self.assertIn(":app:testDebugUnitTest", f["tests_not_executed_this_run"])
        self.assertIn("com.example.core.PlanBuilderTest > buildsSummary", [x["test"] for x in f["failing"]])

    def test_deno_jest_node(self):
        deno = "running 4 tests\nplan_test => ./backend/functions/plan_test.ts:12:6\nFAILED | 3 passed | 1 failed (56ms)\n"
        jest = "Tests:       1 failed, 9 passed, 10 total\n"
        node = "✖ rest timer (1.2ms)\nℹ tests 5\nℹ pass 4\nℹ fail 1\nℹ skipped 0\n"
        f = parsers.parse_test_logs([doc(deno, "d.txt"), doc(jest, "j.txt"), doc(node, "n.txt")])
        runners = {s["runner"]: s for s in f["suites"]}
        self.assertEqual((runners["deno"]["passed"], runners["deno"]["failed"]), (3, 1))
        self.assertEqual((runners["jest"]["passed"], runners["jest"]["failed"]), (9, 1))
        self.assertEqual((runners["node"]["passed"], runners["node"]["failed"]), (4, 1))
        self.assertEqual(f["totals"]["failed"], 3)

    def test_junit(self):
        xml = """<?xml version="1.0"?><testsuite name="PlanTest" tests="3" failures="1" errors="0" skipped="1" time="0.4">
        <testcase classname="com.example.PlanTest" name="a"/>
        <testcase classname="com.example.PlanTest" name="b"><failure message="expected 3 but was 2">trace</failure></testcase>
        <testcase classname="com.example.PlanTest" name="c"><skipped/></testcase></testsuite>"""
        f = parsers.parse_test_logs([doc(xml, "TEST-PlanTest.xml", ".xml")])
        self.assertEqual(f["totals"], {"passed": 1, "failed": 1, "errors": 0, "skipped": 1})
        self.assertEqual(f["failing"][0]["test"], "com.example.PlanTest.b")

    def test_summary_lines_are_not_first_errors(self):
        f = parsers.parse_test_logs([doc("all good\nBUILD FAILED in 3s\n")])
        self.assertEqual(f["first_errors"], [])


class TestFacts(unittest.TestCase):
    def test_dates(self):
        text = ("Reply by October 30, 2026.\nRendez-vous le 3 novembre 2026 à 9h.\nISO 2026-11-15 deadline\n"
                "הפגישה ב-12 בנובמבר 2026\nPay $45.00 or 45,00 $ CAD")
        f = parsers.letter_facts(text)
        found = {d["text"] for d in f["dates"]}
        self.assertTrue({"October 30, 2026", "3 novembre 2026", "2026-11-15"} <= found, found)
        self.assertTrue(any("בנובמבר" in d for d in found), found)
        self.assertTrue(f["amounts"])

    def test_paths_and_lists(self):
        text = "Changed `app/src/Main.kt` and backend\\migrations\\001.sql; see https://x.org/a/b.html"
        paths = [p["path"] for p in parsers.paths_in_text(text)]
        self.assertEqual(paths, ["app/src/Main.kt", "backend/migrations/001.sql"])
        cmp = parsers.compare_lists({"report": paths, "commit": ["app/src/main.kt", "docs/x.md"]})
        pair = cmp["pairs"][0]
        self.assertEqual(pair["only_in_a"], ["backend/migrations/001.sql"])
        self.assertEqual(pair["only_in_b"], ["docs/x.md"])

    def test_manifest_formats(self):
        h = "a" * 64
        self.assertEqual(parsers.parse_manifest(f"{h}  figs/a.webp\n", ".txt"), {"figs/a.webp": h})
        self.assertEqual(parsers.parse_manifest('{"figs/a.webp": "%s"}' % h, ".json"), {"figs/a.webp": h})
        self.assertEqual(parsers.parse_manifest(f"path,sha256\nfigs/a.webp,{h}\n", ".csv"), {"figs/a.webp": h})

    def test_bm25(self):
        chunks = ["the rest timer counts down between sets", "inventory rows are loaded from csv",
                  "theme is chosen by the app not the user"]
        bm = parsers.BM25([parsers.tokens(c) for c in chunks])
        s = bm.scores(parsers.tokens("who chooses the screen theme?"))
        self.assertEqual(max(range(3), key=lambda i: s[i]), 2)
        self.assertIn("theme", parsers.tokens("restTimer theme_value"))


if __name__ == "__main__":
    unittest.main()
