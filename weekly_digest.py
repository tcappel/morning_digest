#!/usr/bin/env python3
"""
Weekly Read Later Digest
Runs every Friday at noon CT via GitHub Actions.
Looks back 5 days, extracts top links and stories, sends a "Week in Review" email.
"""

import os
import re
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
SEND_TO_EMAIL       = os.environ.get("SEND_TO_EMAIL", "timcappel1@gmail.com")
GMAIL_LABEL         = os.environ.get("GMAIL_LABEL", "Newsletters")
GMAIL_TOKEN_JSON    = os.environ["GMAIL_TOKEN_JSON"]
GMAIL_CLIENT_ID     = os.environ["GMAIL_CLIENT_ID"]
GMAIL_CLIENT_SECRET = os.environ["GMAIL_CLIENT_SECRET"]

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


# ── Gmail ─────────────────────────────────────────────────────────────────────

def get_gmail_service():
    token_data = json.loads(GMAIL_TOKEN_JSON)
    token_data.setdefault("client_id", GMAIL_CLIENT_ID)
    token_data.setdefault("client_secret", GMAIL_CLIENT_SECRET)
    creds = Credentials.from_authorized_user_info(token_data, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)


def decode_body(part):
    data = part.get("body", {}).get("data", "")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")


def extract_text_and_links(payload):
    """Extract both plain text and all URLs from a Gmail message payload."""
    mime = payload.get("mimeType", "")
    parts = payload.get("parts", [])

    text = ""
    if mime == "text/plain":
        text = decode_body(payload)
    elif mime == "text/html":
        text = decode_body(payload)
    else:
        for part in parts:
            t, _ = extract_text_and_links(part)
            if t:
                text += "\n" + t

    # Extract URLs from text
    urls = re.findall(r'https?://[^\s\'"<>)\]]+', text)
    # Filter out tracking/unsubscribe/image URLs
    skip = ["unsubscribe", "pixel", "track", "click.","open.","beacon",
            "img","image","logo","cdn","static","font","css","icon",
            "mailchimp","sendgrid","mandrillapp","sparkpost","constantcontact"]
    clean_urls = []
    seen = set()
    for url in urls:
        url = url.rstrip(".,;)")
        low = url.lower()
        if any(s in low for s in skip):
            continue
        if url not in seen and len(url) < 300:
            seen.add(url)
            clean_urls.append(url)

    # Clean text
    clean_text = " ".join(text.split())
    return clean_text[:2500], clean_urls[:20]


def fetch_week_newsletters(service):
    """Fetch all newsletters from the past 5 days."""
    query = f"label:{GMAIL_LABEL} newer_than:5d"
    log.info(f"Gmail query: {query}")
    result = service.users().messages().list(
        userId="me", q=query, maxResults=50
    ).execute()

    messages = result.get("messages", [])
    if not messages:
        return []

    log.info(f"Found {len(messages)} messages this week — fetching…")
    emails = []
    for i, ref in enumerate(messages):
        msg = service.users().messages().get(
            userId="me", id=ref["id"], format="full"
        ).execute()
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        body, links = extract_text_and_links(msg["payload"])
        emails.append({
            "subject": headers.get("Subject", "(No subject)"),
            "from":    headers.get("From", "Unknown"),
            "date":    headers.get("Date", ""),
            "body":    body,
            "links":   links,
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


def build_weekly_digest(client, emails):
    """Build top picks + grouped topics with links from the week's newsletters."""

    # Build input — include links with each email
    email_input = "\n\n---\n\n".join(
        f"[{i}] FROM: {e['from']}\nSUBJECT: {e['subject']}\nDATE: {e['date']}\n"
        f"CONTENT: {e['body'][:600]}\n"
        f"LINKS FOUND: {' | '.join(e['links'][:8]) if e['links'] else 'none'}"
        for i, e in enumerate(emails)
    )

    system = (
        'You are a weekly briefing editor. Analyze this week\'s newsletters and return ONLY valid JSON:\n'
        '{\n'
        '  "week_headline": "One punchy headline summarizing the week (max 12 words)",\n'
        '  "week_overview": "3-4 sentences on the biggest themes and stories of the week",\n'
        '  "top_picks": [\n'
        '    { "title": "Story title", "why": "One sentence on why this matters", "url": "best URL for this story", "source": "Newsletter name" }\n'
        '  ],\n'
        '  "topics": [\n'
        '    { "name": "Topic name", "summary": "2-3 sentences", "links": [ { "title": "Link title", "url": "url", "source": "newsletter" } ] }\n'
        '  ]\n'
        '}\n'
        'top_picks: exactly 5-7 of the most interesting/important stories of the week with their best URL.\n'
        'topics: 4-6 thematic groups, each with 2-4 relevant links extracted from the emails.\n'
        'Only include URLs that actually appeared in the email content — do not invent URLs.\n'
        'No markdown, no code fences, just the JSON.'
    )

    raw = call_claude(client, system, email_input, max_tokens=1000)
    try:
        return parse_json(raw)
    except Exception as e:
        log.warning(f"JSON parse failed: {e}\nRaw: {raw[:200]}")
        return {"week_headline": "This Week in Newsletters", "week_overview": "", "top_picks": [], "topics": []}


# ── HTML email ────────────────────────────────────────────────────────────────

def build_weekly_html(emails, digest, generated_at):
    week_of  = generated_at.strftime("%B %-d, %Y")
    time_str = generated_at.strftime("%-I:%M %p CT")

    # Top picks
    top_picks_html = ""
    for i, pick in enumerate(digest.get("top_picks", []), 1):
        url = pick.get("url", "")
        link_html = (
            f'<a href="{url}" style="color:#e8b84b;text-decoration:none;">{pick.get("title","")}</a>'
            if url else pick.get("title", "")
        )
        top_picks_html += f"""
        <div style="display:flex;gap:12px;margin-bottom:14px;align-items:flex-start;">
          <div style="font-family:monospace;font-size:18px;font-weight:900;color:#2a2a2a;flex-shrink:0;width:24px;padding-top:2px;">{i}</div>
          <div>
            <div style="font-size:15px;font-weight:700;line-height:1.3;margin-bottom:4px;">{link_html}</div>
            <div style="font-size:12px;color:#9a9590;line-height:1.5;">{pick.get('why','')}</div>
            <div style="font-family:monospace;font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#444;margin-top:4px;">via {pick.get('source','')}</div>
          </div>
        </div>"""

    # Topic groups
    topics_html = ""
    for topic in digest.get("topics", []):
        links_html = ""
        for link in topic.get("links", []):
            url = link.get("url", "")
            title = link.get("title", "")
            source = link.get("source", "")
            if url and title:
                links_html += f"""
                <div style="margin-bottom:8px;padding-left:12px;border-left:2px solid #2a2a2a;">
                  <a href="{url}" style="font-size:13px;font-weight:600;color:#e8b84b;text-decoration:none;line-height:1.4;">{title}</a>
                  <span style="font-family:monospace;font-size:10px;color:#444;margin-left:8px;">— {source}</span>
                </div>"""

        topics_html += f"""
        <div style="background:#141414;border:1px solid #2a2a2a;border-radius:4px;padding:16px 18px;margin-bottom:10px;">
          <div style="font-family:monospace;font-size:10px;text-transform:uppercase;letter-spacing:1.5px;color:#4b9ee8;margin-bottom:8px;">{topic.get('name','')}</div>
          <p style="font-size:13px;line-height:1.6;color:#9a9590;margin:0 0 12px;">{topic.get('summary','')}</p>
          {links_html}
        </div>"""

    # Sources this week
    seen_sources = []
    seen_set = set()
    for e in emails:
        name = e["from"].split("<")[0].strip().strip('"')
        if name not in seen_set:
            seen_set.add(name)
            seen_sources.append(name)

    sources_html = "  ·  ".join(
        f'<span style="color:#555;">{s}</span>' for s in seen_sources
    )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#0d0d0d;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#e8e4dc;">
<div style="max-width:660px;margin:0 auto;padding:24px 16px;">

  <!-- Header -->
  <div style="padding:20px 0 16px;border-bottom:2px solid #2a2a2a;margin-bottom:24px;">
    <div style="font-family:monospace;font-size:10px;letter-spacing:2.5px;text-transform:uppercase;color:#e8b84b;margin-bottom:8px;">Week in Review</div>
    <div style="font-size:34px;font-weight:900;color:#e8e4dc;line-height:1;margin-bottom:10px;">
      Read <span style="font-style:italic;font-weight:300;color:#e8b84b;">Later</span>
    </div>
    <div style="font-family:monospace;font-size:11px;color:#444;">
      Week of {week_of}&nbsp;&nbsp;·&nbsp;&nbsp;{len(emails)} newsletters&nbsp;&nbsp;·&nbsp;&nbsp;{time_str}
    </div>
  </div>

  <!-- Week overview -->
  <div style="background:#161616;border:1px solid #2a2a2a;border-left:3px solid #e8b84b;border-radius:4px;padding:20px 22px;margin-bottom:24px;">
    <div style="font-size:20px;font-weight:700;color:#e8e4dc;line-height:1.3;margin-bottom:12px;">{digest.get('week_headline','This Week')}</div>
    <p style="font-size:14px;line-height:1.8;color:#a0a09a;margin:0;">{digest.get('week_overview','')}</p>
  </div>

  <!-- Top picks -->
  <div style="margin-bottom:24px;">
    <div style="font-family:monospace;font-size:10px;letter-spacing:2.5px;text-transform:uppercase;color:#e8b84b;padding-bottom:10px;border-bottom:1px solid #222;margin-bottom:16px;">
      ★ Top Picks This Week
    </div>
    {top_picks_html}
  </div>

  <!-- Topics -->
  <div style="margin-bottom:24px;">
    <div style="font-family:monospace;font-size:10px;letter-spacing:2.5px;text-transform:uppercase;color:#444;padding-bottom:10px;border-bottom:1px solid #222;margin-bottom:12px;">
      By Topic
    </div>
    {topics_html}
  </div>

  <!-- Sources -->
  <div style="padding-top:16px;border-top:1px solid #1a1a1a;font-family:monospace;font-size:10px;text-align:center;line-height:1.8;">
    {sources_html}
  </div>

  <!-- Footer -->
  <div style="margin-top:12px;font-family:monospace;font-size:10px;color:#2a2a2a;text-align:center;">
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
    log.info(f"Weekly digest sent to {SEND_TO_EMAIL}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("📚  Weekly Read Later Digest starting…")

    log.info("[1/4] Connecting to Gmail…")
    service = get_gmail_service()

    log.info("[2/4] Fetching this week's newsletters…")
    emails = fetch_week_newsletters(service)
    if not emails:
        log.info("No newsletters found this week. Exiting.")
        return

    log.info(f"[3/4] Building weekly digest with Claude… ({len(emails)} emails)")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    digest = build_weekly_digest(client, emails)
    log.info(f"  Headline: {digest.get('week_headline','')}")
    log.info(f"  Top picks: {len(digest.get('top_picks',[]))}")
    log.info(f"  Topics: {len(digest.get('topics',[]))}")

    log.info("[4/4] Building and sending email…")
    now     = datetime.now(ZoneInfo("America/Chicago"))
    subject = f"📚 Week in Review — {now.strftime('%B %-d, %Y')}"
    html    = build_weekly_html(emails, digest, now)
    send_email(service, subject, html)

    log.info("✅  Done.")


if __name__ == "__main__":
    main()
