"""Merge Bindery sibling folders like ``Title (2008)`` + ``Title (2008) (2)``.

Storyteller only auto-pairs ebook + audiobook in the same folder. Bindery
sometimes imports the second format into a `` (N)`` sibling; this module
moves that content into the primary folder so both formats share one path.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

log = logging.getLogger("azw3-to-epub.coalesce")

SPLIT_SUFFIX = re.compile(r"^(?P<base>.+) \((?P<n>\d{1,3})\)$")
EBOOK_SUFFIXES = {".epub", ".azw", ".azw3", ".mobi", ".pdf"}
AUDIO_SUFFIXES = {".mp3", ".m4b", ".m4a", ".flac", ".ogg", ".opus", ".aac"}


def _truthy(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() in {"1", "true", "yes"}


def _is_ebook_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in EBOOK_SUFFIXES


def _is_audio_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES


def _folder_has(folder: Path, predicate) -> bool:
    if not folder.is_dir():
        return False
    for path in folder.rglob("*"):
        if predicate(path):
            return True
    return False


def _unique_dest(dest_dir: Path, name: str) -> Path:
    candidate = dest_dir / name
    if not candidate.exists():
        return candidate
    stem = Path(name).stem
    suffix = Path(name).suffix
    n = 2
    while True:
        alt = dest_dir / f"{stem} ({n}){suffix}"
        if not alt.exists():
            return alt
        n += 1


def _move_tree(src: Path, dest_dir: Path) -> int:
    """Move files/dirs from src into dest_dir. Returns number of top-level items moved."""
    moved = 0
    for child in list(src.iterdir()):
        target = dest_dir / child.name
        if target.exists():
            if child.is_file():
                target = _unique_dest(dest_dir, child.name)
            else:
                # Merge directory contents recursively.
                target.mkdir(parents=True, exist_ok=True)
                moved += _move_tree(child, target)
                try:
                    child.rmdir()
                except OSError:
                    pass
                continue
        shutil.move(str(child), str(target))
        moved += 1
    return moved


def _remove_if_empty(folder: Path) -> bool:
    try:
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
            return True
    except OSError:
        log.warning("Could not remove empty folder %s", folder, exc_info=True)
    return False


def find_split_pairs(library_dir: Path) -> list[tuple[Path, Path]]:
    """Return (primary, split) folder pairs under each author directory."""
    pairs: list[tuple[Path, Path]] = []
    if not library_dir.is_dir():
        return pairs

    # Author/Title layout — walk one level of author dirs, then title dirs.
    for author_dir in sorted(p for p in library_dir.iterdir() if p.is_dir()):
        by_base: dict[str, dict[str, Path]] = {}
        for title_dir in (p for p in author_dir.iterdir() if p.is_dir()):
            match = SPLIT_SUFFIX.match(title_dir.name)
            if match:
                base = match.group("base")
                by_base.setdefault(base, {})["split"] = title_dir
            else:
                by_base.setdefault(title_dir.name, {})["primary"] = title_dir

        for base, slots in by_base.items():
            primary = slots.get("primary")
            split = slots.get("split")
            if primary and split:
                pairs.append((primary, split))
    return pairs


def coalesce_pair(primary: Path, split: Path) -> bool:
    """Move split folder contents into primary. Returns True if work was done."""
    if not primary.is_dir() or not split.is_dir():
        return False

    primary_has_ebook = _folder_has(primary, _is_ebook_file)
    primary_has_audio = _folder_has(primary, _is_audio_file)
    split_has_ebook = _folder_has(split, _is_ebook_file)
    split_has_audio = _folder_has(split, _is_audio_file)

    # Prefer keeping ebooks in primary; if ebook only lives in split, swap roles.
    dest, src = primary, split
    if split_has_ebook and not primary_has_ebook:
        dest, src = split, primary
        log.info(
            "Primary %s has no ebook; merging into split folder %s instead",
            primary,
            split,
        )

    # Skip if both sides already look dual-format (avoid stomping).
    if (
        (primary_has_ebook and primary_has_audio)
        and (split_has_ebook or split_has_audio)
    ):
        log.info(
            "Skip coalesce %s + %s — primary already has ebook and audio",
            primary,
            split,
        )
        return False

    # Nothing useful in src.
    if not any(src.iterdir()):
        _remove_if_empty(src)
        return False

    moved = _move_tree(src, dest)
    removed = _remove_if_empty(src)
    if moved or removed:
        log.info(
            "Coalesced %s into %s (moved=%s removed_empty=%s)",
            src,
            dest,
            moved,
            removed,
        )
        # If we merged into the former split folder, rename it to the primary name.
        if dest == split and dest.name != primary.name and not primary.exists():
            dest.rename(primary)
            log.info("Renamed coalesced folder to %s", primary)
        return True
    return False


def coalesce_library(library_dir: Path | None = None) -> int:
    """Find and merge all Bindery ``(N)`` sibling folders. Returns pairs fixed."""
    if not _truthy("FOLDER_COALESCE", "true"):
        return 0
    root = library_dir or Path(os.environ.get("LIBRARY_DIR", "/books"))
    fixed = 0
    for primary, split in find_split_pairs(root):
        try:
            if coalesce_pair(primary, split):
                fixed += 1
        except Exception:
            log.exception("Failed coalescing %s + %s", primary, split)
    if fixed:
        log.info("Folder coalesce fixed %s split pair(s)", fixed)
    else:
        log.debug("Folder coalesce: no split pairs to merge")
    return fixed
