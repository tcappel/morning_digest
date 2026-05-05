#!/usr/bin/env python3
"""
Morning Newsletter Digest
Fetches Gmail "Newsletters" label, summarizes with Claude, emails the digest.
Runs as a scheduled job on Render (cron: 30 13 * * 1-5  = 8:30 AM CT Mon-Fri)
"""

import os
import sys
import base64
import json
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import anthropic
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config (all from environment variables set in Render) ─────────────────────
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
SEND_TO_EMAIL      = os.environ.get("SEND_TO_EMAIL", "timcappel1@gmail.com")
GMAIL_LABEL        = os.environ.get("GMAIL_LABEL", "Newsletters")
MAX_EMAILS         = int(os.environ.get("MAX_EMAILS", "20"))
HOURS_BACK         = int(os.environ.get("HOURS_BACK", "24"))

# Gmail OAuth token stored as a JSON string in env var (set during setup)
GMAIL_TOKEN_JSON   = os.environ["GMAIL_TOKEN_JSON"]
GMAIL_CLIENT_ID    = os.environ["GMAIL_CLIENT_ID"]
GMAIL_CLIENT_SECRET = os.environ["GMAIL_CLIENT_SECRET"]

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


# ── Gmail ─────────────────────────────────────────────────────────────────────

def get_gmail_service():
    token_data = json.loads(GMAIL_TOKEN_JSON)
    # Inject client_id and client_secret so refresh works without credentials file
    token_data.setdefault("client_id", GMAIL_CLIENT_ID)
    token_data.setdefault("client_secret", GMAIL_CLIENT_SECRET)
    creds = Credentials.from_authorized_user_info(token_data, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        log.info("Gmail token refreshed.")
    return build("gmail", "v1", credentials=creds)


def decode_body(part):
    data = part.get("body", {}).get("data", "")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")


def extract_text(payload):
    mime = payload.get("mimeType", "")
    parts = payload.get("parts", [])
    if mime == "text/plain":
        return decode_body(payload)
    collected = []
    for part in parts:
        t = extract_text(part)
        if t:
            collected.append(t)
    return "\n".join(collected)


def fetch_newsletters(service):
    query = f"label:{GMAIL_LABEL} newer_than:{HOURS_BACK}h"
    log.info(f"Gmail query: {query}")
    result = service.users().messages().list(
        userId="me", q=query, maxResults=MAX_EMAILS
    ).execute()

    messages = result.get("messages", [])
    if not messages:
        return []

    log.info(f"Found {len(messages)} messages — fetching full content…")
    emails = []
    for i, ref in enumerate(messages):
        msg = service.users().messages().get(
            userId="me", id=ref["id"], format="full"
        ).execute()
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        body = extract_text(msg["payload"])
        body = " ".join(body.split())  # collapse whitespace
        emails.append({
            "subject": headers.get("Subject", "(No subject)"),
            "from":    headers.get("From", "Unknown"),
            "date":    headers.get("Date", ""),
            "body":    body[:2500],
            "snippet": msg.get("snippet", ""),
        })
        log.info(f"  [{i+1}/{len(messages)}] {emails[-1]['from'][:40]} — {emails[-1]['subject'][:50]}")
    return emails


# ── Claude ────────────────────────────────────────────────────────────────────

def call_claude(client, system, user, max_tokens=1000):
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return resp.content[0].text.strip()


def parse_json(raw):
    raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    return json.loads(raw)


def build_digest(client, emails):
    email_input = "\n\n---\n\n".join(
        f"[{i}] FROM: {e['from']}\nSUBJECT: {e['subject']}\nCONTENT: {e['body'][:800]}"
        for i, e in enumerate(emails)
    )
    system = (
        'You are a morning briefing editor. Return ONLY valid JSON, no markdown:\n'
        '{"headline":"One punchy headline max 12 words",'
        '"overview":"3-4 sentences. Specific — name topics, people, numbers.",'
        '"themes":[{"name":"Theme","summary":"2-3 sentences","sources":[0,1]}]}'
        '\nMax 6 themes, only include themes with real content.'
    )
    raw = call_claude(client, system, email_input, max_tokens=1000)
    try:
        return parse_json(raw)
    except Exception:
        return {"headline": "This Morning's Newsletters", "overview": "", "themes": []}


def build_full_briefing(client, emails):
    email_input = "\n\n---\n\n".join(
        f"FROM: {e['from']}\nSUBJECT: {e['subject']}\nCONTENT: {e['body'][:1000]}"
        for e in emails
    )
    system = (
        "You are a sharp morning briefing writer. Write a single cohesive briefing synthesizing "
        "ALL newsletters into flowing prose a busy person enjoys with coffee. "
        "No bullets, no headers — just connected paragraphs. Be specific: name stories, people, numbers. "
        "350-500 words. End with one forward-looking sentence."
    )
    return call_claude(client, system,
                       f"Write the full morning briefing:\n\n{email_input}",
                       max_tokens=1000)


def summarize_email(client, email):
    system = (
        'Summarize this newsletter for a busy professional. Specific — name topics, numbers, people. '
        'Return ONLY valid JSON: {"summary":"2-3 sentences",'
        '"bullets":["takeaway 1","takeaway 2","takeaway 3","takeaway 4"],'
        '"tags":["Tag1","Tag2","Tag3"]}'
    )
    user = f"Subject: {email['subject']}\nFrom: {email['from']}\n\n{email['body'][:1800]}"
    raw = call_claude(client, system, user, max_tokens=800)
    try:
        return parse_json(raw)
    except Exception:
        return {"summary": email.get("snippet", ""), "bullets": [], "tags": []}


# ── HTML email ────────────────────────────────────────────────────────────────

def build_html_email(emails, digest, briefing_text, summaries, generated_at):
    today    = generated_at.strftime("%A, %B %-d, %Y")
    time_str = generated_at.strftime("%-I:%M %p CT")

    def theme_card(theme):
        sources = ", ".join(
            emails[si]["from"].split("<")[0].strip()
            for si in theme.get("sources", []) if si < len(emails)
        )
        src_html = f'<div style="font-size:11px;color:#555;margin-top:6px;">via {sources}</div>' if sources else ""
        return f"""
        <div style="background:#1a1a1a;border:1px solid #2a2a2a;border-radius:4px;padding:14px 16px;margin-bottom:8px;">
          <div style="font-family:monospace;font-size:10px;text-transform:uppercase;letter-spacing:1.5px;color:#4b9ee8;margin-bottom:6px;">{theme['name']}</div>
          <div style="font-size:13px;line-height:1.6;color:#9a9590;">{theme['summary']}</div>
          {src_html}
        </div>"""

    def nl_card(i, email):
        s = summaries.get(i, {})
        bullets = "".join(f'<li style="margin-bottom:5px;">{b}</li>' for b in s.get("bullets", []))
        tags    = "".join(
            f'<span style="display:inline-block;font-family:monospace;font-size:10px;text-transform:uppercase;'
            f'letter-spacing:1px;padding:2px 7px;background:#1a1a1a;border:1px solid #2a2a2a;'
            f'border-radius:2px;color:#555;margin:0 4px 4px 0;">{t}</span>'
            for t in s.get("tags", [])
        )
        sender = email["from"].split("<")[0].strip().strip('"')
        return f"""
        <div style="background:#141414;border:1px solid #2a2a2a;border-radius:4px;margin-bottom:8px;">
          <div style="padding:12px 16px;border-bottom:1px solid #2a2a2a;">
            <div style="font-family:monospace;font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#444;margin-bottom:3px;">{sender}</div>
            <div style="font-size:15px;font-weight:700;color:#e8e4dc;line-height:1.3;">{email['subject']}</div>
          </div>
          <div style="padding:12px 16px;">
            <p style="font-size:13px;line-height:1.7;color:#9a9590;margin:0 0 10px;">{s.get('summary','')}</p>
            <ul style="font-size:13px;line-height:1.6;color:#d0ccc4;padding-left:18px;margin:0 0 10px;">{bullets}</ul>
            <div>{tags}</div>
          </div>
        </div>"""

    themes_html   = "".join(theme_card(t) for t in digest.get("themes", []))
    nl_cards_html = "".join(nl_card(i, e) for i, e in enumerate(emails))
    briefing_html = "".join(
        f'<p style="margin:0 0 16px;font-size:15px;line-height:1.9;color:#c0bcb4;">{p}</p>'
        for p in briefing_text.strip().split("\n") if p.strip()
    )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#0d0d0d;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<div style="max-width:660px;margin:0 auto;padding:24px 16px;">

  <!-- Header -->
  <div style="padding:20px 0 16px;border-bottom:2px solid #2a2a2a;margin-bottom:24px;">
    <div style="font-family:monospace;font-size:10px;letter-spacing:2.5px;text-transform:uppercase;color:#e8b84b;margin-bottom:8px;">Your Morning Briefing</div>
    <div style="font-size:34px;font-weight:900;color:#e8e4dc;line-height:1;margin-bottom:10px;">
      Morning <span style="font-style:italic;font-weight:300;color:#e8b84b;">Digest</span>
    </div>
    <div style="font-family:monospace;font-size:11px;color:#444;">{today}&nbsp;&nbsp;·&nbsp;&nbsp;{time_str}&nbsp;&nbsp;·&nbsp;&nbsp;{len(emails)} newsletters</div>
  </div>

  <!-- Headline + Overview -->
  <div style="background:#161616;border:1px solid #2a2a2a;border-left:3px solid #e8b84b;border-radius:4px;padding:20px 22px;margin-bottom:20px;">
    <div style="font-size:21px;font-weight:700;color:#e8e4dc;line-height:1.3;margin-bottom:12px;">{digest.get('headline','This Morning')}</div>
    <p style="font-size:14px;line-height:1.8;color:#a0a09a;margin:0;">{digest.get('overview','')}</p>
  </div>

  <!-- Topics -->
  {"<div style='margin-bottom:20px;'><div style='font-family:monospace;font-size:10px;letter-spacing:2.5px;text-transform:uppercase;color:#444;padding-bottom:10px;border-bottom:1px solid #222;margin-bottom:12px;'>Topics This Morning</div>" + themes_html + "</div>" if themes_html else ""}

  <!-- Full Briefing -->
  <div style="background:#111;border:1px solid #2a2a2a;border-radius:4px;padding:22px;margin-bottom:20px;">
    <div style="font-family:monospace;font-size:10px;letter-spacing:2.5px;text-transform:uppercase;color:#e8b84b;padding-bottom:12px;border-bottom:1px solid #222;margin-bottom:18px;">Full Morning Briefing</div>
    {briefing_html}
  </div>

  <!-- Individual Newsletters -->
  <div style="margin-bottom:20px;">
    <div style="font-family:monospace;font-size:10px;letter-spacing:2.5px;text-transform:uppercase;color:#444;padding-bottom:10px;border-bottom:1px solid #222;margin-bottom:12px;">Individual Newsletters</div>
    {nl_cards_html}
  </div>

  <!-- Footer -->
  <div style="padding-top:16px;border-top:1px solid #1a1a1a;font-family:monospace;font-size:10px;color:#2a2a2a;text-align:center;">
    Generated {generated_at.strftime("%-I:%M %p CT on %A, %B %-d, %Y")}
  </div>

</div>
</body>
</html>"""


def send_email(service, subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = "me"
    msg["To"]      = SEND_TO_EMAIL
    msg.attach(MIMEText(html_body, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    log.info(f"Email sent to {SEND_TO_EMAIL}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("☕  Morning Digest starting…")

    log.info("[1/5] Connecting to Gmail…")
    service = get_gmail_service()

    log.info("[2/5] Fetching newsletters…")
    emails = fetch_newsletters(service)
    if not emails:
        log.info("No newsletters found. Exiting.")
        return

    log.info(f"[3/5] Building digest with Claude… ({len(emails)} emails)")
    client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    digest  = build_digest(client, emails)
    log.info(f"  Headline: {digest.get('headline','')}")

    log.info("[4/5] Writing full briefing + individual summaries…")
    briefing = build_full_briefing(client, emails)

    summaries = {}
    for i, email in enumerate(emails):
        log.info(f"  Summarizing {i+1}/{len(emails)}: {email['subject'][:50]}")
        try:
            summaries[i] = summarize_email(client, email)
        except Exception as e:
            log.warning(f"  Skipped summary for {i}: {e}")
        if i < len(emails) - 1:
            time.sleep(1)

    log.info("[5/5] Building and sending email…")
    now     = datetime.now(ZoneInfo("America/Chicago"))
    subject = f"☕ Morning Digest — {now.strftime('%A, %B %-d')}"
    html    = build_html_email(emails, digest, briefing, summaries, now)
    send_email(service, subject, html)

    log.info("✅  Done.")


if __name__ == "__main__":
    main()
