"""
Keep the AegisTrader Streamlit app awake.

Streamlit Community Cloud hibernates apps after ~12h without traffic. Waking a
sleeping app requires clicking a button on the sleep page, so a plain HTTP
request is not enough -- we need a real (headless) browser session. This script:
  1. Opens the app in headless Chromium (a real WebSocket session = real traffic).
  2. If the sleep page is shown, clicks the wake button.
  3. Waits for the container to boot.

Run on a schedule (e.g. every 6h) from GitHub Actions.
"""

import os
import sys
from playwright.sync_api import sync_playwright

APP_URL = os.environ.get("STREAMLIT_APP_URL")
if not APP_URL:
    sys.exit("ERROR: STREAMLIT_APP_URL environment variable is not set.")

# Substring of the sleep-page wake button ("Yes, get this app back up!"),
# matched case-insensitively so minor wording changes do not break the selector.
WAKE_BUTTON_TEXT = "get this app back up"
BOOT_WAIT_MS = 90_000  # ~90s for the container to spin up after a wake click


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # domcontentloaded, NOT networkidle: Streamlit keeps a WebSocket open,
        # so the network never fully idles and networkidle would time out.
        page.goto(APP_URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(5_000)  # let the JS render the sleep page if present

        wake_button = page.get_by_text(WAKE_BUTTON_TEXT, exact=False)

        if wake_button.count() > 0:
            print("Sleep page detected -> clicking the wake button.")
            wake_button.first.click()
            page.wait_for_timeout(BOOT_WAIT_MS)
            print("Wake click sent; the app should be booting.")
        else:
            print("App already awake (no wake button found). Visit counted as traffic.")

        browser.close()


if __name__ == "__main__":
    main()
