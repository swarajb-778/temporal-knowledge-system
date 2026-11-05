"""Parse a member question into intent, product tokens, qualifiers and an effective date."""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field

from .extract import MONTHS, terms
from .search import RATE_WORDS, norm_token, tokens

NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
             "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "seventeen": 17, "eighteen": 18, "twenty": 20,
             "twenty-four": 24, "thirty": 30, "thirty-six": 36, "forty-eight": 48, "sixty": 60,
             "seventy-two": 72, "eighty-four": 84}
MONTH_DATE = re.compile(r"\b(" + "|".join(MONTHS) + r")\.?(?:\s+(\d{1,2})(?:st|nd|rd|th)?,?)?(?:\s+(\d{4}))?\b", re.I)
ISO_DATE = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|million)?\b", re.I)
RATE_INTENT = re.compile(r"\b(rates?|aprs?|apys?|interest|dividends?|yields?|percent)\b|%", re.I)
# Words that qualify a lookup rather than name a product; they must not count against table coverage.
QUALIFIER_WORDS = {norm_token(w) for w in (
    "highest", "best", "top", "maximum", "max", "most", "lowest", "cheapest", "minimum", "least", "standard",
    "tier", "tiers", "between", "link", "url", "website", "page", "web", "site", "find", "look", "option",
    "options", "product", "products", "type", "types", "kind", "available", "all", "list", "every", "offer",
    "much", "many", "term", "month", "year", "new", "used", "range", "personal", "qualify", "intro",
    "introductory", "ignoring", "excluding", "besides", "open", "opening")}
FILLER_CAPS = {"dupaco", "the", "which", "what", "your", "each", "any", "a", "an", "is", "are", "do", "does", "can",
               "when", "where", "how", "our", "my", "give", "tell", "me", "for", "of", "at", "in", "to", "and",
               "nearest", "closest", "local", "main", "new", "newest", "that", "this", "near", "every", "all",
               "hours", "lobby", "drive-up", "address", "open", "still", "full-service", "same", "other", "one",
               "credit", "union", "home", "office", "online", "about", "i", "visit", "go", "from", "with", "or"}


@dataclass
class Query:
    text: str
    as_of: str | None
    toks: list[str]                 # content tokens for passage search
    ptoks: list[str]                # product tokens for rate-table matching
    terms: set[int] = field(default_factory=set)
    years: list[int] = field(default_factory=list)
    amounts: list[float] = field(default_factory=list)
    metric: str | None = None
    flags: set[str] = field(default_factory=set)
    superlative: str | None = None
    intent: str = "general"         # rate | hours | url | count | general
    listing: bool = False
    soft: bool = False              # routed to rate tables by wording only: fall back to passages, never refuse
    hours_kind: str = "lobby"
    branch: str | None = None
    count_word: str | None = None


def _date_from_text(text: str, default_year: int):
    """'as of May 2026' -> '2026-05-31'; returns (iso_date, span) or (None, None)."""
    m = ISO_DATE.search(text)
    if m:
        return m.group(0), m.span()
    for m in MONTH_DATE.finditer(text):
        word = m.group(1).lower()
        if word == "may" and not m.group(3) and not re.search(r"\b(as of|in|back in|during|since)\s+may\b",
                                                               text, re.I):
            continue          # the verb "may"
        year = int(m.group(3)) if m.group(3) else default_year
        month = MONTHS.index(word) + 1
        day = int(m.group(2)) if m.group(2) else calendar.monthrange(year, month)[1]
        return f"{year}-{month:02d}-{day:02d}", m.span()
    return None, None


def parse(question: str, as_of: str | None = None, default_year: int = 2026) -> Query:
    text = " ".join((question or "").split())
    body = text
    found, span = _date_from_text(text, default_year)
    if span:
        body = text[:span[0]] + " " + text[span[1]:]
    if as_of is None:
        as_of = found
    low = body.lower().replace("share certificate", "certificate")
    for w, n in NUM_WORDS.items():
        low = re.sub(rf"\b{w}[\s-]+(month|year)", f"{n} \\1", low)

    q = Query(text=text, as_of=as_of, toks=tokens(low), ptoks=[])
    q.terms = terms(low)
    q.years = [int(y) for y in re.findall(r"\b(19[5-9]\d|20[0-4]\d)\b", low)]
    for m in MONEY.finditer(low):
        v = float(m.group(1).replace(",", ""))
        unit = (m.group(2) or "").lower()
        q.amounts.append(v * (1000 if unit in ("k", "thousand") else 1e6 if unit == "million" else 1))
    q.amounts += [float(n) * 1000 for n in re.findall(r"\b(\d{2,3})k\b", low)]

    if re.search(r"\bapys?\b|annual percentage yield|\byield", low):
        q.metric = "apy"
    elif re.search(r"\baprs?\b|annual percentage rate", low):
        q.metric = "apr"
    elif "dividend" in low:
        q.metric = "dividend"
    for flag, pat in (("jumbo", r"\bjumbo"), ("ira", r"\bira\b|\biras\b|retirement"), ("roth", r"\broth\b"),
                      ("traditional", r"\btraditional\b"), ("business", r"\bbusiness|commercial"),
                      ("refinance", r"refinanc|\brefi\b"), ("intro", r"\bintro|balance transfer")):
        if re.search(pat, low):
            q.flags.add(flag)
    if re.search(r"\b(ignor\w*|exclud\w*|without|not|other than|besides|after)\s+(the\s+|an?\s+)?intro", low):
        q.flags.discard("intro")
    if re.search(r"\b(highest|best|top|maximum|max|most)\b", low):
        q.superlative = "max"
    elif re.search(r"\b(lowest|cheapest|minimum|least)\b", low):
        q.superlative = "min"

    if re.search(r"\b(link|url|web ?page|web address|website address)\b", low):
        q.intent = "url"
    elif re.search(r"\baddress\b|\bwhere is\b|\blocated\b", low) and q.terms == set():
        q.intent = "address"
    elif re.search(r"\bhours\b|\bwhat time\b|\bopen (on|until) (mon|tue|wed|thu|fri|sat|sun)", low):
        q.intent = "hours"
        q.hours_kind = "drive" if re.search(r"drive|drive-up", low) else "lobby"
    elif re.search(r"\bhow many\b", low):
        q.intent = "count"
        after = tokens(low.split("how many", 1)[1])
        q.count_word = after[0] if after else None
    elif RATE_INTENT.search(low) or (re.search(r"\b(pay|pays|earn|earns)\b", low)
                                     and not re.search(r"\b(fee|fees|cost|charge|bill|loan payment)\b", low)):
        q.intent = "rate"
    elif re.search(r"\bspecials?\b|\btiers?\b|\bterms\b", low):
        q.intent, q.soft = "rate", True     # "what are the certificate specials / terms" -> rate table first
    q.listing = bool(re.search(r"\b(cards|certificates|cds|accounts|loans|products|options|specials|terms|all|"
                               r"list|every|types? of|kinds? of)\b", low))
    if q.metric == "apr" and re.search(r"\brate\b", low) and "annual percentage rate" not in low:
        q.metric = None             # "rate and APR": show both columns

    # Case-insensitive on purpose: members type "galena branch" and "GALENA BRANCH" too.
    for pat in (r"((?:[\w'’.&-]+\s+){1,3})(?:branch|office|location)\b(?!es)",
                r"\b(?:branch|office|location)\s+(?:in|at|on)\s+((?:[\w'’.&-]+\s*){1,2})"):
        m = re.search(pat, text, re.I)
        if m:
            words = [re.sub(r"['’]s$", "", w.lower()).strip(".,?") for w in m.group(1).split()]
            while words and words[0] in FILLER_CAPS:
                words.pop(0)
            if words and not any(w in FILLER_CAPS for w in words):
                q.branch = " ".join(words)
                break
    if q.intent == "general" and q.listing and q.branch is None and re.search(
            r"\b(savings|checking|certificates?|cds?|money market|ira|loans?|cards?)\b", low):
        q.intent, q.soft = "rate", True     # "what savings accounts can I open" -> products on the rate sheet

    q.ptoks = [t for t in q.toks if t not in RATE_WORDS and t not in QUALIFIER_WORDS
               and not re.fullmatch(r"\d+k?", t)]
    return q
