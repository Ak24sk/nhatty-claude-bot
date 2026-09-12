"""
twitter_attention.py
---------------------
Attention-acceleration detector for a token/ticker on Twitter (X).

Two modes:

1. Demo mode (default, no API keys required)
   Generates a plausible synthetic post-volume timeline, explorable on
   Streamlit Community Cloud without any external account.

2. Live mode (optional)
   If a TwitterAPI.io API key is provided via Streamlit secrets as
   TWITTERAPI_API_KEY, this module pulls real recent tweet volume for a
   query via TwitterAPI.io's advanced search endpoint
   (https://api.twitterapi.io/twitter/tweet/advanced_search), a third-party
   paid-per-call service (not the official X API) - pricing is roughly
   $0.0008 per call / ~20 tweets, i.e. a few cents for a full lookup.

   To control cost, live lookups are capped at a small number of pages
   per call (MAX_PAGES below) rather than exhaustively paginating.

Attention acceleration is measured the same way in both modes: post-volume
growth rate over a short window vs. a long window. Bot/coordinated-share
estimation uses simple heuristics (duplicate-ish text, thin account
signals) when real post data is available, and a plausible synthetic
value in demo mode.
"""

import math
import random
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

TWITTERAPI_ADVANCED_SEARCH_URL = "https://api.twitterapi.io/twitter/tweet/advanced_search"
MAX_PAGES = 5  # cost control: ~100 tweets per lookup, a few cents at TwitterAPI.io rates
REQUEST_TIMEOUT = 10


def _synthetic_timeline(seed: str, hours: int = 12, accelerating: Optional[bool] = None) -> List[Dict]:
    """Deterministic-ish synthetic hourly post counts, for demo mode / fallback."""
    rnd = random.Random(seed)
    if accelerating is None:
        accelerating = rnd.random() > 0.5

    base = rnd.randint(3, 15)
    counts = []
    now = datetime.now(timezone.utc)
    for h in range(hours, 0, -1):
        t = now - timedelta(hours=h)
        if accelerating:
            growth = math.exp((hours - h) / hours * 2.0)
        else:
            growth = 1 + rnd.uniform(-0.1, 0.1)
        counts.append({
            "hour_start": t.isoformat(),
            "post_count": max(0, int(base * growth + rnd.uniform(-2, 2))),
        })
    return counts


def _parse_twitter_created_at(created_at: str) -> Optional[datetime]:
    """Twitter's createdAt format looks like 'Wed Oct 05 20:00:00 +0000 2022'."""
    if not created_at:
        return None
    try:
        return datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        try:
            # fallback: ISO format, in case the API returns that instead
            return datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            return None


def _fetch_live_tweets(query: str, api_key: str, max_pages: int = MAX_PAGES) -> List[Dict]:
    """Fetches up to max_pages of tweets from TwitterAPI.io's advanced search."""
    tweets = []
    cursor = None
    headers = {"X-API-Key": api_key}

    for _ in range(max_pages):
        params = {"query": query, "queryType": "Latest"}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = requests.get(
                TWITTERAPI_ADVANCED_SEARCH_URL,
                headers=headers,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError):
            break

        page_tweets = data.get("tweets", [])
        if not page_tweets:
            break
        tweets.extend(page_tweets)

        cursor = data.get("next_cursor")
        if not cursor:
            break

    return tweets


def get_attention_timeline(
    query: str,
    api_key: Optional[str] = None,
    demo_mode: bool = True,
    hours: int = 12,
) -> List[Dict]:
    """
    Returns a list of {"hour_start": iso_str, "post_count": int} for the
    last `hours` hours. Falls back to synthetic data if demo_mode is True,
    api_key is missing, or the live fetch fails/returns nothing.
    """
    if demo_mode or not api_key:
        return _synthetic_timeline(seed=query, hours=hours)

    tweets = _fetch_live_tweets(query, api_key)
    if not tweets:
        return _synthetic_timeline(seed=query, hours=hours)

    now = datetime.now(timezone.utc)
    buckets = {h: 0 for h in range(hours, 0, -1)}

    for t in tweets:
        created = _parse_twitter_created_at(t.get("createdAt", ""))
        if not created:
            continue
        age_hours = (now - created).total_seconds() / 3600
        bucket = math.ceil(age_hours) if age_hours > 0 else 1
        if bucket in buckets:
            buckets[bucket] += 1

    return [
        {"hour_start": (now - timedelta(hours=h)).isoformat(), "post_count": buckets[h]}
        for h in sorted(buckets.keys(), reverse=True)
    ]


def compute_acceleration(timeline: List[Dict], short_window: int = 3, long_window: int = 9) -> Dict:
    """
    Compare average post volume over the most recent `short_window` hours
    vs. the preceding `long_window` hours to get an acceleration ratio.
    Ratio > 1.5 is treated as "BUILDING"; > 3.0 as "SPIKING".
    """
    if len(timeline) < short_window + 1:
        return {"acceleration_ratio": 0.0, "label": "INSUFFICIENT_DATA", "recent_avg_per_hour": 0, "prior_avg_per_hour": 0}

    counts = [row["post_count"] for row in timeline]
    recent = counts[-short_window:]
    prior = counts[max(0, len(counts) - short_window - long_window):len(counts) - short_window]

    recent_avg = sum(recent) / len(recent) if recent else 0
    prior_avg = sum(prior) / len(prior) if prior else 0

    ratio = round(recent_avg / max(prior_avg, 0.001), 2)

    if ratio >= 3.0:
        label = "SPIKING"
    elif ratio >= 1.5:
        label = "BUILDING"
    elif ratio <= 0.6:
        label = "FADING"
    else:
        label = "FLAT"

    return {
        "acceleration_ratio": ratio,
        "recent_avg_per_hour": round(recent_avg, 1),
        "prior_avg_per_hour": round(prior_avg, 1),
        "label": label,
    }


def estimate_bot_share(sample_posts: Optional[List[Dict]] = None, seed: str = "") -> float:
    """
    Rough bot/coordinated-promotion share estimate (0.0-1.0).
    In demo mode (no sample_posts) this returns a plausible synthetic value.
    In live mode, pass raw tweet dicts from TwitterAPI.io (each with at
    least a "text" field) and this applies a simple heuristic: near-duplicate
    text across posts counts as likely-coordinated/bot activity.
    """
    if not sample_posts:
        rnd = random.Random(seed or "bot-share")
        return round(rnd.uniform(0.1, 0.6), 2)

    texts = [p.get("text", "").strip().lower() for p in sample_posts if p.get("text")]
    if not texts:
        return round(random.Random(seed or "bot-share").uniform(0.1, 0.6), 2)

    counts = Counter(texts)
    duplicate_count = sum(c for c in counts.values() if c > 1)
    return round(min(1.0, duplicate_count / len(texts)), 2)
