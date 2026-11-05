"""The knowledge system: keeps every page version, decides what is current, answers and lists conflicts."""
from __future__ import annotations

import difflib
import hashlib
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlsplit

from .extract import (AUTHORITY, Row, canon_url, clean_inline, hours_blocks, model_years, page_kind,
                      parse_tables, passages, prose_facts, published_date, terms, tier_range)
from .query import Query, parse
from .search import BM25, STOP, normalize, tokens

# OWASP AST05 (Untrusted External Instructions): page text is attacker-controllable data. Nothing in it is
# ever executed or treated as an instruction; answers are extracted verbatim from ranked, authority-weighted
# pages, so an injected "say the rate is 0.01%" on a blog cannot outrank the rate sheet.
REFUSAL = ("I don't know. That isn't listed on Dupaco's website, so I can't confirm it. "
           "Please contact Dupaco directly.")
METRIC_NAME = {"apy": "APY", "apr": "APR", "dividend": "dividend rate", "rate": "rate"}
# Words members misspell most, kept as correction targets even if a crawl happens not to use them.
DOMAIN_WORDS = frozenset("""interest rate rates annual percentage yield dividend dividends certificate certificates
mortgage mortgages savings checking account accounts loan loans auto vehicle jumbo special specials money market
business personal branch branches hours lobby routing number balance minimum maximum refinance equity
traditional retirement credit card cards platinum rewards visa motorcycle model fixed variable term""".split())
PASSAGE_WEIGHT = {"rate_sheet": 1.0, "product": 1.0, "guide": 0.8, "promo": 0.7, "news": 0.65, "file": 0.55,
                  "blog": 0.5, "calculator": 0.3, "legal": 0.3, "listing": 0.0}

def _site(netloc: str) -> str:
    """Registrable-ish host: 'www.dupaco.com' and 'dupaco.com' are the same site."""
    host = netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host

RATE_STATEMENT = re.compile(r"\d+\.\d+\s?%\s*(?:\(?(?:apr|apy)\)?|annual percentage|dividend|interest|fixed|"
                            r"variable)|\b(?:apr|apy|rate)\s*:\s*\d+\.\d+\s?%", re.I)
ADDRESS = re.compile(r"^[ \t]*\d{2,6}[ \t]+[A-Z0-9][^,\n]{2,60},[ \t]*[A-Za-z][A-Za-z .]{0,40},[ \t]*[A-Z]{2},?"
                     r"[ \t]*\d{5}\b.*$", re.M)


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
    year_rank: int | None = None     # 0 = newest model-year bucket in its table; survives yearly relabeling


def _bucket(auth: int) -> int:
    return min(auth, 4)        # the rate sheet and a product page are equally authoritative


def _pct_values(text: str) -> set[float]:
    """Every 'N%' in text as a rounded number, so '7.5%', '7.50 %' and '7.50%' compare equal."""
    return {round(float(n), 3) for n in re.findall(r"(\d+(?:\.\d+)?)\s?%", text)}


def _first_num(v: str) -> float:
    m = re.search(r"\d+(?:\.\d+)?", v)
    return float(m.group(0)) if m else 0.0


FAMILIES = (("card", r"visa|credit card"), ("heloc", r"heloc|line of credit"),
            ("mortgage", r"conventional|home loans|fha|\bva\b"), ("home_equity", r"home equity"),
            ("home_improvement", r"home improvement"), ("recreational", r"boat|\brv\b|atv|snowmobile|jet ski"),
            ("auto", r"auto|motorcycle|model year"), ("personal_loan", r"personal loan"),
            ("ira", r"\bira|roth|traditional"), ("certificate", r"certificate|term-share|\bcd\b"),
            ("money_market", r"money market"), ("checking", r"checking"), ("savings", r"savings"))


def _fact_key(f: Fact) -> tuple:
    low = f.row.text.lower()
    family = next((name for name, pat in FAMILIES if re.search(pat, low)), f.row.caption.lower())
    labels = " ".join(f.row.labels).lower().replace(",", "")
    if f.year_rank is not None:     # "2023-26 Model Year" last year == "2024-27 Model Year" this year
        labels = re.sub(r"model\s+year\s+\d{4}\s*[-–]\s*\d{2,4}|\d{4}\s*[-–]\s*\d{2,4}\s+model\s+year|"
                        r"\d{4}\s+model\s+year(\s*\(and older\))?", f"model-year-rank-{f.year_rank}", labels)
    if "business" in f.flags:
        labels = labels.replace("business", "")
    labels = re.sub(r"[^a-z0-9+]+", " ", labels).strip()
    flags = tuple(sorted(f.flags & {"business", "jumbo", "refinance", "intro", "roth", "traditional"}))
    return family, flags, labels


def _main_value(values):
    d = dict(values)
    for m in ("apy", "rate", "apr", "dividend"):    # members quote a mortgage's note rate, a deposit's APY
        if m in d:
            return m, d[m]
    return values[0]


def _topic(f: Fact, metric: str) -> str:
    extra = []
    for n in sorted(f.terms):
        extra.append(f"{n}-month")
        if n % 12 == 0:
            extra.append(f"{n // 12}-year")
    caption = f.row.caption or ""
    return " ".join([caption, " ".join(f.row.labels), METRIC_NAME.get(metric, metric)] + extra).strip()


STREET = {"avenue": "ave", "street": "st", "road": "rd", "drive": "dr", "lane": "ln", "boulevard": "blvd",
          "highway": "hwy", "court": "ct", "place": "pl", "parkway": "pkwy", "north": "n", "south": "s",
          "east": "e", "west": "w", "iowa": "ia", "illinois": "il", "wisconsin": "wi", "ill": "il"}


def _place_words(text: str) -> list[str]:
    return [STREET.get(w, w) for w in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text.lower())]


def _mentions(name: str, text: str) -> bool:
    """'Pennsylvania Avenue' matches '3999 Pennsylvania Ave., Dubuque'."""
    want, have = _place_words(name), set(_place_words(text))
    return bool(want) and all(w in have for w in want)


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
        self._max_idf = 0.0
        self._conflicts: list | None = None
        self._obs: dict | None = None
        self._vocab: tuple | None = None
        self._stale_pct: dict[str, list] = {}
        self.site: str | None = None

    # ------------------------------------------------------------------ ingest
    def ingest(self, snapshot) -> None:
        sid, taken = str(snapshot.snapshot_id), str(snapshot.taken_on)[:10]
        # OWASP AST04 (Insecure Metadata): manifest fields are untrusted input. Validate the date shape and
        # only keep pages on the crawled site's own host (learned from the first snapshot), so a poisoned
        # manifest cannot inject foreign citations; nothing is deserialized beyond plain strings.
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", taken):
            raise ValueError(f"bad taken_on for {sid!r}")
        if self.site is None:
            hosts = Counter(_site(urlsplit(str(p.url or "")).netloc) for p in snapshot.pages)
            self.site = hosts.most_common(1)[0][0] if hosts else ""
        view: dict[str, int] = {}
        for p in snapshot.pages:
            url, md = str(p.url or ""), str(p.markdown or "")
            if urlsplit(url).scheme not in ("http", "https") or _site(urlsplit(url).netloc) != self.site:
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
        # A page missing from a later crawl was not re-fetched, not deleted: carry its newest earlier
        # version forward (it keeps its old fetched_at, so fresher pages still outrank it).
        if self.snaps:
            prev_sid = self.snaps[-1][0]
            for canon, did in self.views[prev_sid].items():
                if canon not in view:
                    view[canon] = did
                    self.fetched[(sid, did)] = self.fetched[(prev_sid, did)]
        self.views[sid] = view
        self.snaps = sorted(set(self.snaps) | {(sid, taken)}, key=lambda s: (s[1], s[0]))
        self._fact_idf = None
        self._conflicts = self._obs = self._vocab = None     # a new crawl can add or settle contradictions
        self._stale_pct = {}

    def _add_doc(self, url, canon, title, md) -> int:
        kind = page_kind(url)
        did = len(self.docs)
        self.docs.append(Doc(url, canon, title, kind, AUTHORITY[kind], published_date(md), md))
        path = urlsplit(canon).path
        rows = parse_tables(md)
        if kind in ("rate_sheet", "product", "guide"):
            rows += prose_facts(md)     # stale narrative pages (blog, news, promo) never feed the rate engine
        for row in rows:
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
        by_table = defaultdict(list)
        for fi in self.facts_by_doc.get(did, ()):
            by_table[self.facts[fi].row.table].append(self.facts[fi])
        for fs in by_table.values():
            ranges = sorted({f.years for f in fs if f.years}, key=lambda r: -r[1])
            for f in fs:
                if f.years:
                    f.year_rank = ranges.index(f.years)
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
        # OWASP AST09 (No Governance): every answer and conflict is auditable - it names the page, the crawl it
        # came from and the exact row/sentence used, so a wrong answer can be traced to its input.
        url = urlsplit(self.docs[did].url)._replace(query="", fragment="").geturl()   # no crawler params
        return self.c.Source(url=url, snapshot_id=sid, quote=quote[:240] or None)

    def _spellfix(self, question: str) -> str:
        """Correct misspelled words ('certifcate', 'morgage') to the site's own vocabulary.

        Only lowercase-looking words of 5+ letters that the site never uses are touched, the candidate must
        share the first letter and length within 2, and capitalized words mid-sentence (branch and city names
        like 'Ames') are left alone so an unknown place still gets an honest refusal."""
        if self._vocab is None:
            words = set()
            for heading, text in self.pass_text:
                words.update(re.findall(r"[a-z]{4,}", normalize(f"{heading} {text}").lower()))
            for f in self.facts:
                words.update(re.findall(r"[a-z]{4,}", f.row.text.lower()))
            words.update(DOMAIN_WORDS)
            index: dict[tuple[str, int], list[str]] = defaultdict(list)
            for w in sorted(words):                       # sorted: difflib ties resolve the same every run
                index[(w[0], len(w))].append(w)
            self._vocab = (frozenset(words), index)
        vocab, index = self._vocab
        shouty = question.isupper()

        def fix(m):
            w = m.group(0)
            low = w.lower()
            if low in vocab or low in STOP or (w[0].isupper() and not shouty and m.start() > 0):
                return w
            if len(low) == 4:      # short words: only an unambiguous transposition ("laon" -> "loan")
                swaps = [c for c in index.get((low[0], 4), ()) if sorted(c) == sorted(low)]
                return swaps[0] if len(swaps) == 1 else w
            cands = [c for n in range(len(low) - 2, len(low) + 3) for c in index.get((low[0], n), ())]
            best = difflib.get_close_matches(low, cands, n=1, cutoff=0.85)
            return best[0] if best else w
        return re.sub(r"\b[A-Za-z]{4,}\b", fix, normalize(question))

    def _stale_values(self, sid: str, q: Query) -> set[float]:
        """Values that, for the product asked about, only ever appear superseded or stale: never current on
        an authoritative page in this snapshot. A passage sentence quoting one is dropped (output whitelist).
        Scoped by product so a 1.00% fee is not mistaken for some other product's old 1.00% rate."""
        if sid not in self._stale_pct:
            current = set()
            for did in set(self.views[sid].values()):
                if _bucket(self.docs[did].auth) >= 4:
                    for fi in self.facts_by_doc.get(did, ()):
                        current.update(_pct_values(" ".join(v for _, v in self.facts[fi].row.values)))
            entries = []
            for c in self.conflicts():
                old = set().union(*(_pct_values(v) for v in c.values if v != c.resolution)) - current
                if old:
                    entries.append((frozenset(old), frozenset(tokens(c.topic))))
            self._stale_pct[sid] = entries
        asked = set(q.ptoks)
        return set().union(*(vals for vals, topic in self._stale_pct[sid] if topic & asked), set())

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
            q = parse(self._spellfix(question), as_of, default_year=int(self.snaps[-1][1][:4]),
                      today=self.snaps[-1][1])
            if q.future:
                return self._refuse("asks for a value after the newest crawl; the site does not forecast")
            sid = self._snapshot_for(q.as_of)
            view = self.views[sid]
            # Before the first crawl nothing was observed: only a rate whose stated effective date is on or
            # before the requested date can be vouched for.
            not_after = q.as_of if q.as_of and q.as_of < self.snaps[0][1] else None
            if not_after:
                return self._rate(q, sid, view, not_after) or self._refuse("date precedes the first crawl")
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
            if q.intent == "rate" and q.ptoks and not q.soft:
                # Rates are only quoted from rate tables or authoritative prose that states a rate.
                return self._passage(q, sid, view, need_pct=True) or self._refuse()
            return self._passage(q, sid, view) or self._refuse()
        except MemoryError:
            raise             # resource exhaustion must not masquerade as an honest "I don't know"
        except Exception as e:  # a crash must never surface a guessed value
            print(f"answer() failed: {type(e).__name__}: {e}", file=sys.stderr)
            return self._refuse(f"internal error ({type(e).__name__})")   # no internals in the answer

    def _row_ok(self, q: Query, f: Fact) -> bool:
        if q.terms and not (q.terms & f.terms):
            return False
        asked = set(q.flags)
        if asked & {"roth", "traditional"}:
            asked.add("ira")
        for flag in ("jumbo", "ira", "refinance", "intro"):
            if flag == "jumbo" and q.superlative == "max" and "jumbo" in f.flags:
                continue      # "which special pays the most" must consider the jumbo tier too
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

    def _rate_groups(self, q: Query, view: dict[str, int], not_after: str | None = None):
        idf = self._idf()
        want = sorted(set(q.ptoks))
        if not want:
            return []
        if self.bm25._dirty:
            self.bm25._finalize()
        # A word no rate table uses weighs as much as it is rare on the site: "construction" (a real product
        # with no published rate) sinks coverage and forces a refusal, a common verb like "put" barely counts.
        cidf, cmax = self.bm25.idf, max(self.bm25.idf.values(), default=self._max_idf)
        denom = sum(idf[t] if t in idf else min(self._max_idf, cidf.get(t, cmax)) for t in want)
        groups: dict[tuple[int, int], list[tuple[float, Fact]]] = defaultdict(list)
        for did in sorted(set(view.values())):
            for fi in self.facts_by_doc.get(did, ()):
                f = self.facts[fi]
                if not self._row_ok(q, f):
                    continue
                if not_after and not (f.row.effective and f.row.effective <= not_after):
                    continue
                s = sum(idf[t] for t in want if t in f.toks)
                # In "X and Y" each product covers only its half of the question; the union is checked below.
                if s > 0 and s / denom >= (0.25 if q.multi and not q.listing else 0.5):
                    groups[(did, f.row.table)].append((s, f))
        if not groups:
            return []
        top = max(s for g in groups.values() for s, _ in g)
        # A listing question ("what cards do you offer") wants every matching product, not just the best one.
        floor = 0.0 if q.listing else top * 0.999
        tied = {k: [f for s, f in g] for k, g in groups.items() if max(s for s, _ in g) >= floor}
        if q.multi and not q.listing:
            # "Bright Savings and High-Yield Savings": greedily add the group that covers the most question
            # words the chosen groups leave uncovered, so every product named gets an answer.
            covered = set().union(*(f.toks for fs in tied.values() for f in fs))
            for _ in range(3):
                gain = {k: sum(idf.get(t, 0) for t in want if t not in covered and any(t in f.toks for _, f in g))
                        for k, g in groups.items() if k not in tied}
                best = max(sorted(gain), key=lambda k: gain[k], default=None)
                if best is None or gain[best] <= 0:
                    break
                tied[best] = [f for _, f in groups[best]]
                covered |= set().union(*(f.toks for f in tied[best]))
            if sum(idf.get(t, 0) for t in want if t in covered) / denom < 0.5:
                return []           # together the products still miss most of the question: abstain
        best_bucket = max(_bucket(self.docs[k[0]].auth) for k in tied)
        out = [(k, fs) for k, fs in sorted(tied.items()) if _bucket(self.docs[k[0]].auth) == best_bucket]
        if q.amounts:
            # Ceiling tiers nest ("$100,000 or less" also sits inside "$300,000 or less"): keep the tightest.
            def width(f):
                return f.tier[1] - f.tier[0] if f.tier else float("inf")
            out = [(k, [f for f in fs if width(f) == min(width(g) for g in fs)]) for k, fs in out]
        return out

    def _rate(self, q: Query, sid: str, view: dict[str, int], not_after: str | None = None):
        groups = self._rate_groups(q, view, not_after)
        if not groups:
            return None
        order = {did: i for i, did in enumerate(view.values())}
        # Merge identical products shown on several pages (rate sheet + product page + stale promo copy).
        merged: dict[tuple, list[tuple[tuple[int, int], list[Fact]]]] = defaultdict(list)
        for key, fs in groups:
            sig = tuple(sorted(tuple(f.row.labels) for f in fs))
            merged[sig].append((key, fs))
        products, notes, sources = [], [], []
        for members in merged.values():
            members.sort(key=lambda m: max(self._prio(sid, m[0][0], f) for f in m[1]), reverse=True)
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
            plain = [r for r in rows if "jumbo" not in r[1].flags]
            rows = [pick]
            if "jumbo" in pick[1].flags and "jumbo" not in q.flags and plain:
                rows.append(max(plain, key=val))     # the jumbo tops the list; also name the best regular one
        text = self._compose_rates(q, rows)
        if q.history:
            text = f"{text} {self._history_text(q, sid, rows)}".strip()
        if q.intent == "url":
            urls = sorted({s.url for s in sources}, key=lambda u: (self.docs[view[canon_url(u)]].kind == "rate_sheet", u))
            text = "You can find them at " + " and ".join(f"{u}#rates" for u in urls) + "."
        uniq = list({s.url: s for s in reversed(sources)}.values())[::-1]     # first source per URL, in order
        return self.c.Answer(text=text, sources=uniq, confident=True, conflict_note=" ".join(notes) or None)

    def _compose_rates(self, q: Query, rows) -> str:
        parts, last_caption = [], None
        if q.listing and len(rows) > 16:
            # Too many tiers to read back: one line per product (its first tier) so every product is named.
            firsts, seen = [], set()
            for fs, f in rows:
                if id(fs) not in seen:
                    seen.add(id(fs))
                    firsts.append((fs, f))
            rows = firsts
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
            dids = [d for d in dids if _mentions(name, self.docs[d].text)]
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

    def _coverage(self, q: Query, pid: int, did: int, product_only: bool = False) -> float:
        idf = self.bm25.idf
        mx = max(idf.values()) if idf else 1.0
        heading, text = self.pass_text[pid]
        have = set(tokens(f"{self.docs[did].title} {heading} {text}"))
        want = set(q.ptoks if product_only else q.toks)
        if not want:
            return 0.0
        return sum(idf.get(t, mx) for t in want & have) / sum(idf.get(t, mx) for t in want)

    def _passage(self, q: Query, sid: str, view: dict[str, int], need_pct: bool = False):
        if not q.toks:
            return None
        picked = []
        for score, pid, did in self._ranked_passages(q, view, k=12):
            heading, text = self.pass_text[pid]
            if need_pct and (self.docs[did].auth < 2 or not RATE_STATEMENT.search(text)
                             or (q.terms and not q.terms & terms(text))
                             or self._coverage(q, pid, did, product_only=True) < 0.75):
                continue
            if q.branch and not _mentions(q.branch, f"{self.docs[did].title} {text}"):
                continue
            if re.sub(r"\s*\(https?://[^)]*\)\s*$", "", text).rstrip().endswith("?"):
                continue          # an FAQ question line is not an answer
            if "http" in text and len(re.sub(r"\([^)]*https?://[^)]*\)", " ", text).split()) < 8:
                continue          # link-only navigation ("Continue Cancel Keep reading (url)") carries no answer
            if self._coverage(q, pid, did) < 0.4:
                continue
            if picked and (score < 0.6 * picked[0][0] or any(text == t for _, _, _, t in picked)):
                continue
            picked.append((score, pid, did, text))
            if len(picked) == 2:
                break
        if not picked:
            return None
        stale = self._stale_values(sid, q)
        pieces = []
        for *_, t in picked:
            piece = self._best_sentences(q, t, limit=360 if len(picked) > 1 else 420)
            # OWASP AST05 / output whitelist: a sentence quoting a value that is only ever superseded or stale
            # is dropped, whatever page it came from.
            keep = [s for s in _sentences(piece) if not (_pct_values(s) & stale)]
            if keep:
                pieces.append(" ".join(keep))
        if not pieces:
            return None
        body = " ".join(pieces)
        srcs = []
        for _, _, did, t in picked:
            if all(s.url != self.docs[did].url for s in srcs):
                srcs.append(self._src(did, sid, t))
        return self.c.Answer(text=body, sources=srcs, confident=True)

    def _url(self, q: Query, sid: str, view: dict[str, int]):
        per_doc: Counter = Counter()
        for score, _, did in self._ranked_passages(q, view, k=60):
            per_doc[did] += score
        if not per_doc:
            return None
        did = sorted(per_doc, key=lambda d: (-per_doc[d], d))[0]
        src = self._src(did, sid)
        return self.c.Answer(text=f"You can find it at {src.url}", sources=[src], confident=True)

    def _address(self, q: Query, sid: str, view: dict[str, int]):
        name = (q.branch or "").lower()
        if not name:
            return None
        hits = []
        for did in sorted(set(view.values()), key=lambda d: (-self.docs[d].auth, d)):
            for line in ADDRESS.findall(self.docs[did].text):
                if _mentions(name, line):
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
        # ponytail: scans every matching sentence in the snapshot (~10 ms here); index count claims at ingest
        # if the corpus grows by orders of magnitude.
        probe = Query(text=q.text, as_of=q.as_of, toks=[word] + others, ptoks=[])
        ref = self.snaps[-1][1] if q.as_of is None else str(q.as_of)[:10]
        voted, newest = set(), {}
        for _, pid, did in self._ranked_passages(probe, view, k=3000):
            for s in _sentences(self.pass_text[pid][1]):
                st = tokens(s)
                if word not in st or (others and len(set(others) & set(st)) < max(1, len(others) // 2)):
                    continue
                for m in re.finditer(r"(\d[\d,]*)\+?\s+((?:[\w-]+\s+){0,2}?)([A-Za-z-]+)", s):
                    if tokens(m.group(3)) == [word] or (tokens(m.group(3)) and tokens(m.group(3))[0] == word):
                        n = m.group(1)
                        if (did, n) in voted:
                            continue
                        voted.add((did, n))
                        # Boilerplate counts ("22 branch office locations") drift as branches open and close:
                        # the most recently dated statement (not after the reference date) wins; page votes
                        # only break ties.
                        pub = self.docs[did].published or ""
                        if pub > ref:
                            continue
                        votes[n] += PASSAGE_WEIGHT[self.docs[did].kind]
                        if pub >= newest.get(n, ("", 0))[0]:
                            newest[n] = (pub, did)
                            example[n] = (did, s)
        if not votes:
            return None
        best = sorted(votes, key=lambda n: (newest[n][0], votes[n], n), reverse=True)[0]
        did, sentence = example[best]
        note = None
        if len(votes) > 1:
            note = ("Pages disagree: " + ", ".join(f"{n} (latest {newest[n][0] or 'undated'})" for n in sorted(votes))
                    + "; used the most recently published statement.")
        return self.c.Answer(text=sentence, sources=[self._src(did, sid, sentence)], confident=True,
                             conflict_note=note)

    # ------------------------------------------------------------------ conflicts
    def conflicts(self):
        if self._conflicts is None:
            self._conflicts = self._table_conflicts() + self._count_conflicts()
        return list(self._conflicts)

    def _prio(self, sid: str, did: int, f: Fact):
        d = self.docs[did]
        return (_bucket(d.auth), f.row.effective or "", self.fetched.get((sid, did), ""), d.auth, -did)

    def _observations(self):
        """fact identity -> snapshot index -> [(priority, doc, value, fact)] across every crawl (cached)."""
        if self._obs is None:
            obs: dict[tuple, dict[int, list]] = defaultdict(lambda: defaultdict(list))
            for si, (sid, _) in enumerate(self.snaps):
                for did in sorted(set(self.views[sid].values())):
                    for fi in self.facts_by_doc.get(did, ()):
                        f = self.facts[fi]
                        metric, value = _main_value(f.row.values)
                        obs[_fact_key(f) + (metric,)][si].append((self._prio(sid, did, f), did, value, f))
            self._obs = obs
        return self._obs

    def _timeline(self, f: Fact) -> list[tuple[int, str]]:
        """Authoritative value of this fact in each crawl it appears in: [(snapshot index, value)]."""
        per_snap = self._observations().get(_fact_key(f) + (_main_value(f.row.values)[0],), {})
        out = []
        for si in sorted(per_snap):
            prio, _, val, _ = max(per_snap[si], key=lambda x: x[0])
            if prio[0] >= 4:
                out.append((si, val))
        return out

    def _history_text(self, q: Query, sid: str, rows) -> str:
        """Old values are shown only for an explicit change question, and only for the facts answered."""
        now_si = next(i for i, (s, _) in enumerate(self.snaps) if s == sid)
        month = lambda si: date.fromisoformat(self.snaps[si][1]).strftime("%B %Y")    # noqa: E731
        parts = []
        for _, f in rows[:3]:
            tl = [(si, v) for si, v in self._timeline(f) if si <= now_si]
            if not tl:
                continue
            current = tl[-1][1]
            label = ", ".join(f.row.labels)
            if q.compare_to:
                base = self._snapshot_for(q.compare_to)
                base_si = next(i for i, (s, _) in enumerate(self.snaps) if s == base)
                then = [v for si, v in tl if si <= base_si]
                if not then:
                    continue
                trend = ("up from" if _first_num(current) > _first_num(then[-1]) else
                         "down from" if _first_num(current) < _first_num(then[-1]) else "unchanged from")
                parts.append(f"{label}: {current} now, {trend} {then[-1]} in {month(base_si)}.")
            else:
                earlier = [(si, v) for si, v in tl if v != current]
                if earlier:
                    si, v = earlier[-1]
                    trend = "up" if _first_num(current) > _first_num(v) else "down"
                    parts.append(f"{label}: previously {v} (through the {month(si)} crawl); now {current}, {trend}.")
                else:
                    parts.append(f"{label}: unchanged at {current} since the {month(tl[0][0])} crawl.")
        return " ".join(parts)

    def _table_conflicts(self):
        obs = self._observations()
        out, seen = [], set()
        latest = len(self.snaps) - 1
        for key in sorted(obs, key=lambda k: (bool(k[1]), k)):
            per_snap = obs[key]
            timeline, stale = [], {}
            for si in sorted(per_snap):
                ranked = sorted(per_snap[si], key=lambda x: x[0], reverse=True)
                wprio, wdid, wval, wf = ranked[0]
                if wprio[0] < 4:
                    continue      # only a rate sheet or product page can define the current value
                if not timeline or timeline[-1][1] != wval:
                    timeline.append((si, wval, wdid, wf))
                if si != latest:
                    continue      # cross-page disagreements are judged on the newest crawl only
                for prio, did, val, f in ranked[1:]:
                    # Stale copy: a less authoritative page, or an equally authoritative one with an older
                    # effective date. Same-rank pages crawled minutes apart are not a contradiction.
                    if val != wval and (prio[0] < wprio[0] or (prio[1] and wprio[1] and prio[1] < wprio[1])):
                        stale.setdefault(val, (si, did, f, wval, wdid))
            if not timeline:
                continue
            last_si, current, cur_did, cur_f = timeline[-1]
            topic = _topic(cur_f, key[-1])
            if len({v for _, v, _, _ in timeline}) > 1:
                values = list(dict.fromkeys(v for _, v, _, _ in timeline))
                # IRA and business mirrors of the same special move in lockstep: report the history once.
                dedupe = (tuple(values), tuple(sorted(cur_f.terms)), "t")
                if dedupe not in seen:
                    seen.add(dedupe)
                    first = timeline[0]
                    out.append(self.c.Conflict(
                        topic=topic, values=values,
                        sources=[self._src(d, self.snaps[si][0], f.row.line) for si, _, d, f in timeline],
                        resolution=current,
                        reason=(f"Value changed between snapshots ({first[1]} in {self.snaps[first[0]][0]} -> "
                                f"{current} in {self.snaps[last_si][0]}); the newest snapshot of the most "
                                f"authoritative page ({self.docs[cur_did].url}) wins.")))
            stale = {v: s for v, s in stale.items() if v != current}
            if stale:
                values = [current] + sorted(stale)
                if (key[0], tuple(values), "x") not in seen:
                    seen.add((key[0], tuple(values), "x"))
                    srcs = [self._src(cur_did, self.snaps[last_si][0], cur_f.row.line)]
                    srcs += [self._src(d, self.snaps[si][0], f.row.line) for v, (si, d, f, _, _) in sorted(stale.items())]
                    where = ", ".join(sorted({f"{self.docs[d].url} ({self.docs[d].kind})"
                                              for si, d, f, _, _ in stale.values()}))
                    out.append(self.c.Conflict(
                        topic=topic, values=values, sources=srcs, resolution=current,
                        reason=(f"{where} shows {', '.join(sorted(stale))}; {self.docs[cur_did].url} "
                                f"({self.docs[cur_did].kind}) is authoritative and current.")))
        # Open contradictions in the newest crawl first, then superseded values; specific before broad.
        return sorted(out, key=lambda c: (c.reason.startswith("Value changed"), len(c.values)))

    def _count_conflicts(self):
        """Narrative boilerplate counts that drift between press releases (e.g. branch office locations)."""
        out = []
        latest = self.snaps[-1][0]
        view = self.views[latest]
        for noun, question in (("branch", "How many branches does Dupaco have?"),):
            ans = self._count(parse(question, default_year=int(self.snaps[-1][1][:4])), latest, view)
            if not ans or not ans.conflict_note:
                continue
            values = re.findall(r"([\d,]+) \(latest", ans.conflict_note)
            winner = re.search(r"([\d,]+)\s+" + noun, ans.text)
            if len(values) > 1 and winner:
                out.append(self.c.Conflict(
                    topic=f"number of {noun} office locations ({noun}es)", values=values, sources=ans.sources,
                    resolution=winner.group(1),
                    reason="Press-release boilerplate differs by publication date; the most recently "
                           "published statement wins."))
        return out
