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


def _has_epub(folder: Path) -> bool:
    return _folder_has(
        folder, lambda path: path.is_file() and path.suffix.lower() == ".epub"
    )


def _same_size(left: Path, right: Path) -> bool:
    try:
        return left.is_file() and right.is_file() and left.stat().st_size == right.stat().st_size
    except OSError:
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


def _unlink(path: Path, reason: str) -> None:
    try:
        path.unlink()
        log.info("Removed %s (%s)", path, reason)
    except OSError:
        log.warning("Could not remove %s", path, exc_info=True)


def _remove_if_empty(folder: Path) -> bool:
    try:
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
            return True
    except OSError:
        log.warning("Could not remove empty folder %s", folder, exc_info=True)
    return False


def _remove_empty_tree(folder: Path) -> bool:
    if not folder.is_dir():
        return False
    for child in list(folder.iterdir()):
        if child.is_dir():
            _remove_empty_tree(child)
    return _remove_if_empty(folder)


def _merge_tree(src: Path, dest_dir: Path, dest_has_epub: bool) -> tuple[int, bool]:
    """Move unique media from src into dest_dir. Drop duplicate ebooks.

    Returns (items_moved, dest_has_epub).
    """
    moved = 0
    if not src.is_dir() or not dest_dir.is_dir():
        return 0, dest_has_epub

    for child in list(src.iterdir()):
        target = dest_dir / child.name
        if child.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            nested_moved, dest_has_epub = _merge_tree(child, target, dest_has_epub)
            moved += nested_moved
            _remove_empty_tree(child)
            continue

        if _is_ebook_file(child) and dest_has_epub:
            reason = "duplicate ebook; primary already has EPUB"
            if target.exists() and _same_size(child, target):
                reason = "identical ebook already in primary"
            _unlink(child, reason)
            continue

        if target.exists():
            if _same_size(child, target):
                _unlink(child, "identical file already in primary")
                continue
            target = _unique_dest(dest_dir, child.name)

        shutil.move(str(child), str(target))
        moved += 1
        if target.suffix.lower() == ".epub":
            dest_has_epub = True
    return moved, dest_has_epub


def find_split_groups(library_dir: Path) -> list[tuple[Path | None, list[Path]]]:
    """Return (primary or None, split folders) under each author directory."""
    groups: list[tuple[Path | None, list[Path]]] = []
    if not library_dir.is_dir():
        return groups

    for author_dir in sorted(p for p in library_dir.iterdir() if p.is_dir()):
        primaries: dict[str, Path] = {}
        splits: dict[str, list[Path]] = {}
        for title_dir in (p for p in author_dir.iterdir() if p.is_dir()):
            match = SPLIT_SUFFIX.match(title_dir.name)
            if match:
                base = match.group("base")
                splits.setdefault(base, []).append(title_dir)
            else:
                primaries[title_dir.name] = title_dir

        bases = set(primaries) | set(splits)
        for base in sorted(bases):
            split_dirs = sorted(splits.get(base, []), key=lambda path: path.name)
            if not split_dirs:
                continue
            groups.append((primaries.get(base), split_dirs))
    return groups


def find_split_pairs(library_dir: Path) -> list[tuple[Path, Path]]:
    """Return (primary, split) folder pairs under each author directory."""
    pairs: list[tuple[Path, Path]] = []
    for primary, split_dirs in find_split_groups(library_dir):
        if primary is None:
            continue
        for split in split_dirs:
            pairs.append((primary, split))
    return pairs


def _preferred_dest(primary: Path | None, splits: list[Path]) -> Path:
    """Folder that should keep the coalesced book (prefer primary, else first split)."""
    if primary is not None and primary.is_dir():
        return primary
    return splits[0]


def coalesce_group(primary: Path | None, splits: list[Path]) -> bool:
    """Merge all ``(N)`` siblings into the primary title folder."""
    splits = [path for path in splits if path.is_dir()]
    if not splits:
        return False

    dest = _preferred_dest(primary, splits)
    sources = [path for path in splits if path != dest]
    if primary is not None and primary.is_dir() and primary != dest:
        sources.append(primary)

    changed = False
    dest_has_epub = _has_epub(dest)

    for src in sources:
        if not src.is_dir() or src == dest:
            continue
        if not any(src.iterdir()):
            if _remove_if_empty(src):
                changed = True
            continue
        moved, dest_has_epub = _merge_tree(src, dest, dest_has_epub)
        removed = _remove_empty_tree(src)
        if moved or removed:
            log.info(
                "Coalesced %s into %s (moved=%s removed_empty=%s)",
                src,
                dest,
                moved,
                removed,
            )
            changed = True

    # Orphan ``Title (Year) (2)`` with no primary — drop the collision suffix.
    if primary is None and dest.is_dir():
        match = SPLIT_SUFFIX.match(dest.name)
        if match:
            target = dest.with_name(match.group("base"))
            if not target.exists():
                dest.rename(target)
                log.info("Renamed orphan split folder to %s", target)
                changed = True
    elif (
        primary is not None
        and dest != primary
        and dest.is_dir()
        and not primary.exists()
    ):
        dest.rename(primary)
        log.info("Renamed coalesced folder to %s", primary)
        changed = True

    return changed


def coalesce_pair(primary: Path, split: Path) -> bool:
    """Move split folder contents into primary. Returns True if work was done."""
    return coalesce_group(primary, [split])


def coalesce_library(library_dir: Path | None = None) -> int:
    """Find and merge all Bindery ``(N)`` sibling folders. Returns groups fixed."""
    if not _truthy("FOLDER_COALESCE", "true"):
        return 0
    root = library_dir or Path(os.environ.get("LIBRARY_DIR", "/books"))
    fixed = 0
    for primary, splits in find_split_groups(root):
        try:
            if coalesce_group(primary, splits):
                fixed += 1
        except Exception:
            log.exception("Failed coalescing %s + %s", primary, splits)
    if fixed:
        log.info("Folder coalesce fixed %s split group(s)", fixed)
    else:
        log.debug("Folder coalesce: no split pairs to merge")
    return fixed
