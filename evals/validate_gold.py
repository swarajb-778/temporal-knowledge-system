#!/usr/bin/env python3
"""Test the tests: every gold value must be visible in the snapshot page it cites.

    python evals/validate_gold.py --snapshots ../kit/snapshots

- must_include values must appear (value-aware, same matcher as the scorer) in at least one cited page/snapshot
- must_not_include values should exist somewhere in the corpus (a real stale/other-tier value), else WARN
- conflicts_gold values must each appear in their cited evidence page
Exit code 1 if any hard check fails.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
from scoring import _norm, _numbers, contains  # noqa: E402

FILES = ["paraphrase.jsonl", "temporal.jsonl", "refusal.jsonl", "extended.jsonl"]


def load_corpus(root: Path) -> dict[tuple[str, str], str]:
    pages = {}
    for d in sorted(p for p in root.iterdir() if (p / "manifest.json").exists()):
        m = json.loads((d / "manifest.json").read_text())
        for p in m["pages"]:
            pages[(m["snapshot_id"], p["url"].rstrip("/"))] = (d / p["file"]).read_text(encoding="utf-8")
    return pages


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", type=Path, required=True)
    args = ap.parse_args()
    corpus = load_corpus(args.snapshots)
    unique = set(corpus.values())
    all_numbers = set().union(*(_numbers(t) for t in unique))
    all_text = "\n".join(_norm(t) for t in unique)

    def anywhere(v: str) -> bool:
        if contains("", v) is False and _numbers(v) and len(v) <= 20:
            return _numbers(v) <= all_numbers or any(n[1] in {a[1] for a in all_numbers} for n in _numbers(v))
        return _norm(v) in all_text
    hard, warn, checked = [], [], 0
    for name in FILES:
        path = ROOT / "evals" / name
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            it = json.loads(line)
            ev = [corpus.get((e["snapshot_id"], e["url"].rstrip("/")), "") for e in it.get("evidence", [])]
            for v in it.get("must_include", []):
                checked += 1
                if not any(contains(t, v) for t in ev):
                    hard.append(f"{name}:{it['id']} must_include {v!r} not found in cited evidence")
            for v in it.get("must_not_include", []):
                if not anywhere(v):
                    warn.append(f"{name}:{it['id']} must_not_include {v!r} never appears in the corpus")
            if it.get("category") != "should_refuse" and it.get("must_include") and not ev:
                hard.append(f"{name}:{it['id']} has no evidence")
    gold = ROOT / "evals" / "conflicts_gold.json"
    if gold.exists():
        for g in json.loads(gold.read_text()):
            for e in g.get("evidence", []):
                checked += 1
                page = corpus.get((e["snapshot_id"], e["url"].rstrip("/")), "")
                if not contains(page, e["value"]):
                    hard.append(f"conflicts_gold:{g['id']} value {e['value']!r} not in {e['url']} @ {e['snapshot_id']}")
            if g.get("correct_value") and g["correct_value"] not in g["values"]:
                hard.append(f"conflicts_gold:{g['id']} correct_value not among values")
    for w in warn:
        print("WARN", w)
    for h in hard:
        print("FAIL", h)
    print(f"checked {checked} gold values: {len(hard)} failures, {len(warn)} warnings")
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
