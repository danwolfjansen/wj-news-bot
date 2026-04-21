"""
Wolf Jansen News Bot
====================
Fetches latest stories from reputable RSS feeds across SAP, Data & Digital,
and Financial & Advisory. Rewrites each story in Wolf Jansen's voice using
the Claude API, then emails you the full draft for review.

Clicking Approve in the email triggers a Power Automate flow that posts the
draft to WordPress. Nothing reaches WordPress until you approve it.

Run manually:   python3 news_bot.py
Schedule:       Add to cron / Mac launchd (see README)
"""

import feedparser
import anthropic
import base64
import json
import os
import hashlib
import logging
import smtplib
import uuid
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone
from typing import Optional
from dotenv import load_dotenv

# Resolve paths relative to this script's location, not the working directory.
# This ensures launchd (which may not set WorkingDirectory) can always find
# the .env file and write the log to the correct OneDrive folder.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

load_dotenv(os.path.join(_SCRIPT_DIR, ".env"))

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(_SCRIPT_DIR, "news_bot.log")),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
CONFIG = {
    # Anthropic
    "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),

    # Email
    "smtp_host":     os.getenv("SMTP_HOST", "smtp.gmail.com"),
    "smtp_port":     int(os.getenv("SMTP_PORT", "587")),
    "smtp_user":     os.getenv("SMTP_USER", ""),
    "smtp_password": os.getenv("SMTP_PASSWORD", ""),
    "email_from":    os.getenv("EMAIL_FROM", "Wolf Jansen News Bot <noreply@wolfjansen.com>"),
    "email_to":      os.getenv("EMAIL_TO", "dan@wolfjansen.com"),  # comma-separated for multiple

    # Power Automate flow URLs
    "pa_approve_url": os.getenv("PA_APPROVE_URL", ""),
    "pa_reject_url":  os.getenv("PA_REJECT_URL", ""),

    # WordPress (used by Power Automate, stored here for reference in pending JSON)
    "wp_url":      os.getenv("WP_URL", "https://wolfjansen.com"),
    "wp_user":     os.getenv("WP_USER", ""),
    "wp_password": os.getenv("WP_APP_PASSWORD", ""),

    # OneDrive folder path (local synced path — Mac only)
    "onedrive_folder": os.getenv("ONEDRIVE_FOLDER", ""),

    # Microsoft Graph API — used when running in GitHub Actions
    "ms_tenant_id":     os.getenv("MS_TENANT_ID", ""),
    "ms_client_id":     os.getenv("MS_CLIENT_ID", ""),
    "ms_client_secret": os.getenv("MS_CLIENT_SECRET", ""),
    "ms_user_email":    os.getenv("MS_USER_EMAIL", ""),  # e.g. dan@wolfjansen.com

    # How many stories per division per run
    "max_stories_per_division": 3,

    "seen_stories_file": "seen_stories.json",
}


def _pending_file_path() -> str:
    folder = CONFIG["onedrive_folder"].strip()
    if folder:
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, "pending_approvals.json")
    return "pending_approvals.json"


# ---------------------------------------------------------------------------
# RSS FEED SOURCES
# ---------------------------------------------------------------------------
FEEDS = {
    "sap": {
        "wp_category_slug": "sap",
        "wp_category_name": "SAP",
        "sources": [
            {"name": "SAP News Centre",         "url": "https://news.sap.com/feed/"},
            {"name": "SAP Community Blog",      "url": "https://community.sap.com/feed/"},
            {"name": "The Register – SAP",      "url": "https://www.theregister.com/software/sap/headlines.atom"},
            {"name": "Diginomica – SAP",        "url": "https://diginomica.com/tag/sap/feed"},
            {"name": "ERP Today",               "url": "https://erp.today/feed/"},
        ]
    },
    "data-digital": {
        "wp_category_slug": "data-digital",
        "wp_category_name": "Data & Digital",
        "sources": [
            {"name": "Datanami",                    "url": "https://www.datanami.com/feed/"},
            {"name": "Diginomica",                  "url": "https://diginomica.com/feed"},
            {"name": "Information Age",             "url": "https://www.information-age.com/feed/"},
            {"name": "VentureBeat AI",              "url": "https://venturebeat.com/category/ai/feed/"},
            {"name": "The Batch (DeepLearning.AI)", "url": "https://www.deeplearning.ai/the-batch/feed/"},
        ]
    },
    "financial-advisory": {
        "wp_category_slug": "financial-advisory",
        "wp_category_name": "Financial & Advisory",
        "sources": [
            {"name": "CFO Dive",                "url": "https://www.cfodive.com/feeds/news/"},
            {"name": "Accountancy Age",         "url": "https://www.accountancyage.com/feed/"},
            {"name": "ACCA Global",             "url": "https://www.accaglobal.com/content/dam/acca/global/XML/acca-rss.xml"},
            {"name": "FT – Financial Services", "url": "https://www.ft.com/financialservices?format=rss"},
            {"name": "CFO Magazine",            "url": "https://www.cfo.com/rss/"},
        ]
    }
}

# ---------------------------------------------------------------------------
# REWRITE PROMPT
# ---------------------------------------------------------------------------
REWRITE_SYSTEM_PROMPT = """
You are writing content AS Wolf Jansen — speaking in the first person plural
("we", "our", "in our experience") on behalf of the company.

## Who we are
Wolf Jansen is a specialist recruitment firm focused on the DACH region
(Germany, Austria, Switzerland). We operate across three divisions: SAP,
Data & Digital, and Financial & Advisory. We have been recruiting in Germany
since 2000. We are true headhunters — we do not advertise permanent roles or
rely on job boards. We target passive candidates who are excelling in their
current positions and are typically hidden from 95% of the market. Every
consultant at Wolf Jansen is deeply experienced in the German market.

## Terminology rules
- Say "DACH region" or "German market" — not just "Germany" when Austria/Switzerland are relevant
- Say "passive candidates" or "passive talent" — this is central to our positioning
- Say "specialist recruitment" — never "staffing" or "temp agency"
- Say "consultants" — not "recruiters" when referring to our team
- Division names exactly: "SAP", "Data & Digital", "Financial & Advisory"

## Voice and tone
- Always write as "we" — Wolf Jansen is speaking, not a third party writing about us
- Confident and direct — we have deep expertise and a genuine point of view
- Concise and scannable — our audience are senior professionals and decision-makers
- Add real perspective — don't summarise the story, say what we think it means
  for talent, hiring trends, or the DACH market
- Professional but not stuffy — authoritative without being dry
- Never use recruitment clichés ("rockstar", "ninja", "dynamic team player")
- Never reference specific years (e.g. "in 2025", "through 2026") — use relative
  time references instead ("over the next 12–18 months", "in the coming year", "recently")

## Example of the right tone
"We've seen this pattern before in our SAP practice — when a major vendor shifts
strategy, the talent market follows within 12–18 months. Here's what we're watching."

## NEVER use these AI writing patterns
These phrases make content sound machine-generated. Avoid all of them:
- Significance puffery: "pivotal moment", "key turning point", "stands as a testament to",
  "is a reminder that", "underscores the importance of", "highlights its significance",
  "reflects broader", "marks a shift", "evolving landscape", "indelible mark",
  "deeply rooted", "setting the stage for", "focal point"
- Tacked-on present participles: "...highlighting that", "...underscoring how",
  "...reflecting the", "...symbolizing its", "...contributing to the",
  "...ensuring that", "...fostering a", "...encompassing"
- Promotional fluff: "boasts a", "vibrant", "groundbreaking", "renowned",
  "showcasing", "exemplifies", "valuable insights", "align with", "resonate with",
  "commitment to excellence", "nestled", "in the heart of"
- Vague attribution: "industry reports suggest", "experts argue", "observers note",
  "some critics say", "it has been described as", "is widely regarded as"
- Formulaic structure: Do NOT end with a "Challenges" section or "Future Outlook"
  paragraph. Do NOT write a conclusion that starts "Despite its challenges..."

## Headline rules
The title must be punchy and original. Use a wide variety of structures — rotate
through these ten approaches and never use the same structure twice in one batch:

1. Direct market observation: "SAP is quietly reshaping how finance teams hire"
2. Tension or contradiction: "More AI tools, fewer AI hires — the gap is widening"
3. A question a senior professional would actually ask: "Is the CFO role becoming a tech role?"
4. First-person trend report: "We're seeing a surge in SAP demand — here's why"
5. A bold specific claim: "The data skills gap in DACH is three years ahead of where most firms think"
6. The unexpected angle: "Nobody's talking about what this means for mid-level SAP managers"
7. A hiring signal framed as news: "When HSBC moves like this, DACH banks follow within 18 months"
8. The candidate's perspective: "Senior finance professionals are being asked to do something new"
9. A market verdict: "The case for generalist CFOs just got weaker"
10. A pattern we've spotted: "Three mandates this month alone — the Financial Advisory market is moving"

"What this means for X" is permitted as structure 6 only if none of the others fit —
it must never be the default. Never repeat a headline structure used elsewhere in the same batch.

## Output format
Return ONLY a JSON object with these fields:
{
  "title": "A punchy, original headline — see headline rules above",
  "excerpt": "2–3 sentences in first person, teasing our take on the story",
  "body": "The full post in HTML format. Use <p>, <h2>, <strong> tags as appropriate.
           Aim for 250–400 words. Write throughout as Wolf Jansen speaking — use 'we',
           'our', 'in our view', 'what we're seeing'. End with a subtle source credit
           in small italic text: <p><em>Prompted by reporting from SOURCE_NAME.</em></p>",
  "tags": ["tag1", "tag2", "tag3"]
}
"""

# ---------------------------------------------------------------------------
# DIVISION METADATA
# ---------------------------------------------------------------------------
DIVISION_CATEGORY_IDS = {
    "sap":                29,
    "data-digital":       30,
    "financial-advisory": 31,
}

LATEST_NEWS_CATEGORY_ID = 26  # All posts also go to Latest News

DIVISION_COLOURS = {
    "sap":                "#1a6b3c",
    "data-digital":       "#1a3f6b",
    "financial-advisory": "#6b1a1a",
}
DIVISION_LABELS = {
    "sap":                "SAP",
    "data-digital":       "Data & Digital",
    "financial-advisory": "Financial & Advisory",
}

# ---------------------------------------------------------------------------
# HELPERS: seen stories
# ---------------------------------------------------------------------------
def load_seen_stories() -> set:
    path = CONFIG["seen_stories_file"]
    if os.path.exists(path):
        with open(path, "r") as f:
            return set(json.load(f))
    return set()


def save_seen_stories(seen: set):
    with open(CONFIG["seen_stories_file"], "w") as f:
        json.dump(list(seen), f)


def story_id(entry) -> str:
    key = entry.get("link") or entry.get("id") or entry.get("title", "")
    return hashlib.md5(key.encode()).hexdigest()


# ---------------------------------------------------------------------------
# HELPERS: pending drafts (stored in OneDrive)
# ---------------------------------------------------------------------------
def _running_in_cloud() -> bool:
    """True when executing inside GitHub Actions."""
    return os.getenv("GITHUB_ACTIONS") == "true"


def _get_graph_token() -> str:
    """Obtain a Microsoft Graph API access token via client credentials."""
    url = (f"https://login.microsoftonline.com/"
           f"{CONFIG['ms_tenant_id']}/oauth2/v2.0/token")
    resp = requests.post(url, data={
        "grant_type":    "client_credentials",
        "client_id":     CONFIG["ms_client_id"],
        "client_secret": CONFIG["ms_client_secret"],
        "scope":         "https://graph.microsoft.com/.default",
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()["access_token"]


def _onedrive_url(filename: str) -> str:
    email = CONFIG["ms_user_email"]
    return (f"https://graph.microsoft.com/v1.0/users/{email}"
            f"/drive/root:/NewsBot/{filename}:/content")


def load_pending() -> dict:
    if _running_in_cloud():
        try:
            token = _get_graph_token()
            resp  = requests.get(_onedrive_url("pending_approvals.json"),
                                 headers={"Authorization": f"Bearer {token}"}, timeout=15)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            log.warning(f"Could not load pending from OneDrive: {e}")
        return {}
    path = _pending_file_path()
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}


def save_pending(data: dict):
    content = json.dumps(data, indent=2)
    if _running_in_cloud():
        try:
            token = _get_graph_token()
            resp  = requests.put(
                _onedrive_url("pending_approvals.json"),
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                data=content.encode(),
                timeout=15,
            )
            resp.raise_for_status()
            log.info("  Drafts saved to: OneDrive/NewsBot/pending_approvals.json (Graph API)")
        except Exception as e:
            log.error(f"Failed to save pending to OneDrive: {e}")
        return
    path = _pending_file_path()
    with open(path, "w") as f:
        f.write(content)
    log.info(f"  Drafts saved to: {path}")


def register_draft(token: str, title: str, excerpt: str, body: str,
                   tags: list, division: str):
    """Save full draft content to OneDrive so Power Automate can post it on approval."""
    pending = load_pending()
    pending[token] = {
        "title":        title,
        "excerpt":      excerpt,
        "body":         body,
        "tags":         tags,
        "division":     division,
        "category_slug": FEEDS[division]["wp_category_slug"],
        "category_name": FEEDS[division]["wp_category_name"],
        "category_id":  DIVISION_CATEGORY_IDS[division],
        "category_ids": [DIVISION_CATEGORY_IDS[division], LATEST_NEWS_CATEGORY_ID],
        "used":         False,
        "created":      datetime.now(timezone.utc).isoformat(),
    }
    save_pending(pending)


# ---------------------------------------------------------------------------
# STEP 1: Fetch stories
# ---------------------------------------------------------------------------
def fetch_stories(division_key: str, seen: set) -> list:
    stories = []
    for source in FEEDS[division_key]["sources"]:
        try:
            log.info(f"  Fetching: {source['name']}")
            feed = feedparser.parse(source["url"])
            for entry in feed.entries:
                sid = story_id(entry)
                if sid in seen:
                    continue
                title   = entry.get("title", "").strip()
                link    = entry.get("link", "")
                summary = entry.get("summary", entry.get("description", ""))[:1500]
                if not title or not link:
                    continue
                stories.append({
                    "id":       sid,
                    "source":   source["name"],
                    "title":    title,
                    "link":     link,
                    "summary":  summary,
                    "division": division_key,
                })
                if len(stories) >= CONFIG["max_stories_per_division"]:
                    return stories
        except Exception as e:
            log.warning(f"  Failed to fetch {source['name']}: {e}")
    return stories


# ---------------------------------------------------------------------------
# STEP 2a: Relevance check
# ---------------------------------------------------------------------------
DIVISION_RELEVANCE_CONTEXT = {
    "sap": (
        "SAP software, S/4HANA, RISE with SAP, SAP BTP, SAP implementation, "
        "SAP consulting, ERP, enterprise software in the DACH region"
    ),
    "data-digital": (
        "enterprise data engineering, data analytics, business intelligence, "
        "digital transformation, cloud data platforms, AI/ML in business and "
        "enterprise contexts, data strategy in the DACH region"
    ),
    "financial-advisory": (
        "corporate finance, CFO leadership, accounting, financial management, "
        "audit, financial regulation, DACH business economics, finance careers"
    ),
}

def is_story_relevant(story: dict, division_key: str, client: anthropic.Anthropic) -> bool:
    """Quick yes/no relevance check before spending credits on a full rewrite."""
    context = DIVISION_RELEVANCE_CONTEXT[division_key]
    prompt = (
        f"Is this news story genuinely relevant to a specialist recruitment firm in the "
        f"DACH region (Germany, Austria, Switzerland) that focuses on: {context}?\n\n"
        f"Title: {story['title']}\n"
        f"Summary: {story['summary'][:400]}\n\n"
        f"Answer with ONLY 'yes' or 'no'. Reject stories about consumer lifestyle, health, "
        f"sports, or topics with no clear connection to enterprise technology, business "
        f"leadership, or the DACH talent market."
    )
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{"role": "user", "content": prompt}]
        )
        answer = response.content[0].text.strip().lower()
        return answer.startswith("yes")
    except Exception as e:
        log.warning(f"  Relevance check failed ({e}) — including story by default")
        return True


# ---------------------------------------------------------------------------
# STEP 2b: Rewrite with Claude
# ---------------------------------------------------------------------------
def rewrite_story(story: dict, client: anthropic.Anthropic) -> Optional[dict]:
    user_message = f"""
Original headline: {story['title']}
Source: {story['source']}
Source URL: {story['link']}
Division: {story['division'].replace('-', ' ').title()}

Summary / extract:
{story['summary']}

Please rewrite this as an original Wolf Jansen commentary post. Remember to end
the body with a link back to the original source at {story['link']}.
"""
    try:
        response = client.messages.create(
            model="claude-opus-4-5-20251101",
            max_tokens=1024,
            system=REWRITE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}]
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.rsplit("```", 1)[0].strip()
        return json.loads(raw)
    except Exception as e:
        log.error(f"  Rewrite failed for '{story['title']}': {e}")
        return None


# ---------------------------------------------------------------------------
# STEP 3: Build & send approval email
# ---------------------------------------------------------------------------
_GITHUB_PAGES_BASE = "https://danwolfjansen.github.io/wj-news-bot"


def _pages_url(action: str, pa_url: str, token: str) -> str:
    """Build a GitHub Pages intermediary URL that avoids mobile app interception."""
    encoded = base64.b64encode(pa_url.encode()).decode()
    return f"{_GITHUB_PAGES_BASE}/{action}.html?url={encoded}&token={token}"


def _post_card_html(token: str, title: str, excerpt: str, body: str, division: str) -> str:
    approve_url = _pages_url("approve", CONFIG["pa_approve_url"], token)
    reject_url  = _pages_url("reject",  CONFIG["pa_reject_url"],  token)

    colour = DIVISION_COLOURS.get(division, "#333")
    label  = DIVISION_LABELS.get(division, division.replace("-", " ").title())

    # Strip outer <p> tags from body for inline display, keep inner HTML
    preview_body = body.replace('\n', ' ').strip()

    return f"""
<table width="100%" cellpadding="0" cellspacing="0"
       style="margin-bottom:28px; border:1px solid #e0e0e0; border-radius:8px;
              border-left:4px solid {colour}; background:#fff;">
  <tr>
    <td style="padding:24px 28px;">

      <!-- Division label -->
      <p style="margin:0 0 6px; font-size:11px; font-weight:700; letter-spacing:0.08em;
                text-transform:uppercase; color:{colour};">{label}</p>

      <!-- Title -->
      <h2 style="margin:0 0 12px; font-size:19px; font-weight:700;
                 color:#111; line-height:1.3;">{title}</h2>

      <!-- Excerpt -->
      <p style="margin:0 0 16px; font-size:14px; color:#444;
                line-height:1.65; font-style:italic;">{excerpt}</p>

      <!-- Divider -->
      <hr style="border:none; border-top:1px solid #eee; margin:0 0 16px;">

      <!-- Full post body -->
      <div style="font-size:14px; color:#333; line-height:1.7;">
        {preview_body}
      </div>

      <!-- Divider -->
      <hr style="border:none; border-top:1px solid #eee; margin:20px 0 20px;">

      <!-- Approve / Reject buttons -->
      <table cellpadding="0" cellspacing="0">
        <tr>
          <td style="padding-right:12px;">
            <a href="{approve_url}"
               style="display:inline-block; padding:11px 28px; background:#1a6b3c;
                      color:#fff; font-size:14px; font-weight:700; text-decoration:none;
                      border-radius:5px; letter-spacing:0.02em;">✓ Approve &amp; Publish</a>
          </td>
          <td>
            <a href="{reject_url}"
               style="display:inline-block; padding:11px 24px; background:#f5f5f5;
                      color:#666; font-size:14px; font-weight:600; text-decoration:none;
                      border-radius:5px; border:1px solid #ddd;">✗ Reject</a>
          </td>
        </tr>
      </table>

    </td>
  </tr>
</table>"""


def send_no_stories_email():
    smtp_user = CONFIG["smtp_user"]
    smtp_pass = CONFIG["smtp_password"]
    if not smtp_user or not smtp_pass:
        return

    date_str   = datetime.now(timezone.utc).strftime("%d %B %Y")
    subject    = f"Wolf Jansen News Bot — no new stories today ({date_str})"
    recipients = [r.strip() for r in CONFIG["email_to"].split(",") if r.strip()]

    html_body = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:32px;background:#f0f0f0;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:480px;margin:0 auto;background:#fff;border-radius:8px;
              padding:32px;text-align:center;">
    <img src="https://wolfjansen.com/wp-content/uploads/2024/03/WJ-Logo.png"
         alt="Wolf Jansen" width="140" style="margin-bottom:24px;">
    <h2 style="color:#1a1a1a;margin:0 0 12px;">No new stories today</h2>
    <p style="color:#555;font-size:15px;line-height:1.6;margin:0;">
      The bot ran at 08:01 and checked all feeds — nothing new to review for {date_str}.
    </p>
  </div>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = CONFIG["email_from"]
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(subject, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(CONFIG["smtp_host"], CONFIG["smtp_port"]) as server:
            server.ehlo()
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, recipients, msg.as_string())
        log.info(f"✉  No-stories notification sent to {', '.join(recipients)}")
    except Exception as e:
        log.error(f"Failed to send no-stories email: {e}")


def send_approval_email(new_drafts: list[dict]):
    if not new_drafts:
        return

    smtp_user = CONFIG["smtp_user"]
    smtp_pass = CONFIG["smtp_password"]

    if not smtp_user or not smtp_pass:
        log.warning("SMTP credentials not set — skipping email.")
        return
    if not CONFIG["pa_approve_url"] or not CONFIG["pa_reject_url"]:
        log.warning("Power Automate URLs not set — skipping email.")
        return

    count    = len(new_drafts)
    date_str = datetime.now(timezone.utc).strftime("%d %B %Y")
    subject  = f"Wolf Jansen News: {count} draft{'s' if count != 1 else ''} ready for review — {date_str}"

    cards_html = "\n".join(
        _post_card_html(d["token"], d["title"], d["excerpt"], d["body"], d["division"])
        for d in new_drafts
    )

    html_body = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0f0f0;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f0f0;padding:32px 16px;">
    <tr><td align="center">
      <table width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%;">

        <!-- Header -->
        <tr><td style="background:#111;border-radius:8px 8px 0 0;padding:24px 32px;">
          <p style="margin:0;font-size:20px;font-weight:700;color:#fff;">Wolf Jansen</p>
          <p style="margin:4px 0 0;font-size:13px;color:#aaa;">News Bot — Daily Digest · {date_str}</p>
        </td></tr>

        <!-- Intro -->
        <tr><td style="background:#fff;padding:24px 32px 12px;">
          <p style="margin:0;font-size:15px;color:#333;line-height:1.6;">
            <strong>{count} new draft{'s' if count != 1 else ''}</strong> ready for your review.
            Read each post below and click <strong>Approve &amp; Publish</strong> to post it live,
            or <strong>Reject</strong> to discard it.
          </p>
        </td></tr>

        <!-- Cards -->
        <tr><td style="background:#fff;padding:12px 32px 28px;">
          {cards_html}
        </td></tr>

        <!-- Footer -->
        <tr><td style="background:#f8f8f8;border-top:1px solid #e8e8e8;
                       border-radius:0 0 8px 8px;padding:16px 32px;">
          <p style="margin:0;font-size:12px;color:#999;line-height:1.5;">
            Generated automatically by the Wolf Jansen News Bot.<br>
            Each approval link is single-use and expires once actioned.
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body></html>"""

    # Plain text fallback
    plain_lines = [f"Wolf Jansen News Bot — {count} draft(s) for review\n"]
    for d in new_drafts:
        plain_lines += [
            f"[{DIVISION_LABELS.get(d['division'], d['division'])}]",
            f"{d['title']}",
            f"Approve: {_pages_url('approve', CONFIG['pa_approve_url'], d['token'])}",
            f"Reject:  {_pages_url('reject',  CONFIG['pa_reject_url'],  d['token'])}\n",
        ]

    recipients = [r.strip() for r in CONFIG["email_to"].split(",") if r.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = CONFIG["email_from"]
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText("\n".join(plain_lines), "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(CONFIG["smtp_host"], CONFIG["smtp_port"]) as server:
            server.ehlo()
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, recipients, msg.as_string())
        log.info(f"✉  Approval email sent to {', '.join(recipients)}")
    except Exception as e:
        log.error(f"Failed to send email: {e}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def wait_for_network(host="smtp.gmail.com", port=587, timeout=60):
    """Wait up to `timeout` seconds for network/DNS to be ready after wake."""
    import socket, time
    for attempt in range(timeout):
        try:
            socket.setdefaulttimeout(3)
            socket.getaddrinfo(host, port)
            if attempt > 0:
                log.info(f"Network ready after {attempt}s.")
            return True
        except socket.gaierror:
            if attempt == 0:
                log.info("Waiting for network...")
            time.sleep(1)
    log.error(f"Network not available after {timeout}s — aborting.")
    return False


def main():
    log.info("=" * 60)
    log.info(f"Wolf Jansen News Bot — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    log.info("=" * 60)

    if not wait_for_network():
        return

    seen = load_seen_stories()
    log.info(f"Already processed: {len(seen)} stories")

    ai_client  = anthropic.Anthropic(api_key=CONFIG["anthropic_api_key"])
    new_drafts = []

    for division_key in FEEDS:
        log.info(f"\n--- Division: {division_key.upper()} ---")
        stories = fetch_stories(division_key, seen)
        log.info(f"  New stories found: {len(stories)}")

        for story in stories:
            log.info(f"  Processing: {story['title'][:70]}...")
            if not is_story_relevant(story, division_key, ai_client):
                log.info(f"  ✗ Skipped (not relevant to {division_key})")
                seen.add(story["id"])
                continue
            rewritten = rewrite_story(story, ai_client)
            seen.add(story["id"])

            if not rewritten:
                log.warning("  Skipping — rewrite failed")
                continue

            token = str(uuid.uuid4())
            register_draft(
                token    = token,
                title    = rewritten["title"],
                excerpt  = rewritten["excerpt"],
                body     = rewritten["body"],
                tags     = rewritten.get("tags", []),
                division = division_key,
            )
            new_drafts.append({
                "token":    token,
                "title":    rewritten["title"],
                "excerpt":  rewritten["excerpt"],
                "body":     rewritten["body"],
                "division": division_key,
            })
            log.info(f"  ✓ Draft saved: '{rewritten['title']}'")

    save_seen_stories(seen)

    log.info(f"\n{len(new_drafts)} draft(s) ready.")

    if new_drafts:
        send_approval_email(new_drafts)
    else:
        log.info("No new stories found — sending notification.")
        send_no_stories_email()

    log.info("=" * 60)


if __name__ == "__main__":
    main()
