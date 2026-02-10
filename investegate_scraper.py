#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Investegate scraper — STRICT + Watchlist Prioritisation + Persistent Seen-History

What it does:
- Scrapes Investegate list pages and pulls announcement detail pages.
- Scores announcements using:
    (a) your keywords.txt phrases (case-insensitive, phrase-aware, comment-safe)
    (b) built-in investor trigger regexes
- Filters routine TR-1 + PDMR noise unless watchlist is mentioned.
- Persists a seen-history in .state/seen.json so you never re-alert duplicates.
- Writes CSV of *new hits* for today.

Key hardening:
- Inline comments in keywords file are stripped correctly (e.g. "hidden gem # note").
- WATCHLIST pattern includes *all* WATCHLIST sections in the keywords file.
- count_matches() included (fixes NameError).
- min_score is enforced.
- Marks items seen even if filtered out (prevents repeated refetch/noise loops).
"""

import os
import re
import json
import csv
import time
import argparse
import pathlib
import html
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Pattern
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparse


DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36"
}
REQUEST_TIMEOUT = 20
SLEEP_BETWEEN_ITEM_SEC = 0.8

INVESTEGATE_BASE = "https://www.investegate.co.uk"

# Broad “investor triggers” (extra scoring)
BUILTIN_INVESTOR_TRIGGERS = [
    r"\b(merger|acquisition|takeover|bid|scheme of arrangement)\b",
    r"\b(buyback|repurchase|tender offer)\b",
    r"\b(fundraise|placing|rights issue)\b",
    r"\b(trading update|guidance|profit warning)\b",
    r"\b(earnings|cash flow|margin|EBITDA)\b",
    r"\b(net debt|liquidity|refinancing|covenant)\b",
    r"\b(contract win|order book|framework)\b",
    r"\b(CEO|CFO|chair|resign|appointment|board change)\b",
    r"\b(TR-1|holding\(s\) in company)\b",
    r"\b(dividend|capital return)\b",
]


# -----------------------------
# Utilities
# -----------------------------

def canonical_rns_id(url: str) -> str:
    """Extract an ID like '9272576' from an announcement url."""
    parts = url.rstrip("/").split("/")
    last = parts[-1] if parts else url
    return last.split("?")[0].strip().lower()

def ensure_dir(p: pathlib.Path):
    p.mkdir(parents=True, exist_ok=True)

def load_seen(path: pathlib.Path) -> set:
    if path.is_file():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_seen(ids: set, path: pathlib.Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, indent=2)

def clean_text(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()

def fetch(url: str, session: requests.Session) -> Optional[str]:
    try:
        r = session.get(url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            return r.text
        return None
    except Exception:
        return None

def count_matches(text: str, patterns: List[Pattern]) -> int:
    """Count how many patterns match at least once."""
    if not text or not patterns:
        return 0
    return sum(1 for rx in patterns if rx.search(text))

def strip_inline_comment(line: str) -> str:
    """
    Remove inline comments from keywords file lines.
    Example: 'hidden gem   # keep this' -> 'hidden gem'
    """
    return line.split("#", 1)[0].strip()


# -----------------------------
# Parsing Investegate pages
# -----------------------------

def parse_list_page(html_text: str):
    soup = BeautifulSoup(html_text, "lxml")
    rows = []
    for a in soup.select('a[href*="/announcement/"]'):
        href = a.get("href", "")
        if not href:
            continue
        full = urljoin(INVESTEGATE_BASE, href)
        headline = a.get_text(strip=True)
        rows.append({"href": full, "headline": headline})

    # Dedup by canonical ID
    out, seen = [], set()
    for r in rows:
        cid = canonical_rns_id(r["href"])
        if cid and cid not in seen:
            seen.add(cid)
            out.append(r)
    return out

def parse_detail_page(html_text: str):
    soup = BeautifulSoup(html_text, "lxml")

    title = soup.find("h1").get_text(strip=True) if soup.find("h1") else ""
    parts = [t.get_text(" ", strip=True) for t in soup.find_all(["p", "li"])]
    body = clean_text("\n".join(parts))

    # Best-effort date extraction (Investegate pages usually contain a date string)
    dt_iso = None
    match = re.search(r"\b\d{1,2}\s+\w+\s+\d{4}\b", soup.get_text(" ", strip=True))
    if match:
        try:
            dt_iso = dtparse.parse(match.group(0), dayfirst=True).isoformat()
        except Exception:
            dt_iso = None

    return title, body, dt_iso


# -----------------------------
# Keyword loading & patterns
# -----------------------------

def load_keywords(keywords_file: str) -> List[str]:
    """
    Load keyword phrases from keywords_file:
    - Strips inline comments safely.
    - Ignores blank lines and header/comment-only lines.
    - Dedupes case-insensitive.
    """
    kws: List[str] = []
    if keywords_file and os.path.isfile(keywords_file):
        with open(keywords_file, "r", encoding="utf-8") as f:
            for raw in f:
                s = strip_inline_comment(raw)
                if not s:
                    continue
                # Allow section headers like "# =====" to be ignored
                if s.startswith("#"):
                    continue
                kws.append(s)

    seen, out = set(), []
    for k in kws:
        key = k.lower()
        if key not in seen:
            seen.add(key)
            out.append(k)
    return out

def looks_like_regex(s: str) -> bool:
    """
    Heuristic: if keyword contains regex metacharacters, treat as regex.
    This lets you include entries like: refinanc(e|ed) or \bword\b
    """
    return any(ch in s for ch in r"\[](){}|?+*.^$")

def compile_phrase_patterns(words: List[str]) -> List[Pattern]:
    """
    Compile a list of patterns:
    - If keyword looks regex-y, compile as given (case-insensitive).
    - Else compile as a "phrase" with word boundaries and flexible whitespace.
    """
    pats: List[Pattern] = []
    for w in words:
        w = w.strip()
        if not w:
            continue

        if looks_like_regex(w):
            try:
                pats.append(re.compile(w, re.I))
            except re.error:
                # Fallback: treat as literal phrase if their regex is invalid
                toks = [re.escape(t) for t in w.split()]
                if toks:
                    pats.append(re.compile(r"\b" + r"\s+".join(toks) + r"\b", re.I))
            continue

        toks = [re.escape(t) for t in w.split()]
        if toks:
            pats.append(re.compile(r"\b" + r"\s+".join(toks) + r"\b", re.I))
    return pats

def build_watchlist_pattern(keywords_file: str) -> Optional[Pattern]:
    """
    Build a regex that matches any entry inside ANY section whose header begins with "# ... WATCHLIST".
    This includes both:
      - WATCHLIST — INVESTORS / FUNDS / INDIVIDUALS
      - WATCHLIST — COMPANIES / NAMED ENTITIES

    Inline comments are stripped.
    """
    if not keywords_file or not os.path.isfile(keywords_file):
        return None

    names: List[str] = []
    in_watchlist = False

    with open(keywords_file, "r", encoding="utf-8") as f:
        for raw in f:
            l = raw.strip()

            # Enter a WATCHLIST section
            if re.match(r"^#\s*WATCHLIST\b", l, flags=re.I):
                in_watchlist = True
                continue

            # Exit WATCHLIST when next header begins (and is not another WATCHLIST header)
            if in_watchlist and l.startswith("#") and not re.match(r"^#\s*WATCHLIST\b", l, flags=re.I):
                in_watchlist = False
                continue

            if not in_watchlist:
                continue

            clean_name = strip_inline_comment(raw)
            if clean_name:
                names.append(clean_name)

    # Dedup (case-insensitive)
    seen = set()
    deduped = []
    for n in names:
        k = n.lower()
        if k not in seen:
            seen.add(k)
            deduped.append(n)

    if not deduped:
        return None

    parts = []
    for n in deduped:
        toks = [re.escape(t) for t in n.split()]
        if toks:
            parts.append(r"\b" + r"\s+".join(toks) + r"\b")

    if not parts:
        return None

    return re.compile("|".join(parts), re.I)


# -----------------------------
# Time filtering
# -----------------------------

def within_since_days(dt_iso: Optional[str], days: int) -> bool:
    if not days:
        return True
    if not dt_iso:
        return False
    try:
        dt = dtparse.parse(dt_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= (datetime.now(timezone.utc) - timedelta(days=days))
    except Exception:
        return False


# -----------------------------
# Core runner
# -----------------------------

def run(pages: int,
        per_page: int,
        since_days: int,
        min_score: int,
        keywords_file: str,
        out_dir: str,
        throttle: float):

    session = requests.Session()

    user_keywords = load_keywords(keywords_file)
    user_kw_patterns = compile_phrase_patterns(user_keywords)
    trigger_patterns = [re.compile(rx, re.I) for rx in BUILTIN_INVESTOR_TRIGGERS]
    watchlist_rx = build_watchlist_pattern(keywords_file)

    today = datetime.now().strftime("%Y-%m-%d")
    out_base = pathlib.Path(out_dir) / today
    ensure_dir(out_base)

    # Persistent history path: .state/seen.json
    state_dir = pathlib.Path(".state")
    ensure_dir(state_dir)
    seen_path = state_dir / "seen.json"

    # Cold-start protection: avoid spamming old items on first ever run
    if not seen_path.is_file():
        print("[INFO] No history file found (.state/seen.json). Forcing lookback to 1 day to prevent duplicate spam.")
        since_days = 1

    seen_ids = load_seen(seen_path)

    # Fetch list pages
    rows = []
    for p in range(1, pages + 1):
        html_text = fetch(f"{INVESTEGATE_BASE}/?perPage={per_page}&page={p}", session)
        if html_text:
            rows.extend(parse_list_page(html_text))

    results = []

    for row in rows:
        url = row["href"]
        cid = canonical_rns_id(url)
        if not cid:
            continue
        if cid in seen_ids:
            continue  # persistent skip

        html_text = fetch(url, session)
        if not html_text:
            # Don't mark seen if we couldn't fetch details (transient failure)
            continue

        title, body, dt_iso = parse_detail_page(html_text)

        # Mark seen early so we don't refetch this same item next run,
        # even if it gets filtered out or doesn't match.
        seen_ids.add(cid)

        teaser = (title + "\n" + (body or "")).lower()

        # ----------------------------
        # Noise filters
        # ----------------------------

        # Routine TR-1 filter unless watchlist mentioned in body
        if body:
            tr1 = re.search(r"\bacquisition or disposal of voting rights\b", body, re.I)
            wl = watchlist_rx.search(body) if (watchlist_rx and body) else None
            if tr1 and not wl:
                time.sleep(throttle)
                continue

        # PDMR / director dealings filter unless watchlist mentioned
        title_l = (title or "").lower()
        is_pdmr = title_l.startswith((
            "director/pdmr", "pdmr", "director dealings", "share dealings", "shareholding of directors"
        ))
        wl = watchlist_rx.search(body) if (watchlist_rx and body) else None
        if is_pdmr and not wl:
            time.sleep(throttle)
            continue

        # ----------------------------
        # Scoring
        # ----------------------------
        user_score = count_matches(teaser, user_kw_patterns)
        trigger_score = count_matches(teaser, trigger_patterns)
        total = user_score + trigger_score

        # Enforce: must match at least one user keyword, meet min_score, and be recent enough
        if user_score >= 1 and total >= min_score and within_since_days(dt_iso, since_days):
            results.append({
                "dt_iso": dt_iso,
                "url": url,
                "title": title,
                "score": total,
                "user_score": user_score,
                "trigger_score": trigger_score,
            })

        time.sleep(throttle)

    # Save updated history
    save_seen(seen_ids, seen_path)

    # Output CSV (matches only)
    hits_path = out_base / "investegate_hits.csv"
    with open(hits_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["dt_iso", "title", "score", "user_score", "trigger_score", "url"])
        for r in sorted(results, key=lambda x: (x["score"], x["dt_iso"] or ""), reverse=True):
            w.writerow([r["dt_iso"], r["title"], r["score"], r["user_score"], r["trigger_score"], r["url"]])

    print(f"[DONE] New matches: {len(results)} (duplicates & previously seen suppressed)")
    print(f"Seen history: {seen_path}")
    print(f"CSV: {hits_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--per_page", type=int, default=300)
    ap.add_argument("--since_days", type=int, default=1)
    ap.add_argument("--min_score", type=int, default=1)
    ap.add_argument("--keywords_file", type=str, default="keywords.txt")
    ap.add_argument("--out", type=str, default="out")
    ap.add_argument("--throttle", type=float, default=SLEEP_BETWEEN_ITEM_SEC)
    a = ap.parse_args()

    run(
        pages=a.pages,
        per_page=a.per_page,
        since_days=a.since_days,
        min_score=a.min_score,
        keywords_file=a.keywords_file,
        out_dir=a.out,
        throttle=max(0.2, a.throttle),
    )


if __name__ == "__main__":
    main()
