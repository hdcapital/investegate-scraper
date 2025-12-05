#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Investegate scraper — STRICT + Watchlist Prioritisation + Persistent Seen-History
Updated to store history in .state/seen.json for better caching.
"""

import os, re, json, csv, time, argparse, pathlib, html
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparse

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36"
}
REQUEST_TIMEOUT = 20
SLEEP_BETWEEN_ITEM_SEC = 0.8

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

def canonical_rns_id(url: str) -> str:
    """Extracts ID like 9272576 from url."""
    parts = url.rstrip("/").split("/")
    return parts[-1].split("?")[0].lower()

def ensure_dir(p: pathlib.Path): 
    p.mkdir(parents=True, exist_ok=True)

def load_seen(path) -> set:
    if os.path.isfile(path):
        try:
            return set(json.load(open(path, "r", encoding="utf-8")))
        except:
            return set()
    return set()

def save_seen(ids, path):
    # Ensure the folder exists before writing
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, indent=2)

def clean_text(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()

def fetch(url: str, session: requests.Session) -> Optional[str]:
    try:
        r = session.get(url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT)
        return r.text if r.status_code == 200 else None
    except:
        return None

def parse_list_page(html_text: str):
    soup = BeautifulSoup(html_text, "lxml")
    rows = []
    for a in soup.select('a[href*="/announcement/"]'):
        href = urljoin("https://www.investegate.co.uk", a.get("href",""))
        headline = a.get_text(strip=True)
        rows.append({"href": href, "headline": headline})
    
    out, seen = [], set()
    for r in rows:
        cid = canonical_rns_id(r["href"])
        if cid not in seen:
            seen.add(cid)
            out.append(r)
    return out

def parse_detail_page(html_text: str):
    soup = BeautifulSoup(html_text, "lxml")
    title = soup.find("h1").get_text(strip=True) if soup.find("h1") else ""
    parts = [t.get_text(" ", strip=True) for t in soup.find_all(["p","li"])]
    body = clean_text("\n".join(parts))
    dt_iso = None
    match = re.search(r"\b\d{1,2}\s+\w+\s+\d{4}\b", soup.get_text(" ", strip=True))
    if match:
        try: dt_iso = dtparse.parse(match.group(0), dayfirst=True).isoformat()
        except: pass
    return title, body, dt_iso

def load_keywords(keywords_file):
    kws=[]
    if keywords_file and os.path.isfile(keywords_file):
        for line in open(keywords_file,"r",encoding="utf-8"):
            s=line.strip()
            if s and not s.startswith("#"): kws.append(s)
    seen,out=set(),[]
    for k in kws:
        if k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    return out

def compile_phrase_patterns(words):
    pats=[]
    for w in words:
        toks=[re.escape(t) for t in w.split()]
        if toks:
            pats.append(re.compile(r"\b" + r"\s+".join(toks) + r"\b", re.I))
    return pats

def count_matches(text, patterns):
    return sum(1 for rx in patterns if rx.search(text))

def build_watchlist_pattern(keywords_file):
    if not keywords_file or not os.path.isfile(keywords_file): return None
    names=[]; in_section=False
    for line in open(keywords_file,"r",encoding="utf-8"):
        l=line.rstrip("\n")
        if re.match(r"^#\s*WATCHLIST", l, flags=re.I):
            in_section=True; continue
        if in_section and l.startswith("#"): break
        if in_section and l.strip(): names.append(l.strip())
    if not names: return None
    parts=[]
    for n in names:
        toks=[re.escape(t) for t in n.split()]
        if toks: parts.append(r"\b" + r"\s+".join(toks) + r"\b")
    return re.compile("|".join(parts), re.I) if parts else None

def within_since_days(dt_iso, days):
    if not days: return True
    if not dt_iso: return False
    try:
        dt = dtparse.parse(dt_iso)
        if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
        return dt >= (datetime.now(timezone.utc)-timedelta(days=days))
    except:
        return False

def run(pages, per_page, since_days, min_score, keywords_file, out_dir, throttle):
    session=requests.Session()
    user_keywords=load_keywords(keywords_file)
    user_kw_patterns=compile_phrase_patterns(user_keywords)
    trigger_patterns=[re.compile(rx, re.I) for rx in BUILTIN_INVESTOR_TRIGGERS]
    watchlist_rx=build_watchlist_pattern(keywords_file)

    today=datetime.now().strftime("%Y-%m-%d")
    out_base=pathlib.Path(out_dir)/today
    ensure_dir(out_base)

    # UPDATED: Path is now .state/seen.json
    state_dir = pathlib.Path(".state")
    ensure_dir(state_dir)
    seen_path = state_dir / "seen.json"
    seen_ids = load_seen(seen_path)

    # Fetch list
    rows=[]
    for p in range(1,pages+1):
        html_text=fetch(f"https://www.investegate.co.uk/?perPage={per_page}&page={p}",session)
        if html_text:
            rows.extend(parse_list_page(html_text))

    # Process detail
    results=[]
    for row in rows:
        url=row["href"]
        cid=canonical_rns_id(url)
        if cid in seen_ids: continue  # persistent skip

        html_text=fetch(url,session)
        if not html_text: continue
        title,body,dt_iso=parse_detail_page(html_text)

        teaser=(title+"\n"+body[:2000]).lower()

        # Skip routine TR-1 without watchlist
        if body:
            tr1=re.search(r"\bacquisition or disposal of voting rights\b", body, re.I)
            wl=watchlist_rx.search(body) if watchlist_rx else None
            if tr1 and not wl:
                continue

        # Skip PDMR unless watchlist
        title_l=title.lower()
        is_pdmr=title_l.startswith(("director/pdmr","pdmr","director dealings","share dealings","shareholding of directors"))
        wl=watchlist_rx.search(body) if watchlist_rx else None
        if is_pdmr and not wl:
            continue

        user_score=count_matches(teaser,user_kw_patterns)
        trigger_score=count_matches(teaser,trigger_patterns)
        total=user_score+trigger_score

        if user_score >= 1 and within_since_days(dt_iso,since_days):
            results.append({
                "dt_iso":dt_iso,"url":url,"title":title,"score":total,
                "user_score":user_score,"trigger_score":trigger_score
            })
            seen_ids.add(cid)  # mark as processed

        time.sleep(throttle)

    # Save updated seen list to .state/seen.json
    save_seen(seen_ids, seen_path)

    # Output CSV (matches only)
    hits_path=out_base/"investegate_hits.csv"
    with open(hits_path,"w",newline="",encoding="utf-8") as f:
        w=csv.writer(f); w.writerow(["dt_iso","title","score","user_score","trigger_score","url"])
        for r in sorted(results,key=lambda x:(x["score"],x["dt_iso"] or ""),reverse=True):
            w.writerow([r["dt_iso"],r["title"],r["score"],r["user_score"],r["trigger_score"],r["url"]])

    print(f"[DONE] New matches: {len(results)} (duplicates & previously seen suppressed)")
    print(f"CSV: {hits_path}")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--pages",type=int,default=2)
    ap.add_argument("--per_page",type=int,default=300)
    ap.add_argument("--since_days",type=int,default=1)
    ap.add_argument("--min_score",type=int,default=1)
    ap.add_argument("--keywords_file",type=str,default="keywords.txt")
    ap.add_argument("--out",type=str,default="out")
    ap.add_argument("--throttle",type=float,default=SLEEP_BETWEEN_ITEM_SEC)
    a=ap.parse_args()

    run(a.pages,a.per_page,a.since_days,a.min_score,a.keywords_file,a.out,max(0.2,a.throttle))

if __name__ == "__main__":
    main()
