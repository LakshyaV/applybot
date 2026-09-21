"""Read-only checks on a posting's own text before any form is touched.

The list repos under-report restrictions (e.g. defense contractors flagged "Other"), so the job
description is the authority. Text here is untrusted data: it is pattern-matched, never obeyed.
"""

from __future__ import annotations

import re

from . import models as m

CITIZENSHIP_RE = re.compile(
    r"(must be|be|is|are) (a )?(u\.?s\.?|united states) citizens?\b|u\.?s\.? citizenship (is )?required|"
    r"citizenship[^.]{0,40}required|(active|obtain|maintain|eligib\w+ for)[^.]{0,40}security clearance|"
    r"\b(secret|ts/sci|top secret) clearance|u\.?s\.? persons? (only|as defined)|itar[^.]{0,60}(u\.?s\.? person|citizen)",
    re.I,
)
NO_SPONSORSHIP_RE = re.compile(
    r"(not|unable to|cannot|can't|won't|will not|does not|do not)[^.]{0,40}(sponsor|visa)|"
    r"without[^.]{0,30}sponsorship|no (visa )?sponsorship",
    re.I,
)
NO_AUTOMATION_RE = re.compile(
    r"(do not|don't|please refrain from|not permitted to)[^.]{0,40}(use|using)[^.]{0,20}\b(ai|chatgpt|llm|generative)|"
    r"(ai|llm|bot|automated)[- ]generated applications?[^.]{0,40}(reject|disqualif|not be considered)|"
    r"(automated|bot) (submissions?|applications?)[^.]{0,40}(prohibit|reject|disqualif)",
    re.I,
)
# Text aimed at language models inside a posting. We never act on it; we just refuse to auto-apply.
INJECTION_RE = re.compile(
    r"if you are an? (ai|llm|language model|bot)|ignore (all )?(previous|prior) instructions|"
    r"(ai|llm) (agents?|assistants?|models?)[^.]{0,40}(must|should|include|mention)",
    re.I,
)
# A degree requirement is "advanced only" when the clause names a graduate degree and no undergraduate one.
# ("pursuing a Bachelor's or Master's" must NOT match — that wrongly skipped real roles once.)
PURSUING_RE = re.compile(r"(pursuing|enrolled in|working towards?|candidates? for|students? in)([^.;•]{0,140})", re.I)
GRAD_DEGREE_RE = re.compile(r"ph\.?\s?d|doctora|master'?s|\bm\.?s\.?c?\b|\bmba\b|\bgraduate (degree|program|student)", re.I)
UNDERGRAD_DEGREE_RE = re.compile(r"bachelor|undergrad|\bb\.?s\.?c?\b|\bb\.?a\.?\b|\bb\.?eng\b|\bbse\b|\bbasc\b", re.I)


def advanced_degree_only(description: str) -> bool:
    clauses = [match.group(2) for match in PURSUING_RE.finditer(description)]
    graduate = [c for c in clauses if GRAD_DEGREE_RE.search(c)]
    return bool(graduate) and not any(UNDERGRAD_DEGREE_RE.search(c) for c in clauses)


def check(description: str, countries: list[str]) -> tuple[str | None, str]:
    """→ (status, reason). status None means nothing blocks an automated application."""
    if INJECTION_RE.search(description) or NO_AUTOMATION_RE.search(description):
        return m.NEEDS_HUMAN, "posting addresses or forbids AI/automated applications"
    if "US" in countries and "CA" not in countries:
        hit = CITIZENSHIP_RE.search(description)
        if hit:
            return m.SKIPPED_INELIGIBLE, f"JD requires citizenship/clearance: “{hit.group(0)[:60]}”"
    if advanced_degree_only(description):
        return m.SKIPPED_INELIGIBLE, "JD targets graduate students only"
    return None, ""


def sponsorship_unavailable(description: str) -> bool:
    return bool(NO_SPONSORSHIP_RE.search(description))
