"""Tokenizer with domain synonyms and a small in-memory BM25 index (stdlib only)."""
from __future__ import annotations

import math
import re
from collections import defaultdict

WORD = re.compile(r"[a-z0-9]+(?:[$'’][a-z0-9]+)*")

STOP = set("""a about after all also am an and any are as at be been before being but by can could did do
does doing for from get give had has have having he her here how i if in into is it its just know let like
me might more most much my need no not now of off on once only or other our out over please provide right
same see she should show so some tell than that the their them then there these they this those through to
too under until up us use very want was we were what when where which while who why will with would you
your yours dupaco dupacos credit union union's currently current today still offer offered many between
going there hi hello thanks thank so put keep stretch shopping looking apply applies wanted getting i'm i've
i'd planning plan thinking publish published listed quote quoted carry carries financed finance borrow
borrowing""".split())
STOP -= {"credit", "off", "up"}

# Members and the site use different words for the same product.
SYN = {"cd": "certificate", "cds": "certificate", "certificates": "certificate", "car": "auto", "cars": "auto",
       "vehicle": "auto", "vehicles": "auto", "automobile": "auto", "truck": "auto", "motorcycles": "motorcycle",
       "mortgages": "mortgage", "house": "home", "visa": "visa", "rv": "rv", "boat": "boat", "boats": "boat",
       "branches": "branch", "locations": "location", "atms": "atm", "hrs": "hours", "hour": "hours",
       "savings": "savings", "saving": "savings", "checkings": "checking", "rewards": "reward", "loans": "loan",
       "cards": "card", "employees": "employee", "members": "member", "yr": "year", "years": "year",
       "months": "month", "mo": "month", "drive-up": "drive", "drivethru": "drive", "lobbies": "lobby"}


SUFFIXES = ("ations", "ation", "itions", "ition", "ions", "ion", "ings", "ing", "ized", "izes", "ize", "ed")


def norm_token(w: str) -> str:
    w = w.replace("’", "'")
    if w.endswith("'s"):
        w = w[:-2]
    if w in SYN:
        return SYN[w]
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    w = SYN.get(w, w)
    # ponytail: suffix stripping instead of a real stemmer; keeps stems >= 5 chars so "rates" stays "rate".
    for suf in SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 5:
            return w[:-len(suf)]
    return w


RATE_WORDS = {norm_token(w) for w in ("rate", "rates", "apr", "apy", "interest", "dividend", "dividends", "yield",
                                      "annual", "percentage", "pay", "pays", "paying", "earn", "earns", "earning",
                                      "charge", "charged")}


def tokens(text: str, keep_stop: bool = False) -> list[str]:
    out = []
    for w in WORD.findall(text.lower().replace("drive-up", "drive").replace("drive up", "drive")):
        if not keep_stop and w in STOP:
            continue
        t = norm_token(w)
        if keep_stop or t not in STOP:
            out.append(t)
    return out


class BM25:
    """Inverted index; documents are opaque integer ids added in order."""

    def __init__(self, k1: float = 1.2, b: float = 0.5):
        self.k1, self.b = k1, b
        self.post: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self.lens: list[int] = []
        self.idf: dict[str, float] = {}
        self._dirty = True

    def add(self, toks: list[str]) -> int:
        pid = len(self.lens)
        tf: dict[str, int] = defaultdict(int)
        for t in toks:
            tf[t] += 1
        for t in sorted(tf):
            self.post[t].append((pid, tf[t]))
        self.lens.append(len(toks))
        self._dirty = True
        return pid

    def _finalize(self):
        n = len(self.lens) or 1
        self.avg = sum(self.lens) / n
        self.idf = {t: math.log(1 + (n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self.post.items()}
        self._dirty = False

    def search(self, qtoks: list[str], allowed=None, k: int = 50) -> list[tuple[float, int]]:
        if self._dirty:
            self._finalize()
        scores: dict[int, float] = defaultdict(float)
        for t in dict.fromkeys(qtoks):
            idf = self.idf.get(t)
            if idf is None:
                continue
            for pid, tf in self.post[t]:
                if allowed is not None and not allowed(pid):
                    continue
                dl = self.lens[pid]
                scores[pid] += idf * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * dl / self.avg))
        # Stable tie-break on id keeps answers identical across runs and hash seeds.
        return sorted(((s, p) for p, s in scores.items()), key=lambda x: (-x[0], x[1]))[:k]
