#!/usr/bin/env python3
import os, csv, json, smtplib, requests, re, html
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formataddr
from bs4 import BeautifulSoup
from openai import OpenAI

# ------------------------------------------------
# CONFIG VIA ENV VARS
# ------------------------------------------------
csv_path = Path(os.environ["CSV_FILE"])
run_num = os.environ.get("GITHUB_RUN_NUMBER", "?")
from_name = os.environ.get("FROM_NAME", "HD Capital")
username = os.environ["SMTP_USERNAME"]
password = os.environ["SMTP_PASSWORD"]
to_email = os.environ["TO_EMAIL"]
smtp_server = os.environ["SMTP_SERVER"]
smtp_port = int(os.environ["SMTP_PORT"])
openai_api_key = os.environ["OPENAI_API_KEY"]

MAX_HISTORY_EMAILS = int(os.environ.get("MAX_HISTORY_EMAILS", "6"))

base_state_dir = Path(".state")
base_state_dir.mkdir(parents=True, exist_ok=True)
history_file = base_state_dir / "email_history_urls.json"


# ------------------------------------------------
# HELPERS
# ------------------------------------------------

def get_rns_id(url: str) -> str:
    try:
        parts = url.rstrip("/").split("/")
        return parts[-1].split("?")[0].lower()
    except Exception:
        return ""


def esc(x) -> str:
    return html.escape(str(x or ""), quote=True)


def row_first(row: dict, keys: list[str]) -> str:
    for k in keys:
        v = row.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def guess_company_from_title(title: str) -> str:
    if not title:
        return ""
    separators = [" - ", " – ", " — ", " | ", ": "]
    for sep in separators:
        if sep in title:
            first = title.split(sep)[0].strip()
            if 2 <= len(first) <= 80:
                return first
    return ""


def split_keywords(s: str) -> list[str]:
    if not s:
        return []
    parts = re.split(r"[,;|]", s)
    out = []
    seen = set()
    for p in parts:
        p = p.strip()
        if not p:
            continue
        lk = p.lower()
        if lk not in seen:
            seen.add(lk)
            out.append(p)
    return out


def detect_keyword_hits(text: str, keywords: list[str]) -> list[str]:
    if not text or not keywords:
        return []
    haystack = text.lower()
    hits = []
    seen = set()
    for kw in keywords:
        k = kw.strip()
        if not k:
            continue
        lk = k.lower()
        if lk in haystack and lk not in seen:
            seen.add(lk)
            hits.append(k)
    return hits


def summary_text_to_html(text: str) -> str:
    """Convert plain text with newlines into safe email HTML."""
    if not text:
        return ""

    safe = esc(text).strip()
    paras = [p.strip() for p in re.split(r"\n\s*\n", safe) if p.strip()]

    if not paras:
        return safe.replace("\n", "<br>")

    html_paras = []
    for para in paras:
        para_html = para.replace("\n", "<br>")
        html_paras.append(f'<p style="margin:0 0 8px 0;">{para_html}</p>')

    return "\n".join(html_paras)


def extract_json_object(s: str) -> dict:
    if not s:
        return {}
    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except Exception:
        pass
    m = re.search(r"\{.*\}", s, flags=re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return {}
    return {}


def normalise_missing_desc(desc: str) -> str:
    desc = re.sub(r"\s+", " ", desc or "").strip()
    if not desc:
        return ""
    bad = {
        "business description unavailable",
        "description unavailable",
        "unavailable",
        "n/a",
        "na",
        "none",
    }
    if desc.lower().rstrip(".") in bad:
        return ""
    return desc


def build_company_parts(row: dict, title: str) -> dict:
    company_name = row_first(row, [
        "company_name", "company", "issuer", "issuer_name", "issuer_hint", "name"
    ]) or guess_company_from_title(title) or "Unknown company"

    description = normalise_missing_desc(row_first(row, [
        "business_description", "company_description", "description"
    ]))

    market_cap = row_first(row, [
        "market_cap_display", "market_cap", "mkt_cap", "marketcap",
        "current_market_cap", "market_capitalisation", "market_capitalization"
    ]) or "n/a"

    ticker = row_first(row, ["ticker", "ticker_hint", "epic", "symbol"])
    enrichment_status = row_first(row, ["enrichment_status"])
    data_source = row_first(row, ["data_source"])

    return {
        "company_name": company_name,
        "description": description,
        "market_cap": market_cap,
        "ticker": ticker,
        "enrichment_status": enrichment_status,
        "data_source": data_source,
    }


def build_company_line(parts: dict, ai_description: str = "") -> str:
    desc = normalise_missing_desc(parts.get("description", "")) or normalise_missing_desc(ai_description)
    if not desc:
        desc = "Business description unavailable"

    return f"{parts.get('company_name') or 'Unknown company'} — {desc}. Market cap: {parts.get('market_cap') or 'n/a'}"


# ------------------------------------------------
# LOAD HISTORY-STATE
# ------------------------------------------------

history_state = {"emails": []}
if history_file.exists():
    try:
        data = json.loads(history_file.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("emails"), list):
            history_state["emails"] = data["emails"]
    except Exception:
        history_state = {"emails": []}

ref_seen_ids = set()
for urls in history_state["emails"]:
    for u in urls:
        if isinstance(u, str):
            rid = get_rns_id(u)
            if rid:
                ref_seen_ids.add(rid)

client = OpenAI(api_key=openai_api_key)


# ------------------------------------------------
# LOAD KEYWORDS
# ------------------------------------------------

def load_keywords(path="keywords.txt") -> list[str]:
    out = []
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.split("#", 1)[0].strip()
                if s:
                    out.append(s)

    uq = []
    seen_kw = set()
    for k in out:
        lk = k.lower()
        if lk not in seen_kw:
            seen_kw.add(lk)
            uq.append(k)
    return uq


user_keywords = load_keywords()


# ------------------------------------------------
# FETCH & SUMMARIZE
# ------------------------------------------------

def fetch_article_text(url: str) -> str:
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        parts = [t.get_text(" ", strip=True) for t in soup.find_all(["p", "li"])]
        text = " ".join(parts)
        text = re.sub(r"\s{2,}", " ", text)
        return text[:12000]
    except Exception:
        return ""


def analyze_rns(
    title: str,
    url: str,
    body: str,
    company_parts: dict,
    matched_keywords: list[str],
    matched_builtin_triggers: list[str] | None = None,
) -> dict:
    trigger_text = ", ".join(matched_keywords) if matched_keywords else "No explicit keyword detected"
    builtin_trigger_text = ", ".join(matched_builtin_triggers or []) if matched_builtin_triggers else "n/a"

    current_description = normalise_missing_desc(company_parts.get("description", ""))
    needs_description = "yes" if not current_description else "no"

    prompt = f"""
You are a buy-side analyst writing an automated RNS digest for a PM.

Return VALID JSON ONLY. No markdown. No code fences.

Use this exact JSON schema:
{{
  "company_description": "short business description, or empty string if already supplied",
  "summary": "Analyst-quality review of the announcement."
}}

Rules for company_description:
- Need company description? {needs_description}
- Company name: {company_parts.get('company_name') or 'Unknown company'}
- If a description is already supplied, return an empty string for company_description.
- If no description is supplied, infer a super short business description from the announcement text/company boilerplate only.
- Do not use stale outside knowledge. If the announcement text does not support a description, return "Business description unavailable".
- Do not invent market cap.

Rules for summary:
- Write 90-160 words, or up to 190 words only if the announcement is complex.
- Preserve enough context for the PM to understand the actual announcement without opening the link.
- Explicitly connect the announcement back to the matched keyword/keywords.
- Include key numbers, dates, consideration, funding amount, dilution, completion timing, covenants, liquidity, NAV, valuation or deal terms where relevant.
- Focus on what happened, why it matters, whether the keyword hit is meaningful, and what a PM should check next.
- If a keyword hit is a weak/false positive, say so briefly and then explain the real announcement.
- No long bullet lists. No generic filler. Interpret, do not just describe.

Deterministic company data supplied:
Company name: {company_parts.get('company_name') or 'Unknown company'}
Ticker: {company_parts.get('ticker') or 'n/a'}
Existing description: {current_description or 'n/a'}
Market cap: {company_parts.get('market_cap') or 'n/a'}
Enrichment status: {company_parts.get('enrichment_status') or 'n/a'}
Data source: {company_parts.get('data_source') or 'n/a'}

Actual matched keyword/keywords:
{trigger_text}

Built-in investor trigger flags, for additional context only:
{builtin_trigger_text}

RNS Title: {title}
URL: {url}

Announcement text:
{body}
"""

    try:
        resp = client.responses.create(
            model="gpt-5.1",
            input=prompt,
            max_output_tokens=520,
        )
        raw = resp.output_text.strip()
        data = extract_json_object(raw)
        summary = str(data.get("summary", "")).strip()
        company_description = str(data.get("company_description", "")).strip()
        return {
            "summary": summary or raw or "Summary unavailable.",
            "company_description": company_description,
        }
    except Exception as e:
        return {
            "summary": f"Summary unavailable: {e}",
            "company_description": "",
        }


# ------------------------------------------------
# LOAD CSV ROWS & DEDUPE
# ------------------------------------------------

rows = []
if csv_path.exists():
    with csv_path.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("url"):
                rows.append(row)

new_rows = []
for r in rows:
    url = r.get("url", "")
    rid = get_rns_id(url)
    if rid and rid not in ref_seen_ids:
        new_rows.append(r)


# ------------------------------------------------
# BUILD & SEND EMAIL
# ------------------------------------------------

if new_rows:
    html_parts = [
        '<div style="font:14px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,Helvetica,sans-serif; color:#222;">'
    ]

    html_parts.append(f"""
    <div style="margin-bottom:18px;">
        <h2 style="margin:0 0 4px 0; font-size:20px;">Investegate – NEW Matches</h2>
        <div style="color:#777; font-size:12px;">Run #{esc(run_num)} · {len(new_rows)} new item(s)</div>
    </div>
    """)

    html_parts.append('<ol style="padding-left:22px; margin-top:0;">')

    for r in new_rows:
        title = r.get("title", "").strip() or "Untitled announcement"
        url = r.get("url", "").strip()
        dt = r.get("dt_iso", "").strip()

        supplied_keyword_string = row_first(r, [
            "matched_keywords", "matched_keyword", "keyword", "keywords",
            "keyword_hit", "trigger", "triggers", "matches"
        ])
        supplied_builtin_trigger_string = row_first(r, [
            "matched_builtin_triggers", "builtin_triggers", "investor_triggers", "trigger_types"
        ])

        body = fetch_article_text(url)

        matched_keywords = split_keywords(supplied_keyword_string)
        matched_builtin_triggers = split_keywords(supplied_builtin_trigger_string)

        # Backwards fallback for old CSVs that do not yet contain matched_keywords.
        if not matched_keywords:
            matched_keywords = detect_keyword_hits(f"{title}\n{body}", user_keywords)

        trigger_display = ", ".join(matched_keywords) if matched_keywords else "No explicit keyword detected"
        company_parts = build_company_parts(r, title)

        analysis = analyze_rns(
            title=title,
            url=url,
            body=body,
            company_parts=company_parts,
            matched_keywords=matched_keywords,
            matched_builtin_triggers=matched_builtin_triggers,
        )

        company_line = build_company_line(company_parts, analysis.get("company_description", ""))
        summary = analysis.get("summary", "")

        html_parts.append(f"""
        <li style="margin:0 0 24px 0; padding-bottom:18px; border-bottom:1px solid #eee;">
            <div style="font-size:15px; font-weight:650; margin-bottom:2px;">
                <a href="{esc(url)}" target="_blank" style="color:#0b57d0; text-decoration:none;">
                    {esc(title)}
                </a>
            </div>

            <div style="color:#888; font-size:12px; margin-bottom:10px;">
                {esc(dt)}
            </div>

            <div style="font-size:13px; color:#333;">
                <p style="margin:0 0 7px 0;">
                    <strong>Company:</strong> {esc(company_line)}
                </p>

                <p style="margin:0 0 7px 0;">
                    <strong>Trigger:</strong> {esc(trigger_display)}
                </p>

                <div style="margin:0;">
                    <strong>Summary:</strong>
                    <div style="margin-top:4px;">
                        {summary_text_to_html(summary)}
                    </div>
                </div>
            </div>
        </li>
        """)

    html_parts.append("</ol>")
    html_parts.append("""
    <p style="font-size:12px; color:#777; margin-top:18px;">
        Full CSV attached.
    </p>
    """)
    html_parts.append("</div>")
    html_body = "\n".join(html_parts)

    msg = MIMEMultipart("mixed")
    msg["Subject"] = f"Investegate RNS Digest – run #{run_num}"
    msg["From"] = formataddr((from_name, username))
    msg["To"] = to_email

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(alt)

    with csv_path.open("rb") as f:
        part = MIMEBase("text", "csv")
        part.set_payload(f.read())

    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename="investegate_hits.csv")
    msg.attach(part)

    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.ehlo()
        try:
            server.starttls()
            server.ehlo()
        except Exception:
            pass
        server.login(username, password)
        server.sendmail(username, [to_email], msg.as_string())

    print(f"Email sent. NEW items: {len(new_rows)} / total {len(rows)}")

    sent_urls = [r["url"] for r in new_rows if r.get("url")]
    history_state["emails"].append(sent_urls)
    if len(history_state["emails"]) > MAX_HISTORY_EMAILS:
        history_state["emails"] = history_state["emails"][-MAX_HISTORY_EMAILS:]
    history_file.write_text(json.dumps(history_state), encoding="utf-8")

else:
    print("No new items (all duplicates suppressed). No email sent.")
