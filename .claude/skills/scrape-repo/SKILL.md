---
name: scrape-repo
description: Scrape every job out of a GitHub internship-list repo into the applybot tracker. Use when the user pastes or mentions a GitHub repo URL containing job/internship listings, or says "scrape this repo".
---

# scrape-repo

Input: a GitHub repo URL (any form: https, .git, renamed repos are followed automatically).

1. Run `uv run applybot ingest-repo <url> --json`.
2. If it succeeds, report in ≤6 lines: adapter used, rows found, new vs already seen, newly queued, queued by ATS,
   and the top reasons rows were not queued. Do not dump job lists.
3. If it returns 0 rows or errors, the repo uses an unknown format. Inspect it cheaply:
   `gh api repos/<owner>/<repo>/git/trees/<branch>?recursive=1 --jq '.tree[].path' | head -50`, then fetch only
   the first ~60 lines of the file holding the listings. Add an adapter (or extend `HEADER_ALIASES` /
   `_links`) in `src/applybot/sources/github_repo.py`, save a ≤10-row fixture in `tests/fixtures/`, add a test
   in `tests/test_ingest.py`, run `uv run pytest -q`, then re-run step 1. Never hand-copy rows into the tracker.
4. If the repo should be re-checked regularly, add it to `sources.repos` in `config.yaml`.
5. Then continue with the `apply` skill for the newly queued jobs, unless the user only asked to scrape.

Repo mode queues **every eligible row** (all categories, relevance-ordered). Pass `--relevant-only` only when the
user asks for SWE/ML roles only. Eligibility, season, per-company cap, and blocked hosts come from `config.yaml`.
