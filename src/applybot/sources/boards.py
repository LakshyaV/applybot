"""Poll companies' own public job boards (Greenhouse, Ashby, Lever) for intern roles.

Read-only JSON endpoints that the boards publish for exactly this purpose: no login, no browser, no scraping of
rendered pages. Boards come from two places: every (ats, board) already seen in the tracker, and the curated
`companies.yaml` next to this file (labs and startups the list repos tend to miss).
"""

from __future__ import annotations

import calendar
import html
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import yaml

from .. import models as m
from ..filters import INTERN_RE, SEASON_RE, is_relevant_title
from .github_repo import build_job, infer_category

COMPANIES_PATH = Path(__file__).with_name("companies.yaml")
POLLED = ("greenhouse", "ashby", "lever")
HEADERS = {"User-Agent": "applybot (personal internship tracker)"}
YEAR_RE = re.compile(r"\b(20\d{2})\b")

# A whole company board lists every discipline, so "engineer" alone is not enough here (the list repos are already
# curated to tech). A title qualifies on a software / ML / data / quant word, or on a bare "Engineering Intern" with
# no other discipline named.
STRONG_RE = re.compile(
    r"\b(software|swe|sde|developer|programmer|machine learning|ml|ai|deep learning|data (scien\w*|engineer\w*|analy\w*)|"
    r"nlp|computer vision|autonomy|perception|infrastructure|platform|back.?end|front.?end|full.?stack|mobile|ios|"
    r"android|cloud|devops|sre|site reliability|security|compiler|quant\w*|algorithm\w*|gpu|cuda|computer science|web|"
    r"research (scientist|engineer)|applied scien\w*|member of technical staff|technical staff)\b",
    re.I,
)
OFF_FIELD_RE = re.compile(
    r"\b(civil|structural|mechanical|electrical|environmental|water|wastewater|transportation|geotech\w*|chemical|"
    r"manufactur\w*|construction|survey\w*|architectur\w*|hvac|plumbing|nuclear|aerospace|propulsion|thermal|"
    r"materials?|process|quality|supply chain|field|traffic|bridge|roadway|land|mep|fire|lighting|avionics|gnc|launch|"
    r"fluids?|welding|tooling|facilities|utilities|mining|geolog\w*|reliability|power|energy|rf|optical|photonics|"
    r"mechatronics|industrial|biomedical|bio\w*|lab|laboratory|clinical|sales|marketing|finance|accounting|legal|"
    r"recruit\w*|policy|operations|business|customer|hardware|fpga|asic|silicon|circuit|test|integration|"
    r"production|vehicle|flight|mission|satellite|spacecraft|battery|cell|drilling|safety|planning)\b",
    re.I,
)


# "Fellow" and "student" also name gig work and chip-design roles that are not software internships.
NOT_TARGET_RE = re.compile(r"human frontier collective|contract student worker|physical design|\bic design", re.I)


def is_target_title(title: str) -> bool:
    if not INTERN_RE.search(title) or NOT_TARGET_RE.search(title):
        return False
    return bool(STRONG_RE.search(title)) or (is_relevant_title(title) and not OFF_FIELD_RE.search(title))


def curated_boards() -> dict[tuple[str, str], str]:
    """{(ats, board): company name} from companies.yaml. A company may list several candidate boards."""
    out = {}
    for name, boards in (yaml.safe_load(COMPANIES_PATH.read_text()) or {}).items():
        for spec in boards:
            ats, _, board = spec.partition(":")
            if ats in POLLED and board:
                out[(ats, board.lower())] = name
    return out


def season_terms(title: str, description: str, target: str) -> tuple[list[str], bool]:
    """(terms, inferred). Boards carry no term field, so the season is read from the posting itself.

    The title wins. In the description only seasons up to the target year count: "graduating Spring 2028" says
    nothing about when the internship runs. A bare year in the title ("2026 Software Intern") also decides.
    """
    target_year = int(target.split()[-1])
    found = _seasons(title)
    if not found:
        years = {int(y) for y in YEAR_RE.findall(title)}
        if years and target_year not in years:
            return [str(min(years))], False
        found = {s for s in _seasons(description) if int(s.split()[-1]) <= target_year}
    return sorted(found), not found


def _seasons(text: str) -> set[str]:
    out = set()
    for season, year in SEASON_RE.findall(text or ""):
        year = year if len(year) == 4 else "20" + year
        out.add(f"{season.capitalize().replace('Autumn', 'Fall')} {year}")
    return out


def _iso_epoch(text: str | None) -> int:
    m_ = re.match(r"(\d{4})-(\d{2})-(\d{2})", text or "")
    return calendar.timegm((int(m_[1]), int(m_[2]), int(m_[3]), 12, 0, 0)) if m_ else 0


def _plain(markup: str) -> str:
    return re.sub(r"<[^>]+>", " ", html.unescape(markup or ""))


# --- one fetcher per board type: returns None when the board does not exist --------------------------------


def _greenhouse(client: httpx.Client, board: str, name: str | None, target: str, source: str) -> list[m.Job] | None:
    base = f"https://boards-api.greenhouse.io/v1/boards/{board}"
    resp = client.get(f"{base}/jobs")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    jobs = []
    for row in resp.json().get("jobs", []):
        if not is_target_title(row.get("title", "")):
            continue
        detail = client.get(f"{base}/jobs/{row['id']}")  # only intern rows: the description decides the season
        description = _plain(detail.json().get("content", "")) if detail.status_code == 200 else ""
        terms, inferred = season_terms(row["title"], description, target)
        jobs.append(
            build_job(
                company=name or row.get("company_name") or board, title=row["title"],
                raw_url=f"https://job-boards.greenhouse.io/{board}/jobs/{row['id']}",
                locations=[(row.get("location") or {}).get("name", "")], source=source,
                category=infer_category(row["title"]), terms=terms, season_inferred=inferred,
                date_posted=_iso_epoch(row.get("first_published") or row.get("updated_at")),
            )
        )  # fmt: skip
    return jobs


def _ashby(client: httpx.Client, board: str, name: str | None, target: str, source: str) -> list[m.Job] | None:
    resp = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{board}")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    jobs = []
    for row in resp.json().get("jobs", []):
        if not row.get("isListed", True) or not is_target_title(row.get("title", "")):
            continue
        terms, inferred = season_terms(row["title"], row.get("descriptionPlain", ""), target)
        locations = [row.get("location") or ""] + [s.get("location", "") for s in row.get("secondaryLocations") or []]
        if row.get("isRemote") and not any("remote" in loc.lower() for loc in locations):
            locations.append("Remote")
        jobs.append(
            build_job(
                company=name or board, title=row["title"], raw_url=row["jobUrl"], locations=locations, source=source,
                category=infer_category(row["title"]), terms=terms, season_inferred=inferred,
                date_posted=_iso_epoch(row.get("publishedAt")),
            )
        )  # fmt: skip
    return jobs


def _lever(client: httpx.Client, board: str, name: str | None, target: str, source: str) -> list[m.Job] | None:
    resp = client.get(f"https://api.lever.co/v0/postings/{board}", params={"mode": "json"})
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    jobs = []
    for row in resp.json():
        if not is_target_title(row.get("text", "")):
            continue
        categories = row.get("categories") or {}
        terms, inferred = season_terms(row["text"], row.get("descriptionPlain", ""), target)
        jobs.append(
            build_job(
                company=name or board, title=row["text"], raw_url=row["hostedUrl"],
                locations=categories.get("allLocations") or [categories.get("location", "")], source=source,
                category=infer_category(row["text"]), terms=terms, season_inferred=inferred,
                date_posted=int((row.get("createdAt") or 0) / 1000),
            )
        )  # fmt: skip
    return jobs


FETCHERS = {"greenhouse": _greenhouse, "ashby": _ashby, "lever": _lever}


def poll(boards: dict[tuple[str, str], str | None], target: str, workers: int = 8) -> tuple[list[m.Job], list[str], list[str]]:
    """Poll every board. Returns (jobs, missing boards, boards that errored). One bad board never stops the sweep."""
    jobs: list[m.Job] = []
    missing: list[str] = []
    errored: list[str] = []

    def one(item: tuple[tuple[str, str], str | None]) -> tuple[str, str, list[m.Job] | None | Exception]:
        (ats, board), name = item
        error: Exception = RuntimeError("not polled")
        for attempt in (1, 2):
            try:
                with httpx.Client(timeout=20, headers=HEADERS, follow_redirects=True) as client:
                    return ats, board, FETCHERS[ats](client, board, name, target, f"board:{ats}:{board}")
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                error = exc
                if attempt == 1:
                    time.sleep(2)
        return ats, board, error

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for ats, board, result in pool.map(one, boards.items()):
            if result is None:
                missing.append(f"{ats}:{board}")
            elif isinstance(result, Exception):
                errored.append(f"{ats}:{board}")
            else:
                jobs.extend(result)
    return jobs, sorted(missing), sorted(errored)
