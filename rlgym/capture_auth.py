"""Mint the pre-authentication cookie used by every attempt.

Run once (and again whenever the session expires):

    agent/.venv/bin/python -m rlgym.capture_auth

Logs into Metabase through the actual login form and saves Playwright `storage_state` to
agent/storage_state.json (git-ignored). Attempts then inject that state into each fresh
browser context and start already authenticated.

Why pre-authenticate at all: login is a confound, not the capability under test. Doing it
here means the password never enters model context or a rollout transcript, no attempt burns
3-5 steps of its budget on a login form, and a login failure can't be mistaken for an
analysis failure. This is the standard shape -- WebArena ships per-site storage_state files
for exactly this reason.

**Metabase-specific caveat:** sessions live in the `core_session` table of the app database,
so restoring the seed dump invalidates this cookie. Harmless today (all tasks are read-only,
so nothing resets), but in write-mode this must be re-run after each restore. Don't try to
bake a session row into the pristine template -- Metabase ages sessions out (~14 days by
default) and a baked one would rot silently.
"""

from __future__ import annotations

import os
import pathlib
import sys

from dotenv import load_dotenv

from .vendor import PROJECT_ROOT, STORAGE_STATE

load_dotenv(PROJECT_ROOT / ".env")

EMAIL_SELECTOR = 'input[name="username"], input[type="email"]'
PASSWORD_SELECTOR = 'input[name="password"], input[type="password"]'
SUBMIT_SELECTOR = 'button[type="submit"]'


def capture(
    gym_url: str | None = None,
    email: str | None = None,
    password: str | None = None,
    out_path: pathlib.Path = STORAGE_STATE,
) -> pathlib.Path:
    from playwright.sync_api import sync_playwright

    gym_url = (gym_url or os.environ.get("METABASE_URL", "http://localhost:3000")).rstrip("/")
    email = email or os.environ.get("METABASE_EMAIL")
    password = password or os.environ.get("METABASE_PASSWORD")

    if not email or not password:
        raise SystemExit(
            "METABASE_EMAIL and METABASE_PASSWORD must be set in .env "
            "(or passed explicitly)."
        )

    print(f"logging into {gym_url} as {email} ...")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()

        page.goto(f"{gym_url}/auth/login")
        page.wait_for_load_state()

        page.fill(EMAIL_SELECTOR, email)
        page.fill(PASSWORD_SELECTOR, password)
        page.click(SUBMIT_SELECTOR)

        # Metabase is a SPA: a successful login replaces the route rather than reloading,
        # so wait for the URL to stop being the login page instead of waiting for a load.
        try:
            page.wait_for_url(lambda url: "/auth/login" not in url, timeout=30_000)
        except Exception:
            page.screenshot(path=str(out_path.parent / "login_failed.png"))
            raise SystemExit(
                "login did not leave /auth/login -- credentials rejected or Metabase not "
                f"ready. Screenshot: {out_path.parent / 'login_failed.png'}"
            )

        page.wait_for_load_state()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(out_path))
        print(f"  landed on {page.url}")
        context.close()
        browser.close()

    print(f"saved storage_state -> {out_path}")
    return out_path


def verify(gym_url: str | None = None, state_path: pathlib.Path = STORAGE_STATE) -> bool:
    """Prove the cookie works in a *fresh* context, which is how attempts will use it."""
    from playwright.sync_api import sync_playwright

    gym_url = (gym_url or os.environ.get("METABASE_URL", "http://localhost:3000")).rstrip("/")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900}, storage_state=str(state_path)
        )
        page = context.new_page()
        page.goto(gym_url)
        page.wait_for_load_state()
        url = page.url
        authenticated = "/auth/login" not in url
        print(f"  fresh context landed on {url}")
        context.close()
        browser.close()

    return authenticated


def main() -> int:
    capture()
    print("verifying in a fresh context ...")
    if not verify():
        print("FAIL: saved state did not authenticate")
        return 1
    print("RESULT: PASS -- attempts will start authenticated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
