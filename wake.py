"""
Keep the AegisTrader Streamlit app awake (Streamlit Community Cloud).

Community Cloud hibernates every app that receives no traffic for 12 hours
(https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app).
A sleeping app cannot be revived with a plain HTTP request: the sleep page
("Zzzz - This app has gone to sleep due to inactivity") only wakes up when the
button "Yes, get this app back up!" is clicked in a real browser session.
This script therefore drives a headless Chromium instance and:

  1. opens the app (a real browser visit = real traffic, resets the 12h timer);
  2. detects the sleep page and clicks the wake button;
  3. verifies that the Streamlit app itself is really rendered, so a silent
     failure can no longer be reported as success;
  4. stays connected for a few seconds so the session is registered;
  5. exits with a non-zero status if the app is still not running, which turns
     the GitHub Actions run red and triggers a failure notification.

Important DOM detail (verified live on 2026-08-28): on *.streamlit.app the
running app is served inside an iframe (src ".../~/+/"), while the sleep page
lives in the top-level document. The app root ([data-testid="stApp"]) therefore
has to be looked for in EVERY frame, not only in the main one.

Configuration: STREAMLIT_APP_URL (optional; DEFAULT_APP_URL is used otherwise).
"""

from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime, timezone

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Hardcoded fallback: the app URL is public, so it does not have to be a secret.
# A missing/renamed repository secret can therefore no longer break the job.
DEFAULT_APP_URL = "https://aegistrader-ml-thesis-app.streamlit.app"
APP_URL = (os.environ.get("STREAMLIT_APP_URL") or "").strip() or DEFAULT_APP_URL

# The sleep page renders a real <button> whose accessible name is
# "Yes, get this app back up!" (verified live). Matched case-insensitively so
# small wording changes on Streamlit's side do not break the detection.
WAKE_BUTTON_RE = re.compile(r"get this app back up", re.IGNORECASE)

# Root element rendered by a running Streamlit app (inside the app iframe).
APP_ROOT_SELECTOR = "[data-testid='stApp'], .stApp"

# When Streamlit Cloud cannot serve the app (crash, container restart, platform
# issue) the app iframe points to this URL instead of ".../~/+/" (observed in
# run #677 on 2026-10-23). The wake script cannot fix this state.
CLOUD_ERROR_MARKER = "share.streamlit.io/error"

NAV_TIMEOUT_MS = 60_000      # page.goto timeout
READY_TIMEOUT_S = 90         # max wait to decide "running" vs "sleeping"
BOOT_TIMEOUT_S = 300         # max wait for the container to boot after a click
POLL_INTERVAL_MS = 2_000     # polling granularity
DWELL_S = 20                 # stay on the running app so the session is counted
MAX_ATTEMPTS = 3             # full reload attempts before giving up
FAILURE_SCREENSHOT = "failure.png"

# Reason recorded for every failed attempt; the final report is built from it.
FAILURE_REASONS: list[str] = []
REASON_TEXT = {
    "cloud_error": (
        "Streamlit Cloud error page",
        "The app URL served https://share.streamlit.io/error instead of the app. "
        "The app is not sleeping, it is not being served: the wake script cannot fix this. "
        "Check the logs in share.streamlit.io (Manage app). Often transient.",
    ),
    "boot_timeout": (
        "App did not finish booting",
        f"The wake button was clicked but the app root did not render within {BOOT_TIMEOUT_S}s.",
    ),
    "button_vanished": (
        "Wake button disappeared",
        "The sleep page was detected but the wake button could not be clicked.",
    ),
    "undetermined": (
        "Page state undetermined",
        "Neither the sleep page, the app root, nor the Streamlit Cloud error page was found. "
        "Download the failure.png artifact to see what the browser rendered.",
    ),
    "timeout": ("Playwright timeout", "Navigation or an element lookup timed out."),
    "exception": ("Unexpected script error", "See the traceback in the step log."),
}


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{stamp}] {message}", flush=True)


# --------------------------------------------------------------------------- #
# Page state helpers
# --------------------------------------------------------------------------- #

def wake_button(page: Page):
    """Return a locator for the wake button, or None if it is not on the page."""
    try:
        by_role = page.get_by_role("button", name=WAKE_BUTTON_RE)
        if by_role.count() > 0:
            return by_role.first
        by_text = page.get_by_text(WAKE_BUTTON_RE)
        if by_text.count() > 0:
            return by_text.first
    except Exception:  # noqa: BLE001 - a navigating page can raise transiently
        return None
    return None


def app_root_visible(page: Page) -> bool:
    """True if the Streamlit app root exists in ANY frame of the page."""
    for frame in page.frames:
        try:
            if frame.locator(APP_ROOT_SELECTOR).count() > 0:
                return True
        except Exception:  # noqa: BLE001 - frame detached mid-check
            continue
    return False


def wait_for_state(page: Page, timeout_s: int, accept_sleeping: bool = True) -> str:
    """Poll the page until it is 'running', 'sleeping', or the timeout expires."""
    deadline = time.monotonic() + timeout_s
    while True:
        if app_root_visible(page):
            return "running"
        if accept_sleeping and wake_button(page) is not None:
            return "sleeping"
        if time.monotonic() >= deadline:
            return "unknown"
        page.wait_for_timeout(POLL_INTERVAL_MS)


def describe(page: Page) -> str:
    """Small diagnostic dump, printed when the page state cannot be determined."""
    try:
        frames = [f.url for f in page.frames]
        return f"title={page.title()!r} url={page.url!r} frames={frames}"
    except Exception as exc:  # noqa: BLE001
        return f"<could not inspect the page: {exc!r}>"


def cloud_error_page(page: Page) -> bool:
    """True if any frame points to Streamlit Cloud's error page."""
    try:
        return any(CLOUD_ERROR_MARKER in (f.url or "") for f in page.frames)
    except Exception:  # noqa: BLE001 - frame detached mid-check
        return False


# --------------------------------------------------------------------------- #
# Main routine
# --------------------------------------------------------------------------- #

def attempt(page: Page, attempt_no: int) -> bool:
    log(f"Attempt {attempt_no}/{MAX_ATTEMPTS}: opening {APP_URL}")
    # domcontentloaded, NOT networkidle: Streamlit keeps a WebSocket open, so
    # the network never idles and networkidle would always time out.
    page.goto(APP_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

    state = wait_for_state(page, READY_TIMEOUT_S)

    if state == "sleeping":
        button = wake_button(page)
        if button is None:
            log("Sleep page detected but the wake button vanished - reloading.")
            FAILURE_REASONS.append("button_vanished")
            return False
        log("Sleep page detected -> clicking 'Yes, get this app back up!'")
        button.click()
        state = wait_for_state(page, BOOT_TIMEOUT_S, accept_sleeping=False)
        if state != "running":
            log("The app did not finish booting within the timeout.")
            FAILURE_REASONS.append("boot_timeout")
            return False
        log("The app is up again after the wake click.")
    elif state == "running":
        log("The app was already awake; this visit counts as traffic.")
    elif cloud_error_page(page):
        log("Streamlit Cloud is serving its ERROR PAGE (share.streamlit.io/error) "
            f"instead of the app - reloading. {describe(page)}")
        FAILURE_REASONS.append("cloud_error")
        return False
    else:
        log(f"Page state undetermined - reloading. {describe(page)}")
        FAILURE_REASONS.append("undetermined")
        return False

    # Keep the session open so Streamlit registers a genuine viewer session.
    page.wait_for_timeout(DWELL_S * 1000)
    return app_root_visible(page)


def main() -> None:
    log(f"Target: {APP_URL}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        success = False
        for i in range(1, MAX_ATTEMPTS + 1):
            try:
                success = attempt(page, i)
            except PlaywrightTimeoutError as exc:
                log(f"Timeout during attempt {i}: {exc}")
                FAILURE_REASONS.append("timeout")
                success = False
            except Exception as exc:  # noqa: BLE001 - never crash before the report
                log(f"Unexpected error during attempt {i}: {exc!r}")
                FAILURE_REASONS.append("exception")
                success = False
            if success:
                break
            if i < MAX_ATTEMPTS:
                log("Retrying in 15s ...")
                time.sleep(15)

        if not success:
            try:
                page.screenshot(path=FAILURE_SCREENSHOT, full_page=True)
                log(f"Saved debug screenshot to {FAILURE_SCREENSHOT}")
            except Exception as exc:  # noqa: BLE001
                log(f"Could not save the debug screenshot: {exc!r}")

        context.close()
        browser.close()

    if not success:
        # The reason of the LAST attempt is the most up-to-date diagnosis.
        reason = FAILURE_REASONS[-1] if FAILURE_REASONS else "undetermined"
        title, detail = REASON_TEXT.get(reason, REASON_TEXT["undetermined"])
        history = ", ".join(FAILURE_REASONS) or "n/a"
        # "::error" lines become annotations on the run page and in the
        # failure e-mail, so the cause is visible without opening the log.
        print(f"::error title={title}::{detail} (attempts: {history})", flush=True)
        sys.exit(f"FAILED: the app is not confirmed to be running - {title}.")
    log("SUCCESS: the app is running.")


if __name__ == "__main__":
    main()
