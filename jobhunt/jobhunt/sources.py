"""Reads public job listings (Greenhouse, Lever, Ashby, Workday) and filters postings.

These are the JSON feeds behind the companies' own careers pages. No login is involved.
"""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

from .config import Search

USER_AGENT = "jobhunt/0.1"


class FeedError(Exception):
    code: int | None = None
    retry_after: str | None = None


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


def _get_json(url: str, body: dict | None = None):
    """GET, or POST when a JSON body is given."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        error = FeedError(f"HTTP {e.code}")
        error.code, error.retry_after = e.code, e.headers.get("Retry-After")
        raise error from e
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


def _has(needle: str, haystack: str) -> bool:
    """Whole-word match (plural allowed), so "intern" hits "interns" but not "internal"."""
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}s?(?![a-z0-9])", haystack) is not None


def title_matches(title: str, search: Search) -> bool:
    title = title.lower()
    return any(_has(t, title) for t in search.titles) and not any(_has(t, title) for t in search.exclude_titles)


# ---- Workday -------------------------------------------------------------------------------
# A Workday careers site is named by its address, e.g. osu.wd1.myworkdayjobs.com/OSUCareers.
# It lists 20 jobs per request, newest first, gives the description only on each job's own page,
# and pauses callers that ask too fast. So the reader goes slowly and asks for as little as it can:
#   - it stops paging at postings older than 30 days (Workday only says "30+ days ago" past that),
#   - it stops at a page of postings it has already seen on an earlier run,
#   - it opens a job's own page only when the title matches the search.

WORKDAY_PAGE = 20
WORKDAY_MAX = 6000
WORKDAY_OLD = "30+"                    # as in "Posted 30+ Days Ago"
WORKDAY_GAP = 1.0                      # seconds between requests
WORKDAY_WAITS = (5, 10, 20, 40, 60)    # pauses before retrying after "too many requests"
_workday_lock = threading.Lock()


def _workday_json(url: str, body: dict | None = None):
    """One Workday request at a time, spaced out, retried when Workday says to slow down."""
    for wait in (*WORKDAY_WAITS, None):
        with _workday_lock:
            time.sleep(WORKDAY_GAP)
            try:
                return _get_json(url, body)
            except FeedError as e:
                if e.code != 429 or wait is None:
                    raise
                pause = int(e.retry_after) if (e.retry_after or "").isdigit() else wait
            time.sleep(min(pause, 120))


def workday_token(text: str) -> str:
    """A careers-site URL or `host/site` -> "host/site". Raises ValueError if it is not Workday."""
    text = re.sub(r"^https?://", "", text.strip())
    host, _, path = text.partition("/")
    parts = [p for p in path.split("?")[0].split("/") if p and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)]
    if not host.lower().endswith(".myworkdayjobs.com") or not parts:
        raise ValueError(text)
    return f"{host.lower()}/{parts[0]}"


def _workday_api(token: str) -> str:
    host, _, site = token.partition("/")
    return f"https://{host}/wday/cxs/{host.split('.')[0]}/{urllib.parse.quote(site)}"


def _workday_page(token: str, offset: int) -> dict:
    return _workday_json(f"{_workday_api(token)}/jobs",
                         {"appliedFacets": {}, "limit": WORKDAY_PAGE, "offset": offset, "searchText": ""})


def workday_total(token: str) -> int:
    return int(_workday_page(token, 0).get("total") or 0)


def parse_workday(token: str, detail: dict) -> Job:
    info = detail.get("jobPostingInfo") or {}
    places = [info.get("location") or ""] + list(info.get("additionalLocations") or [])
    places.append((info.get("country") or {}).get("descriptor") or "")
    location = "; ".join(dict.fromkeys(p for p in places if p))
    url = info.get("externalUrl") or ""
    remote_type = (info.get("remoteType") or "").lower()
    return Job(
        id=f"workday:{token}:{info.get('jobPostingId') or info.get('id')}", source="workday", company=token,
        company_name=(detail.get("hiringOrganization") or {}).get("name") or token.split(".")[0],
        title=(info.get("title") or "").strip(), location=location,
        remote=("remote" in remote_type and "hybrid" not in remote_type) or "remote" in location.lower(),
        url=url, apply_url=f"{url}/apply" if url else "",
        description=html_to_text(info.get("jobDescription") or ""), posted_at=_date(info.get("startDate")),
    )


def fetch_workday(token: str, search: Search, seen: set[str]) -> tuple[int, list[Job], list[str]]:
    """(open postings on the site, details of new title matches, paths of every new posting read).

    `seen` holds the paths read on earlier runs. The caller saves the returned paths only once
    this returns, so a run that fails part-way is simply repeated next time.
    """
    total, offset, fresh, wanted = None, 0, [], []
    while total is None or offset < min(total, WORKDAY_MAX):
        data = _workday_page(token, offset)
        if total is None:  # Workday reports the total on the first page only
            total = int(data.get("total") or 0)
        postings = [p for p in data.get("jobPostings") or [] if p.get("title") and p.get("externalPath")]
        new = [p for p in postings if p["externalPath"] not in seen]
        recent = [p for p in new if WORKDAY_OLD not in (p.get("postedOn") or "")]
        fresh += [p["externalPath"] for p in new]
        wanted += [p for p in recent if title_matches(p["title"], search)]
        # Newest first: once a whole page is old or already seen, the rest is too.
        if not postings or not recent:
            break
        offset += WORKDAY_PAGE
    api = _workday_api(token)
    jobs = [parse_workday(token, _workday_json(api + p["externalPath"])) for p in wanted]
    return total or 0, jobs, fresh


def fetch_company(source: str, token: str, search: Search | None = None,
                  seen: set[str] | None = None) -> tuple[int, list[Job], list[str]]:
    """(open postings, jobs, newly read Workday paths). Workday needs `search` and returns only
    new title matches; the other boards return everything and the caller filters."""
    if source == "workday":
        if search is None:
            return workday_total(token), [], []
        return fetch_workday(token, search, seen or set())
    url, parse = FEEDS[source]
    jobs = parse(token, _get_json(url.format(urllib.parse.quote(token))))
    return len(jobs), jobs, []


def matches(job: Job, search: Search, today: datetime | None = None) -> bool:
    if not title_matches(job.title, search):
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
