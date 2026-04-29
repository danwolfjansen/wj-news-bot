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
import json
import os
import hashlib
import logging
import smtplib
import uuid
import fcntl
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("news_bot.log"),
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

    # OneDrive folder path (local synced path)
    "onedrive_folder": os.getenv("ONEDRIVE_FOLDER", ""),

    # How many stories per division per run
    "max_stories_per_division": 3,

    # Relative path — intentional. GitHub Actions checks out the repo and then
    # commits seen_stories.json back via `git add seen_stories.json`.  This only
    # works if the file sits in the repo working directory.  The launchd plist
    # has been disabled (see README), so there is no longer a second runner that
    # would create a divergent local copy of this file.
    "seen_stories_file": "seen_stories.json",
}


def _pending_file_path() -> str:
    """Local working copy — never inside OneDrive, so no sync-lock issues."""
    local_dir = os.path.expanduser("~/.newsbot")
    os.makedirs(local_dir, exist_ok=True)
    return os.path.join(local_dir, "pending_approvals.json")


def _pending_onedrive_path() -> str:
    """OneDrive path used only for pushing updates so Power Automate can read them."""
    folder = CONFIG["onedrive_folder"].strip()
    if folder:
        return os.path.join(folder, "pending_approvals.json")
    return ""


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
since 2000. We are true headhunters. We target passive candidates who are
excelling in their current positions and are typically hidden from 95% of the
market. Consultants at Wolf Jansen bring a wide range of tenure and backgrounds,
and are focused on the German market.

## Terminology rules
- Say "DACH region" or "German market", not just "Germany" when Austria/Switzerland are relevant
- Say "passive candidates" or "passive talent". This is central to our positioning
- Say "specialist recruitment". Never "staffing" or "temp agency"
- Say "consultants", not "recruiters" when referring to our team
- Division names exactly: "SAP", "Data & Digital", "Financial & Advisory"

## Voice and tone
- Always write as "we". Wolf Jansen is speaking, not a third party writing about us
- Confident and direct. We have deep expertise and a genuine point of view
- Concise and scannable. Our audience are senior professionals and decision-makers
- Add real perspective. Don't summarise the story, say what we think it means
  for talent, hiring trends, or the DACH market
- Professional but not stuffy. Authoritative without being dry
- Never use recruitment clichés ("rockstar", "ninja", "dynamic team player")
- Never reference specific years (e.g. "in 2025", "through 2026"). Use relative
  time references instead ("over the next 12-18 months", "in the coming year", "recently")

## Example of the right tone
"We've seen this pattern before in our SAP practice. When a major vendor shifts
strategy, the talent market follows within 12-18 months. Experience with the
previous transition tends to become the most valuable thing on a CV."

## NEVER use these AI writing patterns
These phrases make content sound machine-generated. Avoid all of them.

### Banned punctuation
- NO EM DASHES (—) anywhere in the title, excerpt, or body. Not one. This is
  the single biggest AI tell. Use commas, full stops, colons, or rephrase.
- No en dashes (–) in prose. En dashes are only acceptable inside number ranges
  like "12-18 months". Never as a sentence break.

### Banned rhetorical patterns (structural tells)
- The contrastive "not X, it's Y" / "this isn't X, it's Y" / "not X — it's Y"
  construction in any variation. Examples to avoid:
    * "That's not a criticism, it's a gap..."
    * "This isn't about X, it's about Y..."
    * "It's not just X, it's Y..."
  State the point directly. Don't do the rhetorical reversal.
- "Playing out" / "unfolding" / "in real time" meta-narration:
    * "We're seeing this play out in real time"
    * "Watching this shift unfold"
  If something is happening, just describe what is happening.
- "Here's the [question/thing/reality/kicker/problem]..." rhetorical setup:
    * "Here's the question we're asking clients:"
    * "Here's what we're seeing:"
    * "Here's the reality:"
  No throat-clearing. State the point.
- "Worth noting / worth paying attention to / worth heeding" sign-offs.
- "The signal is..." / "The signal here is..." overused framing.
- Generic "boards are asking different questions" without naming the questions.

### Banned phrases
- Significance puffery: "pivotal moment", "key turning point", "stands as a testament to",
  "is a reminder that", "underscores the importance of", "highlights its significance",
  "reflects broader", "marks a shift", "evolving landscape", "indelible mark",
  "deeply rooted", "setting the stage for", "focal point"
- Tacked-on present participles: "...highlighting that", "...underscoring how",
  "...reflecting the", "...symbolizing its", "...contributing to the",
  "...ensuring that", "...fostering a", "...encompassing", "...signalling that"
- Promotional fluff: "boasts a", "vibrant", "groundbreaking", "renowned",
  "showcasing", "exemplifies", "valuable insights", "align with", "resonate with",
  "commitment to excellence", "nestled", "in the heart of"
- Vague attribution: "industry reports suggest", "experts argue", "observers note",
  "some critics say", "it has been described as", "is widely regarded as"
- Corporate filler: "leverage", "ecosystem", "landscape" (as metaphor),
  "navigate" (as metaphor), "increasingly", "in the broader context of",
  "deep dive", "double down", "moving the needle"
- Formulaic openers and subheadings: NEVER use "What we're seeing", "What we are seeing",
  "What this means", "What X tells us", "What X tells us about Y", "Our perspective",
  "Our advice", "Implications for", "What we're watching", "Why this matters"
  as subheadings or section labels. These are the most overused patterns in AI content.
  If a subheading is needed, make it specific to this story — not a generic label.
- Formulaic closers: Do NOT end with "the time to think about X is now", "We expect this
  trend to accelerate", "We expect this pattern to hold", "the question is whether to X or Y",
  "the organisations that move early", or any platitude. End on a specific observation.
- Formulaic structure: Do NOT end with a "Challenges" section or "Future Outlook"
  paragraph. Do NOT write a conclusion that starts "Despite its challenges..."
- Repeated phrases across posts: Never use the same subheading, sentence opener, or
  closing thought more than once across all posts in a single batch.

## Post structure rules
Every post must have a DIFFERENT internal structure. Do not use the same sequence of
sections across posts in the same batch. Some structures that work:

- Open with a sharp observation, then go straight into implications with no subheadings at all
- Use a single subheading that is specific to this story (not a generic label)
- Open with a candidate's-eye view, then flip to the hiring manager's perspective
- Lead with the counterintuitive angle, then explain the evidence
- Tell it as a short narrative — what happened, why it matters, one concrete implication

The goal is that someone reading four posts in a row should feel they are reading
four different writers, not one template.

## Voice test before returning
Read the draft back as if a specialist recruiter were saying it in a meeting.
If any sentence sounds like a thought-leader blog caption or a LinkedIn guru
post, rewrite it. Plain, direct, with a concrete point.

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

BANNED headline patterns — never use regardless of structure number:
- "What X means for Y" in any form
- "What X tells us about Y" in any form
- "X: What Y means for Z" (colon + what it means)
- Any headline containing the word "momentum" or "continues"
Never repeat a headline structure used elsewhere in the same batch.

## Final check before returning
This is mandatory. Before producing the JSON output, scan every <h2> tag in the body.
If any <h2> matches or starts with any of the following, you MUST rename it to something
specific to this story before continuing:

BANNED <h2> openings (any variation, any capitalisation):
- "What this means" / "What that means"
- "What we're seeing" / "What we are seeing" / "What we're watching"
- "What we're telling" / "What we told"
- "Why this matters" / "Why it matters"
- "Our advice" / "Our perspective" / "Our view" / "Our take"
- "Implications for" / "Impact on" / "The impact"
- "The talent angle" / "The hiring angle" / "The hiring implications"
- "Timing considerations" / "Looking ahead" / "The broader pattern"

If you find any of these, stop and rename the subheading to something concrete and
story-specific (e.g. instead of "What this means for talent" use "Why procurement
managers are re-skilling now" or just remove the subheading and fold the content
into a paragraph).

Then also check:
- Em-dash "—" anywhere. Replace with a comma, colon, or rephrase.
- En-dash "–" outside number ranges. Remove.
- Contrastive "not X, it's Y" constructions. Rewrite.
- Phrases: "play out", "unfold", "in real time", "worth noting", "the signal",
  "Here's the", "Here's what", "12 to 18 months" appearing more than once across
  the batch. Rewrite.
- Headline containing "What X means", "What X tells us", "momentum", "continues". Rewrite.

Do not output the JSON until all checks pass.

## Output format
Return ONLY a JSON object with these fields:
{
  "title": "A punchy, original headline written from our perspective (not copied from the source)",
  "excerpt": "2–3 sentences in first person, teasing our take on the story",
  "body": "The full post in HTML format. Use <p> and <strong> tags only. DO NOT use
           <h2> or any subheadings. Write in flowing prose paragraphs — 4 to 6 paragraphs,
           250–400 words total. Write throughout as Wolf Jansen speaking — use 'we', 'our',
           'in our view'. Vary sentence length: mix short punchy sentences with longer ones.
           End with a subtle source credit in small italic text:
           <p><em>Prompted by reporting from SOURCE_NAME.</em></p>",
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
def load_pending() -> dict:
    import shutil
    local = _pending_file_path()
    onedrive = _pending_onedrive_path()
    # Bootstrap local copy from OneDrive the first time
    if not os.path.exists(local) and onedrive and os.path.exists(onedrive):
        try:
            shutil.copy2(onedrive, local)
            log.info("  Bootstrapped local pending copy from OneDrive.")
        except OSError:
            pass
    if not os.path.exists(local):
        return {}
    try:
        with open(local, "r") as f:
            content = f.read().strip()
        if not content:
            log.warning("  pending_approvals.json is empty — starting fresh.")
            return {}
        return json.loads(content)
    except json.JSONDecodeError as e:
        log.warning(f"  pending_approvals.json corrupt ({e}) — starting fresh.")
        return {}


def save_pending(data: dict):
    import time, shutil
    local = _pending_file_path()
    # Write locally first — instant, no lock issues
    with open(local, "w") as f:
        json.dump(data, f, indent=2)
    log.info(f"  Drafts saved locally: {local}")

    # Path 1 — macOS: write directly to the OneDrive sync folder
    onedrive = _pending_onedrive_path()
    if onedrive:
        for attempt in range(20):
            try:
                with open(local, "r") as f_in:
                    content = f_in.read()
                with open(onedrive, "w") as f_out:
                    f_out.write(content)
                log.info(f"  Drafts synced to OneDrive (local path): {onedrive}")
                return
            except OSError as e:
                if e.errno in (11, 35) and attempt < 19:
                    log.warning(f"  OneDrive sync locked (attempt {attempt+1}/20), retrying in 5s…")
                    time.sleep(5)
                else:
                    log.error(f"  OneDrive sync failed after retries: {e}")
                    return

    # Path 2 — GitHub Actions / no local OneDrive mount: upload via Microsoft Graph API.
    # Uses the same MS_* credentials already stored as GitHub Actions secrets.
    _upload_pending_via_graph(local)


def _upload_pending_via_graph(local_path: str):
    """Upload pending_approvals.json to the NewsBot OneDrive folder via
    Microsoft Graph API.  Called when ONEDRIVE_FOLDER is not set (i.e. when
    running in GitHub Actions where there is no local OneDrive mount).

    Requires the Azure app registration to have Files.ReadWrite.All
    application permission consented in the tenant.
    """
    import requests as _req

    tenant   = os.getenv("MS_TENANT_ID", "").strip()
    client   = os.getenv("MS_CLIENT_ID", "").strip()
    secret   = os.getenv("MS_CLIENT_SECRET", "").strip()
    user     = os.getenv("MS_USER_EMAIL", "").strip()

    if not all([tenant, client, secret, user]):
        log.warning("  MS Graph credentials not set — pending_approvals.json not uploaded to OneDrive.")
        return

    try:
        # 1. Get access token (client-credentials flow)
        token_resp = _req.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data={
                "grant_type":    "client_credentials",
                "client_id":     client,
                "client_secret": secret,
                "scope":         "https://graph.microsoft.com/.default",
            },
            timeout=30,
        )
        token_resp.raise_for_status()
        access_token = token_resp.json()["access_token"]

        # 2. Upload file to /NewsBot/pending_approvals.json in the user's OneDrive
        with open(local_path, "rb") as f:
            content = f.read()

        upload_url = (
            f"https://graph.microsoft.com/v1.0/users/{user}"
            f"/drive/root:/NewsBot/pending_approvals.json:/content"
        )
        up_resp = _req.put(
            upload_url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type":  "application/json",
            },
            data=content,
            timeout=60,
        )
        up_resp.raise_for_status()
        log.info("  Drafts uploaded to OneDrive via Microsoft Graph API.")

    except Exception as e:
        log.error(f"  Graph API upload failed: {e}. Power Automate will not see new drafts this run.")


def create_wp_draft(title: str, body: str, excerpt: str, division: str) -> Optional[int]:
    """Create a WordPress draft post immediately at generation time.

    Storing the post_id in pending_approvals.json means the Power Automate
    approval flow only needs to PATCH status → publish on the existing draft,
    which is idempotent.  If two people click Approve simultaneously, both
    just re-publish the same post — no duplicate is created.

    Returns the WordPress post ID, or None if creation fails (bot continues
    without WP draft; Power Automate falls back to creating the post itself).
    """
    import requests as _requests
    import base64 as _base64

    wp_url   = CONFIG["wp_url"].rstrip("/")
    wp_user  = CONFIG["wp_user"]
    wp_pass  = CONFIG["wp_password"]

    if not (wp_url and wp_user and wp_pass):
        log.warning("  WP credentials not configured — skipping WP draft creation.")
        return None

    credentials = _base64.b64encode(f"{wp_user}:{wp_pass}".encode()).decode()
    category_id = DIVISION_CATEGORY_IDS[division]

    payload = {
        "title":      title,
        "content":    body,
        "excerpt":    excerpt,
        "status":     "draft",
        "categories": [26, category_id],  # 26 = Latest News parent category
    }

    try:
        resp = _requests.post(
            f"{wp_url}/wp-json/wp/v2/posts?lang=en",
            json=payload,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type":  "application/json",
            },
            timeout=30,
        )
        resp.raise_for_status()
        post_id = resp.json()["id"]
        log.info(f"  ✓ WP draft created: post_id={post_id}")
        return post_id
    except Exception as e:
        log.error(f"  WP draft creation failed: {e}")
        return None


def register_draft(token: str, title: str, excerpt: str, body: str,
                   tags: list, division: str) -> Optional[int]:
    """Create a WordPress draft, store the token in pending_approvals.json,
    and return the WordPress post_id.

    The post_id is embedded directly in the approve/reject URL so Power
    Automate can call WordPress without needing to read OneDrive at all.
    This makes the approval flow work from any environment (GitHub Actions,
    local, etc.) and fixes the mobile 'Invoke download' issue caused by the
    flow erroring before reaching its Response action.
    """
    post_id = create_wp_draft(title, body, excerpt, division)

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
        "post_id":      post_id,
        "used":         False,
        "created":      datetime.now(timezone.utc).isoformat(),
    }
    save_pending(pending)
    return post_id


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


_BANNED_HEADLINE_FRAGMENTS = [
    "what this means", "what that means", "what it means",
    "what they mean", "what these mean",
    "what this tells us", "what that tells us", "what it tells us",
    "what we're watching", "what we are watching",
    "momentum", " continues",
]

def _headline_is_banned(title: str) -> bool:
    t = title.lower()
    return any(frag in t for frag in _BANNED_HEADLINE_FRAGMENTS)


def _enforce_headline(title: str, story: dict, client: anthropic.Anthropic,
                      max_attempts: int = 3) -> str:
    """If the generated headline uses a banned pattern, ask Claude to rewrite
    just the title until it passes or we run out of attempts."""
    if not _headline_is_banned(title):
        return title

    log.warning(f"  Banned headline pattern detected: '{title}' — regenerating")
    prompt = f"""The following headline uses a banned pattern and must be rewritten:

BANNED HEADLINE: {title}

Story context:
- Source: {story['source']}
- Division: {story['division']}
- Original title: {story['title']}
- Summary: {story['summary'][:300]}

Write ONE new headline that:
- Does NOT contain "what this means", "what it means", "what tells us",
  "momentum", or any "What X means for Y" construction
- Is punchy and specific to this story
- Uses one of these structures: a direct market observation, a tension/contradiction,
  a bold specific claim, a market verdict, or a candidate's-eye observation
- Is under 12 words

Return ONLY the headline text, nothing else."""

    for attempt in range(max_attempts):
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=60,
                messages=[{"role": "user", "content": prompt}]
            )
            new_title = resp.content[0].text.strip().strip('"').strip("'")
            if not _headline_is_banned(new_title):
                log.info(f"  Headline replaced: '{new_title}'")
                return new_title
            log.warning(f"  Attempt {attempt+1} still banned: '{new_title}'")
        except Exception as e:
            log.warning(f"  Headline regeneration failed: {e}")
            break

    log.warning("  Could not fix headline — using original")
    return title


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
        result = json.loads(raw)
        # Belt-and-braces: scrub em/en dashes from all text fields even if
        # the model slipped. Em dash (—, U+2014) is the biggest AI tell.
        if isinstance(result, dict):
            for field in ("title", "excerpt", "body"):
                if result.get(field) and isinstance(result[field], str):
                    result[field] = _scrub_dashes(result[field])
            # Strip all <h2> subheadings from body regardless of prompt compliance.
            if result.get("body"):
                result["body"] = _strip_subheadings(result["body"])
        # Hard-reject banned headline patterns and regenerate title only.
        if isinstance(result, dict) and result.get("title"):
            result["title"] = _enforce_headline(result["title"], story, client)
        return result
    except Exception as e:
        log.error(f"  Rewrite failed for '{story['title']}': {e}")
        return None


def _strip_subheadings(html: str) -> str:
    """Remove all <h2>...</h2> tags from post body, replacing with a blank
    paragraph break so the prose flows without section labels."""
    import re as _re
    # Replace <h2>text</h2> with nothing — the paragraph that follows carries on
    html = _re.sub(r"<h2[^>]*>.*?</h2>", "", html, flags=_re.IGNORECASE | _re.DOTALL)
    # Also catch <h3> in case the model uses those
    html = _re.sub(r"<h3[^>]*>.*?</h3>", "", html, flags=_re.IGNORECASE | _re.DOTALL)
    # Clean up any double blank lines left behind
    html = _re.sub(r"(\s*<p>\s*</p>)+", "", html)
    return html.strip()


def _scrub_dashes(text: str) -> str:
    """Replace em dashes with commas and bare en dashes with hyphens."""
    import re as _re
    # Em dash: " — " → ", " (with or without surrounding spaces)
    text = text.replace(" — ", ", ")
    text = text.replace("—", ",")
    # En dash: keep inside number ranges like "12-18", otherwise replace
    text = _re.sub(r"(\d)\s*–\s*(\d)", r"\1-\2", text)
    text = text.replace(" – ", ", ")
    text = text.replace("–", ",")
    return text


# ---------------------------------------------------------------------------
# STEP 3: Build & send approval email
# ---------------------------------------------------------------------------
def _post_card_html(token: str, title: str, excerpt: str, body: str,
                    division: str, post_id=None) -> str:
    approve_base = CONFIG["pa_approve_url"].rstrip("&")
    reject_base  = CONFIG["pa_reject_url"].rstrip("&")
    sep_a = "&" if "?" in approve_base else "?"
    sep_r = "&" if "?" in reject_base else "?"
    # post_id is embedded directly in the URL so Power Automate can call
    # WordPress without looking anything up in OneDrive.
    pid = post_id if post_id is not None else ""
    approve_url = f"{approve_base}{sep_a}token={token}&post_id={pid}"
    reject_url  = f"{reject_base}{sep_r}token={token}&post_id={pid}"

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
        _post_card_html(d["token"], d["title"], d["excerpt"], d["body"],
                        d["division"], d.get("post_id"))
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
        approve_base = CONFIG["pa_approve_url"].rstrip("&")
        reject_base  = CONFIG["pa_reject_url"].rstrip("&")
        sep_a = "&" if "?" in approve_base else "?"
        sep_r = "&" if "?" in reject_base else "?"
        pid = d.get("post_id") or ""
        plain_lines += [
            f"[{DIVISION_LABELS.get(d['division'], d['division'])}]",
            f"{d['title']}",
            f"Approve: {approve_base}{sep_a}token={d['token']}&post_id={pid}",
            f"Reject:  {reject_base}{sep_r}token={d['token']}&post_id={pid}\n",
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
# LOCK FILE — prevents two instances running at the same time
# ---------------------------------------------------------------------------
_LOCK_PATH = os.path.join(os.path.expanduser("~/.newsbot"), "newsbot.lock")


def _acquire_lock():
    """Open and exclusively lock ~/.newsbot/newsbot.lock.

    Returns the open file descriptor on success.  Calls sys.exit(0) if
    another instance already holds the lock — this is intentional: a
    duplicate launchd trigger or accidental manual run should exit cleanly
    rather than crash with a traceback.
    """
    os.makedirs(os.path.dirname(_LOCK_PATH), exist_ok=True)
    lock_fd = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_fd.close()
        log.warning("Another instance of the news bot is already running. Exiting.")
        sys.exit(0)
    lock_fd.write(str(os.getpid()))
    lock_fd.flush()
    return lock_fd


def _release_lock(lock_fd):
    """Release and remove the lock file."""
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
        os.unlink(_LOCK_PATH)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    lock_fd = _acquire_lock()
    try:
        log.info("=" * 60)
        log.info(f"Wolf Jansen News Bot — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        log.info("=" * 60)

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
                post_id = register_draft(
                    token    = token,
                    title    = rewritten["title"],
                    excerpt  = rewritten["excerpt"],
                    body     = rewritten["body"],
                    tags     = rewritten.get("tags", []),
                    division = division_key,
                )
                new_drafts.append({
                    "token":    token,
                    "post_id":  post_id,   # embedded in approve/reject URLs
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
            log.info("No new stories found — nothing to send.")

        log.info("=" * 60)
    finally:
        _release_lock(lock_fd)


if __name__ == "__main__":
    main()
