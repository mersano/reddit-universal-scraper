#!/usr/bin/env python3
"""
SCLA Reddit research scanner v4.

GitHub Actions often receives HTTP 403 from reddit.com/search.json. This version avoids
Reddit search and instead scans subreddit listing JSON endpoints from old.reddit/redlib
mirrors, then filters the collected posts locally.

Goal:
- Build a manual-review list of recent/top Reddit threads related to education, career,
  honor societies, resume value, networking, scholarships, and membership decisions.
- Exclude threads that already mention SCLA / thescla.org.
- Export opportunities + diagnostics.

This script does not post comments and does not automate Reddit engagement.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import random
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Optional

import requests

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"

MIRRORS = [
    "https://old.reddit.com",
    "https://redlib.privadency.com",
    "https://redlib.orangenet.cc",
    "https://red.artemislena.eu",
]

SUBREDDITS = [
    "college",
    "careerguidance",
    "careeradvice",
    "findapath",
    "GetEmployed",
    "ApplyingToCollege",
    "csMajors",
    "cscareerquestions",
    "GradSchool",
    "jobs",
    "resumes",
    "internships",
    "student",
    "students",
    "collegeadvice",
]

LISTING_MODES = [
    ("new", {}),
    ("hot", {}),
    ("top", {"t": "month"}),
    ("top", {"t": "year"}),
]

KEYWORD_GROUPS = {
    "honor_society": [
        "honor society", "honor societies", "society invitation", "academic society",
        "worth joining", "worth it", "membership fee", "legit", "scam",
    ],
    "career_resume": [
        "resume booster", "resume", "career resources", "career advice", "career help",
        "networking", "student organization", "student organizations", "leadership",
        "certificate", "certification", "club", "clubs", "internship", "internships",
    ],
    "scholarship_membership": [
        "scholarship", "scholarships", "membership", "join", "paying", "fee",
        "college students", "student membership",
    ],
}

EXCLUDE_PATTERNS = [
    r"\bSCLA\b",
    r"the\s+SCLA",
    r"thescla\.org",
    r"Society\s+for\s+Collegiate\s+Leadership",
    r"Collegiate\s+Leadership\s+&\s+Achievement",
]

STRICT_SUBREDDIT_FLAGS = {
    "college": "high - self-promotion rules are strict",
    "jobs": "high - self-promotion/job services usually removed",
    "resumes": "high - no advertising/services",
    "GetEmployed": "high - self-promotion rules are strict",
    "GradSchool": "high - advertising/spam rules are strict",
    "careerguidance": "high - link/ad rules are strict",
    "careeradvice": "medium/high - ask mods before advertising",
    "findapath": "medium - offsite resources may need mod clearance",
    "cscareerquestions": "medium/high - safer only in self-promo threads",
}

@dataclass
class RedditThread:
    source: str
    subreddit: str
    query_group: str
    query: str
    title: str
    permalink: str
    score: int
    num_comments: int
    created_utc: str
    author: str
    selftext_preview: str
    matched_terms: str
    suggested_angle: str
    risk_level: str
    relevance_score: int


def clean_text(value: object, max_len: int = 300) -> str:
    text = str(value or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


def contains_excluded(text: str) -> bool:
    return any(re.search(pattern, text, flags=re.I) for pattern in EXCLUDE_PATTERNS)


def find_matches(title: str, selftext: str) -> tuple[str, list[str]]:
    haystack = f"{title} {selftext}".lower()
    best_group = "general"
    matches: list[str] = []
    for group, terms in KEYWORD_GROUPS.items():
        group_matches = [term for term in terms if term in haystack]
        if len(group_matches) > len(matches):
            matches = group_matches
            best_group = group
    return best_group, matches


def suggest_angle(group: str, terms: Iterable[str]) -> str:
    s = set(terms)
    if group == "honor_society" or "honor society" in s or "legit" in s or "scam" in s:
        return "Compare cost, actual benefits, scholarships, career tools, networking, and independent student feedback."
    if "resume" in s or "leadership" in s or "certificate" in s or "certification" in s:
        return "Focus on concrete resume value: leadership proof, projects, networking, career support, or scholarship access."
    if "career resources" in s or "networking" in s or "internship" in s:
        return "Share neutral student-career resource comparison advice, not a hard recommendation."
    return "Helpful neutral advice; link only if rules allow it and it directly answers the thread."


def calc_score(post: dict, terms: list[str], group: str, listing_mode: str) -> int:
    score = int(post.get("score") or 0)
    comments = int(post.get("num_comments") or 0)
    base = min(score, 150) + min(comments * 4, 220) + len(terms) * 14
    if group == "honor_society":
        base += 70
    elif group == "career_resume":
        base += 35
    if listing_mode == "new":
        base += 20
    created = int(post.get("created_utc") or 0)
    if created:
        age_days = max(0, (dt.datetime.now(dt.timezone.utc).timestamp() - created) / 86400)
        if age_days <= 14:
            base += 60
        elif age_days <= 90:
            base += 30
        elif age_days > 730:
            base -= 50
    return int(base)


def request_listing(session: requests.Session, base_url: str, subreddit: str, mode: str, params_extra: dict, limit: int, diagnostics: list[dict]) -> Optional[dict]:
    url = f"{base_url}/r/{subreddit}/{mode}.json"
    params = {"limit": min(limit, 100), "raw_json": "1"}
    params.update(params_extra)
    try:
        resp = session.get(url, params=params, timeout=15)
        diagnostics.append({
            "base_url": base_url,
            "subreddit": subreddit,
            "mode": mode,
            "status": resp.status_code,
            "content_type": resp.headers.get("content-type", ""),
            "url": url,
        })
        if resp.status_code == 200:
            try:
                return resp.json()
            except Exception:
                return None
        return None
    except Exception as exc:
        diagnostics.append({"base_url": base_url, "subreddit": subreddit, "mode": mode, "status": "exception", "error": str(exc), "url": url})
        return None


def extract_posts(data: Optional[dict]) -> list[dict]:
    if not data:
        return []
    children = data.get("data", {}).get("children", [])
    return [child.get("data", {}) for child in children if child.get("kind") == "t3"]


def add_post(results: list[RedditThread], seen: set[str], source: str, post: dict, listing_mode: str) -> None:
    title = clean_text(post.get("title"), 500)
    selftext = clean_text(post.get("selftext"), 1500)
    subreddit = str(post.get("subreddit") or "")
    combined = f"{title} {selftext} {post.get('url', '')} {subreddit}"
    permalink = post.get("permalink") or ""
    if not permalink or permalink in seen:
        return
    seen.add(permalink)
    if contains_excluded(combined):
        return

    group, terms = find_matches(title, selftext)
    if not terms:
        return

    created_ts = int(post.get("created_utc") or 0)
    created_iso = dt.datetime.fromtimestamp(created_ts, tz=dt.timezone.utc).isoformat() if created_ts else ""
    relevance = calc_score(post, terms, group, listing_mode)
    full_permalink = permalink if permalink.startswith("http") else "https://www.reddit.com" + permalink

    results.append(
        RedditThread(
            source=source,
            subreddit=subreddit,
            query_group=group,
            query=listing_mode,
            title=title,
            permalink=full_permalink,
            score=int(post.get("score") or 0),
            num_comments=int(post.get("num_comments") or 0),
            created_utc=created_iso,
            author=str(post.get("author") or ""),
            selftext_preview=clean_text(selftext, 350),
            matched_terms=", ".join(terms),
            suggested_angle=suggest_angle(group, terms),
            risk_level=STRICT_SUBREDDIT_FLAGS.get(subreddit, "unknown/medium - check rules before linking"),
            relevance_score=relevance,
        )
    )


def scan(per_listing_limit: int, max_results: int, delay: float) -> tuple[list[RedditThread], list[dict], list[dict]]:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    seen: set[str] = set()
    results: list[RedditThread] = []
    diagnostics: list[dict] = []
    query_summary: list[dict] = []

    for subreddit in SUBREDDITS:
        for mode, params_extra in LISTING_MODES:
            random.shuffle(MIRRORS)
            posts: list[dict] = []
            used_source = ""
            for base_url in MIRRORS:
                print(f"Fetch {base_url}/r/{subreddit}/{mode}.json", flush=True)
                data = request_listing(session, base_url, subreddit, mode, params_extra, per_listing_limit, diagnostics)
                posts = extract_posts(data)
                if posts:
                    used_source = base_url
                    break
                time.sleep(delay)

            query_summary.append({
                "subreddit": subreddit,
                "mode": mode,
                "params": json.dumps(params_extra),
                "posts_returned": len(posts),
                "used_source": used_source,
            })
            for post in posts:
                add_post(results, seen, used_source or "no_source", post, mode)
            time.sleep(delay)

    results.sort(key=lambda x: x.relevance_score, reverse=True)
    return results[:max_results], diagnostics, query_summary


def write_csv_dicts(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("empty\n", encoding="utf-8")
        return
    fieldnames = sorted({key for row in rows for key in row.keys()})
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_md(results: list[RedditThread], path: Path) -> None:
    lines = [
        "# SCLA Reddit Research Opportunities",
        "",
        "Filtered from recent/top subreddit listings for education/career/honor-society related threads that do not already mention SCLA/thescla.org.",
        "Manual review required. Check each subreddit rule before posting any link.",
        "",
    ]
    for i, item in enumerate(results, 1):
        lines.extend([
            f"## {i}. r/{item.subreddit} — {item.title}",
            "",
            f"- URL: {item.permalink}",
            f"- Score/comments: {item.score} / {item.num_comments}",
            f"- Created: {item.created_utc}",
            f"- Source/listing: {item.source} / {item.query}",
            f"- Matched terms: {item.matched_terms}",
            f"- Risk: {item.risk_level}",
            f"- Suggested angle: {item.suggested_angle}",
            f"- Preview: {item.selftext_preview}",
            "",
        ])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="research_output")
    parser.add_argument("--per-query-limit", "--per-listing-limit", dest="per_listing_limit", type=int, default=100)
    parser.add_argument("--max-results", type=int, default=250)
    parser.add_argument("--delay", type=float, default=0.35)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    results, diagnostics, query_summary = scan(args.per_listing_limit, args.max_results, args.delay)
    rows = [asdict(r) for r in results]

    write_csv_dicts(rows, output_dir / "scla_reddit_opportunities.csv")
    (output_dir / "scla_reddit_opportunities.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    write_md(results, output_dir / "scla_reddit_opportunities.md")
    write_csv_dicts(query_summary, output_dir / "query_summary.csv")
    write_csv_dicts(diagnostics, output_dir / "request_diagnostics.csv")

    print(f"Saved {len(results)} opportunities to {output_dir}", flush=True)
    print(f"Diagnostics rows: {len(diagnostics)} | Query summary rows: {len(query_summary)}", flush=True)


if __name__ == "__main__":
    main()
