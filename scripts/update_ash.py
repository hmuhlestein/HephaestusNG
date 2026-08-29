#!/usr/bin/env python3
"""Check/update the pinned Automated Security Helper (ASH) version in
scripts/ash.

scripts/ash execs a single git tag of awslabs/automated-security-helper via
uvx -- that pin never moves on its own, so a stale version can silently miss
months of new/improved security rules. Stdlib-only (urllib/json), so it runs
without the project's own venv -- doc_review invokes it directly from a
worktree's plain `python3`.

Usage:
  python3 scripts/update_ash.py --check   # report only, exit 1 if stale
  python3 scripts/update_ash.py           # rewrite the pin to latest
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = "awslabs/automated-security-helper"
DEFAULT_ASH_SCRIPT = Path(__file__).resolve().parent / "ash"
STALE_DAYS = 90
PIN_RE = re.compile(r"(git\+https://github\.com/awslabs/automated-security-helper\.git@)([^\"]+)")


def _get_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "hephaestus-ash-updater"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def current_pin(ash_script: Path) -> str:
    match = PIN_RE.search(ash_script.read_text())
    if not match:
        raise RuntimeError(f"Could not find a pinned ash version in {ash_script}")
    return match.group(2)


def release_info(tag: str) -> dict:
    return _get_json(f"https://api.github.com/repos/{REPO}/releases/tags/{tag}")


def latest_release() -> dict:
    return _get_json(f"https://api.github.com/repos/{REPO}/releases/latest")


def age_days(published_at: str) -> int:
    published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - published).days


def apply_update(ash_script: Path, new_tag: str) -> None:
    text = ash_script.read_text()
    new_text = PIN_RE.sub(rf"\g<1>{new_tag}", text)
    if new_text == text:
        raise RuntimeError("Pin substitution produced no change -- refusing to write")
    ash_script.write_text(new_text)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="Report only (exit 1 if the pin is stale) -- don't modify anything",
    )
    parser.add_argument(
        "--stale-days", type=int, default=STALE_DAYS,
        help=f"Age threshold in days (default: {STALE_DAYS})",
    )
    parser.add_argument(
        "--ash-script", type=Path, default=DEFAULT_ASH_SCRIPT,
        help="Path to the ash wrapper script (default: scripts/ash next to this file)",
    )
    args = parser.parse_args(argv)

    pin = current_pin(args.ash_script)
    try:
        pin_info = release_info(pin)
        latest = latest_release()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        print(f"[ash-update] Could not reach GitHub to check ash's version: {e}", file=sys.stderr)
        return 2

    pin_age = age_days(pin_info["published_at"])
    latest_tag = latest["tag_name"]
    is_stale = pin_age > args.stale_days

    print(f"ash is pinned to {pin} (released {pin_age} days ago, threshold {args.stale_days}). Latest release: {latest_tag}.")

    if args.check:
        print("STALE -- run without --check to update." if is_stale else "Within the freshness threshold.")
        return 1 if is_stale else 0

    if pin == latest_tag:
        print("Already on the latest release -- nothing to update.")
        return 0

    apply_update(args.ash_script, latest_tag)
    print(f"Updated {args.ash_script}: {pin} -> {latest_tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
