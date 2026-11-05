"""Parse a member question into intent, product tokens, qualifiers and an effective date."""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from .extract import MONTHS, terms
from .search import RATE_WORDS, norm_token, normalize, tokens

MONTH_ALT = "|".join(MONTHS)
ABBR = {"jan": 1, "feb": 2, "mar": 3, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11,
        "dec": 12}            # no "apr" (APR) and no bare "may" (the verb)
SEASON_END = {"spring": (3, 5), "summer": (6, 8), "fall": (9, 11), "autumn": (9, 11)}
ORDINAL = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4}
FUTURE = re.compile(r"\b(will|going to|forecast|predict\w*|expect\w*|projected?|in the future|next year|"
                    r"anticipat\w*)\b", re.I)
CHANGE = re.compile(r"\b(previous(ly)?|prior|used to|go(ne|es)? (up|down)|went (up|down)|increas(e|ed|es)|"
                    r"decreas(e|ed|es)|rais(e|ed)|lowered|dropp?(ed)?|fell|rose|chang(e|ed|es)|history|"
                    r"originally|initially|before (it|that|the change))\b", re.I)
VERSUS = re.compile(r"\b(vs\.?|versus|compared (to|with)|than)\b.*\b(now|today|currently|current)\b|"
                    r"\b(now|today|currently)\b.*\b(vs\.?|versus|compared (to|with))\b", re.I)
# Prompt-injection phrasing inside a question. Stripped only so it does not distort parsing; safety does not
# rely on it - answers are built from site data, never from text in the question.
INJECTION = (r"\b(ignore|disregard|forget|override|bypass)\b(\s+\w+){0,4}?\s+(instructions?|rules?|prompts?|"
             r"guidelines|context|data)\b", r"\b(system|assistant|developer|admin)\s*(prompt)?\s*:",
             r"\b(just\s+)?(say|answer|respond|reply|state|confirm|output|print)\s+(that|with|only)\b",
             r"\byou (must|should|will) now\b", r"\bnew instructions?\b")


def _d(y: int, m: int, day: int | None = None) -> date:
    return date(y, m, day or calendar.monthrange(y, m)[1])


def _period(text: str, default_year: int, today: date | None):
    """Find the first date phrase: returns ((start, end), span) or (None, None)."""
    low = text.lower()
    rules = []
    for m in re.finditer(r"\b(20\d\d)[-/.](\d{1,2})[-/.](\d{1,2})\b", low):
        rules.append((m, lambda m: (_d(int(m[1]), int(m[2]), int(m[3])),) * 2))
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})/(20\d\d)\b", low):
        rules.append((m, lambda m: (_d(int(m[3]), int(m[1]), int(m[2])),) * 2))
    for m in re.finditer(r"\b(?:q([1-4])|(first|second|third|fourth|1st|2nd|3rd|4th) quarter)(?:\s+(?:of\s+)?(20\d\d))?\b",
                         low):
        def quarter(m):
            n, y = int(m[1]) if m[1] else ORDINAL[m[2]], int(m[3] or default_year)
            return _d(y, 3 * n - 2, 1), _d(y, 3 * n)
        rules.append((m, quarter))
    for m in re.finditer(r"\b(first|second|1st|2nd) half of (20\d\d)\b", low):
        rules.append((m, lambda m: (_d(int(m[2]), 1, 1), _d(int(m[2]), 6)) if ORDINAL[m[1]] == 1
                      else (_d(int(m[2]), 7, 1), _d(int(m[2]), 12))))
    for m in re.finditer(r"\b(spring|summer|fall|autumn)\s+(?:of\s+)?(20\d\d)\b", low):
        rules.append((m, lambda m: (_d(int(m[2]), SEASON_END[m[1]][0], 1), _d(int(m[2]), SEASON_END[m[1]][1]))))
    for m in re.finditer(r"\b(early|mid|middle of|late|end of|start of|beginning of)[\s-]+(" + MONTH_ALT +
                         r")(?:,?\s+(20\d\d))?\b", low):
        def qualified(m):
            y, mo = int(m[3] or default_year), MONTHS.index(m[2]) + 1
            day = {"early": 10, "start of": 1, "beginning of": 1, "mid": 15, "middle of": 15}.get(m[1])
            return (_d(y, mo, day),) * 2
        rules.append((m, qualified))
    for m in re.finditer(r"\b(" + MONTH_ALT + "|" + "|".join(ABBR) + r")\.?(?:\s+(\d{1,2})(?:st|nd|rd|th)?,?)?"
                         r"(?:\s+(20\d\d))?\b", low):
        word = m[1]
        if word in ABBR and not (m[2] or m[3]):
            continue
        if word == "may" and not m[3] and not m[2] and not re.search(
                r"\b(as of|in|back in|during|since|late|early|mid|before|after|of)\s+may\b", low):
            continue          # the verb "may"
        def month_day(m):
            y, mo = int(m[3] or default_year), (ABBR.get(m[1]) or MONTHS.index(m[1]) + 1)
            if m[2]:
                return (_d(y, mo, min(int(m[2]), calendar.monthrange(y, mo)[1])),) * 2
            return _d(y, mo, 1), _d(y, mo)
        rules.append((m, month_day))
    if today:
        for m in re.finditer(r"\b(\d+|a|an|one|two|three|four|five|six)\s+(months?|weeks?)\s+ago\b", low):
            def ago(m):
                n = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}.get(m[1])
                n = n or int(m[1])
                back = timedelta(weeks=n) if m[2].startswith("week") else timedelta(days=30 * n)
                return (today - back,) * 2
            rules.append((m, ago))
        for m in re.finditer(r"\blast month\b", low):
            first = today.replace(day=1) - timedelta(days=1)
            rules.append((m, lambda m, f=first: (f.replace(day=1), f)))
    for m, fn in sorted(rules, key=lambda r: r[0].start()):
        try:
            start, end = fn(m)
        except (ValueError, KeyError):
            continue
        if re.search(r"\b(before|prior to)\s+(the\s+)?$", low[:m.start()]):
            end = start - timedelta(days=1)     # "before the August update" -> the crawl before August
        return (start, end), m.span()
    return None, None


def norm_as_of(value) -> str | None:
    """Accept as_of as ISO date/datetime, 2026/06/30, 06/30/2026, 'June 2026' or empty -> ISO date or None."""
    if value is None:
        return None
    s = normalize(str(value)).strip()
    if not s:
        return None
    period, _ = _period(s, 2026, None)
    if period:
        return period[1].isoformat()
    m = re.fullmatch(r"(20\d\d)-(\d{1,2})", s)
    if m:
        return _d(int(m[1]), int(m[2])).isoformat()
    m = re.fullmatch(r"(20\d\d)", s)
    return f"{s}-12-31" if m else None

NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
             "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "seventeen": 17, "eighteen": 18, "twenty": 20,
             "twenty-four": 24, "thirty": 30, "thirty-six": 36, "forty-eight": 48, "sixty": 60,
             "seventy-two": 72, "eighty-four": 84}
MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|million)?\b", re.I)
RATE_INTENT = re.compile(r"\b(rates?|aprs?|apys?|interest|dividends?|yields?|percent|charging)\b|%", re.I)
# Member phrasings rewritten to the site's vocabulary before tokenizing.
PHRASES = ((r"\bhome loans?\b", "mortgage"), (r"\bdrive[- ]?(thru|through)\b", "drive-up"),
           (r"\bsign(ing)? up\b", "join"), (r"\bbecome an? (dupaco )?member\b", "join"),
           (r"\bover the internet\b|\bon the internet\b", "online"), (r"\blobbies\b", "lobby"))
# Words that qualify a lookup rather than name a product; they must not count against table coverage.
QUALIFIER_WORDS = {norm_token(w) for w in (
    "highest", "best", "top", "maximum", "max", "most", "lowest", "cheapest", "minimum", "least", "standard",
    "tier", "tiers", "between", "link", "url", "website", "page", "web", "site", "find", "look", "option",
    "options", "product", "products", "type", "types", "kind", "available", "all", "list", "every", "offer",
    "much", "many", "term", "month", "year", "new", "used", "range", "personal", "qualify", "intro",
    "introductory", "ignoring", "excluding", "besides", "open", "opening", "previous", "previously", "prior",
    "change", "changed", "before", "now", "vs", "versus", "compared", "since", "went", "go", "gone", "up",
    "down", "increase", "increased", "decrease", "decreased", "raise", "raised", "lowered", "dropped",
    "history", "originally", "better", "worse", "higher", "lower", "both", "compare", "still", "why", "true",
    "really", "correct", "right", "wrong", "actually")}
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
    future: bool = False            # asks for a value after the newest crawl ("what will it be next year")
    history: bool = False           # asks how a value changed, not just what it is
    compare_to: str | None = None   # the earlier date a history question compares against
    multi: bool = False             # "X and Y": answer every product named, not only the best match


def _date_from_text(text: str, default_year: int, today: date | None = None):
    """'as of May 2026' -> '2026-05-31'; returns (iso_date, span) or (None, None)."""
    period, span = _period(text, default_year, today)
    return (period[1].isoformat(), span) if period else (None, None)


def parse(question: str, as_of: str | None = None, default_year: int = 2026, today: str | None = None) -> Query:
    text = " ".join(normalize(question).split())
    for pat in INJECTION:
        text = re.sub(pat, " ", text, flags=re.I)
    text = " ".join(text.split())
    body = text
    ref = date.fromisoformat(today) if today else None
    found, span = _date_from_text(text, default_year, ref)
    if span:
        body = text[:span[0]] + " " + text[span[1]:]
    as_of = norm_as_of(as_of)
    history = bool(CHANGE.search(text) or (found and VERSUS.search(text)))
    compare_to = None
    if history and found and as_of is None:
        compare_to, found = found, None         # "in May vs now": May is the baseline, now is the answer date
    if as_of is None:
        as_of = found
    low = body.lower().replace("share certificate", "certificate")
    for a, b in PHRASES:
        low = re.sub(a, b, low)
    for w, n in NUM_WORDS.items():
        low = re.sub(rf"\b{w}[\s-]+(month|year)", f"{n} \\1", low)

    q = Query(text=text, as_of=as_of, toks=tokens(low), ptoks=[])
    q.compare_to = compare_to
    q.future = bool(ref and found and found > ref.isoformat() and FUTURE.search(text))
    q.multi = bool(re.search(r"\b(and|or|vs\.?|versus|both|compare[ds]?|as well as)\b|&|,", low))
    q.terms = terms(low)
    q.years = [int(y) for y in re.findall(r"\b(19[5-9]\d|20[0-4]\d)\b", low)]
    for m in MONEY.finditer(low):
        v = float(m.group(1).replace(",", ""))
        unit = (m.group(2) or "").lower()
        q.amounts.append(v * (1000 if unit in ("k", "thousand") else 1e6 if unit == "million" else 1))
    q.amounts += [float(n) * 1000 for n in re.findall(r"(?<![$\d.,])(?<!\$ )\b(\d{2,3})k\b", low)]  # bare "50k"

    if re.search(r"\bapys?\b|annual percentage yield|\byield", low):
        q.metric = "apy"
    elif re.search(r"\baprs?\b|annual percentage rate", low):
        q.metric = "apr"
    elif "dividend rate" in low:
        q.metric = "dividend"
    for flag, pat in (("jumbo", r"\bjumbo"), ("ira", r"\bira\b|\biras\b|retirement"), ("roth", r"\broth\b"),
                      ("traditional", r"\btraditional\b"), ("business", r"\bbusiness|commercial"),
                      ("refinance", r"refinanc|\brefi\b"), ("intro", r"\bintro|balance transfer")):
        if re.search(pat, low):
            q.flags.add(flag)
    if "business" in q.flags and re.search(r"\bpersonal\b", low):
        q.flags.discard("business")     # "personal and business cards": list both segments
    if re.search(r"\b(ignor\w*|exclud\w*|without|not|other than|besides|after)\s+(the\s+|an?\s+)?intro", low):
        q.flags.discard("intro")
    if re.search(r"\b(highest|best|top|maximum|max|most)\b", low):
        q.superlative = "max"
    elif re.search(r"\b(lowest|cheapest|minimum|least)\b", low):
        q.superlative = "min"

    if re.search(r"\b(link|url|web ?page|web address|website|which page|what page)\b", low):
        q.intent = "url"
    elif re.search(r"\bhours\b|\bwhat time\b|\bopen (on|until) (mon|tue|wed|thu|fri|sat|sun)|"
                   r"\bwhen (is|are)\b.*\bopen\b", low):
        q.intent = "hours"
        q.hours_kind = "drive" if re.search(r"drive", low) else "lobby"
    elif re.search(r"\baddress\b|\bwhere is\b|\bwhere's\b|\bwhere are\b|\blocated\b|\bdirections\b",
                   low) and q.terms == set():
        q.intent = "address"
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
    for pat in (r"((?:[\w'’.&-]+,?\s+){1,3})(?:branch|office|location)\b(?!es)",
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

    q.history = history and q.intent == "rate"
    q.ptoks = [t for t in q.toks if t not in RATE_WORDS and t not in QUALIFIER_WORDS
               and not re.fullmatch(r"\d+k?", t)]
    return q
