"""The dedicated automation Chrome. One long-lived runner process owns it (launched over a pipe — no
remote-debugging port is opened, so no other local process can drive the logged-in session).

Deliberately absent: stealth plugins, fingerprint spoofing, proxies, CAPTCHA solvers (CLAUDE.md rule 9).
"""

from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import BrowserContext, Playwright

PROFILE_DIR = Path.home() / ".applybot" / "chrome"
# Keep background tabs responsive so typeaheads in parallel fills do not stall. Not detection-related.
ARGS = ["--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
        "--disable-background-timer-throttling"]  # fmt: skip


def launch(pw: Playwright, headless: bool = False) -> BrowserContext:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(PROFILE_DIR.parent, 0o700)
    context = pw.chromium.launch_persistent_context(
        str(PROFILE_DIR), channel="chrome", headless=headless, viewport={"width": 1280, "height": 900}, args=ARGS
    )
    context.set_default_timeout(15_000)
    return context
