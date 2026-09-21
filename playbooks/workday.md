# Workday playbook

Observed on live tenants (read-only reconnaissance, 2026-09-21). Selectors are `data-automation-id`
values, which are stable across tenants. Append what you learn; keep it factual.

## Entry path (verified)

1. Posting page → `adventureButton` ("Apply").
2. Modal offers `autofillWithResume`, `applyManually`, `useMyLastApplication`.
   **Always `applyManually`.** Resume autofill parses the PDF into education/experience blocks that then
   have to be corrected; `useMyLastApplication` copies a previous tenant-specific application.
3. Account screen: `email`, `password`, `verifyPassword`, `createAccountSubmitButton`; `signInLink` switches to
   sign-in (`signInSubmitButton` expected); `forgotPasswordLink`.
   Some tenants add a `createAccountCheckbox` (terms) — that is a consent → treat as a question, not a default.

## Rules

- **`beecatcher` is a honeypot input.** It is invisible to a person. Never type into it. The filler must only
  fill fields it has an answer for, by id — never "fill every input on the page".
- One account per tenant (`<tenant>.wdN.myworkdayjobs.com`). Record it in the `accounts` table (tenant + email,
  never the password). "Account already exists" → sign in. A password-reset prompt → `needs_human`.
- The password comes from `applybot.secrets.ats_password()` inside the runner only. It is never logged,
  printed, put in a run directory, or passed through an LLM context.
- Email verification after account creation is often a **link**, not a code. Only open a link whose host is the
  tenant's own Workday domain.
- The application is a wizard: My Information → My Experience → Application Questions → Voluntary Disclosures →
  Self Identify → Review. Questions exist only per step, so: extract the step → resolve → fill → readback →
  "Save and Continue". If a step has unresolved questions: leave the draft (Workday saves it server-side),
  close the window, mark `needs_answers`. Never hold a session open waiting on a person — it idles out.
- Resume state is keyed on the *active progress step*, not on a tab. On return: sign in → open the job →
  "Continue application" if present.
- An "already applied" banner or the job appearing under Candidate Home → `already_applied`. Never apply twice.
- The final **Submit** on the Review step follows the same rules as Greenhouse: only when `mode` allows it,
  write-ahead intent first, anything not positively identified as a confirmation → `verify`.

## Not yet known (find out during the supervised pilot)

- Exact ids on each wizard step (`legalNameSection_firstName`, `addressSection_*`, `phone-number`,
  `file-upload-input-ref`, … are reported by others; confirm on a live tenant before relying on them).
- Which tenants require email verification before first sign-in.
- Whether any tenant shows a CAPTCHA at account creation (→ `needs_human`, once per tenant).

## Eligibility note

Many Workday employers in the queue are defense contractors (RTX, The Aerospace Corporation, L3Harris…) whose
postings require US citizenship. The source lists under-report this, and the Workday job description is only
available from the posting page or the `cxs` JSON endpoint — run the JD preflight on Workday postings *before*
creating an account on that tenant.
