#!/usr/bin/env python3
"""
SCLA Reddit research scanner.

Purpose:
- Find Reddit threads related to education/career/honor-society style questions.
- Exclude threads that already mention SCLA / thescla.org.
- Score opportunities for manual review.
- Export CSV + Markdown reports.

This does not post comments or automate engagement.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Optional

import requests

USER_AGENT = "Mozilla/5.0 (compatible; SCLAResearchBot/1.0; +https://github.com/mersano/reddit-universal-scraper)"

SUBREDDITS = [
    "college",
    "careerguidance",
    "careeradvice",
    "findapath",
    "GetEmployed",
    "jobs",
    "resumes",
    "GradSchool",
    "AskAcademia",
    "ApplyingToCollege",
    "csMajors",
    "cscareerquestions",
    "student",
    "students",
    "internships",
    "resume",
    "LifeAfterSchool",
    "collegeadvice",
]

QUERY_GROUPS = {
    "honor_society": [
        "honor society worth joining",
        "honor society invitation legit",
        "college honor society worth it",
        "pay for honor society",
        "leadership honor society",
    ],
    "career_resources": [
        "career resources for college students",
        "career development student organization",
        "student career resources networking",
        "college networking career help",
        "career readiness certificate",
    ],
    "resume_booster": [
        "resume booster college student",
        "student organization resume worth it",
        "leadership certificate resume",
        "what looks good on resume college",
        "clubs organizations resume college",
    ],
    "scholarships_membership": [
        "membership scholarships college students",
        "student organization membership fee worth it",
        "scholarships networking college organization",
        "college membership worth paying",
    ],
    "legitimacy_reviews": [
        "is this honor society legit",
        "how to tell if honor society is legit",
        "honor society reviews",
        "college society invitation scam",
        "should I join this honor society",
    ],
}

EXCLUDE_PATTERNS = [
    r"\bSCLA\b",
    r"the\s+SCLA",
    r"thescla\.org",
    r"Society\s+for\s+Collegiate\s+Leadership",
    r"Collegiate\s+Leadership\s+&\s+Achievement",
]

# Communities where direct links/promotional comments are usually risky. This is used only
# as a review flag, not as a ban list.
STRICT_SUBREDDIT_FLAGS = {
    "college": "high - self-promotion rules are strict",
    "jobs": "high - self-promotion/job services usually removed",
    "resumes": "high - no advertising/services",
    "GetEmployed": "high - self-promotion rules are strict",
    "GradSchool": "high - advertising/spam rules are strict",
    "AskAcademia": "high - pitch/promo risk",
    "careerguidance": "high - link/ad rules are strict",
    "careeradvice": "medium/high - ask mods before advertising",
    "findapath": "medium - offsite resources may need mod clearance",
    "cscareerquestions": "medium/high - safer only in self-promo threads",
}

@dataclass
class RedditThread:
    subreddit: str
    query_group: str
    query: str
    title: str
    url: str
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


def request_json(session: requests.Session, url: str, params: dict, retries: int = 3) -> Optional[dict]:
    for attempt in range(retries):
        try:
            resp = session.get(url, params=params, timeout=20)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in {403, 429, 503}:
                time.sleep(4 + attempt * 4)
                continue
            print(f"WARN HTTP {resp.status_code} for {url} {params}")
            return None
        except Exception as exc:
            print(f"WARN request failed: {exc}")
            time.sleep(2 + attempt * 2)
    return None


def clean_text(value: object, max_len: int = 300) -> str:
    text = str(value or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


def contains_excluded(text: str) -> bool:
    return any(re.search(pattern, text, flags=re.I) for pattern in EXCLUDE_PATTERNS)


def matched_terms(title: str, selftext: str) -> list[str]:
    haystack = f"{title} {selftext}".lower()
    terms = [
        "honor society", "worth joining", "legit", "scam", "resume", "career", "networking",
        "student organization", "membership", "fee", "scholarship", "leadership", "certificate",
        "invitation", "review", "college", "internship",
    ]
    return [term for term in terms if term in haystack]


def suggest_angle(terms: Iterable[str]) -> str:
    terms_set = set(terms)
    if "honor society" in terms_set or "invitation" in terms_set or "legit" in terms_set:
        return "Compare membership cost, actual benefits, scholarships, career tools, and independent student feedback."
    if "resume" in terms_set or "leadership" in terms_set or "certificate" in terms_set:
        return "Focus on whether the experience gives concrete resume value: projects, leadership proof, networking, or career support."
    if "career" in terms_set or "networking" in terms_set or "internship" in terms_set:
        return "Share practical student-career resources and suggest comparing multiple options before paying."
    return "Helpful neutral advice; only mention external resources if directly useful and allowed by subreddit rules."


def calc_score(post: dict, terms: list[str], group: str) -> int:
    score = int(post.get("score") or 0)
    comments = int(post.get("num_comments") or 0)
    base = min(score, 100) + min(comments * 3, 150) + len(terms) * 12
    if group in {"honor_society", "legitimacy_reviews"}:
        base += 35
    created = int(post.get("created_utc") or 0)
    if created:
        age_days = max(0, (dt.datetime.now(dt.timezone.utc).timestamp() - created) / 86400)
        if age_days <= 14:
            base += 40
        elif age_days <= 60:
            base += 20
        elif age_days > 365:
            base -= 30
    return int(base)


def reddit_search(session: requests.Session, subreddit: str, query: str, limit: int) -> list[dict]:
    url = f"https://www.reddit.com/r/{subreddit}/search.json"
    params = {
        "q": query,
        "restrict_sr": "1",
        "sort": "new",
        "t": "year",
        "limit": min(limit, 100),
        "raw_json": "1",
    }
    data = request_json(session, url, params)
    if not data:
        return []
    children = data.get("data", {}).get("children", [])
    return [child.get("data", {}) for child in children if child.get("kind") == "t3"]


def scan(per_query_limit: int, max_results: int) -> list[RedditThread]:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    seen: set[str] = set()
    results: list[RedditThread] = []

    for subreddit in SUBREDDITS:
        for group, queries in QUERY_GROUPS.items():
            for query in queries:
                print(f"Searching r/{subreddit}: {query}")
                posts = reddit_search(session, subreddit, query, per_query_limit)
                for post in posts:
                    title = clean_text(post.get("title"), 500)
                    selftext = clean_text(post.get("selftext"), 1500)
                    combined = f"{title} {selftext} {post.get('url', '')}"
                    permalink = post.get("permalink") or ""
                    if not permalink or permalink in seen:
                        continue
                    seen.add(permalink)
                    if contains_excluded(combined):
                        continue
                    terms = matched_terms(title, selftext)
                    if len(terms) < 2:
                        continue
                    created_ts = int(post.get("created_utc") or 0)
                    created_iso = dt.datetime.fromtimestamp(created_ts, tz=dt.timezone.utc).isoformat() if created_ts else ""
                    risk = STRICT_SUBREDDIT_FLAGS.get(subreddit, "unknown/medium - check rules before linking")
                    relevance = calc_score(post, terms, group)
                    results.append(
                        RedditThread(
                            subreddit=subreddit,
                            query_group=group,
                            query=query,
                            title=title,
                            url=post.get("url") or "",
                            permalink="https://www.reddit.com" + permalink,
                            score=int(post.get("score") or 0),
                            num_comments=int(post.get("num_comments") or 0),
                            created_utc=created_iso,
                            author=str(post.get("author") or ""),
                            selftext_preview=clean_text(selftext, 350),
                            matched_terms=", ".join(terms),
                            suggested_angle=suggest_angle(terms),
                            risk_level=risk,
                            relevance_score=relevance,
                        )
                    )
                time.sleep(1.5)

    results.sort(key=lambda x: x.relevance_score, reverse=True)
    return results[:max_results]


def write_csv(results: list[RedditThread], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(results[0]).keys()) if results else ["empty"])
        writer.writeheader()
        for item in results:
            writer.writerow(asdict(item))


def write_json(results: list[RedditThread], path: Path) -> None:
    path.write_text(json.dumps([asdict(r) for r in results], indent=2, ensure_ascii=False), encoding="utf-8")


def write_md(results: list[RedditThread], path: Path) -> None:
    lines = [
        "# SCLA Reddit Research Opportunities",
        "",
        "Filtered for education/career/honor-society related threads that do not already mention SCLA/thescla.org.",
        "Use this as a manual review list. Check each subreddit rule before posting links.",
        "",
    ]
    for i, item in enumerate(results, 1):
        lines.extend([
            f"## {i}. r/{item.subreddit} — {item.title}",
            "",
            f"- URL: {item.permalink}",
            f"- Score/comments: {item.score} / {item.num_comments}",
            f"- Created: {item.created_utc}",
            f"- Query group: {item.query_group}",
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
    parser.add_argument("--per-query-limit", type=int, default=50)
    parser.add_argument("--max-results", type=int, default=250)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    results = scan(args.per_query_limit, args.max_results)
    if results:
        write_csv(results, output_dir / "scla_reddit_opportunities.csv")
    else:
        (output_dir / "scla_reddit_opportunities.csv").write_text("empty\n", encoding="utf-8")
    write_json(results, output_dir / "scla_reddit_opportunities.json")
    write_md(results, output_dir / "scla_reddit_opportunities.md")

    print(f"Saved {len(results)} results to {output_dir}")


if __name__ == "__main__":
    main()
