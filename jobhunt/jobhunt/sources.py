"""Reads public job-board feeds (Greenhouse, Lever, Ashby) and filters postings.

These are the JSON feeds the companies publish for their own careers pages. No login, no scraping.
"""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

from .config import Search

USER_AGENT = "jobhunt/0.1"


class FeedError(Exception):
    pass


@dataclass
class Job:
    id: str            # "greenhouse:stripe:8172510"
    source: str
    company: str       # board token
    company_name: str
    title: str
    location: str
    remote: bool
    url: str           # posting page
    apply_url: str     # application form
    description: str   # plain text
    posted_at: str     # ISO date, or ""


class _Text(HTMLParser):
    BLOCK = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "ul", "ol", "tr"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK:
            self.parts.append("\n- " if tag == "li" else "\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _Text()
    parser.feed(markup or "")
    text = "".join(parser.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _get_json(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        raise FeedError(f"HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise FeedError(str(getattr(e, "reason", e))) from e


def _date(value) -> str:
    """ISO timestamp string or epoch milliseconds -> YYYY-MM-DD ("" if unknown)."""
    if not value:
        return ""
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
    except (ValueError, OSError, OverflowError):
        return ""


def parse_greenhouse(token: str, data: dict) -> list[Job]:
    jobs = []
    for j in data.get("jobs", []):
        location = (j.get("location") or {}).get("name") or ""
        jobs.append(Job(
            id=f"greenhouse:{token}:{j['id']}", source="greenhouse", company=token,
            company_name=j.get("company_name") or token, title=(j.get("title") or "").strip(),
            location=location, remote="remote" in location.lower(),
            url=j.get("absolute_url") or "",
            # Company careers sites embed this same hosted form, so it works for every Greenhouse board.
            apply_url=f"https://job-boards.greenhouse.io/embed/job_app?for={urllib.parse.quote(token)}&token={j['id']}",
            description=html_to_text(html.unescape(j.get("content") or "")),
            posted_at=_date(j.get("first_published") or j.get("updated_at")),
        ))
    return jobs


def parse_lever(token: str, data: list) -> list[Job]:
    jobs = []
    for j in data:
        categories = j.get("categories") or {}
        locations = categories.get("allLocations") or [categories.get("location") or ""]
        location = "; ".join(x for x in locations if x)
        parts = [j.get("descriptionPlain") or ""]
        for section in j.get("lists") or []:
            parts.append(f"{section.get('text', '')}\n{html_to_text(section.get('content', ''))}")
        parts.append(j.get("additionalPlain") or "")
        jobs.append(Job(
            id=f"lever:{token}:{j['id']}", source="lever", company=token, company_name=token,
            title=(j.get("text") or "").strip(), location=location,
            remote=j.get("workplaceType") == "remote" or "remote" in location.lower(),
            url=j.get("hostedUrl") or "", apply_url=j.get("applyUrl") or "",
            description="\n\n".join(p.strip() for p in parts if p.strip()),
            posted_at=_date(j.get("createdAt")),
        ))
    return jobs


def parse_ashby(token: str, data: dict) -> list[Job]:
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locations = [j.get("location") or ""] + [s.get("location") or "" for s in j.get("secondaryLocations") or []]
        location = "; ".join(x for x in locations if x)
        description = j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml") or "")
        pay = (j.get("compensation") or {}).get("compensationTierSummary")
        if pay:
            description += f"\n\nCompensation: {pay}"
        jobs.append(Job(
            id=f"ashby:{token}:{j['id']}", source="ashby", company=token, company_name=token,
            title=(j.get("title") or "").strip(), location=location,
            # isRemote is also true for hybrid roles, so trust workplaceType when the board gives one.
            remote=(j["workplaceType"] == "Remote" if j.get("workplaceType") else bool(j.get("isRemote")))
            or "remote" in location.lower(),
            url=j.get("jobUrl") or "", apply_url=j.get("applyUrl") or "",
            description=description.strip(), posted_at=_date(j.get("publishedAt")),
        ))
    return jobs


FEEDS = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{}/jobs?content=true", parse_greenhouse),
    "lever": ("https://api.lever.co/v0/postings/{}?mode=json", parse_lever),
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{}?includeCompensation=true", parse_ashby),
}


def fetch_company(source: str, token: str) -> list[Job]:
    url, parse = FEEDS[source]
    return parse(token, _get_json(url.format(urllib.parse.quote(token))))


def _has(needle: str, haystack: str) -> bool:
    """Whole-word match (plural allowed), so "intern" hits "interns" but not "internal"."""
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}s?(?![a-z0-9])", haystack) is not None


def matches(job: Job, search: Search, today: datetime | None = None) -> bool:
    title = job.title.lower()
    if not any(_has(t, title) for t in search.titles):
        return False
    if any(_has(t, title) for t in search.exclude_titles):
        return False
    if search.locations:
        where = job.location.lower() + (" remote" if job.remote else "")
        if not any(_has(loc, where) for loc in search.locations):
            return False
    if job.posted_at and search.max_age_days > 0:
        today = today or datetime.now(timezone.utc)
        if job.posted_at < (today - timedelta(days=search.max_age_days)).date().isoformat():
            return False
    return True
