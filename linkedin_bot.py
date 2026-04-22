"""
Wolf Jansen LinkedIn Bot
========================
Runs weekly (Thursdays, 09:00 German time via GitHub Actions).

Looks back over the approved + published stories from the past 7 days,
asks Claude to pick the single most LinkedIn-worthy one, rewrites it as
a 150–250 word LinkedIn post in Wolf Jansen company voice, and emails
Dan a preview card with Approve / Reject buttons.

On approval, a dedicated Power Automate flow reads the post text from
OneDrive (pending_linkedin.json) and POSTs it to a Make.com webhook,
which creates the post on the Wolf Jansen LinkedIn company page.

Source pool:    OneDrive/NewsBot/pending_approvals.json  (where used=True)
LinkedIn pool:  OneDrive/NewsBot/pending_linkedin.json   (new file this bot writes)
"""

import anthropic
import base64
import json
import logging
import os
import smtplib
import uuid
import requests
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

# Reuse helpers / config from the existing news bot so we stay DRY.
# (news_bot.py lives next to this file — same repo root.)
from news_bot import (
    CONFIG,
    DIVISION_COLOURS,
    DIVISION_LABELS,
    _get_graph_token,
    _onedrive_url,
    _running_in_cloud,
    load_pending,
)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(_SCRIPT_DIR, "linkedin_bot.log")),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# EXTRA CONFIG (LinkedIn-specific secrets live alongside the main bot's env)
# ---------------------------------------------------------------------------
LINKEDIN_CONFIG = {
    # Power Automate flow that fires the Make.com webhook on approval.
    "pa_linkedin_approve_url": os.getenv("PA_LINKEDIN_APPROVE_URL", ""),
    # Power Automate flow that marks the draft as rejected.
    "pa_linkedin_reject_url":  os.getenv("PA_LINKEDIN_REJECT_URL", ""),

    # How far back to look for candidate stories (days).
    "lookback_days": int(os.getenv("LINKEDIN_LOOKBACK_DAYS", "7")),
}

_GITHUB_PAGES_BASE = "https://danwolfjansen.github.io/wj-news-bot"


# ---------------------------------------------------------------------------
# LINKEDIN POST PROMPT — adapted from REWRITE_SYSTEM_PROMPT, tuned for LI
# ---------------------------------------------------------------------------
LINKEDIN_SYSTEM_PROMPT = """
You are writing a LinkedIn post AS Wolf Jansen — speaking in the first person plural
("we", "our", "in our experience") on behalf of the company page.

## Who we are
Wolf Jansen is a specialist recruitment firm focused on the DACH region
(Germany, Austria, Switzerland). We operate across three divisions: SAP,
Data & Digital, and Financial & Advisory. We have been recruiting in Germany
since 2000. We are true headhunters — we do not rely on job boards. We target
passive candidates who are excelling in their current positions and are typically
hidden from 95% of the market. Every consultant at Wolf Jansen is deeply
experienced in the German market.

## Terminology rules
- Say "DACH region" or "German market" — not just "Germany" when Austria/Switzerland are relevant
- Say "passive candidates" or "passive talent" — this is central to our positioning
- Say "specialist recruitment" — never "staffing" or "temp agency"
- Say "consultants" — not "recruiters" when referring to our team
- Division names exactly: "SAP", "Data & Digital", "Financial & Advisory"

## LinkedIn format rules
- Length: 150–250 words. Longer than a tweet, shorter than a blog post.
- Open with a hook in the first line — a sharp observation, a question, or a
  concrete number. No preamble. No "we recently published a post about…".
- Use short paragraphs — 1 to 3 lines each. LinkedIn readers skim.
- Keep a line break between paragraphs (blank line).
- Write in our voice throughout: confident, direct, with a genuine point of view.
- Say what we think this means for hiring, talent movement, or the DACH market.
  Don't just summarise — add perspective.
- End with a subtle nudge back to the full post. The final paragraph should
  reference "Read the full take on wolfjansen.com" or similar — only if a
  wp_post_url is provided. If no URL is available, end with a pointed closing
  thought instead (no dangling CTA).
- 2–4 relevant hashtags on the last line (lowercase, no spaces), e.g.
  #SAPHiring #DACH #DataEngineering. Never more than 4.

## What not to do
- No emojis in the body. The hashtags line is the only place they could appear,
  and even there we prefer none.
- No engagement bait ("Agree? 👇", "Share your thoughts!", "Thoughts?").
- No AI tells: avoid "pivotal moment", "underscores", "highlights the importance",
  "evolving landscape", "groundbreaking", "exciting times", "pivotal", "stands
  as a testament to", "setting the stage for", "in today's fast-paced world".
- No present-participle sentence-enders: "...highlighting that", "...underscoring how",
  "...reflecting the".
- No year references ("in 2025", "through 2026"). Use relative time
  ("over the next 12–18 months", "in the coming year", "recently").
- No LinkedIn clichés ("excited to share", "thrilled to announce", "humbled",
  "deep dive"). We are the company page, not an individual looking for likes.

## Output format
Return ONLY a JSON object:
{
  "post_text": "The full LinkedIn post body, including line breaks (use \\n\\n between paragraphs) and the hashtags line at the end.",
  "hook":      "The first line of the post, repeated here for the email preview.",
  "word_count": 187
}
"""


# ---------------------------------------------------------------------------
# STEP 1: Candidate pool — published stories from the past N days
# ---------------------------------------------------------------------------
def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        # Accept trailing Z or offset form
        ts = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def candidate_pool() -> list[dict]:
    """All approved+published entries from pending_approvals.json in the lookback window."""
    pending = load_pending()
    if not pending:
        log.info("No pending_approvals.json content returned — pool empty.")
        return []

    # Which source_tokens have already been turned into a LinkedIn post?
    # (Any entry in pending_linkedin.json that's already fired / rejected.)
    linkedin_pending = load_linkedin_pending()
    already_consumed = {
        e.get("source_token")
        for e in linkedin_pending.values()
        if e.get("used") or e.get("rejected")
    }

    cutoff = datetime.now(timezone.utc) - timedelta(days=LINKEDIN_CONFIG["lookback_days"])
    pool = []
    for token, entry in pending.items():
        if not entry.get("used"):
            continue  # not yet published to WordPress
        created = _parse_iso(entry.get("created", ""))
        if not created:
            continue
        if created < cutoff:
            continue
        if token in already_consumed:
            continue  # already turned into a LinkedIn post or rejected
        pool.append({"token": token, **entry})

    log.info(f"Candidate pool: {len(pool)} published post(s) in the last "
             f"{LINKEDIN_CONFIG['lookback_days']} days.")
    return pool


# ---------------------------------------------------------------------------
# STEP 2: Try to find the published wolfjansen.com URL for a draft
# ---------------------------------------------------------------------------
def resolve_wp_url(entry: dict) -> Optional[str]:
    """Look up the live WordPress URL via the WP REST API (search by title)."""
    wp_url  = CONFIG.get("wp_url", "https://wolfjansen.com").rstrip("/")
    wp_user = CONFIG.get("wp_user", "")
    wp_pass = CONFIG.get("wp_password", "")
    title   = entry.get("title", "").strip()
    if not (wp_url and title):
        return None
    try:
        resp = requests.get(
            f"{wp_url}/wp-json/wp/v2/posts",
            params={"search": title[:80], "per_page": 5, "status": "publish"},
            auth=(wp_user, wp_pass) if wp_user and wp_pass else None,
            timeout=10,
        )
        resp.raise_for_status()
        posts = resp.json() or []
        # Best match: exact (rendered) title equality, else first result
        for p in posts:
            rendered = (p.get("title", {}).get("rendered") or "").strip()
            if rendered.lower() == title.lower():
                return p.get("link")
        if posts:
            return posts[0].get("link")
    except Exception as e:
        log.warning(f"WP URL lookup failed for '{title}': {e}")
    return None


# ---------------------------------------------------------------------------
# STEP 3: Pick the single most LinkedIn-worthy story
# ---------------------------------------------------------------------------
def pick_best(pool: list[dict], client: anthropic.Anthropic) -> Optional[dict]:
    if not pool:
        return None
    if len(pool) == 1:
        return pool[0]

    # Build a compact list for Claude to rank.
    numbered = []
    for i, e in enumerate(pool, start=1):
        numbered.append(
            f"{i}. [{DIVISION_LABELS.get(e['division'], e['division'])}] "
            f"{e.get('title','')}\n   {e.get('excerpt','')[:300]}"
        )
    listing = "\n\n".join(numbered)

    prompt = f"""You are picking ONE story from the list below to turn into a Wolf Jansen
LinkedIn company-page post. We want the item with the strongest point of view,
the most relevance to senior DACH hiring/talent, and the best chance of sparking
professional conversation. Avoid items that are purely announcement-shaped,
vendor PR, or too narrow.

List:
{listing}

Reply with ONLY the number of your pick (e.g. "3"). No explanation.
"""
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{"role": "user", "content": prompt}],
        )
        answer = (resp.content[0].text or "").strip()
        # Extract first integer
        digits = "".join(c for c in answer if c.isdigit())
        idx = int(digits) - 1 if digits else 0
        if 0 <= idx < len(pool):
            return pool[idx]
    except Exception as e:
        log.warning(f"Pick failed ({e}) — defaulting to first item.")
    return pool[0]


# ---------------------------------------------------------------------------
# STEP 4: Rewrite as a LinkedIn post
# ---------------------------------------------------------------------------
def rewrite_for_linkedin(entry: dict, wp_post_url: Optional[str],
                         client: anthropic.Anthropic) -> Optional[dict]:
    division_label = DIVISION_LABELS.get(entry["division"], entry["division"])
    user_message = f"""Published Wolf Jansen post to adapt for LinkedIn:

Division:   {division_label}
Title:      {entry.get("title","")}
Excerpt:    {entry.get("excerpt","")}

Full HTML body:
{entry.get("body","")}

wp_post_url: {wp_post_url or "(no URL available — omit the 'read more' line)"}

Rewrite this as a single Wolf Jansen LinkedIn company-page post following
every rule in the system prompt. Return JSON only.
"""
    try:
        resp = client.messages.create(
            model="claude-opus-4-5-20251101",
            max_tokens=1024,
            system=LINKEDIN_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.rsplit("```", 1)[0].strip()
        return json.loads(raw)
    except Exception as e:
        log.error(f"LinkedIn rewrite failed: {e}")
        return None


# ---------------------------------------------------------------------------
# STEP 5: Register the LinkedIn draft in OneDrive/NewsBot/pending_linkedin.json
# ---------------------------------------------------------------------------
def _linkedin_pending_path() -> str:
    folder = CONFIG.get("onedrive_folder", "").strip()
    if folder:
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, "pending_linkedin.json")
    return "pending_linkedin.json"


def load_linkedin_pending() -> dict:
    if _running_in_cloud():
        try:
            token = _get_graph_token()
            resp  = requests.get(
                _onedrive_url("pending_linkedin.json"),
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            log.warning(f"Could not load pending_linkedin from OneDrive: {e}")
        return {}
    path = _linkedin_pending_path()
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_linkedin_pending(data: dict):
    content = json.dumps(data, indent=2)
    if _running_in_cloud():
        try:
            token = _get_graph_token()
            resp  = requests.put(
                _onedrive_url("pending_linkedin.json"),
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                data=content.encode(),
                timeout=15,
            )
            resp.raise_for_status()
            log.info("  LinkedIn draft saved to OneDrive/NewsBot/pending_linkedin.json")
        except Exception as e:
            log.error(f"Failed to save pending_linkedin to OneDrive: {e}")
        return
    path = _linkedin_pending_path()
    with open(path, "w") as f:
        f.write(content)
    log.info(f"  LinkedIn draft saved to: {path}")


def register_linkedin_draft(token: str, source_token: str, entry: dict,
                            post_text: str, wp_post_url: Optional[str]):
    pending = load_linkedin_pending()
    pending[token] = {
        "post_text":      post_text,
        "source_token":   source_token,
        "source_title":   entry.get("title", ""),
        "source_excerpt": entry.get("excerpt", ""),
        "division":       entry["division"],
        "wp_post_url":    wp_post_url or "",
        "used":           False,
        "created":        datetime.now(timezone.utc).isoformat(),
    }
    save_linkedin_pending(pending)


# ---------------------------------------------------------------------------
# STEP 6: Email preview — approve / reject buttons
# ---------------------------------------------------------------------------
def _pages_url(action: str, pa_url: str, token: str) -> str:
    encoded = base64.b64encode(pa_url.encode()).decode()
    return f"{_GITHUB_PAGES_BASE}/{action}.html?url={encoded}&token={token}"


def _linkedin_card_html(token: str, post_text: str, entry: dict,
                         wp_post_url: Optional[str]) -> str:
    approve_url = _pages_url("approve-linkedin", LINKEDIN_CONFIG["pa_linkedin_approve_url"], token)
    reject_url  = _pages_url("reject-linkedin",  LINKEDIN_CONFIG["pa_linkedin_reject_url"],  token)

    colour = DIVISION_COLOURS.get(entry["division"], "#0A66C2")
    label  = DIVISION_LABELS.get(entry["division"], entry["division"])

    # Convert \n\n to <br><br> for HTML preview. Escape basic HTML chars in post.
    safe = (post_text.replace("&", "&amp;")
                     .replace("<", "&lt;")
                     .replace(">", "&gt;"))
    html_preview = safe.replace("\n\n", "<br><br>").replace("\n", "<br>")

    source_line = (
        f'<p style="margin:12px 0 0;font-size:12px;color:#888;">'
        f'Source: <a href="{wp_post_url}" style="color:#888;">{wp_post_url}</a></p>'
    ) if wp_post_url else ""

    return f"""
<table width="100%" cellpadding="0" cellspacing="0"
       style="margin-bottom:28px;border:1px solid #e0e0e0;border-radius:8px;
              border-left:4px solid {colour};background:#fff;">
  <tr><td style="padding:24px 28px;">
    <p style="margin:0 0 6px;font-size:11px;font-weight:700;letter-spacing:0.08em;
              text-transform:uppercase;color:{colour};">LinkedIn · {label}</p>
    <p style="margin:0 0 14px;font-size:13px;color:#666;">
      Based on: <em>{entry.get("title","")}</em>
    </p>
    <hr style="border:none;border-top:1px solid #eee;margin:0 0 16px;">

    <!-- LinkedIn-style preview -->
    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#f3f6f8;border-radius:6px;">
      <tr><td style="padding:18px 20px;">
        <table cellpadding="0" cellspacing="0">
          <tr>
            <td width="40" style="vertical-align:top;">
              <div style="width:40px;height:40px;border-radius:50%;background:#111;
                          color:#fff;text-align:center;line-height:40px;
                          font-weight:700;font-size:15px;">WJ</div>
            </td>
            <td style="padding-left:12px;vertical-align:top;">
              <p style="margin:0;font-size:14px;font-weight:700;color:#111;">Wolf Jansen</p>
              <p style="margin:1px 0 0;font-size:12px;color:#666;">
                Specialist recruitment · DACH · 7,875 followers
              </p>
            </td>
          </tr>
        </table>
        <div style="margin-top:14px;font-size:14px;color:#111;line-height:1.55;
                    white-space:pre-wrap;">{html_preview}</div>
        {source_line}
      </td></tr>
    </table>

    <hr style="border:none;border-top:1px solid #eee;margin:20px 0 20px;">

    <table cellpadding="0" cellspacing="0">
      <tr>
        <td style="padding-right:12px;">
          <a href="{approve_url}"
             style="display:inline-block;padding:11px 28px;background:#0A66C2;
                    color:#fff;font-size:14px;font-weight:700;text-decoration:none;
                    border-radius:5px;letter-spacing:0.02em;">✓ Approve &amp; Post to LinkedIn</a>
        </td>
        <td>
          <a href="{reject_url}"
             style="display:inline-block;padding:11px 24px;background:#f5f5f5;
                    color:#666;font-size:14px;font-weight:600;text-decoration:none;
                    border-radius:5px;border:1px solid #ddd;">✗ Reject</a>
        </td>
      </tr>
    </table>
  </td></tr>
</table>"""


def send_linkedin_approval_email(token: str, post_text: str, entry: dict,
                                  wp_post_url: Optional[str]):
    smtp_user = CONFIG["smtp_user"]
    smtp_pass = CONFIG["smtp_password"]
    if not (smtp_user and smtp_pass):
        log.warning("SMTP credentials not set — skipping LinkedIn email.")
        return
    if not (LINKEDIN_CONFIG["pa_linkedin_approve_url"] and LINKEDIN_CONFIG["pa_linkedin_reject_url"]):
        log.warning("PA LinkedIn URLs not set — skipping LinkedIn email.")
        return

    date_str   = datetime.now(timezone.utc).strftime("%d %B %Y")
    subject    = f"Wolf Jansen LinkedIn draft ready for review — {date_str}"
    recipients = [r.strip() for r in CONFIG["email_to"].split(",") if r.strip()]

    card = _linkedin_card_html(token, post_text, entry, wp_post_url)

    html_body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0f0f0;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f0f0;padding:32px 16px;">
  <tr><td align="center">
    <table width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%;">
      <tr><td style="background:#111;border-radius:8px 8px 0 0;padding:24px 32px;">
        <p style="margin:0;font-size:20px;font-weight:700;color:#fff;">Wolf Jansen</p>
        <p style="margin:4px 0 0;font-size:13px;color:#aaa;">LinkedIn Bot — Weekly · {date_str}</p>
      </td></tr>
      <tr><td style="background:#fff;padding:24px 32px 12px;">
        <p style="margin:0;font-size:15px;color:#333;line-height:1.6;">
          Draft LinkedIn post ready. Preview below — click
          <strong>Approve &amp; Post to LinkedIn</strong> to publish immediately
          to the company page, or <strong>Reject</strong> to discard.
        </p>
      </td></tr>
      <tr><td style="background:#fff;padding:12px 32px 28px;">{card}</td></tr>
      <tr><td style="background:#f8f8f8;border-top:1px solid #e8e8e8;
                     border-radius:0 0 8px 8px;padding:16px 32px;">
        <p style="margin:0;font-size:12px;color:#999;line-height:1.5;">
          Generated automatically by the Wolf Jansen LinkedIn Bot.<br>
          Approval triggers Make.com → LinkedIn Pages. Single-use link.
        </p>
      </td></tr>
    </table>
  </td></tr>
</table></body></html>"""

    plain = (
        f"Wolf Jansen LinkedIn draft\n\n"
        f"Based on: {entry.get('title','')}\n\n"
        f"{post_text}\n\n"
        f"Approve: {_pages_url('approve-linkedin', LINKEDIN_CONFIG['pa_linkedin_approve_url'], token)}\n"
        f"Reject:  {_pages_url('reject-linkedin',  LINKEDIN_CONFIG['pa_linkedin_reject_url'],  token)}\n"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = CONFIG["email_from"]
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(CONFIG["smtp_host"], CONFIG["smtp_port"]) as server:
            server.ehlo(); server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, recipients, msg.as_string())
        log.info(f"✉  LinkedIn approval email sent to {', '.join(recipients)}")
    except Exception as e:
        log.error(f"Failed to send LinkedIn email: {e}")


def send_no_linkedin_email():
    smtp_user = CONFIG["smtp_user"]
    smtp_pass = CONFIG["smtp_password"]
    if not (smtp_user and smtp_pass):
        return
    date_str   = datetime.now(timezone.utc).strftime("%d %B %Y")
    subject    = f"Wolf Jansen LinkedIn Bot — no candidates this week ({date_str})"
    recipients = [r.strip() for r in CONFIG["email_to"].split(",") if r.strip()]
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:32px;background:#f0f0f0;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:480px;margin:0 auto;background:#fff;border-radius:8px;
              padding:32px;text-align:center;">
    <h2 style="color:#1a1a1a;margin:0 0 12px;">No LinkedIn post this week</h2>
    <p style="color:#555;font-size:15px;line-height:1.6;margin:0;">
      No approved + published posts in the last {LINKEDIN_CONFIG["lookback_days"]} days,
      or they've all already been shared on LinkedIn.
    </p>
  </div>
</body></html>"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = CONFIG["email_from"]
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(subject, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    try:
        with smtplib.SMTP(CONFIG["smtp_host"], CONFIG["smtp_port"]) as server:
            server.ehlo(); server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, recipients, msg.as_string())
        log.info(f"✉  No-candidates notification sent to {', '.join(recipients)}")
    except Exception as e:
        log.error(f"Failed to send no-candidates email: {e}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    log.info("=" * 60)
    log.info(f"Wolf Jansen LinkedIn Bot — "
             f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    log.info("=" * 60)

    pool = candidate_pool()
    if not pool:
        send_no_linkedin_email()
        return

    ai_client = anthropic.Anthropic(api_key=CONFIG["anthropic_api_key"])

    pick = pick_best(pool, ai_client)
    if not pick:
        send_no_linkedin_email()
        return
    log.info(f"Picked: [{pick['division']}] {pick.get('title','')[:80]}")

    wp_url = resolve_wp_url(pick)
    if wp_url:
        log.info(f"WP URL: {wp_url}")
    else:
        log.info("WP URL not found — post will end without a 'read more' line.")

    rewritten = rewrite_for_linkedin(pick, wp_url, ai_client)
    if not rewritten or not rewritten.get("post_text"):
        log.error("Rewrite produced no usable post.")
        send_no_linkedin_email()
        return

    token = str(uuid.uuid4())
    register_linkedin_draft(
        token        = token,
        source_token = pick["token"],
        entry        = pick,
        post_text    = rewritten["post_text"],
        wp_post_url  = wp_url,
    )
    send_linkedin_approval_email(token, rewritten["post_text"], pick, wp_url)

    log.info("=" * 60)


if __name__ == "__main__":
    main()
