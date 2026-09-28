"""
Wolf Jansen Run Guard
=====================
Gets the digest into the morning despite GitHub's scheduler.

The problem: scheduled workflows are best-effort. GitHub delays them under
load and drops them outright when load is high enough; the top of the hour
is the worst slot. The job itself takes 2-5 minutes, but GitHub has been
CREATING the 07:00 UTC run later and later:
    early Aug: 1-4h late    27/28 Aug: 11-12h late (7-8pm digests)
    mid/late Sep: 5.5-8h late (digests landing 1:30-4pm UK)
A fixed early slot cannot fix a delay that drifts between 1 and 12 hours.

The fix: the workflows fire every hour, around the clock, at an off-peak
minute. Whatever the delay is on a given day, some hourly slot lands inside
the delivery window. This module decides, at the start of each run, whether
this run is the one:

  1. RIGHT DAY. News: Monday-Friday (UK). LinkedIn: Thursdays.
  2. RIGHT TIME. Only between WINDOW_START and WINDOW_END, UK local time
     (07:00-12:00 by default). Never at 1am, never at 8pm.
  3. ONCE. The first run in the window that delivers records it; every later
     run that day stands down.

Standing down consumes nothing: the bot exits before fetching feeds, so no
story is marked seen and anything unsent rolls into the next morning.

State is last_delivery.json in the repo (GitHub Contents API in Actions, a
local file otherwise). A guard that cannot read its own state must never be
why a digest goes missing, so reads fail open; and because a failed WRITE
would let the next hourly run send a duplicate, the news bot also passes a
fallback that checks its own draft records.
"""

import base64
import json
import logging
import os
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

STATE_FILE = "last_delivery.json"

WINDOW_START = int(os.getenv("DIGEST_WINDOW_START", "7"))   # UK hour, inclusive
WINDOW_END = int(os.getenv("DIGEST_WINDOW_END", "12"))      # UK hour, exclusive

WEEKDAYS = {0, 1, 2, 3, 4}   # Monday..Friday
THURSDAY = {3}


def _uk_now() -> datetime:
    """The single clock for this module, in UK local time so the window means
    the same thing in BST and GMT."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/London"))
    except Exception:
        return datetime.now(timezone.utc)


def _in_actions() -> bool:
    return os.getenv("GITHUB_ACTIONS", "").lower() == "true"


def _gh() -> tuple:
    return (os.getenv("GITHUB_TOKEN", ""),
            os.getenv("GITHUB_REPOSITORY", "danwolfjansen/wj-news-bot"))


def _gh_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _load_state() -> dict:
    """Read the delivery ledger. Returns {} on any failure (fail-open)."""
    token, repo = _gh()
    if _in_actions() and token:
        try:
            r = requests.get(
                f"https://api.github.com/repos/{repo}/contents/{STATE_FILE}",
                headers=_gh_headers(token), timeout=15)
            if r.status_code == 200:
                return json.loads(base64.b64decode(r.json()["content"]))
            if r.status_code != 404:
                log.warning(f"Run guard: state load returned {r.status_code}")
        except Exception as e:
            log.warning(f"Run guard: state load failed ({e})")
        return {}
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                return json.load(f)
    except Exception as e:
        log.warning(f"Run guard: local state load failed ({e})")
    return {}


def _save_state(state: dict) -> bool:
    """Persist the ledger. Returns True on success."""
    content = json.dumps(state, indent=2)
    token, repo = _gh()
    if _in_actions() and token:
        try:
            api = f"https://api.github.com/repos/{repo}/contents/{STATE_FILE}"
            sha = None
            probe = requests.get(api, headers=_gh_headers(token), timeout=15)
            if probe.status_code == 200:
                sha = probe.json().get("sha")
            payload = {
                "message": "chore: record digest delivery [skip ci]",
                "content": base64.b64encode(content.encode()).decode(),
                "branch": "main",
            }
            if sha:
                payload["sha"] = sha
            r = requests.put(api, headers=_gh_headers(token), json=payload,
                             timeout=30)
            if r.status_code in (200, 201):
                return True
            log.warning(f"Run guard: state save returned {r.status_code}: "
                        f"{r.text[:200]}")
        except Exception as e:
            log.warning(f"Run guard: state save failed ({e})")
        return False
    try:
        with open(STATE_FILE, "w") as f:
            f.write(content)
        return True
    except Exception as e:
        log.warning(f"Run guard: local state save failed ({e})")
        return False


def _parse(ts: str):
    try:
        return datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except Exception:
        return None


def should_deliver(bot: str, days=WEEKDAYS, fallback_delivered_today=None) -> tuple:
    """(ok, reason). Call first thing in a run; on ok=False, exit at once."""
    now = _uk_now()
    if now.weekday() not in days:
        return False, f"{now:%A} is not a delivery day"
    if not (WINDOW_START <= now.hour < WINDOW_END):
        return False, (f"{now:%H:%M} UK is outside the {WINDOW_START:02d}:00-"
                       f"{WINDOW_END:02d}:00 delivery window")
    last = _parse(_load_state().get(bot, ""))
    if last is not None and last.astimezone(now.tzinfo).date() == now.date():
        return False, f"already delivered today at {last.astimezone(now.tzinfo):%H:%M} UK"
    if fallback_delivered_today is not None:
        try:
            if fallback_delivered_today():
                return False, ("already delivered today (from the bot's own "
                               "records; the ledger missed it)")
        except Exception as e:
            log.warning(f"Run guard: fallback check failed ({e})")
    return True, f"{now:%A %H:%M} UK, first run in the delivery window"


def mark_delivered(bot: str) -> bool:
    """Record today's delivery. Returns False if it could not be persisted,
    which the caller should report: the next hourly run might then send a
    second digest."""
    state = _load_state()
    state[bot] = _uk_now().astimezone(timezone.utc).isoformat()
    ok = _save_state(state)
    if ok:
        log.info(f"Run guard: recorded {bot} delivery at {_uk_now():%H:%M} UK")
    else:
        log.error("Run guard: COULD NOT record the delivery; a later run "
                  "today may send a duplicate.")
    return ok
