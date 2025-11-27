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
# Each email might have dozens of URLs, but with 6 emails this is still only a few KB.
MAX_HISTORY_EMAILS = int(os.environ.get("MAX_HISTORY_EMAILS", "6"))

# Single state directory, persisted across runs by the GitHub Actions cache
base_state_dir = Path(".state")
base_state_dir.mkdir(parents=True, exist_ok=True)

# History file: list of past emails and the URLs they contained
history_file = base_state_dir / "email_history_urls.json"

# ------------------------------------------------
# LOAD HISTORY-STATE (for de-dupe across runs)
# ------------------------------------------------
history_state = {"emails": []}  # emails: List[List[url]]
if history_file.exists():
    try:
        data = json.loads(history_file.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("emails"), list):
            emails = []
            for entry in data["emails"]:
                if isinstance(entry, list):
                    emails.append([u for u in entry if isinstance(u, str)])
            history_state["emails"] = emails
    except Exception:
        history_state = {"emails": []}

# Anything in the last MAX_HISTORY_EMAILS emails is considered "already emailed"
ref_seen = set()
for urls in history_state["emails"]:
    for u in urls:
        ref_seen.add(u)

client = OpenAI(api_key=openai_api_key)


# ------------------------------------------------
# LOAD KEYWORDS.TXT
# ------------------------------------------------
def load_keywords(path="keywords.txt"):
    """Load unique, non-comment user keywords to feed into GPT summaries."""
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
# FETCH FULL ARTICLE TEXT FOR SUMMARISATION
# ------------------------------------------------
def fetch_article_text(url):
    """Fetch article body text with minimal dependencies. No parsing complexity."""
    try:
        r = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        soup = BeautifulSoup(r.text, "lxml")
        parts = [t.get_text(" ", strip=True) for t in soup.find_all(["p", "li"])]
        text = " ".join(parts)
        text = re.sub(r"\s{2,}", " ", text)
        return text[:12000]  # hard safety cut
    except Exception:
        return ""

# ------------------------------------------------
# GPT-5.1 SUMMARY
# ------------------------------------------------
def summarize_rns(title, url, body, user_keywords):
    """Produce PM-grade institutional summary using GPT-5.1."""
    prompt = f"""
You are a top-tier buy-side investment analyst at a global multi-billion-dollar fund.
Your job is NOT to summarise. Your job is to extract **signals**, **red flags**, **upside optionality**, and
**trade-relevant information** from company announcements.

The PM only wants to know ONE thing:
“Does this matter, and if so, why?”

You will analyse the following RNS announcement with extreme discipline and produce a concise,
high-signal brief that includes:

1. **What happened (1–2 sentences max).**
2. **Why this matters financially** — margins, cash flow, liquidity, leverage, capex, working capital,
   operating momentum, covenant headroom, capital returns, structural changes.
3. **Why this matters competitively** — market share, pricing power, industry structure, regulatory
   change, customer concentration, contract wins/losses, product/segment divergence.
4. **Any non-obvious signals** — insider incentives, behavioural tells, quality of disclosure,
   unexpected tone shift, unusual strategic moves, accounting choices.
5. **Keyword linkages**  
   Identify which of the fund’s key themes/keywords were hit and explain
   *why that is investor-relevant*, not just that they appeared.
   Keywords: {", ".join(user_keywords) if user_keywords else "None"}.
6. **PM Actionability**  
   What should the PM *pay attention to*? Include:
   - potential trade ideas (long/short),  
   - catalysts,  
   - positioning implications,  
   - risks,  
   - inflection setups,  
   - hedging relevance.

Tone & style expectations:
- Write like a senior analyst at a top-performing hedge fund.
- No filler. No fluff. No repeating text from the announcement.
- Every sentence must convey insight, not information.
- Prioritise **interpretation**, not description.
- If the RNS is irrelevant, explicitly say so and explain why it does NOT matter.

RNS Title: {title}
URL: {url}

CONTENT:
{body}


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
# LOAD CSV ROWS
# ------------------------------------------------
rows = []
if csv_path.exists():
    with csv_path.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("url"):
                rows.append(row)

# De-dupe against the *other* session's last email
new_rows = [r for r in rows if r["url"] not in ref_seen]

# ------------------------------------------------
# BUILD EMAIL WITH SUMMARIES
# ------------------------------------------------
if new_rows:
    html_parts = ['<div style="font:14px/1.5 -apple-system,Segoe UI,Roboto,Arial,Helvetica,sans-serif">']
    html_parts.append(
        f'<h2>Investegate – NEW vs last {MAX_HISTORY_EMAILS} emails (run #{run_num})</h2>'
    )

    html_parts.append("<ol>")

    for r in new_rows:
        title = r.get("title", "").strip()
        url = r.get("url", "")
        dt = r.get("dt_iso", "")

        # fetch full text + summarise
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

else:
    html_body = (
        "<div style='font:14px/1.5 -apple-system,Segoe UI,Roboto'>"
        "<h2>No new items</h2>"
        "<p>All items already sent previously.</p>"
        "</div>"
    )

# ------------------------------------------------
# SEND EMAIL
# ------------------------------------------------
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

# ------------------------------------------------
# UPDATE HISTORY STATE (append this email and keep only last MAX_HISTORY_EMAILS)
# ------------------------------------------------
sent_this_run = [r["url"] for r in new_rows if r.get("url")]
if sent_this_run:
    # Append this email's URLs to history
    history_state["emails"].append(sent_this_run)
    # Trim to last MAX_HISTORY_EMAILS emails
    if len(history_state["emails"]) > MAX_HISTORY_EMAILS:
        history_state["emails"] = history_state["emails"][-MAX_HISTORY_EMAILS:]
    history_file.write_text(json.dumps(history_state), encoding="utf-8")

