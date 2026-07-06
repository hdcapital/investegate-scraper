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

# How many past emails to remember for de-dupe.
MAX_HISTORY_EMAILS = int(os.environ.get("MAX_HISTORY_EMAILS", "6"))

# Single state directory
base_state_dir = Path(".state")
base_state_dir.mkdir(parents=True, exist_ok=True)

# History file
history_file = base_state_dir / "email_history_urls.json"


# ------------------------------------------------
# HELPERS
# ------------------------------------------------

def get_rns_id(url: str) -> str:
    """
    Extract RNS ID from URL, e.g. 9272576.
    Used for de-duping across small URL changes.
    """
    try:
        parts = url.rstrip("/").split("/")
        return parts[-1].split("?")[0].lower()
    except Exception:
        return ""


def esc(x) -> str:
    """
    HTML-escape text safely.
    """
    return html.escape(str(x or ""), quote=True)


def row_first(row: dict, keys: list[str]) -> str:
    """
    Return first non-empty value from possible CSV column names.
    """
    for k in keys:
        v = row.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def guess_company_from_title(title: str) -> str:
    """
    Crude fallback to infer company name from announcement title.
    Useful when CSV doesn't provide issuer/company.
    """
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
    """
    Split a keyword string from CSV into clean keyword list.
    Handles commas, semicolons, pipes.
    """
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
    """
    Detect actual keyword hits in title/body text.
    Uses simple case-insensitive substring matching because many keywords
    may be phrases, ticker-like strings, or special-situation terms.
    """
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
    """
    Convert AI plain-text summary into clean HTML paragraphs.
    Prevents the email from becoming one dense wall of text.
    """
    if not text:
        return ""

    safe = esc(text).strip()

    # Split on blank lines into paragraphs.
    paras = [p.strip() for p in re.split(r"\n\s*\n", safe) if p.strip()]

    if not paras:
        return safe.replace("\n", "<br>")

    html_paras = []
    for p in paras:
        p = p.replace("\n", "<br>")
        html_paras.append(f'<p style="margin:0 0 8px 0;">{p}</p>')

    return "\n".join(html_paras)


def extract_json_object(s: str) -> dict:
    """
    Robustly extract JSON object from model output.
    Handles accidental ```json fences.
    """
    if not s:
        return {}

    s = s.strip()

    # Remove markdown code fences if present.
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```$", "", s)

    try:
        return json.loads(s)
    except Exception:
        pass

    # Try extracting first JSON-looking object.
    m = re.search(r"\{.*\}", s, flags=re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return {}

    return {}


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

# Build a set of IDs already sent.
# We use IDs instead of full URLs to be safer against small URL changes.
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
                s = line.strip()
                if s and not s.startswith("#"):
                    out.append(s)

    # Unique, preserving original case/order.
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
        r = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "lxml")
        parts = [t.get_text(" ", strip=True) for t in soup.find_all(["p", "li"])]
        text = " ".join(parts)
        text = re.sub(r"\s{2,}", " ", text)

        return text[:12000]

    except Exception:
        return ""


def summarize_rns(
    title: str,
    url: str,
    body: str,
    matched_keywords: list[str],
    supplied_company: str = "",
    supplied_market_cap: str = ""
) -> dict:
    """
    Returns structured fields for the email:
    - company
    - trigger
    - summary

    Important: market cap should only be used if supplied or explicitly present
    in the announcement. Otherwise it should be n/a.
    """

    trigger_text = ", ".join(matched_keywords) if matched_keywords else "No explicit keyword detected"

    prompt = f"""
You are a buy-side analyst writing an automated RNS digest for a PM.

Return VALID JSON ONLY. No markdown. No code fences.

Use this exact JSON schema:
{{
  "company": "Company name — super short business description. Market cap: ...",
  "trigger": "keyword 1, keyword 2",
  "summary": "Analyst-quality review of the announcement."
}}

Rules:
- The "company" field should include:
  1. company name;
  2. a very short business description;
  3. current market cap if supplied below or explicitly stated in the announcement.
- If market cap is not supplied and not explicitly stated in the announcement, write: "Market cap: n/a".
- Do not hallucinate a market cap.
- The "trigger" field must use the matched keyword/keywords supplied below. Do not list the full keyword universe.
- The "summary" field should explain:
  1. what actually happened;
  2. why it matters;
  3. how it relates to the trigger keyword/keywords;
  4. any financial, liquidity, governance, competitive, or timing signals;
  5. what a PM should pay attention to.
- The summary should be concise but genuinely analytical.
- No fluff. Interpret, do not just describe.

Supplied company, if any: {supplied_company or "n/a"}
Supplied market cap, if any: {supplied_market_cap or "n/a"}
Matched keyword/keywords: {trigger_text}

RNS Title: {title}
URL: {url}

Announcement text:
{body}
"""

    try:
        resp = client.responses.create(
            model="gpt-5.1",
            input=prompt,
            max_output_tokens=600
        )

        raw = resp.output_text.strip()
        data = extract_json_object(raw)

        company = str(data.get("company", "")).strip()
        trigger = str(data.get("trigger", "")).strip()
        summary = str(data.get("summary", "")).strip()

        if not company:
            company_name = supplied_company or guess_company_from_title(title) or "Unknown company"
            market_cap = supplied_market_cap or "n/a"
            company = f"{company_name} — Business description unavailable. Market cap: {market_cap}"

        if not trigger:
            trigger = trigger_text

        if not summary:
            summary = raw if raw else "Summary unavailable."

        return {
            "company": company,
            "trigger": trigger,
            "summary": summary
        }

    except Exception as e:
        company_name = supplied_company or guess_company_from_title(title) or "Unknown company"
        market_cap = supplied_market_cap or "n/a"

        return {
            "company": f"{company_name} — Business description unavailable. Market cap: {market_cap}",
            "trigger": trigger_text,
            "summary": f"Summary unavailable: {e}"
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

# Filter: only keep rows where the RNS ID has NOT been seen in history.
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

        supplied_company = row_first(
            r,
            [
                "company",
                "company_name",
                "issuer",
                "issuer_name",
                "name",
                "ticker",
                "epic",
                "symbol"
            ]
        )

        if not supplied_company:
            supplied_company = guess_company_from_title(title)

        supplied_market_cap = row_first(
            r,
            [
                "market_cap",
                "mkt_cap",
                "marketcap",
                "current_market_cap",
                "market_capitalisation",
                "market_capitalization"
            ]
        )

        supplied_keyword_string = row_first(
            r,
            [
                "matched_keywords",
                "matched_keyword",
                "keyword",
                "keywords",
                "keyword_hit",
                "trigger",
                "triggers",
                "matches"
            ]
        )

        body = fetch_article_text(url)

        # Prefer explicit matched-keyword CSV column if present.
        # Otherwise detect actual hits from title + fetched article body.
        matched_keywords = split_keywords(supplied_keyword_string)

        if not matched_keywords:
            matched_keywords = detect_keyword_hits(
                f"{title}\n{body}",
                user_keywords
            )

        analysis = summarize_rns(
            title=title,
            url=url,
            body=body,
            matched_keywords=matched_keywords,
            supplied_company=supplied_company,
            supplied_market_cap=supplied_market_cap
        )

        company_html = esc(analysis.get("company", ""))
        trigger_html = esc(analysis.get("trigger", ""))
        summary_html = summary_text_to_html(analysis.get("summary", ""))

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
                    <strong>Company:</strong> {company_html}
                </p>

                <p style="margin:0 0 7px 0;">
                    <strong>Trigger:</strong> {trigger_html}
                </p>

                <div style="margin:0;">
                    <strong>Summary:</strong>
                    <div style="margin-top:4px;">
                        {summary_html}
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
    part.add_header(
        "Content-Disposition",
        "attachment",
        filename="investegate_hits.csv"
    )
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

    # ------------------------------------------------
    # UPDATE HISTORY
    # ------------------------------------------------

    sent_urls = [r["url"] for r in new_rows if r.get("url")]
    history_state["emails"].append(sent_urls)

    if len(history_state["emails"]) > MAX_HISTORY_EMAILS:
        history_state["emails"] = history_state["emails"][-MAX_HISTORY_EMAILS:]

    history_file.write_text(json.dumps(history_state), encoding="utf-8")

else:
    print("No new items (all duplicates suppressed). No email sent.")
