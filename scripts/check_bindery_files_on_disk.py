#!/usr/bin/env python3
"""Check whether Bindery ebook/audiobook paths exist on the library disk.

Pass --fix to unlink catalogue paths that are missing on disk.
Never deletes a library file: only Bindery metadata, and only when the
mapped path is not an existing file.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
        os.environ.setdefault(key, value)


_load_dotenv()

BINDERY_BASE = os.environ.get("BINDERY_URL", "http://192.168.0.48:8788").rstrip("/")
BINDERY_KEY = os.environ.get("BINDERY_API_KEY", "").strip()
BINDERY_ROOT = os.environ.get("BINDERY_LIBRARY_DIR", "/media/books").replace("\\", "/").rstrip("/")
LIBRARY_DIR = Path(os.environ.get("LIBRARY_DIR", r"M:\books"))
AUDIO_SUFFIXES = {".m4b", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".wav"}
EBOOK_SUFFIXES = {".epub", ".azw3", ".azw", ".mobi", ".pdf", ".kepub"}


def get(path: str):
    req = urllib.request.Request(
        BINDERY_BASE + path,
        headers={"X-Api-Key": BINDERY_KEY, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def list_all_books() -> list[dict]:
    items: list[dict] = []
    offset = 0
    page_size = 100
    while True:
        qs = urllib.parse.urlencode({"limit": str(page_size), "offset": str(offset)})
        page = get(f"/api/v1/book?{qs}")
        batch = []
        total = 0
        if isinstance(page, dict):
            batch = page.get("items") or page.get("books") or []
            total = int(page.get("total") or 0)
        elif isinstance(page, list):
            batch = page
        if not batch:
            break
        items.extend(b for b in batch if isinstance(b, dict))
        offset += len(batch)
        if total and offset >= total:
            break
        if len(batch) < page_size:
            break
    return items


def get_book(book_id: int) -> dict:
    book = get(f"/api/v1/book/{book_id}")
    return book if isinstance(book, dict) else {}


def local_path(bindery_file: str) -> Path | None:
    normalized = bindery_file.replace("\\", "/").rstrip("/")
    root = BINDERY_ROOT.lower()
    path_l = normalized.lower()
    if path_l == root or path_l.startswith(root + "/"):
        rel = normalized[len(BINDERY_ROOT) :].lstrip("/")
        return LIBRARY_DIR / rel.replace("/", os.sep)
    parts = normalized.split("/")
    for i, part in enumerate(parts):
        if part.lower() == "books" and i + 1 < len(parts):
            rel = "/".join(parts[i + 1 :])
            return LIBRARY_DIR / rel.replace("/", os.sep)
    return None


def file_entries(book: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    ebook = book.get("ebookFilePath") or book.get("filePath")
    if ebook:
        out.append(("ebook", str(ebook)))
    audio = book.get("audiobookFilePath")
    if audio:
        out.append(("audiobook", str(audio)))
    for entry in book.get("bookFiles") or []:
        if not isinstance(entry, dict) or not entry.get("path"):
            continue
        fmt = str(entry.get("format") or "ebook")
        out.append((fmt, str(entry["path"])))
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for kind, path in out:
        key = path.replace("\\", "/").lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append((kind, path))
    return unique


def delete_file_link(book_id: int, kind: str):
    qs = urllib.parse.urlencode({"format": kind, "deleteFiles": "false"})
    req = urllib.request.Request(
        f"{BINDERY_BASE}/api/v1/book/{book_id}/file?{qs}",
        headers={"X-Api-Key": BINDERY_KEY, "Accept": "application/json"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, ""
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:400]


def disk_ok(local: Path, kind: str) -> bool:
    if local.is_file():
        return True
    if not local.is_dir():
        return False
    suffixes = AUDIO_SUFFIXES if kind == "audiobook" else (AUDIO_SUFFIXES | EBOOK_SUFFIXES)
    try:
        for child in local.rglob("*"):
            if child.is_file() and child.suffix.lower() in suffixes:
                return True
    except OSError:
        return False
    return False


def main() -> None:
    fix = "--fix" in sys.argv
    if not BINDERY_KEY:
        print("Set BINDERY_API_KEY", file=sys.stderr)
        sys.exit(1)
    if not LIBRARY_DIR.is_dir():
        print(f"LIBRARY_DIR missing: {LIBRARY_DIR}", file=sys.stderr)
        sys.exit(1)
    books = list_all_books()
    print(f"Bindery books={len(books)} library={LIBRARY_DIR} fix={fix}", flush=True)

    missing: list[str] = []
    to_clear: list[tuple[int, str, str, Path]] = []
    exists = 0
    no_path_wanted = 0
    no_path_imported = 0
    no_path_status: Counter[str] = Counter()
    fetched = 0

    for book in books:
        title = book.get("title") or f"id={book.get('id')}"
        status = str(book.get("status") or "").lower()
        book_id = book.get("id")
        entries = file_entries(book)
        if not entries and status in {"imported", "downloaded", "ok"} and isinstance(book_id, int):
            try:
                book = {**book, **get_book(int(book_id))}
                fetched += 1
                entries = file_entries(book)
            except Exception as exc:
                missing.append(f"DETAIL FAIL {title}: {exc}")
                continue
        if not entries:
            no_path_status[status or "(blank)"] += 1
            if status in {"wanted", "missing"}:
                no_path_wanted += 1
            else:
                no_path_imported += 1
            continue
        for kind, path in entries:
            local = local_path(path)
            if local is None:
                missing.append(f"UNMAPPED {kind} {title}: {path}")
                continue
            if disk_ok(local, kind):
                exists += 1
            else:
                missing.append(
                    f"MISSING {kind} {title} [{status}] bindery={path}"
                )
                if isinstance(book_id, int):
                    to_clear.append((int(book_id), kind, title, local))

    print(
        f"files/dirs on disk={exists} missing={len(missing)} "
        f"wanted_no_file={no_path_wanted} other_no_file={no_path_imported} "
        f"detail_fetches={fetched}"
    )
    if no_path_status:
        print("no_path by status:", dict(no_path_status))
    print()
    for line in missing:
        print(line)

    if not fix:
        return

    print("\n--- unlink missing Bindery paths (deleteFiles=false) ---", flush=True)
    cleared = 0
    skipped = 0
    for book_id, kind, title, local in to_clear:
        if local.is_file():
            print(f"SKIP existing file {title} {kind}: {local}")
            skipped += 1
            continue
        status, body = delete_file_link(book_id, kind)
        print(f"CLEAR {title} {kind} HTTP {status} {local} {body}")
        if status in {200, 204}:
            cleared += 1
    print(f"DONE cleared={cleared} skipped_existing_file={skipped}")


if __name__ == "__main__":
    main()
