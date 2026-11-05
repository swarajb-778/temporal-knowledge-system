"""Turn crawled markdown pages into typed pieces: page kind, table-row facts, hours blocks, passages."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

# Higher wins when pages disagree. Rate sheets and product pages are maintained by the
# credit union; calculators, promos, PDFs and dated news/blog posts are where stale values live.
AUTHORITY = {"rate_sheet": 5, "product": 4, "guide": 2, "promo": 2, "file": 2, "legal": 1,
             "calculator": 1, "news": 1, "blog": 0, "listing": -1}
PRODUCT_PREFIXES = ("/personal", "/business", "/about", "/connect", "/contact", "/savings-account")
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september",
          "october", "november", "december")

PCT = re.compile(r"(\d+(?:\.\d+)?)\s?%")
MONEY = r"\$\s?(\d[\d,]*(?:\.\d+)?)"
TERM = re.compile(r"\b(\d{1,3})\s*-?\s*(month|mo|year|yr)s?\b", re.I)
MODEL_YEARS = re.compile(r"\b(\d{4})\s*[-–]\s*(\d{2,4})\s+model\s+year|\b(\d{4})\s+model\s+year(\s*\(and older\))?", re.I)
EFFECTIVE = re.compile(r"effective as of (\d{1,2})/(\d{1,2})/(\d{4})", re.I)
LONG_DATE = re.compile(r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2}),\s+(\d{4})\b", re.I)
DAY_LINE = re.compile(r"^((?:mon|tue|wed|thu|fri|sat|sun)[a-z]*(?:\s*[-–]\s*(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*)?"
                      r"\s*:\s*\S.*)$", re.I)


def canon_url(url: str) -> str:
    p = urlsplit(url.strip())
    path = p.path.rstrip("/").lower() or "/"
    return f"https://{p.netloc.lower()}{path}"


def page_kind(url: str) -> str:
    path = urlsplit(url).path.lower().rstrip("/") or "/"
    if path in ("/rates", "/business-rates") or path.startswith("/rates/"):
        return "rate_sheet"
    if path.startswith("/hubfs") or path.endswith((".pdf", ".xls", ".xlsx")):
        return "file"
    if "/calculators" in path or path.startswith("/business-calculators"):
        return "calculator"
    if path.startswith(("/newsroom/tag", "/newsroom/author", "/learn/blog/tag", "/learn/blog/author")):
        return "listing"
    if path.startswith("/newsroom"):
        return "news"
    if path.startswith(("/learn/blog", "/business/resources")):
        return "blog"
    if path.startswith("/learn"):
        return "guide"
    if path.startswith("/privacy-policy"):
        return "legal"
    if path == "/" or path.startswith(PRODUCT_PREFIXES):
        return "product"
    return "promo"   # top-level landing pages (/switch-and-earn, /earn-more, *-lp) go stale


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def tier_range(text: str):
    """Dollar tier in a label, e.g. '$10,000 – $24,999.99' -> (10000, 24999.99); None if no dollar tier."""
    m = re.search(MONEY + r"\s*(?:–|-|to)\s*\$?\s?(\d[\d,]*(?:\.\d+)?)", text)
    if m:
        return _num(m.group(1)), _num(m.group(2))
    m = re.search(r"(up to\s*)?" + MONEY + r"\s*(\+|or more|and up|and over|or less|and under)?", text, re.I)
    if not m:
        return None
    a, tail = _num(m.group(2)), (m.group(3) or "").lower()
    if m.group(1) or tail in ("or less", "and under"):
        return 0.0, a
    if tail:
        return a, float("inf")
    return a, a


def terms(text: str) -> set[int]:
    """Loan/deposit terms in months: '60 Months' -> {60}, '30-Year' -> {360}."""
    out = set()
    for n, unit in TERM.findall(text):
        out.add(int(n) * (12 if unit.lower().startswith("y") else 1))
    return out


def model_years(text: str):
    m = re.search(r"model\s+year\s+(\d{4})\s*[-–]\s*(\d{2,4})", text, re.I)    # 'Model Year 2026-23'
    if m:
        a, b = int(m.group(1)), m.group(2)
        b = int(b) if len(b) == 4 else int(m.group(1)[:2] + b)
        return min(a, b), max(a, b)
    m = MODEL_YEARS.search(text)
    if not m:
        return None
    if m.group(1):
        lo, hi = int(m.group(1)), m.group(2)
        hi = int(hi) if len(hi) == 4 else int(m.group(1)[:2] + hi)
        return lo, hi
    y = int(m.group(3))
    return (0, y) if m.group(4) else (y, y)


def clean_inline(s: str) -> str:
    s = s.replace("\\r\\n", " ").replace("\\\\", "\\")
    # Bounded, bracket-free spans keep every scan local: unbounded [^\]]* rescans to the end of a hostile
    # page from each "[" (quadratic, measured 33 s on 130 KB of brackets).
    s = re.sub(r"!\[[^\][]{0,300}\]\([^)]{0,2048}\)", " ", s)        # images

    def link(m):
        text, url = m.group(1).strip(), (m.group(2).split() or [""])[0]
        if not text or url.startswith(("tel:", "mailto:", "#")):
            return text
        return f"{text} ({url})"
    # One class for the target: two adjacent overlapping classes backtrack quadratically on "[x](" + 130 KB.
    s = re.sub(r"\[([^\][]{0,300})\]\(([^)\s][^)]{0,2048})\)", link, s)
    s = re.sub(r"\\([*#_|\[\]()\\-])", r"\1", s)
    s = s.replace("**", "").replace("__", "")
    s = re.sub(r"(?<!\w)\*(?!\s)|(?<=\S)\*(?!\w)", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _metric(header: str):
    h = header.lower()
    if re.search(r"\bmin\b|min\.|\bmax\b|max\.|payment|points", h):
        return None
    if "apy" in h or "yield" in h:
        return "apy"
    if "apr" in h or "annual percentage rate" in h:
        return "apr"
    if "dividend" in h:
        return "dividend"
    if re.fullmatch(r"\s*(interest\s+)?rate\s*\d?\s*", h):
        return "rate"
    return "label"


def _cells(line: str) -> list[str]:
    return [clean_inline(c) for c in line.strip().strip("|").split("|")]


@dataclass
class Row:
    caption: str
    labels: list[str]
    label_headers: list[str]
    values: list[tuple[str, str]]
    effective: str | None
    line: str
    table: int
    text: str = field(default="")   # caption + headers + labels, for matching


def _caption(lines: list[str], i: int) -> str:
    got = []
    j = i - 1
    while j >= 0 and len(got) < 2:
        raw = lines[j].strip()
        j -= 1
        if not raw:
            continue
        if raw.startswith(("|", "[", "!", "- [", "* [")) or EFFECTIVE.search(raw) or len(raw) > 80:
            break
        got.append(clean_inline(raw.lstrip("#").strip()))
        if raw.startswith("#"):
            break
    return " ".join(reversed(got))


def _effective(lines: list[str], i: int):
    seen = 0
    for raw in lines[i:i + 12]:
        if raw.startswith("|"):
            break
        if raw.strip():
            seen += 1
            m = EFFECTIVE.search(raw)
            if m:
                return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
            if seen >= 4:
                break
    return None


SECTION = re.compile(r"^\s*(?:#{1,6}\s+(.{3,70})|[-*]\s*\[([^\]]{3,70})\]\([^)]*#[^)]*\))\s*$")


def _section(lines: list[str], i: int) -> str:
    """Nearest section marker above a table: a heading or an in-page tab link ('- [Certificate Rates](...#)')."""
    for j in range(i - 1, max(-1, i - 150), -1):
        m = SECTION.match(lines[j])
        if m:
            return clean_inline(m.group(1) or m.group(2))
    return ""


def parse_tables(md: str) -> list[Row]:
    lines = md.splitlines()
    rows, i, t = [], 0, 0
    while i < len(lines) - 1:
        if not (lines[i].startswith("|") and re.match(r"^\|\s*:?-{3}", lines[i + 1])):
            i += 1
            continue
        header = _cells(lines[i])
        kinds = [_metric(h) for h in header]
        caption = _caption(lines, i)
        section = _section(lines, i)
        j = i + 2
        body = []
        while j < len(lines) and lines[j].startswith("|"):
            body.append(lines[j])
            j += 1
        effective = _effective(lines, j)
        for line in body:
            cells = _cells(line)
            labels, lheads, values = [], [], []
            for k, cell in enumerate(cells[:len(header)]):
                kind = kinds[k]
                if kind == "label" and cell and cell not in ("—", "-"):
                    labels.append(cell)
                    lheads.append(header[k])
                elif kind not in (None, "label"):
                    pcts = PCT.findall(cell)
                    if pcts:
                        values.append((kind, "–".join(p + "%" for p in pcts)))
            if values and labels:
                text = " ".join([caption, section] + lheads + labels)
                rows.append(Row(caption, labels, lheads, values, effective, line.strip(), t, text))
        t += 1
        i = j
    return rows


PROSE_RATE = re.compile(r"^(?P<label>[A-Za-z0-9][^:%|]{2,60}?)\s*:\s*(?P<val>\d+(?:\.\d+)?%(?:\s*[–-]\s*\d+(?:\.\d+)?%)?)"
                        r"\s*(?P<metric>APY|APR)?\.?$", re.I)


def prose_facts(md: str) -> list[Row]:
    """Short 'label: N% APY' statements outside tables, e.g. '6-month certificate APY: 3.50%'."""
    rows, heading = [], ""
    for i, raw in enumerate(md.splitlines()):
        if raw.lstrip().startswith("#"):
            heading = clean_inline(raw.lstrip("# ").strip())
            continue
        s = clean_inline(raw.strip().lstrip("-* ").strip())
        s = re.sub(r"\s*\(https?://[^)]*\)", "", s)
        s = re.sub(r"(?<=APR|APY)\s*\d\b(?![.%\d])", "", s).strip()     # footnote "APR 2", not "APY 5%"
        m = PROSE_RATE.match(s)
        if not m:
            continue
        label, low = m.group("label").strip(), s.lower()
        label = re.sub(r"\s+(apy|apr|rate|dividend rate)$", "", label, flags=re.I)
        metric = ("apy" if "apy" in low else "apr" if "apr" in low else "dividend" if "dividend" in low
                  else "rate")
        text = f"{heading} {label}"
        # Negative table ids never collide with parse_tables' 0, 1, 2, ... so each prose line is its own group.
        rows.append(Row(heading, [label], [], [(metric, m.group("val").replace(" ", ""))], None, s, -(i + 1), text))
    return rows


def hours_blocks(md: str) -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    """Branch hour schedules published as '**Lobby**' / '**Drive-up**' day lists."""
    out, mode, cur = [], None, {"lobby": [], "drive": []}

    def flush():
        if cur["lobby"] or cur["drive"]:
            out.append((tuple(cur["lobby"]), tuple(cur["drive"])))
        cur["lobby"], cur["drive"] = [], []

    for raw in md.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        marker = None
        if low.startswith("**lobby**"):
            marker = "lobby"
        elif low.startswith("**drive-up**") or low.startswith("**drive up**"):
            marker = "drive"
        if marker:
            if marker == "lobby" and mode is not None:
                flush()
            mode = marker
            line = line.split("**", 2)[-1].strip()
            if not line:
                continue
        if mode and DAY_LINE.match(clean_inline(line)):
            cur[mode].append(clean_inline(line))
        elif mode:
            flush()
            mode = None
    flush()
    return out


def passages(md: str, max_len: int = 700) -> list[tuple[str, str]]:
    out, heading = [], ""
    for block in re.split(r"\n\s*\n", md):
        block = block.strip()
        if not block or block.startswith("|"):
            continue
        if block.startswith("#") and "\n" not in block:
            heading = clean_inline(block.lstrip("#").strip())
            continue
        text = clean_inline(block)
        if len(re.findall(r"[A-Za-z0-9]", text)) < 3:
            continue
        while len(text) > max_len:
            cut = text.rfind(". ", 0, max_len)
            cut = cut + 1 if cut > max_len // 3 else max_len
            out.append((heading, text[:cut].strip()))
            text = text[cut:].strip()
        if text:
            out.append((heading, text))
    return out


def published_date(md: str):
    m = LONG_DATE.search(md[:2000])
    if not m:
        return None
    return f"{m.group(3)}-{MONTHS.index(m.group(1).lower()) + 1:02d}-{int(m.group(2)):02d}"
