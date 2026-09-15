#!/usr/bin/env python3
"""One-off: assign Storyteller series from Bindery series.

Requires env (or .env): BINDERY_API_KEY, STORYTELLER_USERNAME, STORYTELLER_PASSWORD.
Optional: BINDERY_URL / STORYTELLER_URL (Docker DNS defaults for on-box runs).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "azw3-to-epub"))


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key == "BINDERY_URL" and ("bindery:" in value or value.endswith(":8787")):
            continue
        if key == "STORYTELLER_URL" and "storyteller:" in value:
            continue
        os.environ.setdefault(key, value)


_load_dotenv()

from bindery_client import BinderyClient  # noqa: E402
from storyteller_client import StorytellerClient  # noqa: E402


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Optional positional overrides for off-box one-shots; no password defaults.
    if len(sys.argv) > 1:
        os.environ["BINDERY_URL"] = sys.argv[1]
    if len(sys.argv) > 2:
        os.environ["STORYTELLER_URL"] = sys.argv[2]
    if len(sys.argv) > 3:
        os.environ["STORYTELLER_USERNAME"] = sys.argv[3]
    if len(sys.argv) > 4:
        os.environ["STORYTELLER_PASSWORD"] = sys.argv[4]

    os.environ.setdefault("BINDERY_URL", "http://bindery:8787")
    os.environ.setdefault("STORYTELLER_URL", "http://storyteller:8001")
    os.environ.setdefault("BINDERY_SYNC", "true")
    os.environ["STORYTELLER_SYNC_SERIES"] = "true"

    if not os.environ.get("BINDERY_API_KEY"):
        print("Set BINDERY_API_KEY", file=sys.stderr)
        sys.exit(1)
    if not os.environ.get("STORYTELLER_USERNAME") or not os.environ.get(
        "STORYTELLER_PASSWORD"
    ):
        print("Set STORYTELLER_USERNAME and STORYTELLER_PASSWORD", file=sys.stderr)
        sys.exit(1)

    bindery = BinderyClient()
    storyteller = StorytellerClient()
    series = bindery.list_all_series()
    books = bindery.list_all_books()
    print(f"Bindery series={len(series)} books={len(books)}", flush=True)
    changed = storyteller.sync_series_from_bindery(series, books)
    print(f"DONE changes={changed}", flush=True)


if __name__ == "__main__":
    main()
