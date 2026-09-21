---
name: status
description: Show applybot progress — queue size, submissions, and what is waiting on the user. Use when the user asks how applications are going or what needs their attention.
---

# status

Run `uv run applybot status`, then `uv run applybot status --by ats`. If anything is in `needs_input`,
`needs_human`, or `verify`, show those with `uv run applybot list --status <status> --limit 10`.
Summarize in ≤10 lines, leading with what needs the user. Never paste raw logs or page content.
