#!/usr/bin/env python3
"""
SCLA Reddit research scanner v3.

Goal:
- Build a manual-review list of Reddit threads related to education, career, honor societies,
  resume value, networking, scholarships, and membership decisions.
- Exclude threads that already mention SCLA / thescla.org.
- Export opportunities + raw diagnostics so we can see whether Reddit search returned data.

This script does not post comments and does not automate Reddit engagement.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Optional

import requests

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SCLAResearchScanner/3.0 manual-research"

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
]

# Shorter/broader Reddit queries perform better than long exact phrases.
QUERY_GROUPS = {
    "honor_society": [
        "honor society",
        "honor societies",
        "society invitation",
        "worth joining",
        "membership fee",
    ],
    "career_resume": [
        "resume booster",
        "student organization",
        "career resources",
        "networking",
        "leadership certificate",
    ],
    "legitimacy_reviews": [
        "is this legit",
        "legit or scam",
        "reviews",
        "should I join",
        "worth paying",
    ],
    "scholarships": [
        "scholarships",
        "college scholarship",
        "student membership",
    ],
}

GLOBAL_QUERIES = [
    "honor society worth joining",
    "college honor society legit",
    "honor society invitation",
    "student organization resume worth it",
    "career resources for college students",
    "resume booster college student",
    "networking for college students",
    "membership fee worth it college",
    "leadership certificate resume",
    "college scholarship membership",
]

EXCLUDE_PATTERNS = [
    r"\bSCLA\b",
    r"the\s+SCLA",
    r"thescla\.org",
    r"Society\s+for\s+Collegiate\s+Leadership",
    r"Collegiate\s+Leadership\s+&\s+Achievement",
]

STRICT_SUBREDDIT_FLAGS = {
    "college": "high - self-promotion rules are strict",
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


def request_json(session: requests.Session, url: str, params: dict, diagnostics: list[dict], retries: int = 1) -> Optional[dict]:
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, params=params, timeout=12)
            diagnostics.append({
                "url": url,
                "q": params.get("q", ""),
                "status": resp.status_code,
                "attempt": attempt + 1,
                "content_type": resp.headers.get("content-type", ""),
            })
            if resp.status_code == 200:
                return resp.json()
            print(f"WARN HTTP {resp.status_code} for {url} q={params.get('q')}", flush=True)
            if resp.status_code in {429, 503} and attempt < retries:
                time.sleep(2.0)
                continue
            return None
        except Exception as exc:
            diagnostics.append({"url": url, "q": params.get("q", ""), "status": "exception", "error": str(exc)})
            print(f"WARN request failed: {exc}", flush=True)
            if attempt < retries:
                time.sleep(1.0)
    return None


def clean_text(value: object, max_len: int = 300) -> str:
    text = str(value or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


def contains_excluded(text: str) -> bool:
    return any(re.search(pattern, text, flags=re.I) for pattern in EXCLUDE_PATTERNS)


def matched_terms(title: str, selftext: str, query: str) -> list[str]:
    haystack = f"{title} {selftext} {query}".lower()
    terms = [
        "honor society", "honor societies", "society", "worth joining", "legit", "scam",
        "resume", "career", "networking", "student organization", "membership", "fee",
        "scholarship", "leadership", "certificate", "invitation", "review", "college",
        "internship", "job", "paying", "join",
    ]
    return [term for term in terms if term in haystack]


def suggest_angle(terms: Iterable[str]) -> str:
    s = set(terms)
    if "honor society" in s or "honor societies" in s or "invitation" in s or "legit" in s:
        return "Compare cost, actual benefits, scholarships, career tools, networking, and independent student feedback."
    if "resume" in s or "leadership" in s or "certificate" in s:
        return "Focus on concrete resume value: leadership proof, projects, networking, career support, or scholarship access."
    if "career" in s or "networking" in s or "internship" in s:
        return "Share neutral student-career resource comparison advice, not a hard recommendation."
    return "Helpful neutral advice; link only if rules allow it and it directly answers the thread."


def calc_score(post: dict, terms: list[str], group: str) -> int:
    score = int(post.get("score") or 0)
    comments = int(post.get("num_comments") or 0)
    base = min(score, 120) + min(comments * 4, 180) + len(terms) * 12
    if group in {"honor_society", "legitimacy_reviews"}:
        base += 45
    created = int(post.get("created_utc") or 0)
    if created:
        age_days = max(0, (dt.datetime.now(dt.timezone.utc).timestamp() - created) / 86400)
        if age_days <= 14:
            base += 50
        elif age_days <= 90:
            base += 25
        elif age_days > 730:
            base -= 45
    return int(base)


def extract_posts(data: Optional[dict]) -> list[dict]:
    if not data:
        return []
    children = data.get("data", {}).get("children", [])
    return [child.get("data", {}) for child in children if child.get("kind") == "t3"]


def subreddit_search(session: requests.Session, subreddit: str, query: str, limit: int, diagnostics: list[dict], sort: str = "relevance", t: str = "all") -> list[dict]:
    url = f"https://www.reddit.com/r/{subreddit}/search.json"
    params = {
        "q": query,
        "restrict_sr": "1",
        "sort": sort,
        "t": t,
        "limit": min(limit, 50),
        "raw_json": "1",
    }
    return extract_posts(request_json(session, url, params, diagnostics))


def global_search(session: requests.Session, query: str, limit: int, diagnostics: list[dict], sort: str = "relevance", t: str = "all") -> list[dict]:
    url = "https://www.reddit.com/search.json"
    params = {
        "q": query,
        "type": "link",
        "sort": sort,
        "t": t,
        "limit": min(limit, 50),
        "raw_json": "1",
    }
    return extract_posts(request_json(session, url, params, diagnostics))


def add_post(results: list[RedditThread], seen: set[str], source: str, post: dict, group: str, query: str) -> None:
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
    terms = matched_terms(title, selftext, query)
    if not terms:
        return
    created_ts = int(post.get("created_utc") or 0)
    created_iso = dt.datetime.fromtimestamp(created_ts, tz=dt.timezone.utc).isoformat() if created_ts else ""
    relevance = calc_score(post, terms, group)
    results.append(
        RedditThread(
            source=source,
            subreddit=subreddit,
            query_group=group,
            query=query,
            title=title,
            permalink="https://www.reddit.com" + permalink,
            score=int(post.get("score") or 0),
            num_comments=int(post.get("num_comments") or 0),
            created_utc=created_iso,
            author=str(post.get("author") or ""),
            selftext_preview=clean_text(selftext, 350),
            matched_terms=", ".join(terms),
            suggested_angle=suggest_angle(terms),
            risk_level=STRICT_SUBREDDIT_FLAGS.get(subreddit, "unknown/medium - check rules before linking"),
            relevance_score=relevance,
        )
    )


def scan(per_query_limit: int, max_results: int, delay: float) -> tuple[list[RedditThread], list[dict], list[dict]]:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    seen: set[str] = set()
    results: list[RedditThread] = []
    diagnostics: list[dict] = []
    query_summary: list[dict] = []

    # Global search first: this often finds better Reddit threads than subreddit-local search.
    for query in GLOBAL_QUERIES:
        print(f"Global search: {query}", flush=True)
        posts = global_search(session, query, per_query_limit, diagnostics, sort="relevance", t="all")
        query_summary.append({"source": "global", "subreddit": "*", "query": query, "posts_returned": len(posts)})
        for post in posts:
            add_post(results, seen, "global", post, "global", query)
        time.sleep(delay)

    # Subreddit-local search with broad terms.
    for subreddit in SUBREDDITS:
        for group, queries in QUERY_GROUPS.items():
            for query in queries:
                print(f"Subreddit search r/{subreddit}: {query}", flush=True)
                posts = subreddit_search(session, subreddit, query, per_query_limit, diagnostics, sort="relevance", t="all")
                query_summary.append({"source": "subreddit", "subreddit": subreddit, "query_group": group, "query": query, "posts_returned": len(posts)})
                for post in posts:
                    add_post(results, seen, f"r/{subreddit}", post, group, query)
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


def write_opportunities_csv(results: list[RedditThread], path: Path) -> None:
    rows = [asdict(item) for item in results]
    write_csv_dicts(rows, path)


def write_md(results: list[RedditThread], path: Path) -> None:
    lines = [
        "# SCLA Reddit Research Opportunities",
        "",
        "Filtered for education/career/honor-society related threads that do not already mention SCLA/thescla.org.",
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
            f"- Source/query: {item.source} / {item.query}",
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
    parser.add_argument("--per-query-limit", type=int, default=30)
    parser.add_argument("--max-results", type=int, default=250)
    parser.add_argument("--delay", type=float, default=0.20)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    results, diagnostics, query_summary = scan(args.per_query_limit, args.max_results, args.delay)
    rows = [asdict(r) for r in results]

    write_opportunities_csv(results, output_dir / "scla_reddit_opportunities.csv")
    (output_dir / "scla_reddit_opportunities.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    write_md(results, output_dir / "scla_reddit_opportunities.md")
    write_csv_dicts(query_summary, output_dir / "query_summary.csv")
    write_csv_dicts(diagnostics, output_dir / "request_diagnostics.csv")

    print(f"Saved {len(results)} opportunities to {output_dir}", flush=True)
    print(f"Diagnostics rows: {len(diagnostics)} | Query summary rows: {len(query_summary)}", flush=True)


if __name__ == "__main__":
    main()
