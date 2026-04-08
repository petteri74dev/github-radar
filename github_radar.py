"""
GitHub Radar — x402/ERC-8004 ecosystem intelligence scanner.

Uses GitHub Search API to find new repositories matching AsterPay-relevant
keywords, scores them, and sends Telegram notifications for high-scoring hits.
Persists seen repos to Supabase to survive stateless CI runs.
"""

import os
import sys
import json
import base64
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
import yaml

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://wibwhwxsoutngqyjvhgz.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
SCORE_THRESHOLD = int(os.environ.get("SCORE_THRESHOLD", "4"))

GITHUB_API = "https://api.github.com"
UA = "github-radar/2.0 (AsterPay)"

# ── Scoring keywords ──────────────────────────────────────────────

SCORING_RULES: list[tuple[int, list[str]]] = [
    (3, ["x402"]),
    (3, ["erc-8004", "erc8004"]),
    (2, ["facilitator"]),
    (2, ["eurc", "eur settlement", "sepa"]),
    (2, ["mica", "mica compliance"]),
    (1, ["telecom", "telco", "voip", "camara"]),
    (1, ["marketplace", "bazaar"]),
]

COMBO_RULES: list[tuple[int, list[str], list[str]]] = [
    (2, ["payment", "payments"], ["agent", "agentic", "ai agent", "ai-agent"]),
]


# ── GitHub helpers ─────────────────────────────────────────────────

def gh_headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "User-Agent": UA}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def gh_search(query: str, created_after: str) -> list[dict[str, Any]]:
    """Search GitHub repos created after a date. Returns up to 30 results."""
    full_q = f"{query} created:>{created_after}"
    params = {"q": full_q, "sort": "updated", "order": "desc", "per_page": 30}

    try:
        resp = requests.get(
            f"{GITHUB_API}/search/repositories",
            headers=gh_headers(),
            params=params,
            timeout=15,
        )
        if resp.status_code == 403:
            print(f"  Rate limited on search, sleeping 60s")
            time.sleep(60)
            return []
        if resp.status_code == 422:
            print(f"  Search query rejected: {query}")
            return []
        resp.raise_for_status()
        return resp.json().get("items", [])
    except Exception as e:
        print(f"  Search error: {e}")
        return []


def gh_readme(owner: str, repo: str) -> str:
    """Fetch README content (base64 decoded)."""
    try:
        resp = requests.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/readme",
            headers=gh_headers(),
            timeout=10,
        )
        if resp.status_code != 200:
            return ""
        content = resp.json().get("content", "")
        return base64.b64decode(content).decode("utf-8", errors="ignore")[:5000]
    except Exception:
        return ""


# ── Supabase persistence ──────────────────────────────────────────

def sb_headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def load_seen_ids() -> set[int]:
    """Load already-seen repo IDs from Supabase."""
    if not SUPABASE_KEY:
        return set()
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/github_radar_seen?select=repo_id",
            headers=sb_headers(),
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"  Supabase load error: {resp.status_code}")
            return set()
        return {r["repo_id"] for r in resp.json()}
    except Exception as e:
        print(f"  Supabase load exception: {e}")
        return set()


def save_seen(repo_id: int, full_name: str, score: int, reason: str) -> None:
    """Save a scored repo to Supabase."""
    if not SUPABASE_KEY:
        return
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/github_radar_seen",
            headers={**sb_headers(), "Prefer": "return=minimal,resolution=merge-duplicates"},
            json={
                "repo_id": repo_id,
                "full_name": full_name,
                "score": score,
                "reason": reason,
                "scored_at": datetime.now(timezone.utc).isoformat(),
                "notified": score >= SCORE_THRESHOLD,
            },
            timeout=10,
        )
    except Exception as e:
        print(f"  Supabase save error: {e}")


# ── Scoring ────────────────────────────────────────────────────────

def score_repo(
    description: str,
    topics: list[str],
    readme: str,
) -> tuple[int, list[str]]:
    """Score a repo based on keyword analysis. Returns (score, matched_reasons)."""
    text = " ".join([description or "", " ".join(topics or []), readme or ""]).lower()
    total = 0
    reasons: list[str] = []

    for points, keywords in SCORING_RULES:
        for kw in keywords:
            if kw in text:
                total += points
                reasons.append(kw)
                break

    for points, group_a, group_b in COMBO_RULES:
        a_hit = any(k in text for k in group_a)
        b_hit = any(k in text for k in group_b)
        if a_hit and b_hit:
            total += points
            reasons.append("payment+agent combo")

    if not description or len(description) < 10:
        total -= 1
        reasons.append("no description")

    return total, reasons


# ── Telegram ───────────────────────────────────────────────────────

def send_telegram(repo: dict[str, Any], score: int, reasons: list[str]) -> None:
    """Send Telegram notification for a high-scoring repo."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  Telegram not configured, skipping notification")
        return

    full_name = repo.get("full_name", "unknown")
    html_url = repo.get("html_url", "")
    desc = repo.get("description", "") or ""
    lang = repo.get("language", "?") or "?"
    stars = repo.get("stargazers_count", 0)
    forks = repo.get("forks_count", 0)
    created = (repo.get("created_at", "") or "")[:10]

    text = (
        f"🔭 *GitHub Radar — uusi repo*\n\n"
        f"*Repo:* [{full_name}]({html_url})\n"
        f"⭐ {stars} | 🍴 {forks} | 📅 {created}\n"
        f"*Kieli:* {lang}\n"
        f"*Kuvaus:* {desc[:200]}\n\n"
        f"*Score:* {score}\n"
        f"*Osumat:* {', '.join(reasons)}\n"
    )

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if not resp.ok:
            print(f"  Telegram error: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"  Telegram exception: {e}")


# ── Main scan ──────────────────────────────────────────────────────

def interval_to_date(interval: str) -> str:
    """Convert interval name to ISO date string."""
    now = datetime.now(timezone.utc)
    if interval == "daily":
        dt = now - timedelta(days=1)
    elif interval == "weekly":
        dt = now - timedelta(weeks=1)
    elif interval == "monthly":
        dt = now - timedelta(days=30)
    else:
        dt = now - timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def run_scan() -> None:
    searches_path = os.path.join(os.path.dirname(__file__), "searches.yaml")
    with open(searches_path, "r") as f:
        config = yaml.safe_load(f)

    searches = config.get("searches", [])
    seen_ids = load_seen_ids()
    total_new = 0
    total_notified = 0

    print(f"Loaded {len(seen_ids)} seen repos from Supabase")
    print(f"Running {len(searches)} searches (threshold={SCORE_THRESHOLD})...\n")

    for search in searches:
        query = search["query"]
        interval = search.get("interval", "daily")
        created_after = interval_to_date(interval)

        print(f"[{query}] (created>{created_after})")
        repos = gh_search(query, created_after)
        print(f"  Found {len(repos)} repos")

        for repo in repos:
            repo_id = repo.get("id")
            if not repo_id or repo_id in seen_ids:
                continue

            if repo.get("fork"):
                continue

            full_name = repo.get("full_name", "?")
            desc = repo.get("description", "") or ""
            topics = repo.get("topics", []) or []

            readme = ""
            owner, name = full_name.split("/", 1) if "/" in full_name else ("", "")
            if owner:
                readme = gh_readme(owner, name)

            score, reasons = score_repo(desc, topics, readme)
            seen_ids.add(repo_id)
            total_new += 1

            save_seen(repo_id, full_name, score, ", ".join(reasons))

            if score >= SCORE_THRESHOLD:
                print(f"  🔔 {full_name} score={score} [{', '.join(reasons)}]")
                send_telegram(repo, score, reasons)
                total_notified += 1
            else:
                print(f"  ○ {full_name} score={score}")

        # Respect GitHub Search API rate limit (30 req/min)
        time.sleep(2.5)

    print(f"\nDone. {total_new} new repos scanned, {total_notified} notifications sent.")


if __name__ == "__main__":
    if not GITHUB_TOKEN:
        print("WARNING: GITHUB_TOKEN not set — severe rate limits apply")
    if not SUPABASE_KEY:
        print("WARNING: SUPABASE_KEY not set — seen repos won't persist")

    run_scan()
