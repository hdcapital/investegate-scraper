#!/usr/bin/env python3
import os, csv, json, smtplib
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formataddr

# -----------------------
# CONFIG VIA ENV VARS
# -----------------------
csv_path = Path(os.environ["CSV_FILE"])
run_num = os.environ.get("GITHUB_RUN_NUMBER", "?")
from_name = os.environ.get("FROM_NAME", "HD Capital")
username = os.environ["SMTP_USERNAME"]
password = os.environ["SMTP_PASSWORD"]
to_email = os.environ["TO_EMAIL"]
smtp_server = os.environ["SMTP_SERVER"]
smtp_port = int(os.environ["SMTP_PORT"])

# Which state file to use
state_mode = os.environ.get("STATE_MODE", "morning")  
# morning → dedupe against last evening results
# evening → dedupe against last morning results

state_file_map = {
    "morning": ".state/seen_evening.json",
    "evening": ".state/seen_morning.json"
}
state_file = Path(state_file_map[state_mode])
state_file.parent.mkdir(parents=True, exist_ok=True)

# -----------------------
# LOAD STATE
# -----------------------
seen = set()
if state_file.exists():
    try:
        seen = set(json.loads(state_file.read_text(encoding="utf-8")))
    except Exception:
        pass

# -----------------------
# LOAD CSV ROWS
# -----------------------
rows = []
if csv_path.exists():
    with csv_path.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("url"):
                rows.append(row)

new_rows = [r for r in rows if r["url"] not in seen]

# -----------------------
# BUILD EMAIL
# -----------------------
if new_rows:
    html = ['<div style="font:14px/1.5 -apple-system,Segoe UI,Roboto,Arial,Helvetica,sans-serif">']
    html.append(f'<h2>Investegate – NEW since last {state_mode} reference (run #{run_num})</h2>')
    html.append("<ol>")
    for r in new_rows:
        title = r.get("title", "").strip()
        url = r.get("url", "")
        dt = r.get("dt_iso", "")
        html.append(f'<li><a href="{url}" target="_blank">{title}</a> <span style="color:#888">{dt}</span></li>')
    html.append("</ol>")
    html.append("<p>Full CSV attached.</p></div>")
    html_body = "\n".join(html)
else:
    html_body = (
        "<div style='font:14px/1.5 -apple-system,Segoe UI,Roboto'>"
        "<h2>No new items</h2>"
        "<p>All items already sent previously.</p>"
        "</div>"
    )

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

# -----------------------
# UPDATE STATE
# -----------------------
for r in rows:
    if r.get("url"):
        seen.add(r["url"])

state_file.write_text(json.dumps(sorted(seen)), encoding="utf-8")
