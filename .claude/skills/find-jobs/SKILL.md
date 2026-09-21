---
name: find-jobs
description: Refresh the applybot tracker from every configured source (GitHub internship lists; later ATS boards). Use when the user asks to find/refresh/discover new internship postings.
---

# find-jobs

1. For each URL under `sources.repos` in `config.yaml`, run `uv run applybot ingest-repo <url> --json`
   (sequentially; each is one HTTP fetch plus tracker-link resolution). A failing repo must not stop the rest —
   note it and move on.
2. Report one line per source (rows / new / newly queued) and then `uv run applybot status`.
3. If the user asked for roles beyond the lists, use WebSearch for employer ATS links only
   (greenhouse.io, lever.co, ashbyhq.com, myworkdayjobs.com …). Never LinkedIn, Indeed, Glassdoor, or Handshake.
   Add finds to a scratch markdown table and ingest them the same way — do not invent a second ingest path.
