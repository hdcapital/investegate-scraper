#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Company enrichment for Investegate CSVs.

Input:
  out/YYYY-MM-DD/investegate_hits.csv

Output:
  Same CSV path, overwritten with extra columns:
  company_name, ticker, exchange, business_description, market_cap,
  market_cap_display, currency, data_source, enrichment_status

Data source:
  EODHD fundamentals API, if EODHD_API_KEY or EODHD_TOKEN is set.

Notes:
- This script is safe to run without an API key; it will add no data and exit 0.
- It caches successful lookups in .state/company_cache.json.
- It assumes Investegate RNS tickers are usually London-listed and therefore tries TICKER.LSE first.
"""

import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

import requests

API_BASE = "https://eodhd.com/api/v1.1"
CACHE_PATH = Path(".state/company_cache.json")
CACHE_DAYS = int(os.environ.get("COMPANY_CACHE_DAYS", "7"))
REQUEST_TIMEOUT = 20


def load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


def cache_fresh(item: dict) -> bool:
    try:
        ts = datetime.fromisoformat(item.get("last_updated", ""))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts >= datetime.now(timezone.utc) - timedelta(days=CACHE_DAYS)
    except Exception:
        return False


def parse_issuer_ticker_from_url(url: str) -> tuple[str, str]:
    try:
        parts = [p for p in urlparse(url).path.split("/") if p]
        lower = [p.lower() for p in parts]
        if "rns" not in lower:
            return "", ""
        idx = lower.index("rns")
        issuer_seg = parts[idx + 1] if idx + 1 < len(parts) else ""
        if "--" not in issuer_seg:
            return issuer_seg.replace("-", " ").title(), ""
        left, right = issuer_seg.rsplit("--", 1)
        issuer = left.replace("-", " ").title()
        ticker = re.sub(r"[^A-Za-z0-9]", "", right).upper()
        return issuer, ticker
    except Exception:
        return "", ""


def first(row: dict, keys: list[str]) -> str:
    for k in keys:
        v = row.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def symbol_candidates(row: dict) -> list[str]:
    out = []

    for raw in [
        first(row, ["eodhd_symbol"]),
        first(row, ["ticker", "ticker_hint", "epic", "symbol"]),
    ]:
        raw = (raw or "").strip()
        if not raw:
            continue
        if "." in raw:
            out.append(raw.upper())
        else:
            out.append(f"{raw.upper()}.LSE")

    _, ticker_from_url = parse_issuer_ticker_from_url(row.get("url", ""))
    if ticker_from_url:
        out.append(f"{ticker_from_url}.LSE")

    # Dedup preserving order.
    seen = set()
    deduped = []
    for s in out:
        if s not in seen:
            seen.add(s)
            deduped.append(s)
    return deduped


def currency_symbol(currency: str) -> str:
    c = (currency or "").upper()
    return {
        "GBP": "£",
        "GBX": "£",
        "USD": "$",
        "EUR": "€",
        "SEK": "SEK ",
        "AUD": "A$",
        "CAD": "C$",
    }.get(c, f"{c} " if c else "")


def format_market_cap(value, currency: str) -> str:
    try:
        v = float(value)
    except Exception:
        return ""

    if v <= 0:
        return ""

    sym = currency_symbol(currency)
    abs_v = abs(v)
    if abs_v >= 1_000_000_000:
        return f"{sym}{v / 1_000_000_000:.2f}bn"
    if abs_v >= 1_000_000:
        return f"{sym}{v / 1_000_000:.1f}m"
    if abs_v >= 1_000:
        return f"{sym}{v / 1_000:.1f}k"
    return f"{sym}{v:.0f}"


def clean_description(desc: str, max_chars: int = 240) -> str:
    desc = re.sub(r"\s+", " ", desc or "").strip()
    if len(desc) <= max_chars:
        return desc
    cut = desc[:max_chars].rsplit(" ", 1)[0].strip()
    return cut + "…"


def get_eodhd_fundamentals(symbol: str, token: str) -> dict | None:
    url = f"{API_BASE}/fundamentals/{symbol}"
    try:
        r = requests.get(
            url,
            params={"api_token": token, "fmt": "json"},
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "HD-Capital-Investegate-Bot/1.0"},
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, dict) or data.get("code") == 404 or data.get("message"):
            return None
        return data
    except Exception:
        return None


def extract_company_fields(symbol: str, data: dict) -> dict:
    general = data.get("General") or {}
    highlights = data.get("Highlights") or {}

    company_name = general.get("Name") or general.get("CompanyName") or ""
    ticker = general.get("Code") or symbol.split(".")[0]
    exchange = general.get("Exchange") or symbol.split(".")[-1]
    currency = general.get("CurrencyCode") or general.get("CurrencyName") or ""
    description = clean_description(general.get("Description") or "")

    market_cap = (
        highlights.get("MarketCapitalization")
        or highlights.get("MarketCapitalisation")
        or highlights.get("MarketCap")
        or ""
    )
    market_cap_display = format_market_cap(market_cap, currency)

    return {
        "company_name": company_name,
        "ticker": ticker,
        "exchange": exchange,
        "business_description": description,
        "market_cap": str(market_cap or ""),
        "market_cap_display": market_cap_display,
        "currency": currency,
        "data_source": f"EODHD fundamentals:{symbol}",
        "enrichment_status": "ok" if (company_name or description or market_cap_display) else "empty_response",
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


def enrich_row(row: dict, token: str, cache: dict) -> dict:
    issuer_from_url, ticker_from_url = parse_issuer_ticker_from_url(row.get("url", ""))

    # Preserve hints even if enrichment fails.
    row.setdefault("company_name", "")
    row.setdefault("ticker", "")
    row.setdefault("exchange", "")
    row.setdefault("business_description", "")
    row.setdefault("market_cap", "")
    row.setdefault("market_cap_display", "")
    row.setdefault("currency", "")
    row.setdefault("data_source", "")
    row.setdefault("enrichment_status", "")

    if row.get("company_name") or row.get("business_description") or row.get("market_cap_display"):
        return row

    candidates = symbol_candidates(row)
    if not candidates:
        row["company_name"] = first(row, ["issuer_hint"]) or issuer_from_url
        row["ticker"] = first(row, ["ticker_hint"]) or ticker_from_url
        row["enrichment_status"] = "no_symbol_candidate"
        return row

    for symbol in candidates:
        cached = cache.get(symbol)
        if cached and cache_fresh(cached):
            fields = cached.copy()
            fields.pop("last_updated", None)
            row.update(fields)
            return row

        data = get_eodhd_fundamentals(symbol, token)
        time.sleep(0.15)
        if not data:
            continue

        fields = extract_company_fields(symbol, data)
        cache[symbol] = fields
        row.update({k: v for k, v in fields.items() if k != "last_updated"})
        return row

    row["company_name"] = first(row, ["issuer_hint"]) or issuer_from_url
    row["ticker"] = first(row, ["ticker_hint"]) or ticker_from_url
    row["enrichment_status"] = "lookup_failed"
    return row


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python enrich_companies.py path/to/investegate_hits.csv", file=sys.stderr)
        return 2

    csv_path = Path(sys.argv[1])
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        return 1

    token = os.environ.get("EODHD_API_KEY") or os.environ.get("EODHD_TOKEN")
    if not token:
        print("[INFO] EODHD_API_KEY not set. Skipping company enrichment.")
        return 0

    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        original_fields = reader.fieldnames or []

    cache = load_cache()
    enriched = [enrich_row(dict(row), token, cache) for row in rows]
    save_cache(cache)

    extra_fields = [
        "company_name",
        "ticker",
        "exchange",
        "business_description",
        "market_cap",
        "market_cap_display",
        "currency",
        "data_source",
        "enrichment_status",
    ]
    fieldnames = original_fields + [f for f in extra_fields if f not in original_fields]

    tmp_path = csv_path.with_suffix(".tmp.csv")
    with tmp_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in enriched:
            writer.writerow(row)

    tmp_path.replace(csv_path)
    ok_count = sum(1 for r in enriched if r.get("enrichment_status") == "ok")
    print(f"[DONE] Enriched company data: {ok_count}/{len(enriched)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
