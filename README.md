# Temporal knowledge system

Answers credit-union member questions from five crawls of the same website. It gives the current value
(or the value as of a date), picks the right tier, cites its sources, lists the site's contradictions
with a winner for each, and says "I don't know" when the site doesn't say.

- `submission/`: the solution (`solution.py` with `build()`), Python 3.11 standard library only, no API keys
- `harness/`, `eval/`: the organizers' scorer and practice set, unmodified
- `evals/`: independent gold sets, gold validator and the extended harness
- `tests/`: unit, fixture and security tests

```bash
# put the participant kit's snapshots_zip/ (or snapshots/) next to harness/, then:
python harness/run_eval.py --submission submission --eval eval/public_eval.jsonl
python evals/run.py --snapshots snapshots --checks
python -m unittest discover -s tests -t .
```

How it decides what is current, the eval results and the security notes are in [NOTES.md](NOTES.md).
