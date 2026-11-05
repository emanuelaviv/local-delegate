"""What the router recommends for typical jobs, per priority preset, on three kinds of hardware.

Run: py -3 tests/scenarios.py   (synthetic calibration profiles; no model runs)
"""

import random

from helpers import GRADLE_LOG, PYTEST_LOG, SAMPLE_REPORT, Env
from local_delegate import calibrate, config, server

PROFILES = {
    "CPU only": dict(a=1 / 35, g=1 / 3.2, load=25, reload=12, vram=0.0),
    "8 GB GPU + 32 GB (reference)": dict(a=1 / 300, g=1 / 4.2, load=38, reload=22, vram=0.29),
    "24 GB GPU": dict(a=1 / 1500, g=1 / 34, load=6, reload=3, vram=1.0),
}


def profile(model, p):
    return {"token_ratio": {"prose": 3.7, "code": 3.2, "fr": 3.4, "he": 2.3},
            "models": {model: {"load_s": p["load"], "reload_s": p["reload"],
                               "prefill": {"a": p["a"], "b": 0.0, "tps_4k": 1 / p["a"]},
                               "decode": {"g": p["g"], "h": 0.0},
                               "memory": {"base_bytes": 18.6e9, "kv_bytes_per_token": 66000, "vram_fraction": p["vram"]},
                               "prefix_cache_ratio": 0.9, "think": {"off_value": "none"},
                               "limits": {"model_max_ctx": 262144, "default_ctx": 16384}}}}


def words(n, seed):
    rng = random.Random(seed)
    vocab = "plan theme inventory screen rest timer migration test report screen user level summary form data".split()
    return " ".join(rng.choice(vocab) for _ in range(n))


def main():
    env = Env()
    w = env.write
    report = w("reports/agent_report.md", SAMPLE_REPORT + "\n".join(f"- step {i}: " + words(40, i) for i in range(260)))
    log = w("logs/build.log", GRADLE_LOG + "\n" + "\n".join(f"INFO step {i} " + words(12, i) for i in range(4000)) + "\n" + PYTEST_LOG)
    postings = [w(f"job/p{i}.txt", "Data Scientist\nRequired: Python, SQL.\n" + words(900, 70 + i)) for i in range(8)]
    posting = postings[0]
    cv = w("job/cv.txt", "Alex Morgan\nData Scientist\n" + words(1500, 8))
    letter = w("family/lettre.txt", "Bonjour,\n" + "Le rendez-vous est fixé au 3 novembre 2026. " * 60)
    jobs = [
        ("digest 50 KB report", "digest", {"paths": [str(report)]}, {}),
        ("triage 200 KB test log", "test_log_triage", {"paths": [str(log)]}, {}),
        ("1 posting -> checklist", "posting_checklist", {"paths": [str(posting)]}, {}),
        ("8 postings -> checklists", "posting_checklist", {"paths": [str(p) for p in postings]}, {}),
        ("CV vs posting", "cv_match", {"posting_path": str(posting), "cv_path": str(cv)}, {}),
        ("translate letter (private)", "translate", {"paths": [str(letter)], "target": "en"}, {"prefer_local": True}),
        ("define 'hardware'", "freeform", {"instruction": "Define 'hardware' in one sentence.", "max_words": 30}, {}),
    ]
    print(f"{'hardware':24} {'job':27} " + " ".join(f"{p:24}" for p in ("rush", "balanced", "saver", "night")))
    for name, p in PROFILES.items():
        calibrate.save_profile(profile(config.load()["model"], p))
        svc = server.Service()
        for label, task, params, extra in jobs:
            cells = []
            for pr in ("rush", "balanced", "saver", "night"):
                e = svc.local_estimate(task, params, priority=pr, **extra)
                rec = e["recommended"]
                run = svc.estimates[e["estimate_id"]]["est"]["_runs"].get(rec)
                if e["action"] in ("script", "do_here") and not extra.get("prefer_local"):
                    cells.append({"script": "script (instant)", "do_here": "Claude"}[e["action"]])
                elif run:
                    cells.append(f"{rec} ~{run['p50_s'] / 60:.0f}m")
                else:
                    cells.append(rec)
            print(f"{name:24} {label:27} " + " ".join(f"{c:24}" for c in cells))
    env.close()


if __name__ == "__main__":
    main()
