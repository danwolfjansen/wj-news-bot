"""
Wolf Jansen LinkedIn Bot
========================
Runs weekly (Thursdays, 09:00 German time via GitHub Actions).

Looks back over the approved + published stories from the past 7 days,
asks Claude to pick the single most LinkedIn-worthy one, rewrites it as
a 150-250 word LinkedIn post in Wolf Jansen company voice, and emails
Dan a preview card with Approve / Reject buttons.

On approval, a dedicated Power Automate flow reads the post text from
OneDrive (pending_linkedin.json) and POSTs it to a Make.com webhook,
which creates the post on the Wolf Jansen LinkedIn company page.

Source pool:    OneDrive/NewsBot/pending_approvals.json  (where used=True)
LinkedIn pool:  OneDrive/NewsBot/pending_linkedin.json   (new file this bot writes)
"""

import anthropic
import base64
import concurrent.futures
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

try:
    import fal_client
except ImportError:
    fal_client = None  # type: ignore  # image generation will be skipped if not installed

# Reuse config + labels from the existing news bot so we stay DRY.
# (news_bot.py lives next to this file — same repo root.)
# NOTE: we do NOT import load_pending from news_bot, because that implementation
# only reads from the local OneDrive sync folder. In GitHub Actions the folder
# doesn't exist, so we implement a cloud-aware load_pending below that uses the
# Microsoft Graph API when running in the cloud.
from news_bot import (
    CONFIG,
    DIVISION_COLOURS,
    DIVISION_LABELS,
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

    # fal.ai for Flux 2 Pro image candidates (swapped from gpt-image-1 Apr 2026).
    "fal_api_key":      os.getenv("FAL_KEY", ""),
    # GitHub API for uploading generated images back to the repo.
    "github_token":     os.getenv("GITHUB_TOKEN", ""),
    "github_repository": os.getenv("GITHUB_REPOSITORY", "danwolfjansen/wj-news-bot"),
    # How many image candidates to generate per post.
    "image_candidates": int(os.getenv("LINKEDIN_IMAGE_CANDIDATES", "3")),
}

_GITHUB_PAGES_BASE = "https://danwolfjansen.github.io/wj-news-bot"


# ---------------------------------------------------------------------------
# OneDrive / Microsoft Graph helpers (only used when running in GitHub Actions)
# ---------------------------------------------------------------------------
# The local news_bot reads pending_approvals.json from a synced OneDrive folder
# on Dan's Mac. In GitHub Actions there is no synced folder, so we fall back to
# the Microsoft Graph API with the app registration whose credentials are in
# the repo secrets (MS_TENANT_ID / MS_CLIENT_ID / MS_CLIENT_SECRET).
_MS_CONFIG = {
    "tenant_id":     os.getenv("MS_TENANT_ID", ""),
    "client_id":     os.getenv("MS_CLIENT_ID", ""),
    "client_secret": os.getenv("MS_CLIENT_SECRET", ""),
    "user_email":    os.getenv("MS_USER_EMAIL", ""),
    # The OneDrive folder (relative to the user's drive root) where the news
    # bot keeps its JSON state files.
    "onedrive_folder": os.getenv("MS_ONEDRIVE_FOLDER", "NewsBot"),
}


def _running_in_cloud() -> bool:
    """True when executing inside GitHub Actions (no local OneDrive sync)."""
    return os.getenv("GITHUB_ACTIONS", "").lower() == "true"


def _get_graph_token() -> str:
    """Client-credentials OAuth flow against Microsoft Graph."""
    tenant = _MS_CONFIG["tenant_id"]
    resp = requests.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={
            "client_id":     _MS_CONFIG["client_id"],
            "client_secret": _MS_CONFIG["client_secret"],
            "scope":         "https://graph.microsoft.com/.default",
            "grant_type":    "client_credentials",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()
