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
            Question(id=str(item["id"]), label=html.unescape(item.get("label") or "").strip(),  # DOM id is the bare number
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
DOM_TEXT_PRESETS = {"end-year--0": "grad_year", "start-year--0": "school_start_year"}
DOM_TYPEAHEAD_PRESETS = {"school--0": "school"}  # async search over thousands of schools


class FillError(Exception):
    pass


def _control(page, field_id: str):
    return page.locator(f'[id="{field_id}"]').locator('xpath=ancestor::div[contains(@class,"select__control")][1]')


def _committed(page, field_id: str) -> str:
    value = _control(page, field_id).locator(".select__single-value, .select__multi-value__label")
    return " | ".join(t.strip() for t in value.all_inner_texts())


# Only the open react-select menu. A page-wide [role=option] query also matches the phone widget's hidden
# country-code list ("Afghanistan+93" …), which once got recorded as the options of "Degree".
MENU_OPTION = ".select__menu .select__option"


def read_options(page, field_id: str) -> list[str]:
    box = page.locator(f'[id="{field_id}"]')
    box.click()
    try:
        page.locator(MENU_OPTION).first.wait_for(state="visible", timeout=4_000)
    except Exception:  # noqa: BLE001 — async pickers (school search) show nothing until you type
        box.press("Escape")
        return []
    options = [t.strip() for t in page.locator(MENU_OPTION).all_inner_texts()]
    box.press("Escape")
    return options


def choose(page, field_id: str, option: str, typeahead: bool = False) -> None:
    box = page.locator(f'[id="{field_id}"]')
    target = page.locator(MENU_OPTION).filter(has_text=re.compile(rf"^\s*{re.escape(option)}\s*$"))
    for attempt in range(3):  # live-search pickers (school) are served by a remote lookup that is sometimes slow
        box.click()
        box.fill("")
        if attempt == 0:
            box.fill(option if typeahead else option[:30])  # filters the list; does NOT commit a value
        else:
            box.press_sequentially(option if typeahead else option[:30], delay=40)  # real key events re-trigger the search
        try:
            target.first.wait_for(state="visible", timeout=10_000 if attempt == 0 else 20_000)
            break
        except Exception:  # noqa: BLE001
            if attempt == 2:
                raise
            box.press("Escape")
    option_flag = _flag_iso(target.first)
    target.first.click()
    held = _committed(page, field_id)
    if held == option:
        return
    # The phone-country picker lists "Canada +1" but displays a flag plus "+1" once chosen. "+1" alone is also
    # the US, so the committed FLAG must be the chosen option's flag — a bare text suffix is not accepted.
    held_flag = _flag_iso(_control(page, field_id).locator(".select__single-value").first)
    if option_flag and held_flag == option_flag and held and option.endswith(held):
        FLAG_VERIFIED[field_id] = held
        return
    raise FillError(f"{field_id}: chose {option!r} but the form holds {held!r} (flag {held_flag or 'none'})")


FLAG_VERIFIED: dict[str, str] = {}  # field id → committed text that was verified through its flag, for readback


def _flag_iso(scope) -> str:
    flag = scope.locator(".iti__flag")
    if not flag.count():
        return ""
    found = re.search(r"iti__([a-z]{2})\b", flag.first.get_attribute("class") or "")
    return found.group(1) if found else ""


LOCATION_FIELD = "candidate-location"  # "Location (City)": a geocoder typeahead that also fills hidden lat/long


def choose_location(page, field_id: str, city: str, region: str, country: str) -> None:
    """Type the applicant's own city and pick the geocoder suggestion naming that city AND country (and the
    region when the suggestion shows one). The suggestion wording is the geocoder's, so it cannot be an
    exact-string match — but only the user's own city is ever accepted."""
    box = page.locator(f'[id="{field_id}"]')
    box.click()
    box.fill(city)
    page.locator(MENU_OPTION).first.wait_for(state="visible", timeout=10_000)
    page.wait_for_timeout(600)  # let the async list settle; selecting early leaves the hidden lat/long empty
    for option in page.locator(MENU_OPTION).all():
        text = option.inner_text().strip()
        parts = [p.strip().lower() for p in text.split(",")]
        if parts[0] == city.lower() and country.lower() in parts and (len(parts) < 3 or region.lower() in parts):
            option.click()
            if city.lower() not in _committed(page, field_id).lower():
                raise FillError(f"{field_id}: location did not commit")
            return
    raise FillError(f"{field_id}: no suggestion for {city}, {region}, {country}")


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
             maxlength: el.maxLength > 0 ? el.maxLength : null,
             label: (document.querySelector(`label[for="${CSS.escape(el.id)}"]`)?.innerText || '').replace(/\\*\\s*$/, '').trim(),
          }))"""
    )


def dom_only_questions(page, api_questions: list[Question], company: str) -> list[Question]:
    """Controls present in the DOM but missing from the API schema, as Questions (options read live)."""
    known = {q.id for q in api_questions}
    extra = []
    for f in dom_census(page):
        base = f["id"].split("[]")[0]
        if f["id"] in known or base in known or f["kind"] == "file":
            continue
        if f["kind"] == "checkbox" and ("[]" in f["id"] or not f["required"]):
            continue  # option groups belong to an API question; optional lone boxes (newsletters…) stay unticked
        # a REQUIRED lone checkbox is a consent/attestation the API schema never mentions → it must be a question
        if f["id"] in DOM_TYPEAHEAD_PRESETS or f["id"] in DOM_TEXT_PRESETS or f["id"] == LOCATION_FIELD:
            continue  # answered from the profile by fixed id
        options: list[str] = []
        if f["kind"] == "select":
            for _attempt in range(3):  # option lists render lazily; an empty read is "not loaded yet", not "no options"
                options = read_options(page, f["id"])
                if options:
                    break
                page.wait_for_timeout(1_500)
            if not options:
                raise FillError(f"{f['id']}: dropdown options never loaded ({f['label'][:40]!r})")  # retryable
        extra.append(Question(f["id"], f["label"], f["kind"], f["required"], options, company=company))
    return extra


def _apply(page, field_id: str, value: object, kind: str) -> None:
    if kind == "file":
        page.locator(f'[id="{field_id}"]').set_input_files(str(value))  # hidden input; never click "Attach"
        # the upload is asynchronous: the filename chip appears only once the server has accepted the file
        page.get_by_text(str(value).rsplit("/", 1)[-1], exact=True).first.wait_for(state="visible", timeout=30_000)
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
        box = page.locator(f'[id="{field_id}"]')
        limit = box.evaluate("el => el.maxLength > 0 ? el.maxLength : null")
        if limit and len(str(value)) > limit:  # the browser would silently cut the text off mid-sentence
            raise FillError(f"{field_id}: answer is {len(str(value))} characters but the field allows {limit}")
        box.fill(str(value))


def fill(page, questions: list[Question], answers: dict[str, object], facts) -> dict:
    """Fill every answered field, then read the committed values back. Never clicks submit.

    Runs in passes because Greenhouse reveals conditional fields (e.g. Race appears only after the
    Hispanic/Latino answer). Answers for fields that never appear are reported, not forced.
    """
    FLAG_VERIFIED.clear()  # per-form state
    dom = {f["id"]: f for f in dom_census(page)}
    for field_id, fact in DOM_TEXT_PRESETS.items():
        if field_id in dom:
            answers.setdefault(field_id, str(facts.get(fact)))
    for field_id, fact in DOM_TYPEAHEAD_PRESETS.items():
        if field_id in dom:
            choose(page, field_id, str(facts.get(fact)), typeahead=True)
    if LOCATION_FIELD in dom:
        choose_location(page, LOCATION_FIELD, str(facts.get("city")), str(facts.get("province_state")),
                        str(facts.get("country_of_residence")))  # fmt: skip

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
        and not (k in FLAG_VERIFIED and held[k] == FLAG_VERIFIED[k])  # verified by flag at selection time
    }  # fmt: skip
    # a required checkbox group is satisfied when any box in it is ticked
    groups = {i.split("[]")[0] for i in empty_required if "[]" in i}
    for g in groups:
        if any(v is True for k, v in held.items() if k.startswith(g + "[]")):
            empty_required = [i for i in empty_required if not i.startswith(g + "[]")]
    return {"held": held, "empty_required": empty_required, "mismatches": mismatches}


# --- submit ------------------------------------------------------------------------------------
# Outcomes are classified conservatively: anything that is not a positively identified confirmation or a
# positively identified validation error is UNKNOWN, and the caller must send the job to `verify`
# (never retried) rather than guess.

CONFIRMED, NEEDS_CODE, INVALID, CHALLENGE, UNKNOWN = "confirmed", "needs_code", "invalid", "challenge", "unknown"
# Employers customize this page (Robinhood: "Thank you for your interest… We will review your application"),
# so the /confirmation URL is the primary signal and this text is the backup.
CONFIRM_TEXT_RE = re.compile(r"thank you for (applying|your interest|your application)|"
                             r"application (has been |was )?(submitted|received)|"
                             r"we('ve| have) received your application|we will review your application", re.I)  # fmt: skip
SECURITY_INPUT = '[id^="security-input-"]'
ERROR_SELECTOR = '[aria-invalid="true"], .helper-text--error, [id$="-error"]'
CHALLENGE_SELECTOR = 'iframe[src*="recaptcha/api2/bframe"], iframe[src*="hcaptcha.com"], iframe[title*="challenge" i]'


def _visible_error(page) -> str:
    """Text of a VISIBLE validation message. Empty error containers exist on healthy forms, so mere presence
    of a matching element proves nothing."""
    for el in page.locator(ERROR_SELECTOR).all():
        try:
            if el.is_visible() and (text := el.inner_text().strip()):
                return text[:120]
        except Exception:  # noqa: BLE001 — element detached mid-check
            continue
    return ""


def _classify(page, statuses: list[int]) -> tuple[str, str]:
    if page.locator(SECURITY_INPUT).count() or 428 in statuses:
        return NEEDS_CODE, ""
    if "/confirmation" in page.url:
        return CONFIRMED, ""
    # Text alone is not proof: job descriptions say things like "thank you for your interest". It only counts once
    # the application form itself is gone from the page. A false "confirmed" is the worst error this tool can make.
    form_gone = page.locator("form #first_name, form #email").count() == 0
    if form_gone and CONFIRM_TEXT_RE.search(page.locator("body").inner_text()[:4000]):
        return CONFIRMED, ""
    for frame in page.locator(CHALLENGE_SELECTOR).all():
        if frame.is_visible():
            return CHALLENGE, ""
    # INVALID means "nothing was sent, a retry is safe" — so it needs positive proof: a visible message AND
    # no application POST that the server accepted. Anything less stays UNKNOWN (→ verify, never retried).
    error = _visible_error(page)
    if error and not any(s < 400 for s in statuses):
        return INVALID, error
    return UNKNOWN, ""


def submit(page, wait_ms: int = 30_000, after_code: bool = False) -> tuple[str, str]:
    """Click submit exactly once and classify what happened. → (outcome, detail)."""
    statuses: list[int] = []
    page.on("response", lambda r: statuses.append(r.status) if r.request.method == "POST" and "greenhouse" in r.url else None)
    buttons = page.get_by_role("button", name=re.compile(r"submit", re.I))
    # after a security code is entered Greenhouse shows a second submit button beneath the code boxes
    (buttons.last if after_code else buttons.first).click()
    waited, outcome, note = 0, UNKNOWN, ""
    while waited < wait_ms:
        page.wait_for_timeout(500)
        waited += 500
        if waited < 2_000:
            continue  # give the request time to leave before judging anything
        outcome, note = _classify(page, statuses)
        if outcome != UNKNOWN:
            break
    return outcome, f"{note} POST statuses={statuses[-4:]} url={page.url}".strip()


def enter_security_code(page, code: str) -> tuple[str, str]:
    """The emailed 8-character code is only valid in THIS browser session; never reload before entering it."""
    boxes = page.locator(SECURITY_INPUT)
    if boxes.count() != len(code):
        return UNKNOWN, f"expected {boxes.count()} characters, got {len(code)}"
    for i, char in enumerate(code):
        boxes.nth(i).fill(char)
    return submit(page, after_code=True)
