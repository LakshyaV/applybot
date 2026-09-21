"""Eligibility and priority. Pure functions of (Job, config) so they are trivially testable."""

from __future__ import annotations

import re

from . import models as m
from .normalize import host_matches

INTERN_RE = re.compile(r"\b(intern(ship)?s?|co-?op|student|apprentice|fellow(ship)?|summer analyst)\b", re.I)
TECH_RE = re.compile(
    r"\b(software|swe|sde|developer|engineer(ing)?|programmer|machine learning|\bml\b|\bai\b|deep learning|"
    r"data (scien|engineer|analy)|research|nlp|computer vision|robotics|autonomy|perception|infrastructure|"
    r"platform|backend|front.?end|full.?stack|mobile|ios|android|cloud|devops|sre|security|firmware|embedded|"
    r"compiler|systems|quant|algorithm|gpu|cuda|simulation|applied scien)",
    re.I,
)
SEASON_RE = re.compile(r"\b(winter|spring|summer|fall|autumn)\s*(?:of\s*)?['’]?(20\d{2}|\d{2})\b", re.I)
ADVANCED_RE = re.compile(r"\b(ph\.?d|doctoral|master'?s|\bms\b|mba|graduate student)\b", re.I)
UNDERGRAD_RE = re.compile(r"\b(undergrad\w*|bachelor'?s?|\bbs\b|\bba\b)\b", re.I)


def seasons_in_title(title: str) -> set[str]:
    out = set()
    for season, year in SEASON_RE.findall(title):
        year = year if len(year) == 4 else "20" + year
        out.add(f"{season.capitalize().replace('Autumn', 'Fall')} {year}")
    return out


def eligibility(job: m.Job, cfg: dict) -> tuple[str, str]:
    """Return (status, reason). status is QUEUED when the job should be applied to."""
    target = cfg["target"]["season"]
    rules = cfg["eligibility"]

    if not job.active:
        return m.CLOSED, "source marks it closed"
    if host_matches(job.url, cfg["blocked_hosts"]):
        return m.NEEDS_HUMAN, "blocked host (never automated)"

    titled = seasons_in_title(job.title)
    if titled and target not in titled:
        return m.OFF_SEASON, f"title says {', '.join(sorted(titled))}"
    if job.terms and target not in job.terms and not (titled and target in titled):
        return m.OFF_SEASON, f"terms {job.terms}"

    if rules["skip_citizenship_required"] and job.sponsorship == m.SPONSOR_CITIZEN:
        return m.SKIPPED_INELIGIBLE, "requires US citizenship"
    advanced = job.advanced_degree_only or (ADVANCED_RE.search(job.title) and not UNDERGRAD_RE.search(job.title))
    if rules["skip_advanced_degree_only"] and advanced:
        return m.SKIPPED_INELIGIBLE, "advanced degree only"
    if job.sponsorship == m.SPONSOR_NONE and not rules["apply_no_sponsorship"] and job.countries == ["US"]:
        return m.SKIPPED_INELIGIBLE, "no sponsorship offered"
    return m.QUEUED, ""


def is_relevant_title(title: str) -> bool:
    """Discovery-mode filter (repo mode applies to every eligible row)."""
    return bool(INTERN_RE.search(title) and TECH_RE.search(title))


def priority(job: m.Job, cfg: dict) -> int:
    """Lower = applied to sooner. Ties are broken by newest date_posted in the claim query."""
    score = cfg["category_priority"].get(job.category, cfg["category_priority"]["Other"]) * 10
    country_pri = cfg["country_priority"]
    score += min((country_pri.get(c, country_pri["OTHER"]) for c in job.countries), default=1) * 10
    if len(job.terms) > cfg["target"]["evergreen_terms_threshold"]:
        score += 8
    if job.season_inferred:
        score += 2
    # A "no sponsorship" flag only matters where the applicant needs sponsorship.
    if job.sponsorship == m.SPONSOR_NONE and "CA" not in job.countries:
        score += cfg["eligibility"]["no_sponsorship_penalty"] * 10
    if job.sponsorship == m.SPONSOR_OFFERS:
        score -= 3
    return score
