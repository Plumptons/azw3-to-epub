#!/usr/bin/env python3
"""Watch a library folder and convert AZW/AZW3 ebooks to EPUB via Calibre."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from bindery_client import BinderyClient
from storyteller_client import StorytellerClient

LIBRARY_DIR = Path(os.environ.get("LIBRARY_DIR", "/books"))
DELETE_SOURCE = os.environ.get("DELETE_SOURCE", "false").lower() in {"1", "true", "yes"}
INITIAL_SCAN = os.environ.get("INITIAL_SCAN", "true").lower() in {"1", "true", "yes"}
STABLE_SECONDS = float(os.environ.get("STABLE_SECONDS", "5"))
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "2"))
RECURSIVE = os.environ.get("RECURSIVE", "true").lower() in {"1", "true", "yes"}
# How often to sweep Storyteller for ebook/audiobook pairs (0 = only after converts)
STORYTELLER_MERGE_INTERVAL = float(os.environ.get("STORYTELLER_MERGE_INTERVAL", "300"))
SOURCE_SUFFIXES = {".azw", ".azw3"}

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("azw3-to-epub")

_pending: dict[Path, float] = {}
_lock = threading.Lock()
_inflight: set[Path] = set()
_bindery = BinderyClient()
_storyteller = StorytellerClient()


def ebook_convert_bin() -> str:
    configured = os.environ.get("EBOOK_CONVERT", "").strip()
    if configured:
        return configured
    found = shutil.which("ebook-convert")
    if not found:
        log.error("ebook-convert not found on PATH")
        sys.exit(1)
    return found


EBOOK_CONVERT = ebook_convert_bin()


def is_source(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES


def epub_path_for(source: Path) -> Path:
    return source.with_suffix(".epub")


def wait_until_stable(path: Path) -> bool:
    """Return True once size stops changing for STABLE_SECONDS."""
    last_size = -1
    stable_since: float | None = None
    while True:
        if not path.exists():
            return False
        try:
            size = path.stat().st_size
        except OSError:
            return False
        now = time.monotonic()
        if size == last_size and size > 0:
            if stable_since is None:
                stable_since = now
            elif now - stable_since >= STABLE_SECONDS:
                return True
        else:
            last_size = size
            stable_since = None
        time.sleep(POLL_SECONDS)


def should_convert(source: Path) -> bool:
    if not is_source(source):
        return False
    target = epub_path_for(source)
    if not target.exists():
        return True
    try:
        return source.stat().st_mtime > target.stat().st_mtime
    except OSError:
        return True


def convert(source: Path) -> None:
    source = source.resolve()
    with _lock:
        if source in _inflight:
            return
        _inflight.add(source)

    try:
        if not should_convert(source):
            log.debug("Skip %s (epub up to date)", source)
            return
        if not wait_until_stable(source):
            log.warning("Source disappeared before convert: %s", source)
            return

        target = epub_path_for(source)
        # Must end in .epub — Calibre picks the output plugin from the extension.
        tmp = target.with_name(f".{target.stem}.converting.epub")
        if tmp.exists():
            tmp.unlink()

        log.info("Converting %s -> %s", source, target)
        result = subprocess.run(
            [EBOOK_CONVERT, str(source), str(tmp)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            log.error(
                "Conversion failed for %s (exit %s): %s",
                source,
                result.returncode,
                (result.stderr or result.stdout or "").strip()[-2000:],
            )
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            return

        tmp.replace(target)
        log.info("Wrote %s", target)

        bindery_handled = False
        if _bindery.enabled:
            try:
                bindery_handled = _bindery.replace_azw3_with_epub(source, target)
            except Exception:
                log.exception("Bindery sync failed for %s", source)

        # Bindery DELETE ?format=ebook already removes the AZW3 when sync works.
        if DELETE_SOURCE and source.exists() and not bindery_handled:
            source.unlink(missing_ok=True)
            log.info("Deleted source %s", source)

        if _storyteller.enabled:
            # Give Storyteller's auto-import a moment to see the new EPUB.
            time.sleep(float(os.environ.get("STORYTELLER_MERGE_DELAY", "15")))
            title_guess = re.sub(
                r"\s*\(\d{4}\)\s*$", "", source.parent.name
            ).strip() or source.stem
            try:
                if not _storyteller.try_merge_for_title(title_guess):
                    _storyteller.merge_all_pairs()
            except Exception:
                log.exception("Storyteller merge failed for %s", title_guess)
    finally:
        with _lock:
            _inflight.discard(source)


def schedule(path: Path) -> None:
    if not is_source(path):
        return
    with _lock:
        _pending[path.resolve()] = time.monotonic()


def drain_pending() -> None:
    while True:
        due: list[Path] = []
        now = time.monotonic()
        with _lock:
            for path, seen_at in list(_pending.items()):
                if now - seen_at >= STABLE_SECONDS:
                    due.append(path)
                    del _pending[path]
        for path in due:
            try:
                convert(path)
            except Exception:
                log.exception("Unhandled error converting %s", path)
        time.sleep(POLL_SECONDS)


def storyteller_merge_loop() -> None:
    """Periodically merge ebook-only + audiobook-only Storyteller books."""
    if not _storyteller.enabled or STORYTELLER_MERGE_INTERVAL <= 0:
        return
    # Initial sweep shortly after startup
    time.sleep(20)
    while True:
        try:
            _storyteller.merge_all_pairs()
        except Exception:
            log.exception("Storyteller periodic merge failed")
        time.sleep(STORYTELLER_MERGE_INTERVAL)


class Handler(FileSystemEventHandler):
    def on_created(self, event):  # type: ignore[no-untyped-def]
        if event.is_directory:
            return
        schedule(Path(event.src_path))

    def on_modified(self, event):  # type: ignore[no-untyped-def]
        if event.is_directory:
            return
        schedule(Path(event.src_path))

    def on_moved(self, event):  # type: ignore[no-untyped-def]
        if event.is_directory:
            return
        schedule(Path(event.dest_path))


def initial_scan() -> None:
    pattern = "**/*" if RECURSIVE else "*"
    found = 0
    for path in LIBRARY_DIR.glob(pattern):
        if is_source(path) and should_convert(path):
            found += 1
            schedule(path)
    log.info("Initial scan queued %s source file(s)", found)


def main() -> None:
    if not LIBRARY_DIR.exists():
        log.error("LIBRARY_DIR does not exist: %s", LIBRARY_DIR)
        sys.exit(1)

    log.info(
        "Watching %s (recursive=%s delete_source=%s initial_scan=%s bindery_sync=%s storyteller_merge=%s)",
        LIBRARY_DIR,
        RECURSIVE,
        DELETE_SOURCE,
        INITIAL_SCAN,
        _bindery.enabled,
        _storyteller.enabled,
    )
    if _bindery.enabled:
        log.info("Bindery API: %s", _bindery.base_url)
        fixed = _bindery.cleanup_all_parked_epubs()
        if fixed:
            log.info("Cleaned %s parked EPUB leftover(s) from prior runs", fixed)
    if _storyteller.enabled:
        log.info("Storyteller API: %s", _storyteller.base_url)
    log.info("Using %s", EBOOK_CONVERT)

    worker = threading.Thread(target=drain_pending, name="converter", daemon=True)
    worker.start()

    merge_worker = threading.Thread(
        target=storyteller_merge_loop, name="storyteller-merge", daemon=True
    )
    merge_worker.start()

    if INITIAL_SCAN:
        initial_scan()

    observer = Observer()
    observer.schedule(Handler(), str(LIBRARY_DIR), recursive=RECURSIVE)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
