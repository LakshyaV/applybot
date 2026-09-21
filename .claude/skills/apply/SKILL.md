---
name: apply
description: Work the applybot queue — survey forms, get unanswered questions resolved, collect the user's approval for critical answers, and run application fills. Use when the user says apply, start applying, run the queue, or after scrape-repo queues new jobs.
---

# apply

Read `mode` in `config.yaml` first. Only the user changes it. **Current capability: Greenhouse, dry-run only
(fill + screenshot). There is no submit code path yet** — say so plainly if asked to submit.

1. `uv run applybot survey --limit 400` — read-only: form schemas via the ATS API, JD preflight, records
   unanswered questions (deduped, employer name templated out).
2. Answer bank, in batches of 40, most common first:
   `uv run applybot questions --json --limit 40 > data/q_batch.jsonl` → launch the `answerer` agent with that
   path and an output path `data/proposals.json`. Repeat while common questions (x≥2) remain. Do not answer
   questions yourself inline; do not paste question dumps into chat.
3. Critical answers need the USER. `uv run applybot approvals --json` → show the claims with AskUserQuestion in
   groups of ≤4 (or as a short numbered list if there are many) and clear only what they explicitly approve:
   `uv run applybot approvals --approve <qhash> …`. Never approve on the user's behalf. If a claim looks wrong,
   say so and leave it uncleared.
4. `verify` tier: launch a fresh general-purpose agent with ONLY the output of
   `uv run applybot approvals --tier verify --json` plus the work-authorization block of `profile/profile.md`,
   asking "is each mapping truthful, polarity included? return the qhashes that are correct". Clear exactly those
   with `--tier verify --approve`.
5. Dry-runs: `uv run applybot list --ats greenhouse --limit 5`, then `uv run applybot dry-run <id>` per job
   (headed, so the user can watch). Report per job: filled count, what is still unanswered, readback mismatches,
   `would_be_submittable`, and the screenshot path. Read a screenshot only if the user asks or a result looks off.
6. End with ≤8 lines: what ran, what is blocked on the user (`needs_your_input`, approvals), what is next.
