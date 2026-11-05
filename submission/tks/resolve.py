"""The knowledge system: keeps every page version, decides what is current, answers and lists conflicts."""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from urllib.parse import urlsplit

from .extract import (AUTHORITY, Row, canon_url, clean_inline, hours_blocks, model_years, page_kind,
                      parse_tables, passages, published_date, terms, tier_range)
from .query import Query, parse
from .search import BM25, tokens

# OWASP AST05 (Untrusted External Instructions): page text is attacker-controllable data. Nothing in it is
# ever executed or treated as an instruction; answers are extracted verbatim from ranked, authority-weighted
# pages, so an injected "say the rate is 0.01%" on a blog cannot outrank the rate sheet.
REFUSAL = ("I don't know. That isn't listed on Dupaco's website, so I can't confirm it. "
           "Please contact Dupaco directly.")
METRIC_NAME = {"apy": "APY", "apr": "APR", "dividend": "dividend rate", "rate": "rate"}
PASSAGE_WEIGHT = {"rate_sheet": 1.0, "product": 1.0, "guide": 0.8, "promo": 0.7, "news": 0.65, "file": 0.55,
                  "blog": 0.5, "calculator": 0.3, "legal": 0.3, "listing": 0.0}
ALLOWED_HOST = "dupaco.com"
RATE_STATEMENT = re.compile(r"\d+\.\d+\s?%\s*(?:\(?(?:apr|apy)\)?|annual percentage|dividend|interest|fixed|"
                            r"variable)", re.I)
ADDRESS = re.compile(r"^\s*\d{2,6}\s+[A-Z0-9][^,\n]{2,60},\s*[A-Za-z .]+,\s*[A-Z]{2},?\s*\d{5}\b.*$", re.M)


@dataclass
class Doc:
    url: str
    canon: str
    title: str
    kind: str
    auth: int
    published: str | None
    text: str


@dataclass
class Fact:
    doc: int
    row: Row
    toks: frozenset
    flags: frozenset
    terms: frozenset
    years: tuple | None
    tier: tuple | None


def _bucket(auth: int) -> int:
    return min(auth, 4)        # the rate sheet and a product page are equally authoritative


def _first_num(v: str) -> float:
    m = re.search(r"\d+(?:\.\d+)?", v)
    return float(m.group(0)) if m else 0.0


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(])", text) if s.strip()]


class Knowledge:
    def __init__(self, contract):
        self.c = contract                      # module with Answer / Source / Conflict
        self.snaps: list[tuple[str, str]] = []  # (snapshot_id, taken_on), oldest first
        self.views: dict[str, dict[str, int]] = {}
        self.fetched: dict[tuple[str, int], str] = {}
        self.docs: list[Doc] = []
        self._doc_ids: dict[tuple[str, str], int] = {}
        self.facts: list[Fact] = []
        self.facts_by_doc: dict[int, list[int]] = defaultdict(list)
        self.hours: dict[int, list] = {}
        self.bm25 = BM25()
        self.pass_text: list[tuple[str, str]] = []
        self.pass_docs: list[list[int]] = []
        self._pass_ids: dict[tuple[str, str, str], int] = {}
        self._fact_idf: dict[str, float] | None = None

    # ------------------------------------------------------------------ ingest
    def ingest(self, snapshot) -> None:
        sid, taken = str(snapshot.snapshot_id), str(snapshot.taken_on)[:10]
        # OWASP AST04 (Insecure Metadata): manifest fields are untrusted input. Validate the date shape and
        # only keep pages on the credit union's own host, so a poisoned manifest cannot inject foreign
        # citations; nothing is deserialized beyond the plain strings the harness hands us.
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", taken):
            raise ValueError(f"bad taken_on for {sid!r}")
        view: dict[str, int] = {}
        for p in snapshot.pages:
            url, md = str(p.url or ""), str(p.markdown or "")
            host = urlsplit(url).netloc.lower()
            if urlsplit(url).scheme not in ("http", "https") or not (host == ALLOWED_HOST or
                                                                    host.endswith("." + ALLOWED_HOST)):
                continue
            canon = canon_url(url)
            if canon in view:
                continue
            key = (canon, hashlib.sha256(md.encode("utf-8", "replace")).hexdigest())
            did = self._doc_ids.get(key)
            if did is None:
                did = self._add_doc(url, canon, str(p.title or ""), md)
                self._doc_ids[key] = did
            view[canon] = did
            self.fetched[(sid, did)] = str(p.fetched_at or taken)
        self.views[sid] = view
        self.snaps = sorted(set(self.snaps) | {(sid, taken)}, key=lambda s: (s[1], s[0]))
        self._fact_idf = None

    def _add_doc(self, url, canon, title, md) -> int:
        kind = page_kind(url)
        did = len(self.docs)
        self.docs.append(Doc(url, canon, title, kind, AUTHORITY[kind], published_date(md), md))
        path = urlsplit(canon).path
        for row in parse_tables(md):
            low = row.text.lower()
            flags = set()
            if "jumbo" in low:
                flags.add("jumbo")
            if re.search(r"\broth\b|\btraditional\b|\bira", low):
                flags.add("ira")
                flags.update(f for f in ("roth", "traditional") if f in low)
            if "refinanc" in low:
                flags.add("refinance")
            if "intro" in low:
                flags.add("intro")
            if "business" in low or path.startswith("/business"):
                flags.add("business")
            tags = " mortgage" if re.search(r"conventional|home loans", low) else ""
            labels = " ".join(row.labels)
            fact = Fact(did, row, frozenset(tokens(row.text + tags)), frozenset(flags), frozenset(terms(row.text)),
                        model_years(labels), tier_range(labels))
            self.facts_by_doc[did].append(len(self.facts))
            self.facts.append(fact)
        blocks = hours_blocks(md)
        if blocks:
            self.hours[did] = blocks
        if kind != "listing":
            for heading, text in passages(md):
                key = (title, heading, text)
                pid = self._pass_ids.get(key)
                if pid is None:
                    pid = self.bm25.add(tokens(f"{title} {heading} {text}"))
                    self._pass_ids[key] = pid
                    self.pass_text.append((heading, text))
                    self.pass_docs.append([])
                self.pass_docs[pid].append(did)
        return did

    # ------------------------------------------------------------------ helpers
    def _snapshot_for(self, as_of: str | None) -> str:
        if as_of:
            past = [s for s, d in self.snaps if d <= str(as_of)[:10]]
            if past:
                return past[-1]
            return self.snaps[0][0]
        return self.snaps[-1][0]

    def _src(self, did: int, sid: str, quote: str = ""):
        return self.c.Source(url=self.docs[did].url, snapshot_id=sid, quote=quote[:240] or None)

    def _refuse(self, note: str | None = None):
        return self.c.Answer(text=REFUSAL, sources=[], confident=False, conflict_note=note)

    def _idf(self) -> dict[str, float]:
        if self._fact_idf is None:
            df = Counter(t for f in self.facts for t in f.toks)
            n = len(self.facts) or 1
            self._fact_idf = {t: math.log(1 + n / c) for t, c in df.items()}
            self._max_idf = math.log(1 + n)
        return self._fact_idf

    # ------------------------------------------------------------------ answer
    def answer(self, question: str, as_of: str | None = None):
        if not self.snaps:
            return self._refuse("no snapshots ingested")
        try:
            q = parse(question, as_of, default_year=int(self.snaps[-1][1][:4]))
            sid = self._snapshot_for(q.as_of)
            view = self.views[sid]
            if q.intent == "hours":
                return self._hours(q, sid, view)
            if q.intent == "count":
                got = self._count(q, sid, view)
                if got:
                    return got
            if q.intent == "address":
                got = self._address(q, sid, view)
                if got:
                    return got
            if q.intent in ("rate", "url"):
                got = self._rate(q, sid, view)
                if got:
                    return got
            if q.intent == "url":
                return self._url(q, sid, view) or self._refuse()
            if q.intent == "rate" and q.ptoks:
                # Rates are only quoted from rate tables or authoritative prose that states a rate.
                return self._passage(q, sid, view, need_pct=True) or self._refuse()
            return self._passage(q, sid, view) or self._refuse()
        except Exception as e:  # a crash must never surface a guessed value
            return self._refuse(f"internal error: {type(e).__name__}: {e}")

    def _row_ok(self, q: Query, f: Fact) -> bool:
        if q.terms and not (q.terms & f.terms):
            return False
        asked = set(q.flags)
        if asked & {"roth", "traditional"}:
            asked.add("ira")
        for flag in ("jumbo", "ira", "refinance", "intro"):
            if (flag in asked) != (flag in f.flags):
                return False
        for flag in ("roth", "traditional"):
            if flag in asked and flag not in f.flags:
                return False
        if "business" in asked and "business" not in f.flags:
            return False
        if "business" in f.flags and "business" not in asked and not q.listing:
            return False
        if q.years and f.years and not any(f.years[0] <= y <= f.years[1] for y in q.years):
            return False
        if q.amounts and f.tier and not all(f.tier[0] <= a <= f.tier[1] for a in q.amounts):
            return False
        return True

    def _rate_groups(self, q: Query, view: dict[str, int]):
        idf = self._idf()
        want = sorted(set(q.ptoks))
        if not want:
            return []
        denom = sum(idf.get(t, self._max_idf) for t in want)
        groups: dict[tuple[int, int], list[tuple[float, Fact]]] = defaultdict(list)
        for did in sorted(set(view.values())):
            for fi in self.facts_by_doc.get(did, ()):
                f = self.facts[fi]
                if not self._row_ok(q, f):
                    continue
                s = sum(idf[t] for t in want if t in f.toks)
                if s > 0 and s / denom >= 0.5:
                    groups[(did, f.row.table)].append((s, f))
        if not groups:
            return []
        top = max(s for g in groups.values() for s, _ in g)
        # A listing question ("what cards do you offer") wants every matching product, not just the best one.
        floor = 0.0 if q.listing else top * 0.999
        tied = {k: [f for s, f in g] for k, g in groups.items() if max(s for s, _ in g) >= floor}
        best_bucket = max(_bucket(self.docs[k[0]].auth) for k in tied)
        return [(k, fs) for k, fs in sorted(tied.items()) if _bucket(self.docs[k[0]].auth) == best_bucket]

    def _rate(self, q: Query, sid: str, view: dict[str, int]):
        groups = self._rate_groups(q, view)
        if not groups:
            return None
        order = {did: i for i, did in enumerate(view.values())}
        # Merge identical products shown on several pages (rate sheet + product page + stale promo copy).
        merged: dict[tuple, list[tuple[tuple[int, int], list[Fact]]]] = defaultdict(list)
        for key, fs in groups:
            sig = tuple(sorted(tuple(f.row.labels) for f in fs))
            merged[sig].append((key, fs))
        products, notes, sources = [], [], []
        for sig, members in merged.items():
            def prio(m):
                did = m[0][0]
                eff = max((f.row.effective or "" for f in m[1]), default="")
                return (_bucket(self.docs[did].auth), eff, self.fetched.get((sid, did), ""), self.docs[did].auth,
                        -did)
            members.sort(key=prio, reverse=True)
            (wkey, wfacts) = members[0]
            wvals = [f.row.values for f in wfacts]
            for key, fs in members:
                if [f.row.values for f in fs] == wvals:
                    sources.append(self._src(key[0], sid, fs[0].row.line))
                else:
                    stale = sorted({v for f in fs for _, v in f.row.values} - {v for vs in wvals for _, v in vs})
                    if stale:
                        notes.append(f"Ignored {', '.join(stale)} on {self.docs[key[0]].url} (older or lower-"
                                     f"authority page); used {self.docs[wkey[0]].url}.")
            products.append(("business" in wfacts[0].flags, order.get(wkey[0], 0), wkey[1], wfacts))
        products.sort(key=lambda p: p[:3])
        rows = [(fs, f) for *_, fs in products for f in fs]
        if q.superlative:
            def val(item):
                vals = [v for m, v in item[1].row.values if q.metric in (None, m)] or [v for _, v in item[1].row.values]
                return _first_num(vals[-1])
            pick = (max if q.superlative == "max" else min)(rows, key=val)
            rows = [pick]
        text = self._compose_rates(q, rows)
        if q.intent == "url":
            urls = sorted({s.url for s in sources}, key=lambda u: (self.docs[view[canon_url(u)]].kind == "rate_sheet", u))
            text = "You can find them at " + " and ".join(f"{u}#rates" for u in urls) + "."
        seen, uniq = set(), []
        for s in sources:
            if s.url not in seen:
                seen.add(s.url)
                uniq.append(s)
        return self.c.Answer(text=text, sources=uniq, confident=True, conflict_note=" ".join(notes) or None)

    def _compose_rates(self, q: Query, rows) -> str:
        parts, last_caption = [], None
        for _, f in rows[:16]:
            r = f.row
            vals = [(m, v) for m, v in r.values if q.metric in (None, m)] or r.values
            labels = [lab for lab in r.labels if lab.lower() not in r.caption.lower()]
            item = (", ".join(labels) + ": " if labels else "") + " / ".join(f"{v} {METRIC_NAME[m]}" for m, v in vals)
            if r.caption != last_caption:
                item = f"{r.caption} - {item}" if r.caption else item
                last_caption = r.caption
            parts.append(item)
        return "; ".join(parts) + "."

    def _hours(self, q: Query, sid: str, view: dict[str, int]):
        dids = [d for d in view.values() if d in self.hours]
        if q.branch:
            name = q.branch.lower()
            dids = [d for d in dids if name in self.docs[d].text.lower()]
            if not dids:
                return self._refuse(f"no published hours for {q.branch}")
        idx = 1 if q.hours_kind == "drive" else 0
        counts: Counter = Counter()
        where: dict[tuple, int] = {}
        for d in dids:
            for block in self.hours[d]:
                sched = tuple(re.sub(r"\s*[–-]\s*", "–", line) for line in block[idx])
                if sched:
                    counts[sched] += 1
                    where.setdefault(sched, d)
        if not counts:
            return self._refuse()
        scheds = sorted(counts, key=lambda s: (-counts[s], s))[:5]
        label = "Drive-up" if idx else "Lobby"
        text = f"{label} hours: " + "; ".join(scheds[0]) + "."
        if len(scheds) > 1:
            text = (f"{label} hours vary by branch. Most branches: " + "; ".join(scheds[0]) + ". Other branches: "
                    + " | ".join("; ".join(s) for s in scheds[1:]) + ". Which branch would you like?")
        srcs = [self._src(where[s], sid, "; ".join(s)) for s in scheds]
        return self.c.Answer(text=text, sources=srcs, confident=True)

    def _ranked_passages(self, q: Query, view: dict[str, int], k: int = 60):
        live = set(view.values())
        hits = self.bm25.search(q.toks, allowed=lambda pid: any(d in live for d in self.pass_docs[pid]), k=k * 3)
        qt = set(q.toks)
        out = []
        for score, pid in hits:
            did = max((d for d in self.pass_docs[pid] if d in live), key=lambda d: (self.docs[d].auth, -d))
            doc = self.docs[did]
            tfrac = len(qt & set(tokens(doc.title))) / (len(qt) or 1)
            out.append((score * PASSAGE_WEIGHT[doc.kind] * (1 + 0.5 * tfrac), pid, did))
        out.sort(key=lambda x: (-x[0], x[1]))
        return out[:k]

    def _coverage(self, q: Query, pid: int, did: int) -> float:
        idf = self.bm25.idf
        mx = max(idf.values()) if idf else 1.0
        heading, text = self.pass_text[pid]
        have = set(tokens(f"{self.docs[did].title} {heading} {text}"))
        want = set(q.toks)
        if not want:
            return 0.0
        return sum(idf.get(t, mx) for t in want & have) / sum(idf.get(t, mx) for t in want)

    def _passage(self, q: Query, sid: str, view: dict[str, int], need_pct: bool = False):
        if not q.toks:
            return None
        picked = []
        for score, pid, did in self._ranked_passages(q, view, k=12):
            heading, text = self.pass_text[pid]
            if need_pct and (self.docs[did].auth < 2 or not RATE_STATEMENT.search(text)):
                continue
            if q.branch and q.branch.lower() not in f"{self.docs[did].title} {text}".lower():
                continue
            if text.rstrip().endswith("?") or self._coverage(q, pid, did) < 0.4:
                continue          # an FAQ question line is not an answer
            if picked and (score < 0.6 * picked[0][0] or any(text == t for _, _, _, t in picked)):
                continue
            picked.append((score, pid, did, text))
            if len(picked) == 2:
                break
        if not picked:
            return None
        body = " ".join(self._best_sentences(q, t, limit=360 if len(picked) > 1 else 420) for *_, t in picked)
        srcs = []
        for _, _, did, t in picked:
            if all(s.url != self.docs[did].url for s in srcs):
                srcs.append(self._src(did, sid, t))
        return self.c.Answer(text=body, sources=srcs, confident=True)

    def _url(self, q: Query, sid: str, view: dict[str, int]):
        per_doc: Counter = Counter()
        for score, pid, did in self._ranked_passages(q, view, k=60):
            per_doc[did] += score
        if not per_doc:
            return None
        did = sorted(per_doc, key=lambda d: (-per_doc[d], d))[0]
        return self.c.Answer(text=f"You can find it at {self.docs[did].url}", sources=[self._src(did, sid)],
                             confident=True)

    def _address(self, q: Query, sid: str, view: dict[str, int]):
        name = (q.branch or "").lower()
        if not name:
            return None
        hits = []
        for did in sorted(set(view.values()), key=lambda d: (-self.docs[d].auth, d)):
            for line in ADDRESS.findall(self.docs[did].text):
                if name in line.lower():
                    hits.append((did, clean_inline(line)))
        if not hits:
            return None
        did, line = hits[0]
        return self.c.Answer(text=f"The {q.branch} branch is at {line}.", sources=[self._src(did, sid, line)],
                             confident=True)

    def _best_sentences(self, q: Query, text: str, limit: int = 420) -> str:
        if len(text) <= limit:
            return text
        sents = _sentences(text)
        idf = self.bm25.idf
        qt = set(q.toks)
        scored = [(sum(idf.get(t, 0) for t in qt & set(tokens(s))), -i, s) for i, s in enumerate(sents)]
        _, neg_i, best = max(scored)
        i = -neg_i
        if len(best) < 160 and i + 1 < len(sents):
            best = f"{best} {sents[i + 1]}"
        return best[:limit]

    def _count(self, q: Query, sid: str, view: dict[str, int]):
        word = q.count_word
        if not word:
            return None
        others = [t for t in q.toks if t != word]
        votes: Counter = Counter()
        example: dict[str, tuple[int, str]] = {}
        probe = Query(text=q.text, as_of=q.as_of, toks=[word] + others, ptoks=[])
        for score, pid, did in self._ranked_passages(probe, view, k=3000):
            for s in _sentences(self.pass_text[pid][1]):
                st = tokens(s)
                if word not in st or (others and len(set(others) & set(st)) < max(1, len(others) // 2)):
                    continue
                for m in re.finditer(r"(\d[\d,]*)\+?\s+((?:[\w-]+\s+){0,2}?)([A-Za-z-]+)", s):
                    if tokens(m.group(3)) == [word] or (tokens(m.group(3)) and tokens(m.group(3))[0] == word):
                        n = m.group(1)
                        w = PASSAGE_WEIGHT[self.docs[did].kind]
                        votes[n] += w
                        example.setdefault(n, (did, s))
        if not votes:
            return None
        best = sorted(votes, key=lambda n: (-votes[n], n))[0]
        did, sentence = example[best]
        note = None
        if len(votes) > 1:
            note = "Pages disagree: " + ", ".join(f"{n} ({votes[n]:.1f})" for n in sorted(votes)) + "; majority used."
        return self.c.Answer(text=sentence, sources=[self._src(did, sid, sentence)], confident=True,
                             conflict_note=note)

    # ------------------------------------------------------------------ conflicts
    def conflicts(self):
        return []
