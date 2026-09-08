#!/usr/bin/env python3
"""One-off: create Storyteller collections from Bindery series."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "azw3-to-epub"))

from bindery_client import BinderyClient  # noqa: E402
from storyteller_client import StorytellerClient  # noqa: E402


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    os.environ.setdefault(
        "BINDERY_URL",
        sys.argv[1] if len(sys.argv) > 1 else "http://192.168.0.48:8788",
    )
    os.environ.setdefault(
        "STORYTELLER_URL",
        sys.argv[2] if len(sys.argv) > 2 else "http://192.168.0.48:8886",
    )
    os.environ.setdefault(
        "STORYTELLER_USERNAME",
        sys.argv[3] if len(sys.argv) > 3 else "admin",
    )
    os.environ.setdefault(
        "STORYTELLER_PASSWORD",
        sys.argv[4] if len(sys.argv) > 4 else "adminadmin",
    )
    os.environ.setdefault("BINDERY_SYNC", "true")
    os.environ["STORYTELLER_SYNC_COLLECTIONS"] = "true"
    if not os.environ.get("BINDERY_API_KEY"):
        print("Set BINDERY_API_KEY", file=sys.stderr)
        sys.exit(1)

    bindery = BinderyClient()
    storyteller = StorytellerClient()
    series = bindery.list_all_series()
    books = bindery.list_all_books()
    print(f"Bindery series={len(series)} books={len(books)}", flush=True)
    changed = storyteller.sync_collections_from_bindery_series(series, books)
    print(f"DONE changes={changed}", flush=True)


if __name__ == "__main__":
    main()
