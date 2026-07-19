"""Minimal Bindery API client for swapping an AZW3 ebook link to EPUB."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("azw3-to-epub.bindery")

TEMP_STEM_SUFFIX = ".__st_epub__"
PARKED_MARKER = f"{TEMP_STEM_SUFFIX}.epub"


def _truthy(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() in {"1", "true", "yes"}


class BinderyClient:
    def __init__(self) -> None:
        self.base_url = os.environ.get("BINDERY_URL", "").rstrip("/")
        self.api_key = os.environ.get("BINDERY_API_KEY", "").strip()
        self.library_dir = Path(os.environ.get("LIBRARY_DIR", "/books")).resolve()
        # Path Bindery itself uses for the library root (may differ from our mount).
        self.bindery_library_dir = os.environ.get(
            "BINDERY_LIBRARY_DIR", str(self.library_dir)
        ).rstrip("/")
        self.enabled = bool(self.base_url) and _truthy("BINDERY_SYNC", "true")

    def bindery_path(self, local: Path) -> str:
        """Map a local library path to the absolute path Bindery expects."""
        resolved = local.resolve()
        try:
            rel = resolved.relative_to(self.library_dir)
        except ValueError:
            return str(resolved).replace("\\", "/")
        return f"{self.bindery_library_dir}/{rel.as_posix()}"

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float = 60,
    ) -> Any:
        url = f"{self.base_url}/api/v1{path}"
        data = None
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Bindery {method} {path} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Bindery {method} {path} unreachable: {exc.reason}") from exc

    def get_book(self, book_id: int) -> dict[str, Any]:
        book = self._request("GET", f"/book/{book_id}")
        if not isinstance(book, dict):
            raise RuntimeError(f"unexpected book payload for id={book_id}")
        return book

    def update_book(self, book_id: int, **fields: Any) -> dict[str, Any]:
        book = self._request("PUT", f"/book/{book_id}", fields)
        if not isinstance(book, dict):
            raise RuntimeError(f"unexpected book payload after PUT id={book_id}")
        return book

    def set_media_type(self, book_id: int, media_type: str) -> dict[str, Any]:
        return self.update_book(book_id, mediaType=media_type)

    def interactive_search(self, book_id: int) -> list[dict[str, Any]]:
        """POST /book/{id}/search — returns release dicts (does not grab)."""
        payload = self._request("POST", f"/book/{book_id}/search", {}, timeout=180)
        if isinstance(payload, dict):
            results = payload.get("results") or []
            return results if isinstance(results, list) else []
        if isinstance(payload, list):
            return payload
        return []

    def list_all_books(self, page_size: int = 100) -> list[dict[str, Any]]:
        """Page through the Bindery catalogue."""
        items: list[dict[str, Any]] = []
        offset = 0
        while True:
            qs = urllib.parse.urlencode(
                {"limit": str(page_size), "offset": str(offset)}
            )
            page = self._request("GET", f"/book?{qs}", timeout=120)
            batch: list[dict[str, Any]] = []
            total = 0
            if isinstance(page, dict):
                raw = page.get("items") or page.get("books") or []
                batch = raw if isinstance(raw, list) else []
                total = int(page.get("total") or 0)
            elif isinstance(page, list):
                batch = page
                total = offset + len(batch)
            if not batch:
                break
            items.extend(b for b in batch if isinstance(b, dict))
            offset += len(batch)
            if total and offset >= total:
                break
            if len(batch) < page_size:
                break
        return items

    def list_wanted(self) -> list[dict[str, Any]]:
        """Return Wanted/missing books (Bindery GET /wanted/missing)."""
        payload = self._request("GET", "/wanted/missing", timeout=120)
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            items = payload.get("items") or payload.get("books") or []
            return items if isinstance(items, list) else []
        return []

    def search_wanted(self, book_ids: list[int], chunk_size: int = 100) -> int:
        """Trigger Bindery search+auto-grab for wanted book IDs (POST /wanted/bulk)."""
        if not book_ids:
            return 0
        queued = 0
        for i in range(0, len(book_ids), chunk_size):
            chunk = book_ids[i : i + chunk_size]
            self._request(
                "POST",
                "/wanted/bulk",
                {"ids": chunk, "action": "search"},
                timeout=120,
            )
            queued += len(chunk)
            log.info("Queued Bindery wanted search for %s book(s)", len(chunk))
        return queued

    def search_all_wanted(self) -> int:
        """List every Wanted book and fire Bindery's search+grab for each."""
        if not self.enabled:
            return 0
        wanted = self.list_wanted()
        ids: list[int] = []
        for book in wanted:
            book_id = book.get("id")
            if isinstance(book_id, int):
                ids.append(book_id)
            elif isinstance(book_id, str) and book_id.isdigit():
                ids.append(int(book_id))
        if not ids:
            log.info("Bindery wanted search: no missing books")
            return 0
        log.info("Bindery wanted search: triggering search+grab for %s book(s)", len(ids))
        return self.search_wanted(ids)

    def _probe_state_path(self) -> Path:
        configured = os.environ.get("BINDERY_AUDIO_PROBE_STATE", "").strip()
        if configured:
            return Path(configured)
        return Path("/tmp/azw3-bindery-audio-probe.json")

    def _load_probe_state(self) -> dict[str, Any]:
        path = self._probe_state_path()
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            log.debug("Could not read audio-probe state %s", path, exc_info=True)
        return {"probed": {}}

    def _save_probe_state(self, state: dict[str, Any]) -> None:
        path = self._probe_state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception:
            log.warning("Could not write audio-probe state %s", path, exc_info=True)

    @staticmethod
    def _has_ebook(book: dict[str, Any]) -> bool:
        if book.get("ebookFilePath") or book.get("filePath"):
            return True
        for entry in book.get("bookFiles") or []:
            if isinstance(entry, dict) and entry.get("format") in (None, "ebook"):
                if entry.get("path"):
                    return True
        return False

    @staticmethod
    def _has_audiobook(book: dict[str, Any]) -> bool:
        if book.get("audiobookFilePath"):
            return True
        for entry in book.get("bookFiles") or []:
            if isinstance(entry, dict) and entry.get("format") == "audiobook":
                if entry.get("path"):
                    return True
        return False

    def probe_audiobooks_for_ebooks(self) -> dict[str, int]:
        """Promote ebook-only books to ``both`` when indexers have audiobooks.

        For each monitored ebook-without-audio book:
        1. Set mediaType to ``both`` (Bindery marks missing audio as wanted)
        2. Interactive-search; if Bindery's dual search is flaky, also probe as
           ``audiobook`` so category filters actually hit audio indexers
        3. Keep ``both`` + queue search+grab when audio releases exist
        4. Otherwise revert to ``ebook``

        Returns counts: candidates, promoted, reverted, skipped, errors.
        """
        stats = {
            "candidates": 0,
            "promoted": 0,
            "reverted": 0,
            "skipped": 0,
            "errors": 0,
        }
        if not self.enabled or not _truthy("BINDERY_AUDIO_PROBE", "true"):
            return stats

        limit = max(1, int(os.environ.get("BINDERY_AUDIO_PROBE_LIMIT", "5")))
        cooldown = float(os.environ.get("BINDERY_AUDIO_PROBE_COOLDOWN", str(7 * 86400)))
        now = time.time()
        state = self._load_probe_state()
        probed: dict[str, Any] = state.setdefault("probed", {})
        if not isinstance(probed, dict):
            probed = {}
            state["probed"] = probed

        books = self.list_all_books()
        candidates: list[dict[str, Any]] = []
        for book in books:
            book_id = book.get("id")
            if not isinstance(book_id, int):
                continue
            if not book.get("monitored"):
                continue
            media = (book.get("mediaType") or "ebook").lower()
            if media != "ebook":
                continue
            if not self._has_ebook(book) or self._has_audiobook(book):
                continue
            last = probed.get(str(book_id))
            if isinstance(last, (int, float)) and now - float(last) < cooldown:
                stats["skipped"] += 1
                continue
            candidates.append(book)

        stats["candidates"] = len(candidates)
        if not candidates:
            log.info("Bindery audio probe: no ebook-only candidates")
            return stats

        log.info(
            "Bindery audio probe: %s candidate(s), processing up to %s",
            len(candidates),
            limit,
        )

        for book in candidates[:limit]:
            book_id = int(book["id"])
            title = book.get("title") or book_id
            try:
                self.set_media_type(book_id, "both")
                results = self.interactive_search(book_id)
                audio_hits = [
                    r
                    for r in results
                    if isinstance(r, dict)
                    and (r.get("mediaType") or "").lower() == "audiobook"
                ]
                # Bindery interactive search for mediaType=both often still
                # queries ebook categories only — probe audiobook mode too.
                if not audio_hits:
                    self.set_media_type(book_id, "audiobook")
                    audio_hits = [
                        r
                        for r in self.interactive_search(book_id)
                        if isinstance(r, dict)
                    ]

                if audio_hits:
                    self.set_media_type(book_id, "both")
                    self.search_wanted([book_id])
                    stats["promoted"] += 1
                    log.info(
                        "Audio available for %s (%s) — left as both (%s release(s))",
                        title,
                        book_id,
                        len(audio_hits),
                    )
                else:
                    self.set_media_type(book_id, "ebook")
                    stats["reverted"] += 1
                    log.info(
                        "No audiobook releases for %s (%s) — reverted to ebook",
                        title,
                        book_id,
                    )
                probed[str(book_id)] = now
            except Exception:
                stats["errors"] += 1
                log.exception("Audio probe failed for book %s (%s)", book_id, title)
                try:
                    self.set_media_type(book_id, "ebook")
                except Exception:
                    log.exception("Could not restore ebook mediaType for %s", book_id)

        self._save_probe_state(state)
        log.info(
            "Bindery audio probe done: promoted=%s reverted=%s errors=%s skipped=%s",
            stats["promoted"],
            stats["reverted"],
            stats["errors"],
            stats["skipped"],
        )
        return stats

    def list_books(self, search: str, limit: int = 50) -> list[dict[str, Any]]:
        qs = urllib.parse.urlencode({"search": search, "limit": str(limit), "offset": "0"})
        page = self._request("GET", f"/book?{qs}")
        if isinstance(page, dict):
            items = page.get("items") or page.get("books") or []
            return items if isinstance(items, list) else []
        if isinstance(page, list):
            return page
        return []

    def delete_ebook_files(self, book_id: int) -> dict[str, Any] | None:
        return self._request("DELETE", f"/book/{book_id}/file?format=ebook")

    def manual_import_ebook(self, path: str, book_id: int) -> Any:
        return self._request(
            "POST",
            "/queue/manual-import",
            {"path": path, "bookId": book_id, "format": "ebook"},
        )

    def trigger_library_scan(self) -> bool:
        """Ask Bindery to rescan the library (reconcile paths after folder moves)."""
        if not self.enabled:
            return False
        try:
            self._request("POST", "/library/scan", timeout=30)
            log.info("Triggered Bindery library scan")
            return True
        except Exception:
            # Older Bindery builds may use a different path; not fatal.
            log.warning("Bindery library scan request failed", exc_info=True)
            return False

    def book_paths(self, book: dict[str, Any]) -> list[str]:
        paths: list[str] = []
        for key in ("ebookFilePath", "filePath"):
            value = book.get(key)
            if value:
                paths.append(str(value))
        for entry in book.get("bookFiles") or []:
            if isinstance(entry, dict) and entry.get("path"):
                paths.append(str(entry["path"]))
        return paths

    @staticmethod
    def _norm(path: str) -> str:
        return path.replace("\\", "/").rstrip("/").lower()

    @classmethod
    def paths_match(cls, a: str, b: str) -> bool:
        na, nb = cls._norm(a), cls._norm(b)
        if na == nb:
            return True
        # Same trailing Author/Title/file when library roots differ
        a_parts, b_parts = na.split("/"), nb.split("/")
        if len(a_parts) >= 3 and len(b_parts) >= 3:
            return a_parts[-3:] == b_parts[-3:]
        return a_parts[-1] == b_parts[-1] and len(a_parts) >= 2 and len(b_parts) >= 2 and a_parts[-2] == b_parts[-2]

    def find_book_id_for_source(self, source: Path) -> int | None:
        """Find the Bindery book that currently tracks this AZW/AZW3 path."""
        bindery_source = self.bindery_path(source)
        folder_name = source.parent.name
        title_guess = re.sub(r"\s*\(\d{4}\)\s*$", "", folder_name).strip() or source.stem
        searches = [title_guess, source.stem, folder_name]
        seen: set[int] = set()

        for term in searches:
            if not term:
                continue
            for item in self.list_books(term):
                book_id = item.get("id")
                if not isinstance(book_id, int) or book_id in seen:
                    continue
                seen.add(book_id)
                # List payload may already include paths; detail has bookFiles.
                candidates = [item]
                try:
                    candidates.append(self.get_book(book_id))
                except Exception:
                    log.debug("Could not fetch book %s detail", book_id, exc_info=True)

                for book in candidates:
                    for path in self.book_paths(book):
                        if self.paths_match(path, bindery_source) or self.paths_match(
                            path, str(source)
                        ):
                            log.info(
                                "Matched Bindery book %s (%s) to %s",
                                book_id,
                                book.get("title"),
                                bindery_source,
                            )
                            return book_id

        # Last resort: if search returned exactly one confident title hit with
        # an azw/azw3 ebook path in the same folder, accept it.
        for item in self.list_books(title_guess):
            book_id = item.get("id")
            if not isinstance(book_id, int):
                continue
            try:
                book = self.get_book(book_id)
            except Exception:
                continue
            for path in self.book_paths(book):
                normalized = path.replace("\\", "/").lower()
                if normalized.endswith((".azw", ".azw3")) and source.parent.name.lower() in normalized:
                    log.info(
                        "Folder-matched Bindery book %s (%s) for %s",
                        book_id,
                        book.get("title"),
                        bindery_source,
                    )
                    return book_id
        return None

    def _ebook_paths(self, book: dict[str, Any]) -> list[str]:
        paths: list[str] = []
        for key in ("ebookFilePath", "filePath"):
            value = book.get(key)
            if value:
                paths.append(str(value))
        for entry in book.get("bookFiles") or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("format") in (None, "ebook") and entry.get("path"):
                paths.append(str(entry["path"]))
        return paths

    def _wait_for_non_parked_ebook(self, book_id: int, timeout: float = 90) -> str | None:
        """Return Bindery's ebook path once it no longer points at our parked file."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                book = self.get_book(book_id)
            except Exception:
                log.debug("Poll book %s failed", book_id, exc_info=True)
                time.sleep(2)
                continue
            for path in self._ebook_paths(book):
                if path and PARKED_MARKER not in path.replace("\\", "/").lower():
                    if path.lower().endswith(".epub"):
                        return path
            time.sleep(2)
        return None

    @staticmethod
    def cleanup_parked_epubs(folder: Path, final_epub: Path | None = None) -> None:
        """Remove *__st_epub__.epub leftovers so Storyteller sees one ebook."""
        if not folder.is_dir():
            return
        for path in folder.glob(f"*{TEMP_STEM_SUFFIX}.epub"):
            try:
                path.unlink(missing_ok=True)
                log.info("Removed parked EPUB leftover %s", path)
            except OSError:
                log.warning("Could not remove parked EPUB %s", path, exc_info=True)
        # If Bindery never produced a final name, restore a normal .epub from parked.
        if final_epub and not final_epub.exists():
            parked = final_epub.with_name(f"{final_epub.stem}{TEMP_STEM_SUFFIX}.epub")
            if parked.exists():
                parked.rename(final_epub)
                log.info("Restored parked EPUB to %s", final_epub)

    def cleanup_all_parked_epubs(self) -> int:
        """One-shot sweep for Storyteller-blocking duplicate parked EPUBs."""
        fixed = 0
        for path in self.library_dir.rglob(f"*{TEMP_STEM_SUFFIX}.epub"):
            final = path.with_name(path.name.replace(TEMP_STEM_SUFFIX, ""))
            try:
                if final.exists():
                    path.unlink(missing_ok=True)
                    log.info("Removed parked EPUB leftover %s", path)
                else:
                    path.rename(final)
                    log.info("Renamed orphan parked EPUB to %s", final)
                fixed += 1
            except OSError:
                log.warning("Failed cleaning parked EPUB %s", path, exc_info=True)
        return fixed

    def replace_azw3_with_epub(self, source: Path, epub: Path) -> bool:
        """
        Clear Bindery's ebook link for the AZW3 and re-import the EPUB.

        Bindery's DELETE /book/{id}/file?format=ebook stem-sweeps sibling
        ebook files that share the same basename, so the EPUB is briefly
        renamed before the delete, then re-imported. Parked leftovers are
        removed afterward so Storyteller does not see two EPUBs in one folder.
        """
        if not self.enabled:
            return False

        book_id = self.find_book_id_for_source(source)
        if book_id is None:
            log.warning(
                "Bindery sync skipped — no book matched for %s. "
                "EPUB is on disk; update Bindery manually if needed.",
                source,
            )
            return False

        # Park EPUB under a different stem so delete's sibling sweep can't kill it.
        parked = epub.with_name(f"{epub.stem}{TEMP_STEM_SUFFIX}.epub")
        if parked.exists():
            parked.unlink()
        epub.rename(parked)
        log.info("Parked EPUB at %s before Bindery ebook delete", parked)

        try:
            self.delete_ebook_files(book_id)
            log.info("Cleared Bindery ebook file(s) for book %s", book_id)
        except Exception:
            # Restore EPUB name so Storyteller still sees a normal .epub
            if parked.exists() and not epub.exists():
                parked.rename(epub)
            raise

        import_path = self.bindery_path(parked)
        try:
            self.manual_import_ebook(import_path, book_id)
            log.info(
                "Queued Bindery manual-import of %s onto book %s",
                import_path,
                book_id,
            )
        except Exception:
            # Keep parked EPUB; operator can rename / import manually.
            log.error(
                "Bindery delete succeeded but import failed for book %s. "
                "EPUB left at %s — import it in Bindery onto book %s.",
                book_id,
                parked,
                book_id,
            )
            raise

        # Bindery often copies into a final .epub and leaves the parked file behind.
        # Storyteller then skips the folder ("multiple epubs of the same kind").
        final_path = self._wait_for_non_parked_ebook(book_id)
        if final_path:
            log.info("Bindery ebook path for book %s is now %s", book_id, final_path)
        else:
            log.warning(
                "Timed out waiting for Bindery to finish importing book %s; "
                "cleaning parked EPUB if a final .epub already exists",
                book_id,
            )

        if epub.exists() or final_path:
            self.cleanup_parked_epubs(epub.parent, final_epub=epub)
        elif parked.exists():
            # Import kept the parked name only — rename for Storyteller.
            parked.rename(epub)
            log.info("Renamed parked EPUB to final name %s", epub)

        return True
