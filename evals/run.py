#!/usr/bin/env python3
"""Eval harness around the organizers' scorer.

    python evals/run.py --snapshots ../kit/snapshots [--sets public,paraphrase,...] [--split dev|holdout]
                        [--update-baseline] [--checks]

Scoring is never re-implemented: every item goes through harness/scoring.py exactly as the graders run it.
On top of that this adds a failure taxonomy, a per-item regression gate against evals/baseline.json and
robustness checks (hash-seed determinism, metamorphic rewrites, as_of equivalence, no network, latency).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))

from contract import Answer  # noqa: E402
from run_eval import load_snapshots, load_submission, to_answer  # noqa: E402
from scoring import score_conflicts, score_item, summarize  # noqa: E402

SETS = {"public": ROOT / "eval" / "public_eval.jsonl", "paraphrase": ROOT / "evals" / "paraphrase.jsonl",
        "temporal": ROOT / "evals" / "temporal.jsonl", "refusal": ROOT / "evals" / "refusal.jsonl",
        "extended": ROOT / "evals" / "extended.jsonl",
        "adversarial": ROOT / "evals" / "adversarial.jsonl", "redteam": ROOT / "evals" / "redteam.jsonl"}
GOLD = ROOT / "evals" / "conflicts_gold.json"
BASELINE = ROOT / "evals" / "baseline.json"
P95_BUDGET_S, INGEST_BUDGET_S = 0.5, 120.0


def no_network():
    """OWASP AST03/AST06 (Over-Privileged Skills / Weak Isolation): the solution must answer from the
    snapshots alone. Any socket use during ingest or answer raises instead of silently phoning home."""
    def deny(*a, **k):
        raise RuntimeError("network access attempted by the submission")
    socket.socket = deny            # type: ignore[assignment]
    socket.create_connection = deny  # type: ignore[assignment]


def bucket(item: dict, r: dict) -> str | None:
    if r.get("error"):
        return "error"
    if r["latency_s"] > 2.0:
        return "slow"
    if r["trap_hits"]:
        return "trap_hit"
    refused = not r["answer"]["confident"]
    if item.get("category") == "should_refuse":
        return None if r["correctness"] >= 0.999 else ("refused_with_numbers" if refused else "missed_refusal")
    if r["correctness"] < 0.999:
        return "false_refusal" if refused else "missing_value"
    if r["source_hit"] == 0.0:
        return "wrong_source"
    return None


def load_items(names: list[str], split: str | None) -> list[dict]:
    items = []
    for name in names:
        path = SETS[name]
        if not path.exists():
            print(f"skip {name}: {path} missing")
            continue
        for line in path.read_text().splitlines():
            if line.strip():
                it = json.loads(line)
                if split and it.get("split", "dev") != split and name != "public":
                    continue
                it["_set"] = name
                items.append(it)
    return items


def ask(ks, it):
    t = time.time()
    try:
        ans, err = to_answer(ks.answer(it["question"], it.get("as_of"))), None
    except Exception as e:  # crashes score zero, like the official runner
        ans, err = Answer(text="", confident=False), f"{type(e).__name__}: {e}"
    return ans, err, time.time() - t


def check_pins(snapshots: Path) -> None:
    """OWASP AST07 (Update Drift): results are only comparable on the exact inputs the gold was verified
    against. The kit zips are hashed and compared with the REA-recorded pins before anything is scored."""
    import hashlib
    pins = json.loads((ROOT / "evals" / "snapshot_pins.json").read_text())
    zips = snapshots.parent / "snapshots_zip"
    for name, want in pins.items():
        path = zips / name
        if name.startswith("_") or not path.exists():
            continue
        got = hashlib.sha256(path.read_bytes()).hexdigest()
        if got != want:
            raise SystemExit(f"snapshot {name} changed: sha256 {got} != pinned {want}")


def run(args) -> int:
    no_network()
    check_pins(args.snapshots)
    snaps = load_snapshots(args.snapshots)
    ks = load_submission(ROOT / "submission")
    t0 = time.time()
    for s in snaps:
        ks.ingest(s)
    ingest_s = time.time() - t0

    items = load_items(args.sets.split(","), args.split)
    results, failures = [], []
    for it in items:
        ans, err, dt = ask(ks, it)
        r = score_item(it, ans)
        r.update(latency_s=round(dt, 3), error=err, answer=asdict(ans), set=it["_set"])
        results.append(r)
        b = bucket(it, r)
        if b:
            failures.append((b, it, r))
    repeats = [{"id": it["id"], "text": ask(ks, it)[0].text} for it in items[::max(1, len(items) // 20)]]
    conflict_report = score_conflicts(json.loads(GOLD.read_text()), ks.conflicts()) if GOLD.exists() else None
    report = summarize(results, conflict_report, repeats)
    report["ingest_seconds"] = round(ingest_s, 1)

    by_set: dict[str, list[float]] = {}
    for r in results:
        by_set.setdefault(r["set"], []).append(r["score"])
    print(json.dumps({k: report[k] for k in ("final_score", "qa_score", "pillars", "stale_trap_hits", "errors",
                                            "ingest_seconds")}, indent=1))
    print("speed", report["speed"], "| consistency", report["consistency"])
    if conflict_report:
        print("conflicts", conflict_report)
    for name, xs in by_set.items():
        print(f"  set {name:11s} {sum(xs) / len(xs):.3f}  (n={len(xs)})")
    for cat, v in report["by_category"].items():
        print(f"  cat {cat:24s} {v['score']:.2f}  (n={v['n']})")
    counts: dict[str, int] = {}
    for b, _, _ in failures:
        counts[b] = counts.get(b, 0) + 1
    print("failures by bucket:", counts)
    if args.verbose:
        for b, it, r in sorted(failures, key=lambda x: (x[0], x[1]["id"])):
            print(f"\n[{b}] {it['id']} ({it['_set']}) as_of={it.get('as_of')} :: {it['question']}")
            print(f"   want {it.get('must_include')}  not {it.get('must_not_include')}  traps={r['trap_hits']}")
            print(f"   got  {r['answer']['text'][:300]!r}")

    out = ROOT / "results" / "evals"
    out.mkdir(parents=True, exist_ok=True)
    (out / "per_question.jsonl").write_text("\n".join(json.dumps(r) for r in results))
    (out / "report.json").write_text(json.dumps(report, indent=2))

    status = 0
    passed = {r["id"] for r in results if r["score"] >= 0.849}
    if args.update_baseline:
        BASELINE.write_text(json.dumps({"passing": sorted(passed), "final_score": report["final_score"]}, indent=1))
        print(f"baseline updated: {len(passed)} passing")
    elif BASELINE.exists():
        base = json.loads(BASELINE.read_text())
        ran = {r["id"] for r in results}
        regressed = sorted(set(base["passing"]) & ran - passed)
        if regressed:
            print("REGRESSIONS (passed in baseline, failing now):", regressed)
            status = 1
    if args.checks:
        status |= robustness(ks, items, report, ingest_s)
    return status


def robustness(ks, items, report, ingest_s) -> int:
    bad = []
    if report["speed"]["p95_s"] > P95_BUDGET_S:
        bad.append(f"p95 latency {report['speed']['p95_s']}s > {P95_BUDGET_S}s")
    if ingest_s > INGEST_BUDGET_S:
        bad.append(f"ingest {ingest_s:.0f}s > {INGEST_BUDGET_S}s")
    from scoring import _core
    sys.path.insert(0, str(ROOT / "submission"))
    from tks.query import _date_from_text
    latest = max(sid.split("_")[1] for sid in ks.views)            # as_of=<latest date> must equal as_of=None
    for it in items:
        if it.get("as_of") or _date_from_text(it["question"], 2026)[0]:
            continue        # an explicit as_of or a date in the text legitimately changes the answer
        base = _core(ks.answer(it["question"]).text)
        rewrites = {"as_of=latest": ks.answer(it["question"], latest).text,
                    "upper": ks.answer(it["question"].upper()).text,
                    "punct": ks.answer(it["question"].rstrip("?.!") + " ??").text,
                    "filler": ks.answer("Hi there! " + it["question"] + " Thanks so much.").text}
        for name, text in rewrites.items():
            if _core(text) != base:
                bad.append(f"metamorphic[{name}] changed numbers for {it['id']}")
    bad += determinism(items)
    for b in bad:
        print("CHECK FAILED:", b)
    print(f"robustness: {'OK' if not bad else f'{len(bad)} problems'}")
    return 1 if bad else 0


def determinism(items) -> list[str]:
    """Answers must not depend on PYTHONHASHSEED (set/dict ordering) - run two fresh processes and diff."""
    probe = ROOT / "results" / "evals" / "probe.json"
    probe.write_text(json.dumps({"snapshots": str(ARGS.snapshots.resolve()),
                                 "questions": [[it["question"], it.get("as_of")] for it in items[:60]]}))
    # Fixed program text; the only input travels as a JSON file path in argv (no code-string assembly).
    code = ("import json,sys;from pathlib import Path;sys.path.insert(0,'harness');"
            "from run_eval import load_snapshots,load_submission;cfg=json.loads(Path(sys.argv[1]).read_text());"
            "ks=load_submission(Path('submission'));[ks.ingest(s) for s in load_snapshots(Path(cfg['snapshots']))];"
            "print(json.dumps([ks.answer(q,a).text for q,a in cfg['questions']]))")
    outs = []
    for seed in ("1", "4242"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        p = subprocess.run([sys.executable, "-c", code, str(probe)], cwd=ROOT, env=env, capture_output=True,
                           text=True)
        if p.returncode:
            return [f"determinism probe crashed: {p.stderr.strip()[-300:]}"]
        outs.append(p.stdout)
    if outs[0] != outs[1] or not outs[0]:
        return ["answers differ between PYTHONHASHSEED=1 and 4242"]
    return []


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", type=Path, required=True)
    ap.add_argument("--sets", default="public,paraphrase,temporal,refusal,extended,adversarial,redteam")
    ap.add_argument("--split", choices=["dev", "holdout"])
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--checks", action="store_true", help="latency, metamorphic and determinism checks")
    ap.add_argument("-v", "--verbose", action="store_true")
    ARGS = ap.parse_args()
    sys.exit(run(ARGS))
