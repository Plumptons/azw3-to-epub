"""Minimal Bindery API client for swapping an AZW3 ebook link to EPUB."""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("azw3-to-epub.bindery")

TEMP_STEM_SUFFIX = ".__st_epub__"


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

    def replace_azw3_with_epub(self, source: Path, epub: Path) -> bool:
        """
        Clear Bindery's ebook link for the AZW3 and re-import the EPUB.

        Bindery's DELETE /book/{id}/file?format=ebook stem-sweeps sibling
        ebook files that share the same basename, so the EPUB is briefly
        renamed before the delete, then re-imported.
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

        return True
