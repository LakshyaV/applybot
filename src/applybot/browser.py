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


def launch(pw: Playwright, headless: bool = False, profile: str = "") -> BrowserContext:
    """One persistent Chrome profile per lane family (a profile can only be open in one process at a time):
    the default profile for Greenhouse/Ashby/IBM, `profile="workday"` for the Workday tenants, and so on."""
    profile_dir = PROFILE_DIR if not profile else PROFILE_DIR.parent / f"chrome-{profile}"
    profile_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(profile_dir.parent, 0o700)
    context = pw.chromium.launch_persistent_context(
        str(profile_dir), channel="chrome", headless=headless, viewport={"width": 1280, "height": 900}, args=ARGS
    )
    context.set_default_timeout(15_000)
    return context
