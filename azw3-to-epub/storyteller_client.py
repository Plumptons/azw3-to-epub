"""Storyteller API client: login + auto-merge ebook/audiobook pairs."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger("azw3-to-epub.storyteller")


def _truthy(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() in {"1", "true", "yes"}


_NOISE_PHRASES = (
    "unabridged",
    "abridged",
    "unabr",
    "with bonus short story",
    "with bonus",
)


def normalize_title(title: str) -> str:
    """Normalize titles for ebook/audiobook pairing across naming styles.

    Handles Bindery `(2)` folders and common audiobook/ebook title drift:
      The Temporal Void (2008)
      The Temporal Void (2008) (2)
      The Temporal Void (Commonwealth: The Void Trilogy Book 2)
      Night Without Stars (Unabridged)
      Commonwealth Saga Book 1: Pandora's Star
    """
    text = title.lower().strip()
    # Prefer the part after the last colon when it looks like a series prefix.
    # "commonwealth saga book 1: pandora's star" -> "pandora's star"
    if ":" in text:
        left, right = text.rsplit(":", 1)
        if len(right.strip()) >= 4 and (
            "book" in left or "saga" in left or "trilogy" in left or "series" in left
        ):
            text = right.strip()
    # Drop all parentheticals: years, (2), series subtitles, Unabridged, etc.
    text = re.sub(r"\([^)]*\)", " ", text)
    # "Commonwealth Saga 2 - Judas Unchained" -> keep trailing title after dash
    # when the left side looks like a series label.
    if " - " in text:
        left, right = text.rsplit(" - ", 1)
        if len(right.strip()) >= 4 and (
            "book" in left or "saga" in left or "trilogy" in left or "void" in left
            or re.search(r"\b\d+\b", left)
        ):
            text = right.strip()
    for phrase in _NOISE_PHRASES:
        text = text.replace(phrase, " ")
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+\d{1,3}$", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def titles_match(a: str, b: str) -> bool:
    """True when normalized titles are equal or one cleanly contains the other."""
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    # Avoid weak matches on tiny titles ("red", "gold").
    if len(shorter) < 8:
        return False
    padded = f" {longer} "
    return longer.startswith(shorter + " ") or f" {shorter} " in padded


def normalize_name(name: str) -> str:
    text = name.lower().strip()
    if "," in text:
        last, first = [p.strip() for p in text.split(",", 1)]
        if first:
            text = f"{first} {last}"
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


class StorytellerClient:
    def __init__(self) -> None:
        self.base_url = os.environ.get("STORYTELLER_URL", "").rstrip("/")
        self.username = os.environ.get("STORYTELLER_USERNAME", "").strip()
        self.password = os.environ.get("STORYTELLER_PASSWORD", "").strip()
        self.enabled = (
            bool(self.base_url)
            and bool(self.username)
            and bool(self.password)
            and _truthy("STORYTELLER_AUTO_MERGE", "true")
        )
        self._token: str | None = None
        self._user_id: str | None = None
        self._token_at = 0.0

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        form: dict[str, str] | None = None,
        auth: bool = True,
        timeout: float = 60,
    ) -> Any:
        url = f"{self.base_url}{path}"
        headers: dict[str, str] = {"Accept": "application/json"}
        data: bytes | None = None

        if form is not None:
            boundary = "----stboundary7MA4YWxkTrZu0gW"
            parts: list[bytes] = []
            for key, value in form.items():
                parts.append(f"--{boundary}\r\n".encode())
                parts.append(
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
                )
                parts.append(value.encode("utf-8"))
                parts.append(b"\r\n")
            parts.append(f"--{boundary}--\r\n".encode())
            data = b"".join(parts)
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        if auth:
            token = self._ensure_token()
            headers["Cookie"] = f"st_token={token}"
            headers["Authorization"] = f"Bearer {token}"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if auth and exc.code in {401, 403}:
                self._token = None
            raise RuntimeError(f"Storyteller {method} {path} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Storyteller {method} {path} unreachable: {exc.reason}") from exc

    def _ensure_token(self) -> str:
        # Refresh hourly; Storyteller sessions are long-lived but this is cheap.
        if self._token and (time.time() - self._token_at) < 3600:
            return self._token
        payload = self._request(
            "POST",
            "/api/token",
            form={"username": self.username, "password": self.password},
            auth=False,
        )
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise RuntimeError("Storyteller login did not return access_token")
        self._token = str(payload["access_token"])
        self._token_at = time.time()
        self._user_id = None
        return self._token

    def current_user_id(self) -> str:
        if self._user_id:
            return self._user_id
        user = self._request("GET", "/api/v2/user")
        if not isinstance(user, dict) or not user.get("id"):
            raise RuntimeError("Storyteller /api/v2/user missing id")
        self._user_id = str(user["id"])
        return self._user_id

    def list_books(self) -> list[dict[str, Any]]:
        books = self._request("GET", "/api/v2/books")
        return books if isinstance(books, list) else []

    @staticmethod
    def author_names(book: dict[str, Any]) -> set[str]:
        names: set[str] = set()
        for author in book.get("authors") or []:
            if isinstance(author, dict) and author.get("name"):
                names.add(normalize_name(str(author["name"])))
            elif isinstance(author, str):
                names.add(normalize_name(author))
        return {n for n in names if n}

    @classmethod
    def authors_compatible(cls, a: dict[str, Any], b: dict[str, Any]) -> bool:
        left, right = cls.author_names(a), cls.author_names(b)
        if not left or not right:
            return True
        return bool(left & right)

    def find_merge_pairs(self, books: list[dict[str, Any]] | None = None) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Return (ebook_book, audiobook_book) pairs that should be merged."""
        books = books if books is not None else self.list_books()
        ebook_only: list[dict[str, Any]] = []
        audio_only: list[dict[str, Any]] = []

        for book in books:
            has_ebook = bool(book.get("ebook"))
            has_audio = bool(book.get("audiobook"))
            if has_ebook and not has_audio:
                ebook_only.append(book)
            elif has_audio and not has_ebook:
                audio_only.append(book)

        pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        used_audio: set[str] = set()

        for ebook in ebook_only:
            e_raw = str(ebook.get("title") or "")
            if not normalize_title(e_raw):
                continue
            candidates = [
                audio
                for audio in audio_only
                if str(audio.get("uuid")) not in used_audio
                and titles_match(e_raw, str(audio.get("title") or ""))
                and self.authors_compatible(ebook, audio)
            ]
            if len(candidates) != 1:
                continue
            audio = candidates[0]
            used_audio.add(str(audio["uuid"]))
            pairs.append((ebook, audio))

        return pairs

    def merge_pair(self, ebook: dict[str, Any], audiobook: dict[str, Any]) -> dict[str, Any]:
        """Merge audiobook into ebook book (ebook uuid kept as primary)."""
        status = (
            (ebook.get("status") or {}).get("uuid")
            or (audiobook.get("status") or {}).get("uuid")
        )
        if not status:
            raise RuntimeError("Cannot merge — neither book has a status uuid")

        creators: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        def add_creator(name: str, role: str) -> None:
            key = (normalize_name(name), role)
            if not name or key in seen:
                return
            seen.add(key)
            creators.append({"name": name, "fileAs": name, "role": role})

        for author in ebook.get("authors") or audiobook.get("authors") or []:
            name = author.get("name") if isinstance(author, dict) else str(author)
            add_creator(str(name), "aut")
        for narrator in audiobook.get("narrators") or ebook.get("narrators") or []:
            name = narrator.get("name") if isinstance(narrator, dict) else str(narrator)
            add_creator(str(name), "nrt")
        for creator in ebook.get("creators") or []:
            if isinstance(creator, dict) and creator.get("name"):
                add_creator(str(creator["name"]), str(creator.get("role") or "aut"))

        collections = list(
            {
                *(c.get("uuid") for c in (ebook.get("collections") or []) if c.get("uuid")),
                *(c.get("uuid") for c in (audiobook.get("collections") or []) if c.get("uuid")),
            }
        )
        tags = list(
            {
                *(t.get("name") for t in (ebook.get("tags") or []) if t.get("name")),
                *(t.get("name") for t in (audiobook.get("tags") or []) if t.get("name")),
            }
        )
        series = ebook.get("series") or audiobook.get("series") or []

        # Keep update minimal — Storyteller often 500s after a successful merge
        # while regenerating covers if we send a heavy metadata payload.
        body = {
            "update": {
                "title": ebook.get("title") or audiobook.get("title"),
            },
            "relations": {
                "creators": creators,
                "status": {"statusUuid": status, "userId": self.current_user_id()},
                "series": series,
                "collections": collections,
                "tags": tags,
            },
            "from": [ebook["uuid"], audiobook["uuid"]],
        }
        try:
            merged = self._request("POST", "/api/v2/books/merge", body=body)
        except RuntimeError as exc:
            # Observed: merge commits, then cover rewrite throws 500.
            if "HTTP 500" in str(exc):
                primary = self._request("GET", f"/api/v2/books/{ebook['uuid']}")
                if (
                    isinstance(primary, dict)
                    and primary.get("ebook")
                    and primary.get("audiobook")
                ):
                    log.warning(
                        "Storyteller returned 500 after merge, but %s now has both formats",
                        primary.get("title"),
                    )
                    return primary
            raise
        log.info(
            "Merged Storyteller books: %s + %s -> %s",
            ebook.get("title"),
            audiobook.get("title"),
            (merged or {}).get("uuid") if isinstance(merged, dict) else "?",
        )
        return merged if isinstance(merged, dict) else {}

    def merge_all_pairs(self) -> int:
        if not self.enabled:
            return 0
        pairs = self.find_merge_pairs()
        merged_count = 0
        for ebook, audiobook in pairs:
            try:
                self.merge_pair(ebook, audiobook)
                merged_count += 1
            except Exception:
                log.exception(
                    "Failed to merge Storyteller pair %s / %s",
                    ebook.get("uuid"),
                    audiobook.get("uuid"),
                )
        if merged_count:
            log.info("Storyteller auto-merge completed %s pair(s)", merged_count)
        else:
            log.debug("Storyteller auto-merge: no eligible pairs")
        return merged_count

    def try_merge_for_title(self, title: str) -> bool:
        """After a convert, try to merge a matching ebook/audiobook pair by title."""
        if not self.enabled or not title.strip():
            return False
        needle = normalize_title(title)
        books = self.list_books()
        pairs = [
            (e, a)
            for e, a in self.find_merge_pairs(books)
            if normalize_title(str(e.get("title") or "")) == needle
            or normalize_title(str(a.get("title") or "")) == needle
        ]
        if not pairs:
            return False
        self.merge_pair(pairs[0][0], pairs[0][1])
        return True
