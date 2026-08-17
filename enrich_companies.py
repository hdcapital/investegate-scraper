#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Company enrichment for Investegate CSVs.

Input:
  out/YYYY-MM-DD/investegate_hits.csv

Output:
  Same CSV path, overwritten with extra columns:
  company_name, ticker, exchange, business_description, market_cap,
  market_cap_display, currency, data_source, enrichment_status,
  enrichment_attempted_symbols, enrichment_error

Data source:
  Yahoo Finance via the yfinance package. No API key required.

Important behaviour:
- Even if yfinance is missing or lookups fail, this still rewrites the CSV and
  adds enrichment columns. That makes failures visible and gives the emailer
  deterministic fields to read.
- It caches successful lookups in .state/company_cache.json.
- It assumes Investegate RNS tickers are usually London-listed and uses
  Yahoo's .L suffix (e.g. TIME.L). Legacy .LSE/.LON suffixes from older CSVs
  are converted to .L.
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

try:
    import yfinance as yf
except Exception:  # pragma: no cover - import failure handled at runtime
    yf = None

CACHE_PATH = Path(".state/company_cache.json")
CACHE_DAYS = int(os.environ.get("COMPANY_CACHE_DAYS", "7"))
REQUEST_PAUSE = 0.5

EXTRA_FIELDS = [
    "company_name",
    "ticker",
    "exchange",
    "business_description",
    "market_cap",
    "market_cap_display",
    "currency",
    "data_source",
    "enrichment_status",
    "enrichment_attempted_symbols",
    "enrichment_error",
]


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
    """
    Parse Investegate URL segment like:
      /announcement/rns/ncc-group--ncc/capital-reduction.../9654919
    into:
      issuer='Ncc Group', ticker='NCC'
    """
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


def normalise_company_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name or "").strip()
    # Clean common Investegate instrument suffixes, but keep the real name.
    name = re.sub(r"\b(NPV|DI|ORD|PLC|LTD|LIMITED)\b$", lambda m: m.group(0), name, flags=re.I)
    return name


def fallback_company_fields(row: dict, status: str, error: str = "") -> dict:
    issuer_from_url, ticker_from_url = parse_issuer_ticker_from_url(row.get("url", ""))
    company_name = first(row, ["company_name", "company", "issuer", "issuer_name", "issuer_hint", "name"]) or issuer_from_url
    ticker = first(row, ["ticker", "ticker_hint", "epic", "symbol"]) or ticker_from_url
    return {
        "company_name": normalise_company_name(company_name),
        "ticker": ticker,
        "exchange": first(row, ["exchange"]) or ("LSE" if ticker else ""),
        "business_description": first(row, ["business_description", "company_description", "description"]),
        "market_cap": first(row, ["market_cap"]),
        "market_cap_display": first(row, ["market_cap_display"]),
        "currency": first(row, ["currency"]),
        "data_source": first(row, ["data_source"]),
        "enrichment_status": status,
        "enrichment_error": error,
    }


def to_yahoo_symbol(raw: str) -> str:
    """Convert a ticker or legacy EODHD-style symbol to Yahoo's convention."""
    raw = (raw or "").strip().upper()
    if not raw:
        return ""
    if "." in raw:
        base, suffix = raw.rsplit(".", 1)
        if suffix in ("LSE", "LON", "L"):
            return f"{base}.L"
        return raw
    return f"{raw}.L"


def symbol_candidates(row: dict) -> list[str]:
    out: list[str] = []

    raw_values = [
        first(row, ["eodhd_symbol"]),
        first(row, ["ticker", "ticker_hint", "epic", "symbol"]),
    ]

    _, ticker_from_url = parse_issuer_ticker_from_url(row.get("url", ""))
    if ticker_from_url:
        raw_values.append(ticker_from_url)

    for raw in raw_values:
        sym = to_yahoo_symbol(raw)
        if sym:
            out.append(sym)

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

    # Yahoo reports marketCap in major currency units (GBP, not pence) even
    # when the quote currency is GBp, so no pence conversion is needed here.
    sym = currency_symbol(currency)
    abs_v = abs(v)
    if abs_v >= 1_000_000_000:
        return f"{sym}{v / 1_000_000_000:.2f}bn"
    if abs_v >= 1_000_000:
        return f"{sym}{v / 1_000_000:.1f}m"
    if abs_v >= 1_000:
        return f"{sym}{v / 1_000:.1f}k"
    return f"{sym}{v:.0f}"


def clean_description(desc: str, max_chars: int = 220) -> str:
    desc = re.sub(r"\s+", " ", desc or "").strip()
    if not desc:
        return ""
    # Strip boilerplate intro if present.
    desc = re.sub(r"^.*?\b(the company|company)\b\s+(is|operates|provides|offers)\b", lambda m: m.group(0), desc, flags=re.I)
    if len(desc) <= max_chars:
        return desc
    cut = desc[:max_chars].rsplit(" ", 1)[0].strip()
    return cut + "…"


def get_yahoo_info(symbol: str) -> tuple[dict | None, str]:
    try:
        info = yf.Ticker(symbol).get_info()
    except Exception as e:
        msg = f"{type(e).__name__}:{e}"
        # One gentle retry on rate limiting, which Yahoo applies per-IP.
        if "429" in msg or "rate" in msg.lower():
            time.sleep(5)
            try:
                info = yf.Ticker(symbol).get_info()
            except Exception as e2:
                return None, f"{symbol}:{type(e2).__name__}:{e2}"
        else:
            return None, f"{symbol}:{msg}"

    if not isinstance(info, dict):
        return None, f"{symbol}:non_dict_response"
    # Unknown symbols come back as a near-empty dict rather than an error.
    if not (info.get("longName") or info.get("shortName") or info.get("marketCap")):
        return None, f"{symbol}:no_name_or_market_cap"
    return info, ""


def extract_company_fields(symbol: str, info: dict) -> dict:
    company_name = info.get("longName") or info.get("shortName") or ""
    ticker = symbol.split(".")[0]
    exchange = info.get("fullExchangeName") or info.get("exchange") or symbol.split(".")[-1]
    currency = info.get("currency") or info.get("financialCurrency") or ""
    description = clean_description(info.get("longBusinessSummary") or "")

    market_cap = info.get("marketCap") or ""
    market_cap_display = format_market_cap(market_cap, currency)

    status = "ok" if (company_name or description or market_cap_display) else "empty_response"
    return {
        "company_name": company_name,
        "ticker": ticker,
        "exchange": exchange,
        "business_description": description,
        "market_cap": str(market_cap or ""),
        "market_cap_display": market_cap_display,
        "currency": currency,
        "data_source": f"yfinance:{symbol}",
        "enrichment_status": status,
        "enrichment_error": "",
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


def enrich_row(row: dict, cache: dict) -> dict:
    # Ensure all fields exist from the outset.
    for f in EXTRA_FIELDS:
        row.setdefault(f, "")

    candidates = symbol_candidates(row)
    row["enrichment_attempted_symbols"] = ", ".join(candidates)

    if row.get("company_name") or row.get("business_description") or row.get("market_cap_display"):
        row["enrichment_status"] = row.get("enrichment_status") or "already_present"
        return row

    # Always populate deterministic fallback fields even if lookups fail.
    row.update({k: v for k, v in fallback_company_fields(row, "pending").items() if v})

    if yf is None:
        row["enrichment_status"] = "yfinance_unavailable"
        row["enrichment_error"] = "yfinance package not installed"
        return row

    if not candidates:
        row.update({k: v for k, v in fallback_company_fields(row, "no_symbol_candidate").items() if v})
        return row

    errors = []
    for symbol in candidates:
        cached = cache.get(symbol)
        if cached and cache_fresh(cached):
            fields = cached.copy()
            fields.pop("last_updated", None)
            row.update(fields)
            row["enrichment_status"] = "ok_cached" if fields.get("enrichment_status") == "ok" else fields.get("enrichment_status", "cached")
            return row

        info, err = get_yahoo_info(symbol)
        time.sleep(REQUEST_PAUSE)
        if err:
            errors.append(err)
        if not info:
            continue

        fields = extract_company_fields(symbol, info)
        cache[symbol] = fields
        row.update({k: v for k, v in fields.items() if k != "last_updated"})
        return row

    row.update({k: v for k, v in fallback_company_fields(row, "lookup_failed", "; ".join(errors[:5])).items() if v})
    return row


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python enrich_companies.py path/to/investegate_hits.csv", file=sys.stderr)
        return 2

    csv_path = Path(sys.argv[1])
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        return 1

    if yf is None:
        print("[WARN] yfinance not installed. CSV will still be rewritten with enrichment columns and fallback issuer/ticker fields.")

    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        original_fields = reader.fieldnames or []

    cache = load_cache()
    enriched = [enrich_row(dict(row), cache) for row in rows]
    save_cache(cache)

    fieldnames = original_fields + [f for f in EXTRA_FIELDS if f not in original_fields]

    tmp_path = csv_path.with_suffix(".tmp.csv")
    with tmp_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in enriched:
            writer.writerow(row)

    tmp_path.replace(csv_path)

    ok_count = sum(1 for r in enriched if str(r.get("enrichment_status", "")).startswith("ok"))
    failed_count = sum(1 for r in enriched if r.get("enrichment_status") == "lookup_failed")
    print(f"[DONE] Company enrichment columns written. ok={ok_count}, lookup_failed={failed_count}, total={len(enriched)}")

    if enriched:
        sample = enriched[0]
        print("[DEBUG] Sample enrichment:", {
            "company_name": sample.get("company_name"),
            "ticker": sample.get("ticker"),
            "market_cap_display": sample.get("market_cap_display"),
            "status": sample.get("enrichment_status"),
            "attempted": sample.get("enrichment_attempted_symbols"),
            "error": sample.get("enrichment_error"),
        })

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
