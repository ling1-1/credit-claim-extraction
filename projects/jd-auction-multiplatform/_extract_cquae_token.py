r"""
Extract CQUAE WAF clearance cookies from a local Chrome profile via Playwright.

Usage (local machine, one-time):
    python _extract_cquae_token.py "C:\Users\legion\AppData\Local\Google\Chrome\User Data\Profile 4"

Output:
    Saves __jsl_clearance_s and ASP.NET_SessionId to .env as CQUAE_COOKIE_* variables

Then on any device:
    The cookies will be auto-loaded from .env and used for HTTP requests,
    bypassing the WAF challenge without a browser.

Note: WAF clearance cookies expire after ~30 min to a few hours.
      Re-run this script when crawling fails with WAF errors.
"""
import sys
import time
import re
from pathlib import Path
from urllib.parse import urlparse

profile_path = sys.argv[1] if len(sys.argv) > 1 else None
if not profile_path:
    print(__doc__)
    sys.exit(1)

from playwright.sync_api import sync_playwright

print("Launching Chrome and navigating to CQUAE...", file=sys.stderr)
with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir=profile_path,
        headless=False,
        channel="chrome",
    )
    page = context.new_page()
    page.goto("https://www.cquae.com/", wait_until="domcontentloaded", timeout=60000)
    time.sleep(5)
    page.wait_for_load_state("networkidle", timeout=30000)

    # Collect relevant cookies
    wanted = {"__jsl_clearance_s", "ASP.NET_SessionId", "__jsluid_s"}
    found = {}
    for cookie in context.cookies():
        if cookie["name"] in wanted and cookie["value"]:
            found[cookie["name"]] = cookie["value"]

    title = page.title()
    html = page.content()
    context.close()

    # Fallback: scrape from HTML if cookie jar didn't have them
    if "__jsl_clearance_s" not in found:
        for m in re.finditer(r'__jsl_clearance_s\s*=\s*([^;]+)', html):
            found["__jsl_clearance_s"] = m.group(1).strip()
            break

    if not found:
        print("ERROR: No CQUAE cookies found. Is the page fully loaded?", file=sys.stderr)
        print(f"  Page title: {title}", file=sys.stderr)
        sys.exit(1)

    # Save to .env
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    # Remove old CQUAE_COOKIE entries
    lines = [l for l in lines if not l.strip().startswith("CQUAE_COOKIE_")]
    for name, value in found.items():
        key = f"CQUAE_COOKIE_{name}"
        lines.append(f"{key}={value}")
        print(f"  {key}={value[:40]}...", file=sys.stderr)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n→ Saved {len(found)} cookies to {env_path}", file=sys.stderr)
    print(f"   These cookies expire after ~30 min to a few hours.", file=sys.stderr)
    print(f"   Re-run this script when WAF errors occur.", file=sys.stderr)
