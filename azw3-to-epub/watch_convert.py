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
from datetime import datetime, timedelta
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from bindery_client import BinderyClient
from library_coalesce import coalesce_library
from storyteller_client import StorytellerClient

LIBRARY_DIR = Path(os.environ.get("LIBRARY_DIR", "/books"))
DELETE_SOURCE = os.environ.get("DELETE_SOURCE", "false").lower() in {"1", "true", "yes"}
INITIAL_SCAN = os.environ.get("INITIAL_SCAN", "true").lower() in {"1", "true", "yes"}
STABLE_SECONDS = float(os.environ.get("STABLE_SECONDS", "5"))
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "2"))
RECURSIVE = os.environ.get("RECURSIVE", "true").lower() in {"1", "true", "yes"}
# How often to sweep Storyteller for ebook/audiobook pairs (0 = only after converts)
STORYTELLER_MERGE_INTERVAL = float(os.environ.get("STORYTELLER_MERGE_INTERVAL", "300"))
# Queue readaloud alignment for books that already have ebook + audiobook
STORYTELLER_AUTO_READALOUD = os.environ.get(
    "STORYTELLER_AUTO_READALOUD", "true"
).lower() in {"1", "true", "yes"}
STORYTELLER_READALOUD_INTERVAL = float(
    os.environ.get("STORYTELLER_READALOUD_INTERVAL", "1800")
)
# Local-time window (container TZ) when readaloud jobs may be queued
STORYTELLER_READALOUD_START = os.environ.get("STORYTELLER_READALOUD_START", "00:01")
STORYTELLER_READALOUD_END = os.environ.get("STORYTELLER_READALOUD_END", "06:00")
# How often to ask Bindery to search+grab Wanted books (Bindery's own loop is ~12h)
BINDERY_SEARCH_INTERVAL = float(os.environ.get("BINDERY_SEARCH_INTERVAL", "3600"))
BINDERY_WANTED_SEARCH = os.environ.get("BINDERY_WANTED_SEARCH", "true").lower() in {
    "1",
    "true",
    "yes",
}
# Clear failed/imported Bindery queue rows (never deletes download files)
BINDERY_CLEAR_QUEUE = os.environ.get("BINDERY_CLEAR_QUEUE", "true").lower() in {
    "1",
    "true",
    "yes",
}
BINDERY_CLEAR_QUEUE_INTERVAL = float(
    os.environ.get("BINDERY_CLEAR_QUEUE_INTERVAL", "300")
)
# Probe ebook-only books for audiobook releases; keep both or revert to ebook
BINDERY_AUDIO_PROBE = os.environ.get("BINDERY_AUDIO_PROBE", "true").lower() in {
    "1",
    "true",
    "yes",
}
BINDERY_AUDIO_PROBE_INTERVAL = float(
    os.environ.get("BINDERY_AUDIO_PROBE_INTERVAL", "3600")
)
# Merge Bindery "Title (Year) (2)" sibling folders into the primary title folder
FOLDER_COALESCE = os.environ.get("FOLDER_COALESCE", "true").lower() in {
    "1",
    "true",
    "yes",
}
FOLDER_COALESCE_INTERVAL = float(os.environ.get("FOLDER_COALESCE_INTERVAL", "300"))
# Periodic full-tree AZW3 scan — needed because Docker Desktop / Windows
# bind mounts often do not deliver reliable inotify events to watchdog.
RESCAN_INTERVAL = float(os.environ.get("RESCAN_INTERVAL", "120"))
SOURCE_SUFFIXES = {".azw", ".azw3"}
# Calibre defaults to EPUB 2; Storyteller prefers EPUB 3 (2 still works with a warning).
EPUB_VERSION = os.environ.get("EPUB_VERSION", "3").strip() or "3"

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


def _after_epub_ready(source: Path, target: Path) -> None:
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
        # Strip Bindery "Title (2008)" / "Title (2008) (2)" folder noise
        title_guess = re.sub(
            r"\s*\(\d{4}\)\s*(\(\d{1,3}\)\s*)?$", "", source.parent.name
        ).strip() or source.stem
        try:
            if not _storyteller.try_merge_for_title(title_guess):
                _storyteller.merge_all_pairs()
        except Exception:
            log.exception("Storyteller merge failed for %s", title_guess)


def convert(source: Path) -> None:
    source = source.resolve()
    with _lock:
        if source in _inflight:
            return
        _inflight.add(source)

    try:
        target = epub_path_for(source)
        needs_convert = should_convert(source)
        if not needs_convert and not (target.exists() and _bindery.enabled):
            log.debug("Skip %s (epub up to date)", source)
            return
        if not wait_until_stable(source):
            log.warning("Source disappeared before convert: %s", source)
            return

        if needs_convert:
            # Must end in .epub — Calibre picks the output plugin from the extension.
            tmp = target.with_name(f".{target.stem}.converting.epub")
            if tmp.exists():
                tmp.unlink()

            log.info("Converting %s -> %s (epub %s)", source, target, EPUB_VERSION)
            cmd = [EBOOK_CONVERT, str(source), str(tmp), "--epub-version", EPUB_VERSION]
            result = subprocess.run(
                cmd,
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
        else:
            log.info(
                "EPUB already present for %s — syncing Bindery ebook link",
                source,
            )

        _after_epub_ready(source, target)
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


def folder_coalesce_loop() -> None:
    """Periodically merge Bindery ``(2)`` sibling folders for Storyteller pairing."""
    if not FOLDER_COALESCE or FOLDER_COALESCE_INTERVAL <= 0:
        return
    time.sleep(30)
    while True:
        try:
            fixed = coalesce_library(LIBRARY_DIR)
            if fixed and _bindery.enabled:
                _bindery.trigger_library_scan()
            if fixed and _storyteller.enabled:
                # Same-folder pairs import on their own; merge covers any leftovers.
                time.sleep(10)
                _storyteller.merge_all_pairs()
        except Exception:
            log.exception("Folder coalesce sweep failed")
        time.sleep(FOLDER_COALESCE_INTERVAL)


def bindery_wanted_search_loop() -> None:
    """Periodically trigger Bindery Wanted search+grab (default every hour)."""
    if not (_bindery.enabled and BINDERY_WANTED_SEARCH and BINDERY_SEARCH_INTERVAL > 0):
        return
    # Short delay so Bindery is up after stack restart; then run immediately.
    time.sleep(60)
    while True:
        try:
            _bindery.search_all_wanted()
        except Exception:
            log.exception("Bindery wanted search failed")
        time.sleep(BINDERY_SEARCH_INTERVAL)


def bindery_clear_queue_loop() -> None:
    """Remove failed/imported Bindery queue rows so re-grabs are not blocked."""
    if not (
        _bindery.enabled and BINDERY_CLEAR_QUEUE and BINDERY_CLEAR_QUEUE_INTERVAL > 0
    ):
        return
    time.sleep(45)
    while True:
        try:
            _bindery.clear_stale_queue()
        except Exception:
            log.exception("Bindery queue cleanup failed")
        time.sleep(BINDERY_CLEAR_QUEUE_INTERVAL)


def bindery_audio_probe_loop() -> None:
    """Promote ebook-only books to both when audiobook releases exist."""
    if not (
        _bindery.enabled and BINDERY_AUDIO_PROBE and BINDERY_AUDIO_PROBE_INTERVAL > 0
    ):
        return
    time.sleep(90)
    while True:
        try:
            _bindery.probe_audiobooks_for_ebooks()
        except Exception:
            log.exception("Bindery audiobook probe failed")
        time.sleep(BINDERY_AUDIO_PROBE_INTERVAL)


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour_s, minute_s = value.strip().split(":", 1)
    hour, minute = int(hour_s), int(minute_s)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid HH:MM {value!r}")
    return hour, minute


def _minutes_since_midnight(hour: int, minute: int) -> int:
    return hour * 60 + minute


def _in_readaloud_window(now: datetime | None = None) -> bool:
    """True when local time is in [START, END) (handles overnight windows)."""
    now = now or datetime.now()
    start_h, start_m = _parse_hhmm(STORYTELLER_READALOUD_START)
    end_h, end_m = _parse_hhmm(STORYTELLER_READALOUD_END)
    cur = _minutes_since_midnight(now.hour, now.minute)
    start = _minutes_since_midnight(start_h, start_m)
    end = _minutes_since_midnight(end_h, end_m)
    if start == end:
        return True
    if start < end:
        return start <= cur < end
    # Overnight window, e.g. 22:00–06:00
    return cur >= start or cur < end


def _seconds_until_readaloud_window(now: datetime | None = None) -> float:
    now = now or datetime.now()
    if _in_readaloud_window(now):
        return 0.0
    start_h, start_m = _parse_hhmm(STORYTELLER_READALOUD_START)
    target = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def storyteller_readaloud_loop() -> None:
    """Queue Storyteller readaloud jobs for dual-format books (night window only)."""
    if not (
        _storyteller.readaloud_enabled
        and STORYTELLER_AUTO_READALOUD
        and STORYTELLER_READALOUD_INTERVAL > 0
    ):
        return
    time.sleep(120)
    while True:
        try:
            wait = _seconds_until_readaloud_window()
            if wait > 0:
                log.info(
                    "Storyteller readaloud idle until %s–%s window (sleep %ss)",
                    STORYTELLER_READALOUD_START,
                    STORYTELLER_READALOUD_END,
                    int(wait),
                )
                time.sleep(wait)
                continue
            # Merge first so ebook-only + audio-only become dual-format candidates.
            if _storyteller.enabled:
                _storyteller.merge_all_pairs()
            _storyteller.start_readalouds()
        except Exception:
            log.exception("Storyteller readaloud sweep failed")
        # Stay responsive to window end; don't sleep past 06:00.
        if _in_readaloud_window():
            time.sleep(STORYTELLER_READALOUD_INTERVAL)
        else:
            time.sleep(min(60.0, STORYTELLER_READALOUD_INTERVAL))


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


def scan_sources(label: str = "scan") -> int:
    """Queue AZW/AZW3 files that need convert and/or Bindery EPUB sync."""
    pattern = "**/*" if RECURSIVE else "*"
    found = 0
    seen: set[Path] = set()
    for path in LIBRARY_DIR.glob(pattern):
        if is_source(path) and should_convert(path):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            found += 1
            schedule(resolved)
    # Bindery may still track AZW3 even when an EPUB already exists beside it
    # (common after a grab when watchdog missed the create event).
    if _bindery.enabled:
        try:
            for path in _bindery.list_tracked_azw_sources():
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                found += 1
                schedule(resolved)
        except Exception:
            log.exception("%s: Bindery AZW3 catalogue sweep failed", label)
    if found:
        log.info("%s queued %s source file(s)", label.capitalize(), found)
    else:
        log.debug("%s found no AZW/AZW3 work", label.capitalize())
    return found


def initial_scan() -> None:
    scan_sources("initial scan")


def rescan_loop() -> None:
    """Poll the library tree for AZW3s missed by watchdog on Docker/Windows."""
    if RESCAN_INTERVAL <= 0:
        return
    while True:
        time.sleep(RESCAN_INTERVAL)
        try:
            scan_sources("periodic scan")
        except Exception:
            log.exception("Periodic AZW3 scan failed")


def main() -> None:
    if not LIBRARY_DIR.exists():
        log.error("LIBRARY_DIR does not exist: %s", LIBRARY_DIR)
        sys.exit(1)

    log.info(
        "Watching %s (recursive=%s delete_source=%s initial_scan=%s "
        "bindery_sync=%s storyteller_merge=%s folder_coalesce=%s)",
        LIBRARY_DIR,
        RECURSIVE,
        DELETE_SOURCE,
        INITIAL_SCAN,
        _bindery.enabled,
        _storyteller.enabled,
        FOLDER_COALESCE,
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

    if RESCAN_INTERVAL > 0:
        rescan_worker = threading.Thread(
            target=rescan_loop, name="azw3-rescan", daemon=True
        )
        rescan_worker.start()
        log.info("Periodic AZW3 scan enabled (every %ss)", int(RESCAN_INTERVAL))

    merge_worker = threading.Thread(
        target=storyteller_merge_loop, name="storyteller-merge", daemon=True
    )
    merge_worker.start()

    coalesce_worker = threading.Thread(
        target=folder_coalesce_loop, name="folder-coalesce", daemon=True
    )
    coalesce_worker.start()
    if FOLDER_COALESCE:
        log.info(
            "Folder coalesce enabled (every %ss)",
            int(FOLDER_COALESCE_INTERVAL),
        )

    search_worker = threading.Thread(
        target=bindery_wanted_search_loop, name="bindery-wanted-search", daemon=True
    )
    search_worker.start()
    if _bindery.enabled and BINDERY_WANTED_SEARCH:
        log.info(
            "Bindery wanted search enabled (every %ss)",
            int(BINDERY_SEARCH_INTERVAL),
        )

    clear_queue_worker = threading.Thread(
        target=bindery_clear_queue_loop, name="bindery-clear-queue", daemon=True
    )
    clear_queue_worker.start()
    if _bindery.enabled and BINDERY_CLEAR_QUEUE:
        log.info(
            "Bindery queue cleanup enabled (every %ss; failed/imported, files kept)",
            int(BINDERY_CLEAR_QUEUE_INTERVAL),
        )

    audio_probe_worker = threading.Thread(
        target=bindery_audio_probe_loop, name="bindery-audio-probe", daemon=True
    )
    audio_probe_worker.start()
    if _bindery.enabled and BINDERY_AUDIO_PROBE:
        log.info(
            "Bindery audiobook probe enabled (every %ss)",
            int(BINDERY_AUDIO_PROBE_INTERVAL),
        )

    readaloud_worker = threading.Thread(
        target=storyteller_readaloud_loop, name="storyteller-readaloud", daemon=True
    )
    readaloud_worker.start()
    if _storyteller.readaloud_enabled:
        log.info(
            "Storyteller auto-readaloud enabled (%s–%s local, every %ss, limit=%s)",
            STORYTELLER_READALOUD_START,
            STORYTELLER_READALOUD_END,
            int(STORYTELLER_READALOUD_INTERVAL),
            os.environ.get("STORYTELLER_READALOUD_LIMIT", "2"),
        )

    if INITIAL_SCAN:
        initial_scan()
        # One-shot coalesce on startup so existing (2) folders don't wait
        # for the first periodic sweep.
        if FOLDER_COALESCE:
            try:
                fixed = coalesce_library(LIBRARY_DIR)
                if fixed and _bindery.enabled:
                    _bindery.trigger_library_scan()
            except Exception:
                log.exception("Startup folder coalesce failed")

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
