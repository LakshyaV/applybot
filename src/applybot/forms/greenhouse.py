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


# --- browser side ------------------------------------------------------------------------------
# DOM ids equal the API field names. Selects are react-select comboboxes: fill() alone never commits a
# value, so every choice is click → filter → click the [role=option] → read the committed value back.

# Fixed-id fields the public API does not report (country + the education block).
DOM_TEXT_PRESETS = {"end-year--0": "grad_year"}
DOM_TYPEAHEAD_PRESETS = {"school--0": "school"}  # async search over thousands of schools


class FillError(Exception):
    pass


def _control(page, field_id: str):
    return page.locator(f'[id="{field_id}"]').locator('xpath=ancestor::div[contains(@class,"select__control")][1]')


def _committed(page, field_id: str) -> str:
    value = _control(page, field_id).locator(".select__single-value, .select__multi-value__label")
    return " | ".join(t.strip() for t in value.all_inner_texts())


def read_options(page, field_id: str) -> list[str]:
    box = page.locator(f'[id="{field_id}"]')
    box.click()
    page.wait_for_timeout(250)
    options = [t.strip() for t in page.locator('[role="option"]').all_inner_texts()]
    box.press("Escape")
    return options


def choose(page, field_id: str, option: str, typeahead: bool = False) -> None:
    box = page.locator(f'[id="{field_id}"]')
    box.click()
    box.fill(option if typeahead else option[:30])  # filters the list; does NOT commit a value
    target = page.locator('[role="option"]').filter(has_text=re.compile(rf"^\s*{re.escape(option)}\s*$"))
    target.first.wait_for(state="visible", timeout=10_000)
    target.first.click()
    if _committed(page, field_id) != option:
        raise FillError(f"{field_id}: chose {option!r} but the form holds {_committed(page, field_id)!r}")


def dom_census(page) -> list[dict]:
    """Every fillable control on the form: id, kind, required, label. The API schema is not complete."""
    return page.evaluate(
        """() => [...document.querySelectorAll('form input[id], form textarea[id]')]
          .filter(el => !el.id.startsWith('iti-') && el.type !== 'hidden' && el.type !== 'search')
          .map(el => ({
             id: el.id,
             kind: el.type === 'file' ? 'file' : el.type === 'checkbox' ? 'checkbox'
                 : el.getAttribute('role') === 'combobox' ? 'select' : el.tagName === 'TEXTAREA' ? 'textarea' : 'text',
             required: el.getAttribute('aria-required') === 'true' || el.required,
             label: (document.querySelector(`label[for="${CSS.escape(el.id)}"]`)?.innerText || '').replace(/\\*\\s*$/, '').trim(),
          }))"""
    )


def dom_only_questions(page, api_questions: list[Question], company: str) -> list[Question]:
    """Controls present in the DOM but missing from the API schema, as Questions (options read live)."""
    known = {q.id for q in api_questions}
    extra = []
    for f in dom_census(page):
        base = f["id"].split("[]")[0]
        if f["id"] in known or base in known or f["kind"] in ("file", "checkbox"):
            continue
        if f["id"] in DOM_TYPEAHEAD_PRESETS or f["id"] in DOM_TEXT_PRESETS:
            continue  # answered from the profile by fixed id
        options = read_options(page, f["id"]) if f["kind"] == "select" else []
        extra.append(Question(f["id"], f["label"], f["kind"], f["required"], options, company=company))
    return extra


def _apply(page, field_id: str, value: object, kind: str) -> None:
    if kind == "file":
        page.locator(f'[id="{field_id}"]').set_input_files(str(value))  # hidden input; never click "Attach"
    elif kind == "select":
        choose(page, field_id, str(value))
    elif kind == "checkbox":
        wanted = value if isinstance(value, list) else [value]
        group = page.locator(f'input[type="checkbox"][id^="{field_id}"]')
        for i in range(group.count()):
            box = group.nth(i)
            label = page.locator(f'label[for="{box.get_attribute("id")}"]').inner_text().strip()
            if value is True or label in wanted:
                box.check()
    else:
        page.locator(f'[id="{field_id}"]').fill(str(value))


def fill(page, questions: list[Question], answers: dict[str, object], facts) -> dict:
    """Fill every answered field, then read the committed values back. Never clicks submit.

    Runs in passes because Greenhouse reveals conditional fields (e.g. Race appears only after the
    Hispanic/Latino answer). Answers for fields that never appear are reported, not forced.
    """
    dom = {f["id"]: f for f in dom_census(page)}
    for field_id, fact in DOM_TEXT_PRESETS.items():
        if field_id in dom:
            answers.setdefault(field_id, str(facts.get(fact)))
    for field_id, fact in DOM_TYPEAHEAD_PRESETS.items():
        if field_id in dom:
            choose(page, field_id, str(facts.get(fact)), typeahead=True)

    pending = {k: v for k, v in answers.items() if v is not None}
    for _ in range(4):
        dom = {f["id"]: f for f in dom_census(page)}
        progressed = False
        for field_id in list(pending):
            if field_id.endswith("[]"):  # checkbox group: DOM ids are "<name>[]_<option id>"
                kind = "checkbox" if any(d.startswith(field_id) for d in dom) else None
            else:
                kind = dom.get(field_id, {}).get("kind")
            if kind is None:
                continue  # not (yet) on the form
            _apply(page, field_id, pending.pop(field_id), kind)
            progressed = True
        if not progressed:
            break
    report = readback(page, answers)
    report["not_on_form"] = sorted(pending)
    return report


def readback(page, answers: dict[str, object]) -> dict:
    """What the form actually holds, plus every required control that is still empty."""
    held, empty_required = {}, []
    for f in dom_census(page):
        loc = page.locator(f'[id="{f["id"]}"]')
        if f["kind"] == "select":
            current = _committed(page, f["id"])
        elif f["kind"] == "checkbox":
            current = loc.is_checked()
        elif f["kind"] == "file":
            current = loc.evaluate("el => [...el.files].map(f => f.name).join(', ')")
        else:
            current = loc.input_value()
        held[f["id"]] = current
        if f["required"] and current in ("", False):
            empty_required.append(f["id"])
    # Greenhouse replaces the file <input> after an upload, so an attachment is verified by its filename chip.
    for field_id, value in answers.items():
        if isinstance(value, str) and value.lower().endswith((".pdf", ".docx", ".doc")) and not held.get(field_id):
            name = value.rsplit("/", 1)[-1]
            held[field_id] = name if page.get_by_text(name, exact=True).count() else ""
            if not held[field_id]:
                empty_required.append(field_id)
    mismatches = {
        k: {"wanted": v, "held": held.get(k)} for k, v in answers.items()
        if k in held and isinstance(v, str) and not v.lower().endswith((".pdf", ".docx", ".doc")) and held[k] != v
    }  # fmt: skip
    # a required checkbox group is satisfied when any box in it is ticked
    groups = {i.split("[]")[0] for i in empty_required if "[]" in i}
    for g in groups:
        if any(v is True for k, v in held.items() if k.startswith(g + "[]")):
            empty_required = [i for i in empty_required if not i.startswith(g + "[]")]
    return {"held": held, "empty_required": empty_required, "mismatches": mismatches}
