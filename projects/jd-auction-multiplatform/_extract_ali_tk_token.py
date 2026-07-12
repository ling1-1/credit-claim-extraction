r"""
Extract the _m_h5_tk cookie value from a local Chrome profile via Playwright.

Usage (local machine):
    python _extract_ali_tk_token.py "C:\Users\legion\AppData\Local\Google\Chrome\User Data\Profile 4"
    python _extract_ali_tk_token.py --save-env "C:\Users\legion\AppData\Local\Google\Chrome\User Data\Profile 4"

Output:
    <token_value>

Then on any device (no browser needed):
    python multi_platform_runner.py crawl --platform ali --mode sample --limit 3 \
        --ali-tk-token <token_value>

The token is the value of the _m_h5_tk cookie, usually a short hex string
like "5f9a8b7c6d". It is the first part before "_" in the full cookie value,
and is used to sign MTOP API requests. If requests start failing, rerun this
script to get a fresh token.
"""

import argparse
import sys
import time
from pathlib import Path


def _save_to_env(env_path: Path, token: str) -> None:
    """Write ALI_TK_TOKEN=xxx into .env, preserving existing content."""
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    lines = [line for line in lines if not line.strip().startswith("ALI_TK_TOKEN=")]
    lines.append(f"ALI_TK_TOKEN={token}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  -> saved to {env_path}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract the _m_h5_tk cookie value from a local Chrome profile.",
    )
    parser.add_argument("profile_path", help="Chrome profile path")
    parser.add_argument(
        "--save-env",
        action="store_true",
        help="Persist ALI_TK_TOKEN into the project .env file.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=args.profile_path,
            headless=True,
            channel="chrome",
        )
        page = context.new_page()
        page.goto("https://zc-paimai.taobao.com/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

        for cookie in context.cookies():
            if cookie["name"] == "_m_h5_tk":
                raw = cookie["value"]
                token = raw.split("_", 1)[0]
                print(token)
                if args.save_env:
                    env_path = Path(__file__).resolve().parent / ".env"
                    _save_to_env(env_path, token)
                context.close()
                return 0

        print("ERROR: _m_h5_tk cookie not found. Are you logged into taobao in this profile?", file=sys.stderr)
        context.close()
        return 1


if __name__ == "__main__":
    sys.exit(main())
