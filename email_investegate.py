#!/usr/bin/env python3
import os, csv, json, smtplib, requests, re
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

# Helper to extract RNS ID (e.g., 9272576) from URL
def get_rns_id(url: str) -> str:
    try:
        parts = url.rstrip("/").split("/")
        return parts[-1].split("?")[0].lower()
    except:
        return ""

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

# Build a set of IDs already sent
# We use IDs instead of full URLs to be safer against small URL changes
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
def load_keywords(path="keywords.txt"):
    out = []
    if os.path.isfile(path):
        for line in open(path, "r", encoding="utf-8"):
            s = line.strip()
            if s and not s.startswith("#"):
                out.append(s)
    # unique + lowercased
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
def fetch_article_text(url):
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "lxml")
        parts = [t.get_text(" ", strip=True) for t in soup.find_all(["p", "li"])]
        text = " ".join(parts)
        text = re.sub(r"\s{2,}", " ", text)
        return text[:12000]
    except Exception:
        return ""

def summarize_rns(title, url, body, user_keywords):
    prompt = f"""
You are a buy-side analyst. Extract signals, red flags, and trade-relevant info.
The PM asks: “Does this matter, and why?”

1. **What happened (1-2 sentences)**
2. **Financial Impact** (Liquidity, margins, etc.)
3. **Competitive Impact**
4. **Non-obvious signals** (Tone, timing)
5. **Keyword Hit**: {", ".join(user_keywords) if user_keywords else "None"}
6. **PM Actionability** (Trade ideas, risks)

No fluff. Interpret, don't just describe.
RNS Title: {title}
URL: {url}
CONTENT: {body}
"""
    try:
        resp = client.responses.create(
            model="gpt-5.1",
            input=prompt,
            max_output_tokens=350
        )
        return resp.output_text.strip()
    except Exception as e:
        return f"(Summary unavailable: {e})"

# ------------------------------------------------
# LOAD CSV ROWS & DEDUPE
# ------------------------------------------------
rows = []
if csv_path.exists():
    with csv_path.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("url"):
                rows.append(row)

# Filter: Only keep rows where the RNS ID has NOT been seen in history
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
    html_parts = ['<div style="font:14px/1.5 -apple-system,Segoe UI,Roboto,Arial,Helvetica,sans-serif">']
    html_parts.append(f'<h2>Investegate – NEW Matches (run #{run_num})</h2>')
    html_parts.append("<ol>")

    for r in new_rows:
        title = r.get("title", "").strip()
        url = r.get("url", "")
        dt = r.get("dt_iso", "")
        
        body = fetch_article_text(url)
        summary = summarize_rns(title, url, body, user_keywords)

        html_parts.append(f"""
        <li>
            <a href="{url}" target="_blank">{title}</a>
            <span style="color:#888">{dt}</span>
            <div style="margin-top:6px; margin-bottom:12px; font-size:13px; color:#333;">
                <strong>Summary:</strong><br>
                {summary}
            </div>
        </li>
        """)

    html_parts.append("</ol>")
    html_parts.append("<p>Full CSV attached.</p></div>")
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
            server.starttls(); server.ehlo()
        except Exception:
            pass
        server.login(username, password)
        server.sendmail(username, [to_email], msg.as_string())

    print(f"Email sent. NEW items: {len(new_rows)} / total {len(rows)}")

    # UPDATE HISTORY
    sent_urls = [r["url"] for r in new_rows if r.get("url")]
    history_state["emails"].append(sent_urls)
    if len(history_state["emails"]) > MAX_HISTORY_EMAILS:
        history_state["emails"] = history_state["emails"][-MAX_HISTORY_EMAILS:]
    history_file.write_text(json.dumps(history_state), encoding="utf-8")

else:
    print("No new items (all duplicates suppressed). No email sent.")
