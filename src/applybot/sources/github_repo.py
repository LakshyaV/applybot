"""Turn any internship-list GitHub repo into Job rows.

Adapters, tried in order:
  listings_json  .github/scripts/listings.json            (SimplifyJobs, vanshb03)
  yaml_dir       data/*.yaml, one firm per file           (northwesternfintech quant)
  tables         markdown pipe tables / HTML tables in root *.md files (everything else)
"""

from __future__ import annotations

import calendar
import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import httpx
import yaml
from bs4 import BeautifulSoup

from .. import models as m
from ..normalize import canonical_url, countries_of, is_tracker, job_key, secondary_key

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
SKIP_MD = re.compile(r"new.?grad|inactive|off.?season|contributing|license|code_of_conduct|20(1\d|2[0-6])", re.I)
IMAGE_HOSTS = ("img.shields.io", "i.imgur.com", "imgur.com", "camo.githubusercontent.com")
MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_abbr) if name}

SPONSORSHIP = {
    "does not offer sponsorship": m.SPONSOR_NONE,
    "u.s. citizenship is required": m.SPONSOR_CITIZEN,
    "offers sponsorship": m.SPONSOR_OFFERS,
}
CATEGORY_ALIASES = [
    (re.compile(r"quant|trading|trader", re.I), "Quant"),
    (re.compile(r"data|machine learning|\bai\b|\bml\b|research scien|deep learning|nlp|vision", re.I), "AI/ML/Data"),
    (re.compile(r"hardware|fpga|asic|rtl|silicon|electrical|embedded|firmware|mechanical|circuit", re.I), "Hardware"),
    (re.compile(r"product manage|program manage|\bpm\b|\bapm\b|product design", re.I), "Product"),
    (re.compile(r"software|developer|engineer|swe|sde|full.?stack|backend|frontend|devops|security", re.I), "Software"),
]
ROLE_TYPES = {
    "QT": "Quantitative Trader Intern",
    "QR": "Quantitative Researcher Intern",
    "SWE": "Software Engineer Intern",
    "FPGA": "FPGA Engineer Intern",
}


# --- GitHub plumbing ---------------------------------------------------------------------------


@lru_cache
def _client() -> httpx.Client:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "applybot"}
    try:
        token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10).stdout.strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    except (OSError, subprocess.SubprocessError):
        pass
    return httpx.Client(headers=headers, follow_redirects=True, timeout=60)


def parse_repo_url(url: str) -> tuple[str, str]:
    match = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s#?]+)", url.strip())
    if not match:
        raise ValueError(f"not a GitHub repo URL: {url}")
    return match.group(1), re.sub(r"\.git$", "", match.group(2))


def repo_info(owner: str, repo: str) -> dict:
    resp = _client().get(f"{API}/repos/{owner}/{repo}")
    resp.raise_for_status()
    return resp.json()  # follows renames (301), so full_name is the current name


def list_tree(full_name: str, branch: str) -> list[str]:
    resp = _client().get(f"{API}/repos/{full_name}/git/trees/{branch}", params={"recursive": "1"})
    resp.raise_for_status()
    return [item["path"] for item in resp.json()["tree"] if item["type"] == "blob"]


def fetch_raw(full_name: str, branch: str, path: str) -> str:
    resp = _client().get(f"{RAW}/{full_name}/{branch}/{path}")
    resp.raise_for_status()
    return resp.text


# --- shared helpers ----------------------------------------------------------------------------


def infer_category(title: str, given: str = "") -> str:
    for pattern, name in CATEGORY_ALIASES:
        if given and pattern.search(given):
            return name
    if given in ("Software", "AI/ML/Data", "Hardware", "Quant", "Product"):
        return given
    for pattern, name in CATEGORY_ALIASES:
        if pattern.search(title):
            return name
    return "Other"


def parse_date(text: str, now: float | None = None) -> int:
    """'Sep 18, 2026' | 'Aug 21' | '2d' | '45m' | '3w' | '1mo' → epoch seconds (0 if unknown)."""
    now = now or time.time()
    text = text.strip()
    age = re.fullmatch(r"(\d+)\s*(m|min|h|d|w|mo)", text, re.I)
    if age:
        unit = {"m": 60, "min": 60, "h": 3600, "d": 86400, "w": 604800, "mo": 2592000}[age.group(2).lower()]
        return int(now - int(age.group(1)) * unit)
    date = re.fullmatch(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:,?\s*(\d{4}))?", text)
    if date and date.group(1).lower() in MONTHS:
        month, day = MONTHS[date.group(1).lower()], int(date.group(2))
        year = int(date.group(3)) if date.group(3) else time.gmtime(now).tm_year
        stamp = calendar.timegm((year, month, day, 12, 0, 0))
        if not date.group(3) and stamp > now + 86400:  # "Dec 28" seen in January
            stamp = calendar.timegm((year - 1, month, day, 12, 0, 0))
        return stamp
    return 0


def build_job(*, company: str, title: str, raw_url: str, locations: list[str], source: str, **extra) -> m.Job:
    url = canonical_url(raw_url)
    key, ats, board, job_id = job_key(url)
    locations = [loc.strip() for loc in locations if loc and loc.strip()]
    return m.Job(
        company=company.strip(), title=title.strip(), url=url, raw_url=raw_url, locations=locations,
        countries=countries_of(locations), ats=ats, board=board, ats_job_id=job_id, key=key,
        key2=secondary_key(company, title, locations), source=source, **extra,
    )  # fmt: skip


# --- adapter: listings.json --------------------------------------------------------------------


def from_listings_json(text: str, source: str, repo_year: str | None) -> list[m.Job]:
    jobs = []
    for row in json.loads(text):
        if not row.get("url") or row.get("is_visible") is False:
            continue
        if "terms" in row:  # SimplifyJobs
            terms = list(row["terms"] or [])
        else:  # vanshb03: bare season string, year implied by the repo name
            seasons = [s for s in re.split(r"[/,]", row.get("season") or "") if s.strip()]
            terms = [f"{s.strip()} {repo_year}" for s in seasons] if repo_year else []
        degrees = row.get("degrees") or []
        jobs.append(
            build_job(
                company=row["company_name"], title=row["title"], raw_url=row["url"],
                locations=row.get("locations") or [], source=source, source_id=str(row.get("id", "")),
                category=infer_category(row["title"], row.get("category", "")),
                sponsorship=SPONSORSHIP.get((row.get("sponsorship") or "").lower(), m.SPONSOR_UNKNOWN),
                advanced_degree_only=bool(degrees) and "Bachelor's" not in degrees,
                terms=terms, season_inferred=not terms, active=bool(row.get("active", True)),
                date_posted=int(row.get("date_posted") or 0),
            )  # fmt: skip
        )
    return jobs


# --- adapter: yaml_dir -------------------------------------------------------------------------


def from_firm_yaml(text: str, source: str, target_season: str) -> list[m.Job]:
    firm = yaml.safe_load(text) or {}
    locations = firm.get("locations") or []
    if isinstance(locations, str):
        locations = [part.strip() for part in re.split(r"[/;]| and ", locations)]
    jobs = []
    for role in firm.get("roles") or []:
        base = ROLE_TYPES.get(role.get("role_type", ""), f"{role.get('role_type', 'Quant')} Intern")
        for link in role.get("links") or []:
            if not link.get("url"):
                continue
            title = f"{base} ({link['label']})" if link.get("label") else base
            jobs.append(
                build_job(company=firm.get("name", ""), title=title, raw_url=link["url"], locations=locations,
                          source=source, category="Quant", terms=[target_season], season_inferred=True)
            )  # fmt: skip
    return jobs


# --- adapter: tables ---------------------------------------------------------------------------

HEADER_ALIASES = {
    "company": "company",
    "role": "title", "position": "title", "job title": "title", "title": "title",
    "location": "location", "locations": "location",
    "application": "link", "application/link": "link", "apply": "link", "posting": "link", "link": "link",
    "date posted": "date", "posted": "date", "age": "date", "date": "date",
    "visa": "visa", "sponsorship": "visa",
}  # fmt: skip


def _links(cell: str) -> list[str]:
    found = re.findall(r'href="([^"]+)"', cell) + re.findall(r"\]\((https?://[^)\s]+)\)", cell)
    return [u for u in found if not any(host in u for host in IMAGE_HOSTS)]


def _text(cell: str) -> str:
    cell = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", cell)  # markdown images
    cell = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cell)  # markdown links → text
    text = BeautifulSoup(cell, "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", text.replace("**", "")).strip()


def _locations(cell: str) -> list[str]:
    cell = re.sub(r"<summary>.*?</summary>", "", cell, flags=re.S | re.I)
    return [_text(part) for part in re.split(r"</?br\s*/?>", cell, flags=re.I) if _text(part)]


def _md_rows(text: str) -> list[list[str]]:
    """Every pipe-table row as raw cell strings; separator rows are dropped."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or re.fullmatch(r"\|[\s:|-]+\|?", line):
            continue
        rows.append([c.strip() for c in re.split(r"(?<!\\)\|", line.strip("|"))])
    return rows


def _html_rows(text: str) -> list[list[str]]:
    rows = []
    for tr in BeautifulSoup(text, "html.parser").find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if cells:
            rows.append([cell.decode_contents().strip() for cell in cells])
    return rows


def from_tables(text: str, source: str, target_season: str) -> list[m.Job]:
    rows = _md_rows(text)
    if "<tr" in text:
        rows += _html_rows(text)

    jobs, columns, company = [], {}, ""
    for cells in rows:
        header = {HEADER_ALIASES.get(_text(c).lower()): i for i, c in enumerate(cells)}
        if "company" in header and "title" in header:
            columns = {k: v for k, v in header.items() if k}
            company = ""
            continue
        if not columns or len(cells) <= max(columns.values()):
            continue

        company_text = _text(cells[columns["company"]]).replace("🔥", "").strip()
        if company_text not in ("↳", ""):
            company = company_text
        raw_title = _text(cells[columns["title"]])
        title = re.sub(r"[🛂🎓🔒🔥]|🇺🇸", "", raw_title).strip()
        link_cell = cells[columns["link"]] if "link" in columns else cells[columns["title"]]
        links = [u for u in _links(link_cell) if "simplify.jobs/p/" not in u] or _links(cells[columns["title"]])
        closed = "🔒" in link_cell or "🔒" in raw_title
        if not company or not title or not links:  # closed rows without a link carry nothing to track
            continue

        sponsorship = m.SPONSOR_UNKNOWN
        if "🇺🇸" in raw_title:
            sponsorship = m.SPONSOR_CITIZEN
        elif "🛂" in raw_title:
            sponsorship = m.SPONSOR_NONE
        elif "visa" in columns and re.search(r"sponsor", _text(cells[columns["visa"]]), re.I):
            sponsorship = m.SPONSOR_OFFERS
        jobs.append(
            build_job(
                company=company, title=title, raw_url=links[0],
                locations=_locations(cells[columns["location"]]) if "location" in columns else [],
                source=source, category=infer_category(title), sponsorship=sponsorship,
                advanced_degree_only="🎓" in raw_title, terms=[target_season], season_inferred=True,
                active=not closed,
                date_posted=parse_date(_text(cells[columns["date"]])) if "date" in columns else 0,
            )  # fmt: skip
        )
    return jobs


# --- tracker links -----------------------------------------------------------------------------


def resolve_trackers(jobs: list[m.Job], max_workers: int = 4) -> int:
    """Replace list-tracker links (zapply.jobs/l/…) with the employer URL they redirect to."""

    def resolve(job: m.Job) -> bool:
        try:
            with httpx.Client(follow_redirects=True, timeout=30, headers={"User-Agent": "Mozilla/5.0"}) as c:
                final = str(c.head(job.raw_url).url)
                if is_tracker(final):
                    final = str(c.get(job.raw_url).url)
        except httpx.HTTPError:
            return False
        if is_tracker(final):
            return False
        job.url = canonical_url(final)
        job.key, job.ats, job.board, job.ats_job_id = job_key(job.url)
        return True

    tracked = [job for job in jobs if is_tracker(job.url)]
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return sum(pool.map(resolve, tracked))


# --- entry point -------------------------------------------------------------------------------


def ingest(url: str, target_season: str) -> tuple[list[m.Job], str]:
    owner, repo = parse_repo_url(url)
    info = repo_info(owner, repo)
    full_name, branch = info["full_name"], info["default_branch"]
    source = f"github:{full_name}"
    year = re.search(r"20\d{2}", full_name)
    paths = list_tree(full_name, branch)

    if ".github/scripts/listings.json" in paths:
        text = fetch_raw(full_name, branch, ".github/scripts/listings.json")
        return from_listings_json(text, source, year.group(0) if year else None), "listings_json"

    firm_files = [p for p in paths if re.fullmatch(r"data/[^/]+\.ya?ml", p)]
    if len(firm_files) >= 5:
        jobs = []
        for path in firm_files:
            jobs += from_firm_yaml(fetch_raw(full_name, branch, path), source, target_season)
        return jobs, "yaml_dir"

    jobs = []
    for path in paths:
        if "/" not in path and path.lower().endswith(".md") and not SKIP_MD.search(path):
            jobs += from_tables(fetch_raw(full_name, branch, path), source, target_season)
    resolve_trackers(jobs)
    return jobs, "tables"
