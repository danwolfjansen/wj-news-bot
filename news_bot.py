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
import base64

import style_guard
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
- Say "company" / "companies". NEVER "firm" or "firms" in any context
  (including "mid-market firm", "the firms getting the most", or our own
  description). Use "company", "business", "organisation", or "employer".
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

## Examples of the right tone
Two examples, deliberately different in shape so you do not copy one template.
Note that NEITHER leans on the word "brief", neither opens a sentence by pointing
back at the previous one, and neither ends on "the ones getting shortlisted first":

Example A (leads with a specific client behaviour):
"A Munich manufacturer spent five months trying to fill an S/4HANA finance role
and kept turning down strong candidates. The sticking point was data residency:
they wanted someone who had actually run a Swiss-German split and could argue the
posting logic with their auditors. We found her in a company that had just been
through the same migration."

Example B (leads with a market read, no self-reference at all):
"Salaries for SAP architects who can also stand up an AI integration have moved
up roughly 15% in Frankfurt and Zurich over the past year. The premium goes to
people who can sit between the SAP team and the data science team and translate
in both directions. Companies are paying it because that gap is where their
migrations stall."

## Do not open with vague "we're seeing" filler
NEVER lead a point with "we are seeing", "we're seeing", "we have seen", "we have
noticed", "we are observing", "we see this as", or "we see this pattern". These
are empty observation openers. State the concrete thing directly instead: name
the actual role, skill, search, or client behaviour. Write "A client this month
asked for X" not "We are seeing demand for X". First-person is welcome when it is
specific ("we placed", "we expect", "a client asked us for"); it is banned
when it is a vague "we're seeing a trend" preamble. Do not lean on a recurring
count of mandates, or on "brief/briefs" as the default evidence noun (see the
anti-repetition rule below).

## NEVER use these AI writing patterns
These phrases make content sound machine-generated. Avoid all of them.

### Banned punctuation
- NO EM DASHES (—) anywhere in the title, excerpt, or body. Not one. This is
  the single biggest AI tell. Use commas, full stops, colons, or rephrase.
- No en dashes (–) in prose. En dashes are only acceptable inside number ranges
  like "12-18 months". Never as a sentence break.

### Banned rhetorical patterns (structural tells)
- The contrastive reversal in EVERY form, not only "not X, it's Y". This is the
  tell that keeps slipping through, so treat it broadly. Banned variations:
    * "not X, it's Y": "That's not a criticism, it's a gap..."
    * "this isn't X, it's Y": "This isn't about X, it's about Y..."
    * "not just X, (it's) Y": "not just those who can configure the software"
    * "X, not Y" noun contrast: "context, not raw data, is the asset"
    * "no longer X, (it's/but/they're) Y": "the question is no longer X but Y",
      "recruitment tech is no longer a back-office afterthought, it is..."
    * "necessary but not sufficient" / "X remains necessary, but no longer enough"
      / "X alone is not enough"
    * "on the surface X, but Y": "On the surface this is a vendor story, but..."
    * "shifted/changed rather than Y": "the load has shifted rather than shrunk"
  State the affirmative point directly. Never define something by what it is not.
- THE CONCESSIVE PIVOT SPREAD ACROSS TWO SENTENCES. This is the single biggest
  leak right now, because it dodges every "not X, but Y" regex above by splitting
  the reversal over a full stop. You concede the old thing in one sentence, then
  pivot to the new thing in the next. Banned in every disguise:
    * "Those remain foundational. What has changed is the third requirement..."
    * "Configuration and Basis administration remain valuable, and roles that
      combine SAP with data science attract the strongest interest."
    * "Commercial acumen and stakeholder presence still matter. But over the past
      18 months a new filter has crept in."
    * "The technical accounting skill remains essential, and advisory fluency has
      become the separator."
    * "Compliance work pays the bills, but advisory work builds careers."
    * Any "X still matters / remains essential / is still valuable / those still
      matter" followed (in the same or next sentence) by "but / what has
      changed / the difference / the separator / now".
  The tell is the concede-then-elevate move itself, not the words. Do not set up
  the old skill as a foil for the new one. If the new demand is the point, state
  what companies now ask for and drop the ritual nod to what still counts.
- THE POINTER-SENTENCE. Do not open a sentence by pointing back at the previous
  one with a demonstrative ("That / This / Such") attached to an abstract noun,
  then announcing its significance. Banned shapes and real examples:
    * "That skill pairing now appears in senior role specifications."
    * "That kind of mid-brief revision signals where demand is headed."
    * "That specificity would have been unusual five years ago."
    * "That observation has direct implications for how companies staff teams."
    * "This creates a challenge for experienced professionals."
    * "This also changes the retention calculation."
    * "That translates into fewer net-new roles."
  Just make the next point as its own concrete statement. If you catch a sentence
  starting with "That [noun]..." or "This [verb]s..." that only restates that the
  prior sentence mattered, delete the pointer and say the substantive thing.
- THE DISMISS-THEN-ELEVATE OPENER. Do not open by calling the subject boring or
  small and then revealing it secretly matters. Banned shapes:
    * "An open gateway joining a foundation sounds like plumbing. But it changes..."
    * "Another open-source initiative, another press release. But the detail that
      caught our attention..."
    * "It sounds like back-office housekeeping. It is not."
  Open with the substantive point directly. Never stage a fake shrug to knock down.
- Short punchy fragment tells. One-line "profound" sentences a LinkedIn guru
  would isolate for effect. Banned examples and their shape:
    * "It works until it doesn't."
    * "The intercompany mess is a symptom."
    * "That framing matters." / "The code was never the hard part."
    * "The cracks show fast."
    * "X is a litmus test for whether..."
    * Parallel two-fragment pairs: "AI handles the coordination. Senior talent handles the judgement."
  Fold the point into a normal sentence with concrete content.
- "For [audience], the implication is clear" / "the implication is clear" /
  "the takeaway is clear" / "the lesson is clear" sign-offs. State the
  implication itself, do not announce that one exists.
- Vague "we are already seeing it" demand-signal filler:
    * "We are already fielding briefs that reflect this shift"
    * "mandates / briefs / requests that reflect this shift"
  Name the concrete role, skill, or client behaviour we are actually seeing.
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

### More tells from the Wolf Jansen Anti-AI Writing Style guide
These follow the house "ANTI AI WRITING STYLE" guide. Avoid all of them:
- Negative parallelism in any form: "not only X but Y", "not X, but Y",
  "no longer X, it's Y", "X, not Y". State the affirmative point directly.
- Rule of three: stop reaching for three-item lists ("keynotes, panels, and
  networking"; "adjective, adjective, adjective"). Use the number of items the
  point actually needs.
- False ranges: no figurative "from X to Y" unless X and Y are real ends of one
  scale. "from configuration to strategy" is a false range, cut it.
- AI-vocabulary words: leverage, delve, tapestry, vibrant, seamless, garner,
  foster, showcase, intricate, realm, navigate (figurative), underpin, myriad,
  bespoke, robust, pivotal, underscore, highlight (as a verb).
- Meta-narration: "in real time" / "in real-time", "we are watching", "playing
  out", "unfolding", "the signal is", "the real question is whether".
- Section-summary openers: "In summary", "In conclusion", "Overall".
- Didactic disclaimers: "it's important to note", "it's worth noting".
- Present-participle tails that tack on significance: ", highlighting that...",
  ", underscoring how...", ", reflecting the...", ", signalling that...".

### Reworded contrastive pivots (the regex-dodgers)
You will be tempted to dodge the negative-parallelism ban above by rewording the
same reversal. These are equally banned, in any form:
- "Neither X nor Y" / "Neither X is wrong, but Y":
  "Neither profile is wrong, but the balance is tilting."
- "X is/are (not) wrong, but Y" of any kind.
- "The headline/framing is about X, but the subtext/real story is Y":
  "The headline framing is about resilience, but the subtext is more interesting."
- "the subtext is...", "the real story is...", "what's (more) interesting is...".
Make the affirmative point on its own. Never set up a surface reading to knock down.

### Abstract-motion and significance fragments
Do not bolt a verb of motion or significance onto an abstract noun as a stand-alone
pronouncement. Banned shapes and real examples:
- "the hiring signal is clear", "the signal is clear", "the picture is clear"
- "the balance is tilting", "the balance is shifting", "the gap is widening"
- "the window to X is narrowing", "the window is closing"
- "the timing matters", "that matters", "this matters", "the distinction matters"
- "that category becomes strategic", "X becomes the asset"
Replace each with a concrete sentence: who is doing what, which skill, which role,
which client behaviour, which number.

### Concreteness requirement (anti-essay rule)
Slop reads as one portentous generalisation per paragraph with nothing under it. At
least half the sentences in the body must carry a concrete particular: a named skill
or system (SAP, S/4HANA, data architecture, treasury), a specific role or seniority,
an observed client behaviour, or a number. If a sentence could appear verbatim in a
post about any other industry, cut it or ground it. Never write a sentence whose only
job is to announce that something is significant.

### Vague comparison attribution
Do not prop up a claim with "than benchmarks suggest", "than the data suggests",
"faster than expected", or similar. Name the source, give the number, or cut the claim.

### Do not template the Wolf Jansen evidence (anti-repetition)
Across a batch these posts keep reaching for the SAME few moves, which makes every
post read like the same template. Each move below is now restricted:
- The rule-of-three placement claim. Stop defaulting to "three" as the count of
  our mandates, briefs, roles, placements or consultants. "We placed three X this
  quarter", "Three of our recent mandates", "Two of our last three" are all banned
  as a reflex. When you cite our own evidence, vary the count, vary the timeframe,
  vary where in the post it sits, and in at least half of posts use no count at
  all. Never let the number be "three".
- The "this did not exist X ago" novelty line. "Two years ago that requirement did
  not exist", "barely existed a year ago", "would not have appeared in the brief 18
  months ago" are banned. Make the point about what the role demands now, in plain
  terms, without the before/after time contrast.
- The "rare skill combination" framing. "That combination is rare / scarce /
  uncommon / hard to find", "candidates with that profile are scarce", "the skill
  combination barely existed" are banned as a default. If scarcity is the real
  point, show it concretely (a specific role we struggled to fill, a named skill
  pairing clients keep requesting) instead of asserting rarity.
- The dual-audience sign-off. Do NOT default to ending (or building) every post as
  "For candidates, ... For hiring managers, ...". Serving both audiences in turn is
  fine once in a while, but it must not be the standard shape. Most posts should
  land one audience, or fold the implication into the body.
- THE WORD "BRIEF" IS OVERUSED TO DEATH. It has become the default noun for every
  piece of evidence ("we have been hearing in briefs", "the briefs we take", "the
  briefs coming through", "a client briefed us", "mid-brief revision", "the brief
  landed on our desk"). Hard cap: the words "brief" or "briefs" may appear AT MOST
  ONCE across the entire post, and preferably zero times. Reach for the specific,
  varied noun instead: a search, a mandate, a role, a vacancy, a spec, a shortlist,
  a requirement, a hiring conversation, a client call, a rewritten job description.
  Vary it post to post. If two posts in a batch both say "brief", rewrite one.
- THE "WE PLACED X LAST [PERIOD]" ANECDOTE IN THE SAME SLOT. Almost every post
  props itself up with one interchangeable placement story ("We placed a senior
  consultant last month...", "We placed a data architect last quarter...", "We
  placed a senior advisory manager earlier this year..."), and it always sits in
  the same position doing the same job. Restrict it: no more than one post in a
  batch may use the bare "We placed a [role] last [period]" construction. In the
  others, either cite the evidence differently (what a client actually asked for,
  a specific role that stalled, a skill clients keep requesting by name) or make
  the market point with no self-reference at all, as in Example B above. Vary the
  verb, the timeframe, and where in the post the evidence sits.
When our own evidence appears it must be specific AND fresh from post to post. If
you cannot make it specific and fresh, leave it out and make the market point.

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
2. Tension or contradiction: "More AI budget, fewer AI hires"
3. A question a senior professional would actually ask: "Is the CFO role becoming a tech role?"
4. First-person trend report: "A wave of S/4HANA migration briefs hit our desk this quarter, all light on one skill"
5. A bold specific claim: "The data skills gap in DACH is three years ahead of where most companies think"
6. The unexpected angle: "Nobody's talking about the mid-level SAP managers caught in this"
7. A hiring signal framed as news: "When HSBC moves like this, DACH banks follow within 18 months"
8. The candidate's perspective: "Senior finance professionals are being asked to do something new"
9. A market verdict: "The case for generalist CFOs just got weaker"
10. A pattern we've spotted: "The same compliance skill keeps showing up in every Financial Advisory brief we take"

BANNED headline patterns — never use regardless of structure number:
- "What X means for Y" in any form
- "What X tells us about Y" in any form
- "X: What Y means for Z" (colon + what it means)
- Any headline containing the word "momentum" or "continues"
- The grand declarative trend-pronouncement, which is currently the default and
  makes every title sound identical. Banned shapes and real examples:
    * "[Thing] just became a(n) [X] problem" — "Tax compliance just became an SAP
      data architecture problem"
    * "The [role] seat now comes with a [X] prerequisite"
    * "The [thing] is where the [X] war will be fought"
    * "The [profession] is splitting in two, and the talent market knows it"
    * Any "..., and the [market/talent market/industry] knows it" tag on the end.
  These read as portentous captions, not headlines a recruiter would write. Prefer
  the concrete, first-person, question, or specific-claim structures numbered above.
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
- Contrastive reversal in ANY form: "not X, it's Y", "not just X", "X, not Y",
  "no longer X (but/it's/they're) Y", "necessary but not sufficient",
  "X alone is not enough", "on the surface ... but", "rather than". Rewrite.
- CONCESSIVE PIVOT across two sentences: any "X remains/still matters/is still
  essential/those still matter/those remain foundational" that is followed by
  "but / what has changed / the difference / the separator / and Y now". This is
  the top leak. Rewrite so the new demand stands on its own with no foil.
- POINTER-SENTENCES: any sentence opening with "That [noun]..." / "This [verb]s..."
  / "Such [noun]..." whose job is to restate that the previous sentence mattered
  ("That skill pairing now appears", "This creates a challenge", "That signals
  where demand is headed"). Delete the pointer and state the substantive thing.
- DISMISS-THEN-ELEVATE openers: "sounds like plumbing/housekeeping, but...",
  "Another X, another Y. But...", "It sounds small. It is not." Open with the point.
- Short fragment tells: "until it doesn't", "is a symptom", "framing matters",
  "the cracks show", "is a litmus test", "was never the hard part". Rewrite into
  full sentences with concrete content.
- "the implication is clear", "the takeaway is clear", "for X the implication is
  clear". Rewrite to state the implication itself.
- "fielding briefs", "reflect(s) this shift". Replace with concrete specifics.
- The words "brief"/"briefs": count them. More than one in the post means rewrite
  down to at most one (ideally zero), using a varied noun (search, mandate, role,
  vacancy, spec, shortlist, requirement, hiring conversation, job description).
- The bare "We placed a [role] last [period]" anecdote: allowed in at most one post
  per batch. If this post is not the one, recast the evidence or drop the self-reference.
- Headline: grand trend-pronouncement shapes ("X just became a Y problem", "now
  comes with a Y prerequisite", "is where the Y war will be fought", "is splitting
  in two", "..., and the talent market knows it"). Rewrite to something concrete.
- The word "firm" or "firms" anywhere. Replace with "company"/"companies".
- Phrases: "play out", "unfold", "in real time", "worth noting", "the signal",
  "Here's the", "Here's what", "12 to 18 months" appearing more than once across
  the batch. Rewrite.
- Headline containing "What X means", "What X tells us", "momentum", "continues". Rewrite.
- Reworded contrastive pivots: "Neither X nor Y", "X is/are (not) wrong, but Y",
  "the headline/framing is about X but Y", "the subtext/real story is...". Rewrite.
- Abstract-motion fragments: "the [noun] signal is clear", "the [noun] is clear",
  "the balance is tilting/shifting", "the gap is widening", "the window is
  narrowing/closing", "the timing/this/that matters", "[noun] becomes strategic". Rewrite.
- Vague comparison: "than benchmarks/the data suggest", "faster than expected".
  Name the source or number, or cut.
- Rule-of-three placement claim: "three" used as the count of our mandates, briefs,
  roles, placements or consultants ("we placed three", "three of our recent
  mandates", "two of our last three"). Vary the count and timeframe, or drop it.
  Never make the number "three".
- Novelty line: "did not exist ... ago", "barely existed ... ago", "would not have
  appeared ... ago". Rewrite to state what the role demands now.
- Scarce-combination framing: "the combination is rare/scarce/uncommon/hard to
  find", "candidates/profiles are scarce". Show scarcity concretely or cut it.
- Dual-audience close: a "For candidates, ... For hiring managers, ..." pairing.
  Land one audience or fold it into the body.

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
def _download_pending_via_graph() -> dict:
    """Download pending_approvals.json from OneDrive via Microsoft Graph API.
    Used in GitHub Actions where there is no local OneDrive sync folder.
    """
    import requests as _req
    tenant = os.getenv("MS_TENANT_ID", "").strip()
    client = os.getenv("MS_CLIENT_ID", "").strip()
    secret = os.getenv("MS_CLIENT_SECRET", "").strip()
    user   = os.getenv("MS_USER_EMAIL", "").strip()
    if not all([tenant, client, secret, user]):
        return {}
    try:
        token_resp = _req.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data={"grant_type": "client_credentials", "client_id": client,
                  "client_secret": secret, "scope": "https://graph.microsoft.com/.default"},
            timeout=30)
        token_resp.raise_for_status()
        access_token = token_resp.json()["access_token"]
        download_url = (f"https://graph.microsoft.com/v1.0/users/{user}"
                        f"/drive/root:/NewsBot/pending_approvals.json:/content")
        resp = _req.get(download_url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            log.info(f"  Downloaded pending_approvals.json from OneDrive ({len(data)} entries).")
            return data
        log.warning(f"  Graph download returned {resp.status_code} — starting fresh.")
        return {}
    except Exception as e:
        log.warning(f"  Graph API download failed: {e}")
        return {}


def load_pending() -> dict:
    import shutil
    local = _pending_file_path()
    onedrive = _pending_onedrive_path()
    # Bootstrap local copy from OneDrive sync folder (macOS local run)
    if not os.path.exists(local) and onedrive and os.path.exists(onedrive):
        try:
            shutil.copy2(onedrive, local)
            log.info("  Bootstrapped local pending copy from OneDrive.")
        except OSError:
            pass
    # No local file — try Graph API download (GitHub Actions)
    if not os.path.exists(local):
        data = _download_pending_via_graph()
        if data:
            # Cache locally so subsequent calls in the same run are instant
            os.makedirs(os.path.dirname(local), exist_ok=True)
            with open(local, "w") as f:
                json.dump(data, f, indent=2)
            return data
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
                "Accept":        "application/json",
                "User-Agent":    "Mozilla/5.0 (compatible; WJNewsBot/1.0)",
            },
            timeout=30,
        )
        if not resp.ok:
            log.error(
                f"  WP draft creation failed: {resp.status_code} {resp.reason} — "
                f"response body: {resp.text[:400]}"
            )
            return None
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
    # Grand declarative trend-pronouncement shapes (the current default look)
    "just became", "now comes with", "splitting in two",
    "war will be fought", "and the talent market knows",
    "market knows it", "and the market knows",
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
def _variety_brief(prior_drafts: Optional[list]) -> str:
    """Build a constraint block listing the openers/closers already used in this
    run, so each new post is told to differ in structure. This is what makes the
    'vary structure' instruction enforceable: without it each post is generated
    blind to its siblings and they converge on the same shape."""
    if not prior_drafts:
        return ""
    import re as _re
    lines = []
    for d in prior_drafts:
        body = _re.sub(r"<[^>]+>", " ", d.get("body", "") or "")
        body = _re.sub(r"\s+", " ", body).strip()
        sents = [s for s in _re.split(r"(?<=[.!?])\s+", body) if s]
        opener = sents[0] if sents else ""
        closer = sents[-1] if sents else ""
        lines.append(
            f'- "{d.get("title","")}"\n'
            f'    opens: "{opener[:160]}"\n'
            f'    closes: "{closer[:160]}"'
        )
    used = "\n".join(lines)
    return (
        "\n\n## VARIETY CONSTRAINT — read carefully\n"
        "The posts below are ALREADY written in today's batch. Yours must read like "
        "a different writer wrote it. Do NOT reuse their opening move, their closing "
        "move, or their overall shape. Specifically:\n"
        "- Do NOT close with a 'those who move early gain an advantage, those who "
        "wait fall behind' contrast if any post below already does.\n"
        "- Do NOT close with a 'we expect this over the next 12-18 months' line if "
        "any post below already does. Vary the time framing or drop it.\n"
        "- If a post below opens with a general market maxim, open yours with "
        "something concrete instead: a specific fact, a number, the candidate's "
        "view, or the actual news.\n"
        "- Pick a different ending type from those used below: a concrete "
        "recommendation, one sharp observation, a question you then answer, or a "
        "named example.\n\n"
        f"Already used in this batch:\n{used}\n"
    )


def rewrite_story(story: dict, client: anthropic.Anthropic,
                  prior_drafts: Optional[list] = None) -> Optional[dict]:
    user_message = f"""
Original headline: {story['title']}
Source: {story['source']}
Source URL: {story['link']}
Division: {story['division'].replace('-', ' ').title()}

Summary / extract:
{story['summary']}

Please rewrite this as an original Wolf Jansen commentary post. Remember to end
the body with a link back to the original source at {story['link']}.
{_variety_brief(prior_drafts)}"""
    messages = [{"role": "user", "content": user_message}]
    try:
        result = _generate_parse_clean(client, messages)
        if result is None:
            return None
        # Programmatic safety net: the system prompt is not always obeyed, so we
        # scan the parsed draft for AI writing tells. If any fire, send ONE
        # corrective re-prompt quoting the offences and keep the cleaner draft.
        tells = _detect_ai_tells(result)
        attempts = 0
        while tells and attempts < 2:
            attempts += 1
            log.warning(
                f"  AI-tell patterns in draft of '{story['title']}' "
                f"(attempt {attempts}): {tells} — requesting corrective rewrite"
            )
            try:
                messages.append({"role": "assistant", "content": json.dumps(result)})
                messages.append({"role": "user", "content": _build_correction_message(tells)})
                retry = _generate_parse_clean(client, messages)
            except Exception as e:
                log.warning(f"  Corrective rewrite failed ({e}); keeping previous draft")
                break
            if retry is None:
                break
            retry_tells = _detect_ai_tells(retry)
            if len(retry_tells) < len(tells):
                result, tells = retry, retry_tells   # accept the cleaner draft
            else:
                # Keep the previous (cleaner or equal) draft but spend the
                # remaining attempt rather than giving up straight away.
                log.warning("  Corrective rewrite no cleaner, keeping previous draft")
        # Hard-reject banned headline patterns and regenerate title only.
        if isinstance(result, dict) and result.get("title"):
            result["title"] = _enforce_headline(result["title"], story, client)
        # Anything that survived the retries is surfaced on the approval email
        # card so it can be reviewed by a human before publishing — a dirty
        # draft must never reach the inbox looking identical to a clean one.
        if isinstance(result, dict):
            result["style_warnings"] = _detect_ai_tells(result)
            if result["style_warnings"]:
                log.warning(f"  UNRESOLVED tells going to review: "
                            f"{result['style_warnings'][:6]}")
        return result
    except Exception as e:
        log.error(f"  Rewrite failed for '{story['title']}': {e}")
        return None


def _generate_parse_clean(client: anthropic.Anthropic, messages: list) -> Optional[dict]:
    """Call the model, strip code fences, parse JSON, then scrub dashes,
    enforce company-over-firm, and strip subheadings on all text fields."""
    response = client.messages.create(
        model="claude-opus-4-5-20251101",
        max_tokens=1024,
        system=REWRITE_SYSTEM_PROMPT,
        messages=messages,
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0].strip()
    result = json.loads(raw)
    if isinstance(result, dict):
        for field in ("title", "excerpt", "body"):
            if result.get(field) and isinstance(result[field], str):
                result[field] = _scrub_dashes(result[field])
                result[field] = _swap_firm_for_company(result[field])
        # Strip all <h2> subheadings from body regardless of prompt compliance.
        if result.get("body"):
            result["body"] = _strip_subheadings(result["body"])
    return result


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


def _swap_firm_for_company(text: str) -> str:
    """House style: use 'company'/'companies', never 'firm'/'firms'.
    Preserves case and possessives; word boundaries leave 'confirm',
    'firmware', 'firmly', 'infirmary' untouched."""
    import re as _re
    def _f(mm):
        w = mm.group(0)
        base = "companies" if w.lower() == "firms" else "company"
        if w.isupper():
            return base.upper()
        if w[0].isupper():
            return base[0].upper() + base[1:]
        return base
    return _re.sub(r"\bfirms?\b", _f, text, flags=_re.IGNORECASE)


# AI-tell detection now lives in style_guard.py, shared with linkedin_bot so
# both bots enforce the identical, unified pattern set (previously each bot
# had its own list and each missed tells the other caught).
def _detect_ai_tells(result: dict) -> list:
    """Return 'label: "fragment"' strings for banned AI patterns in the draft.
    Quoting the actual offending fragment makes the corrective re-prompt far
    more reliable than naming the abstract pattern."""
    if not isinstance(result, dict):
        return []
    hits = style_guard.detect_fields(result, context="news")
    return style_guard.format_hits(hits)


def _build_correction_message(tells: list) -> str:
    """Build a corrective re-prompt quoting the detected tells."""
    bullet_list = "\n".join(f"  - {t}" for t in tells)
    return (
        "Your draft still contains AI writing tells that are banned in the Wolf "
        "Jansen style guide. The automated checker found these EXACT fragments "
        "(quoted from your draft) — every one must be gone from the rewrite:\n"
        f"{bullet_list}\n\n"
        "Rewrite the post to remove ALL of them. Specifically:\n"
        "- Never define something by what it is not. Banned in every form: "
        "\"not X, it's Y\", \"X, not Y\", \"no longer X but Y\", \"not just X\", "
        "\"necessary but not sufficient\", \"on the surface ... but\". Make each "
        "point as a direct affirmative statement.\n"
        "- Remove short one-line 'profound' fragments (\"It works until it "
        "doesn't\", \"X is a symptom\", \"that framing matters\"). Fold the point "
        "into a normal sentence.\n"
        "- Never write \"the implication is clear\" / \"the takeaway is clear\"; "
        "state the implication itself.\n"
        "- Use \"company\"/\"companies\", never \"firm\"/\"firms\".\n"
        "- Remove reworded contrastive pivots: \"Neither X nor Y\", \"X is/are (not) "
        "wrong, but Y\", \"the headline/framing is about X but Y\", \"the subtext/real "
        "story is...\". State the affirmative point on its own.\n"
        "- Remove abstract-motion fragments: \"the [noun] signal is clear\", \"the "
        "[noun] is clear\", \"the balance is tilting\", \"the gap is widening\", \"the "
        "window is narrowing\", \"the timing/this/that matters\", \"[noun] becomes "
        "strategic\". Replace with a concrete sentence naming who does what, which "
        "skill, role, or number.\n"
        "- Replace vague comparison (\"than benchmarks/the data suggest\", \"faster "
        "than expected\") with a named source or specific number, or cut it.\n"
        "- Do NOT use the rule-of-three placement claim (\"we placed three...\", "
        "\"three of our recent mandates\", \"two of our last three\"). Vary the count "
        "and timeframe, or cite no count. The number must not be \"three\".\n"
        "- Remove the \"this did not exist X ago\" novelty line and the "
        "\"rare/scarce skill combination\" framing. State what the role demands now, "
        "concretely, and show scarcity with a specific role or skill pairing if it "
        "is real.\n"
        "- Do not end with the dual \"For candidates... For hiring managers...\" "
        "sign-off. Land one audience or fold the implication into the body.\n"
        "- Remove the concessive pivot: any \"X remains foundational/essential\" or "
        "\"those still matter\" that you then pivot away from with \"but\" or \"what "
        "has changed is Y\". Do not set the old skill up as a foil. State what the "
        "role now demands on its own.\n"
        "- Remove pointer-sentences that open with \"That [noun]...\" or \"This "
        "[verb]s...\" only to restate that the previous sentence mattered (\"That "
        "signals where demand is headed\", \"This creates a challenge\"). Make the "
        "next point as its own concrete statement.\n"
        "- Remove dismiss-then-elevate openers (\"sounds like plumbing, but...\", "
        "\"Another initiative, another press release. But...\"). Open with the point.\n"
        "- Do not lean on the word \"brief\"/\"briefs\" as the evidence noun. Use it "
        "at most once (ideally not at all); reach for a specific noun instead: a "
        "search, a mandate, a role, a vacancy, a spec, a shortlist, a requirement, "
        "a hiring conversation, a job description.\n"
        "Keep the same facts, division, length (250-400 words), prose-only format "
        "(no subheadings), and the italic source credit line. Return ONLY the JSON "
        "object in the same format."
    )


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
_PAGES_BASE = "https://danwolfjansen.github.io/wj-news-bot"

def _pages_url(action: str, pa_url: str, token: str, post_id) -> str:
    """Build a GitHub Pages URL that proxies to Power Automate.
    Mobile browsers open the .html page directly; it calls PA silently.
    """
    encoded = base64.b64encode(pa_url.encode()).decode()
    pid = post_id if post_id is not None else ""
    return f"{_PAGES_BASE}/{action}.html?url={encoded}&token={token}&post_id={pid}"

def _post_card_html(token: str, title: str, excerpt: str, body: str,
                    division: str, post_id=None, style_warnings=None) -> str:
    approve_url = _pages_url("approve", CONFIG["pa_approve_url"], token, post_id)
    reject_url  = _pages_url("reject",  CONFIG["pa_reject_url"],  token, post_id)

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

      {style_guard.warning_strip_html(style_warnings or [])}

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
                        d["division"], d.get("post_id"), d.get("style_warnings"))
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
        pid = d.get("post_id") or None
        plain_lines += [
            f"[{DIVISION_LABELS.get(d['division'], d['division'])}]",
            f"{d['title']}",
            f"Approve: {_pages_url('approve', CONFIG['pa_approve_url'], d['token'], pid)}",
            f"Reject:  {_pages_url('reject',  CONFIG['pa_reject_url'],  d['token'], pid)}\n",
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
                rewritten = rewrite_story(story, ai_client, prior_drafts=new_drafts)
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
                    "style_warnings": rewritten.get("style_warnings", []),
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
