"""Secrets never live in the repo, the tracker, logs, or an LLM context.

The ATS account password is read, in order, from the ATS_ACCOUNT_PASSWORD environment variable (e.g. a
hand-made .env) or the macOS login Keychain item (service "applybot", account "ats_account_password").
Only form-filling code calls this, and only to type the value into a password field.
"""

from __future__ import annotations

import os
import subprocess

from dotenv import load_dotenv

from .config import ROOT

SERVICE, ACCOUNT = "applybot", "ats_account_password"


class MissingSecret(Exception):
    pass


def ats_password() -> str:
    load_dotenv(ROOT / ".env")
    value = os.environ.get("ATS_ACCOUNT_PASSWORD")
    if value:
        return value
    found = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"],
                           capture_output=True, text=True)  # fmt: skip
    if found.returncode == 0 and found.stdout.strip():
        return found.stdout.strip()
    raise MissingSecret("no ATS password: set ATS_ACCOUNT_PASSWORD in .env or add the 'applybot' Keychain item")


def has_ats_password() -> bool:
    try:
        return bool(ats_password())
    except MissingSecret:
        return False


def portal_password(portal: str) -> str:
    """A portal-specific account password (e.g. an IBMid that predates this tool), falling back to the shared one.
    Env var <PORTAL>_PASSWORD or Keychain item (service "applybot", account "<portal>_password")."""
    load_dotenv(ROOT / ".env")
    value = os.environ.get(f"{portal.upper()}_PASSWORD")
    if value:
        return value
    found = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", f"{portal}_password", "-w"],
                           capture_output=True, text=True)  # fmt: skip
    if found.returncode == 0 and found.stdout.strip():
        return found.stdout.strip()
    return ats_password()
