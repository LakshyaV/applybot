"""Greenhouse: the public Job Board API returns the complete application form schema, so questions are
known (and answered) before a browser is opened. Submission itself is browser-only (see fill())."""

from __future__ import annotations

import html
import re

import httpx

from ..normalize import countries_of
from ..resolve import Question

API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}"
FIELD_TYPES = {
    "input_text": "text", "textarea": "textarea", "input_file": "file",
    "multi_value_single_select": "select", "multi_value_multi_select": "multiselect",
}  # fmt: skip
# Fields identified by their stable API name rather than by label → answered straight from the profile.
PRESET_FACTS = {"first_name": "first_name", "last_name": "last_name", "email": "email", "phone": "phone",
                "preferred_name": "preferred_name"}  # fmt: skip


class Gone(Exception):
    """The posting no longer exists (closed upstream)."""


def fetch(board: str, job_id: str, client: httpx.Client | None = None) -> dict:
    client = client or httpx.Client(timeout=30)
    resp = client.get(API.format(board=board, job_id=job_id), params={"questions": "true"})
    if resp.status_code == 404:
        raise Gone(f"{board}/{job_id}")
    resp.raise_for_status()
    return resp.json()


def _questions(items: list[dict] | None, section: str = "") -> list[Question]:
    out = []
    for item in items or []:
        fields = [f for f in item.get("fields") or [] if f.get("type") in FIELD_TYPES]
        if not fields:
            continue
        # "Resume/CV" offers a file field and a paste-text field; the file field satisfies it.
        primary = next((f for f in fields if f["type"] == "input_file"), fields[0])
        out.append(
            Question(
                id=primary["name"], label=html.unescape(item.get("label") or "").strip(),
                type=FIELD_TYPES[primary["type"]], required=bool(item.get("required")),
                options=[html.unescape(str(v.get("label", ""))).strip() for v in primary.get("values") or []],
                section=section,
            )  # fmt: skip
        )
    return out


def parse(data: dict) -> tuple[list[Question], dict]:
    """→ (questions, meta). EEO/compliance and demographic questions are tagged so they are high-stakes."""
    questions = _questions(data.get("questions"))
    questions += _questions(data.get("location_questions"), "location")
    for block in data.get("compliance") or []:
        questions += _questions(block.get("questions"), "eeo")
    demo = data.get("demographic_questions") or {}
    for item in demo.get("questions") or []:
        questions.append(
            Question(id=f"demographic_{item['id']}", label=html.unescape(item.get("label") or "").strip(),
                     type="multiselect" if item.get("type") == "multi_value_multi_select" else "select",
                     required=bool(item.get("required")), section="demographic",
                     options=[html.unescape(o.get("label", "")).strip() for o in item.get("answer_options") or []])
        )  # fmt: skip
    location = (data.get("location") or {}).get("name") or ""
    offices = [o.get("location") or o.get("name") or "" for o in data.get("offices") or []]
    content = html.unescape(data.get("content") or "")
    meta = {
        "title": data.get("title", ""), "company": data.get("company_name", ""), "location": location,
        "countries": countries_of([loc for loc in [location, *offices] if loc]),
        "description": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", content)).strip(),
        "url": data.get("absolute_url", ""),
    }  # fmt: skip
    return questions, meta


def preset_answers(questions: list[Question], facts, resume_path: str) -> dict[str, object]:
    preset: dict[str, object] = {}
    for q in questions:
        if q.id in PRESET_FACTS:
            preset[q.id] = str(facts.get(PRESET_FACTS[q.id]))
        elif q.id == "resume":
            preset[q.id] = resume_path
    return preset
