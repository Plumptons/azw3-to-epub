"""Storyteller API client: login + auto-merge ebook/audiobook pairs."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
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
        self.configured = bool(self.base_url and self.username and self.password)
        # Legacy "enabled" flag = merge feature (keeps existing compose behavior).
        self.enabled = self.configured and _truthy("STORYTELLER_AUTO_MERGE", "true")
        self.readaloud_enabled = self.configured and _truthy(
            "STORYTELLER_AUTO_READALOUD", "true"
        )
        self.collections_enabled = self.configured and _truthy(
            "STORYTELLER_SYNC_COLLECTIONS", "true"
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

    def list_collections(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/v2/collections")
        if isinstance(payload, list):
            return [c for c in payload if isinstance(c, dict)]
        if isinstance(payload, dict):
            raw = payload.get("items") or payload.get("collections") or []
            return [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []
        return []

    def create_collection(
        self, name: str, *, public: bool = True, description: str = ""
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, "public": public}
        if description.strip():
            body["description"] = description.strip()
        created = self._request("POST", "/api/v2/collections", body=body)
        if not isinstance(created, dict) or not created.get("uuid"):
            raise RuntimeError(f"Storyteller create collection {name!r} returned no uuid")
        return created

    def add_books_to_collections(
        self, collection_uuids: list[str], book_uuids: list[str]
    ) -> None:
        if not collection_uuids or not book_uuids:
            return
        self._request(
            "POST",
            "/api/v2/collections/books",
            body={"collections": collection_uuids, "books": book_uuids},
        )

    def remove_books_from_collections(
        self, collection_uuids: list[str], book_uuids: list[str]
    ) -> None:
        if not collection_uuids or not book_uuids:
            return
        self._request(
            "DELETE",
            "/api/v2/collections/books",
            body={"collections": collection_uuids, "books": book_uuids},
        )

    def delete_collection(self, collection_uuid: str) -> None:
        if not collection_uuid:
            return
        self._request("DELETE", f"/api/v2/collections/{collection_uuid}")

    def update_collection(self, collection_uuid: str, **fields: Any) -> dict[str, Any]:
        updated = self._request(
            "PUT", f"/api/v2/collections/{collection_uuid}", body=fields
        )
        return updated if isinstance(updated, dict) else {}

    @staticmethod
    def _collection_name_key(name: str) -> str:
        return re.sub(r"\s+", " ", name.strip().casefold())

    @staticmethod
    def _bindery_authors_by_id(bindery_books: list[dict[str, Any]]) -> dict[int, str]:
        names: dict[int, str] = {}
        for book in bindery_books:
            aid = book.get("authorId")
            if not isinstance(aid, int) or aid in names:
                continue
            author = book.get("author")
            if isinstance(author, dict):
                label = author.get("authorName") or author.get("name")
            elif isinstance(author, str):
                label = author
            else:
                label = None
            if label:
                names[aid] = str(label)
        return names

    @classmethod
    def _bindery_member_as_book(
        cls, member: dict[str, Any], authors_by_id: dict[int, str]
    ) -> dict[str, Any] | None:
        nested = member.get("book") if isinstance(member.get("book"), dict) else member
        if not isinstance(nested, dict):
            return None
        title = str(nested.get("title") or "").strip()
        if not title:
            return None
        authors: list[dict[str, str]] = []
        author = nested.get("author")
        if isinstance(author, dict) and (author.get("authorName") or author.get("name")):
            authors.append(
                {"name": str(author.get("authorName") or author.get("name"))}
            )
        elif isinstance(author, str) and author.strip():
            authors.append({"name": author})
        aid = nested.get("authorId")
        if not authors and isinstance(aid, int) and aid in authors_by_id:
            authors.append({"name": authors_by_id[aid]})
        position = member.get("positionInSeries")
        if position is None:
            position = nested.get("positionInSeries")
        return {
            "title": title,
            "authors": authors,
            "positionInSeries": position,
        }

    @staticmethod
    def _parse_series_position(raw: Any) -> float | None:
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            match = re.search(r"\d+(?:\.\d+)?", text)
            return float(match.group(0)) if match else None

    @staticmethod
    def _format_series_position(value: float | None) -> str | None:
        if value is None:
            return None
        if value.is_integer():
            return str(int(value))
        return format(value, "g")

    def _collection_reading_order(
        self,
        entries: list[tuple[str, float | None]],
        books_by_uuid: dict[str, dict[str, Any]],
    ) -> str:
        def sort_key(item: tuple[str, float | None]) -> tuple[int, float, str]:
            uuid, pos = item
            title = str((books_by_uuid.get(uuid) or {}).get("title") or "")
            return (0 if pos is not None else 1, pos if pos is not None else 0.0, title.casefold())

        lines: list[str] = []
        for uuid, pos in sorted(entries, key=sort_key):
            title = str((books_by_uuid.get(uuid) or {}).get("title") or uuid)
            number = self._format_series_position(pos)
            lines.append(f"{number}. {title}" if number else title)
        return "\n".join(lines)

    def _match_storyteller_book(
        self, bindery_book: dict[str, Any], storyteller_books: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        title = str(bindery_book.get("title") or "")
        matches = [
            book
            for book in storyteller_books
            if titles_match(title, str(book.get("title") or ""))
            and self.authors_compatible(bindery_book, book)
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def _book_collection_ids(self, book: dict[str, Any]) -> set[str]:
        ids: set[str] = set()
        for coll in book.get("collections") or []:
            if isinstance(coll, dict) and coll.get("uuid"):
                ids.add(str(coll["uuid"]))
        return ids

    def sync_collections_from_bindery_series(
        self,
        series_list: list[dict[str, Any]],
        bindery_books: list[dict[str, Any]] | None = None,
    ) -> int:
        """Create Storyteller collections from Bindery series.

        A book that appears in multiple series is assigned only to the
        largest series (Bindery member count). Smaller overlapping series are
        not created; leftover collections from a prior sync are deleted.
        """
        if not self.collections_enabled:
            return 0
        storyteller_books = self.list_books()
        books_by_uuid = {
            str(b.get("uuid")): b for b in storyteller_books if b.get("uuid")
        }
        existing = {
            self._collection_name_key(str(c.get("name") or "")): c
            for c in self.list_collections()
            if c.get("name") and c.get("uuid")
        }
        authors_by_id = self._bindery_authors_by_id(bindery_books or [])

        # series name -> (bindery_count, matched uuid+position)
        series_matches: list[tuple[str, int, list[tuple[str, float | None]]]] = []
        bindery_series_keys: set[str] = set()
        for series in series_list:
            name = str(series.get("title") or "").strip()
            if not name:
                continue
            members = series.get("books") or []
            if not isinstance(members, list):
                continue
            bindery_series_keys.add(self._collection_name_key(name))
            bindery_count = len(members)
            matched: list[tuple[str, float | None]] = []
            seen: set[str] = set()
            for member in members:
                if not isinstance(member, dict):
                    continue
                bindery_book = self._bindery_member_as_book(member, authors_by_id)
                if not bindery_book:
                    continue
                st_book = self._match_storyteller_book(bindery_book, storyteller_books)
                uuid = str((st_book or {}).get("uuid") or "")
                if not uuid or uuid in seen:
                    continue
                seen.add(uuid)
                pos = self._parse_series_position(bindery_book.get("positionInSeries"))
                matched.append((uuid, pos))
            series_matches.append((name, bindery_count, matched))

        # Each Storyteller book -> winning series (most Bindery members).
        winners: dict[str, tuple[int, int, str, float | None]] = {}
        for name, bindery_count, matched in series_matches:
            matched_count = len(matched)
            rank = (bindery_count, matched_count)
            for uuid, pos in matched:
                current = winners.get(uuid)
                if current is None or rank > current[:2]:
                    winners[uuid] = (*rank, name, pos)
                elif rank == current[:2] and name < current[2]:
                    winners[uuid] = (*rank, name, pos)

        assigned: dict[str, list[tuple[str, float | None]]] = {}
        for uuid, (_count, _matched_count, name, pos) in winners.items():
            assigned.setdefault(name, []).append((uuid, pos))

        created = 0
        added = 0
        removed = 0
        deleted = 0
        numbered = 0

        for name, entries in assigned.items():
            uuids = [uuid for uuid, _pos in entries]
            description = self._collection_reading_order(entries, books_by_uuid)
            key = self._collection_name_key(name)
            collection = existing.get(key)
            if collection is None:
                collection = self.create_collection(
                    name, public=True, description=description
                )
                existing[key] = collection
                created += 1
                numbered += 1
                log.info("Created Storyteller collection %s", name)
            collection_uuid = str(collection.get("uuid") or "")
            if not collection_uuid:
                continue
            already = {
                uuid
                for uuid in uuids
                if collection_uuid
                in self._book_collection_ids(books_by_uuid.get(uuid) or {})
            }
            to_add = [uuid for uuid in uuids if uuid not in already]
            extras = [
                uuid
                for uuid, book in books_by_uuid.items()
                if collection_uuid in self._book_collection_ids(book)
                and uuid not in set(uuids)
            ]
            if to_add:
                self.add_books_to_collections([collection_uuid], to_add)
                added += len(to_add)
                log.info(
                    "Storyteller collection %s: added %s book(s)",
                    name,
                    len(to_add),
                )
            if extras:
                self.remove_books_from_collections([collection_uuid], extras)
                removed += len(extras)
                log.info(
                    "Storyteller collection %s: removed %s book(s) belonging to a larger series",
                    name,
                    len(extras),
                )
            if (collection.get("description") or "") != description:
                updated = self.update_collection(
                    collection_uuid, description=description
                )
                if updated:
                    existing[key] = updated
                numbered += 1
                log.info("Storyteller collection %s: wrote reading order", name)

        winner_col_by_book: dict[str, str] = {}
        for uuid, (_count, _matched_count, name, _pos) in winners.items():
            col = existing.get(self._collection_name_key(name))
            if col and col.get("uuid"):
                winner_col_by_book[uuid] = str(col["uuid"])

        strip_by_collection: dict[str, list[str]] = {}
        for book_uuid, book in books_by_uuid.items():
            winner_col = winner_col_by_book.get(book_uuid)
            if not winner_col:
                continue
            for coll in book.get("collections") or []:
                if not isinstance(coll, dict) or not coll.get("uuid"):
                    continue
                ckey = self._collection_name_key(str(coll.get("name") or ""))
                if ckey not in bindery_series_keys:
                    continue
                cuuid = str(coll["uuid"])
                if cuuid != winner_col:
                    strip_by_collection.setdefault(cuuid, []).append(book_uuid)
        for collection_uuid, book_uuids in strip_by_collection.items():
            unique = list(dict.fromkeys(book_uuids))
            self.remove_books_from_collections([collection_uuid], unique)
            removed += len(unique)
            log.info(
                "Removed %s book(s) from smaller overlapping collection %s",
                len(unique),
                collection_uuid,
            )

        # Membership may have changed; refresh before deleting emptied series.
        storyteller_books = self.list_books()
        books_by_uuid = {
            str(b.get("uuid")): b for b in storyteller_books if b.get("uuid")
        }

        assigned_keys = {self._collection_name_key(name) for name in assigned}
        for key, collection in list(existing.items()):
            if key not in bindery_series_keys or key in assigned_keys:
                continue
            collection_uuid = str(collection.get("uuid") or "")
            name = str(collection.get("name") or key)
            if not collection_uuid:
                continue
            members = [
                uuid
                for uuid, book in books_by_uuid.items()
                if collection_uuid in self._book_collection_ids(book)
            ]
            if members:
                self.remove_books_from_collections([collection_uuid], members)
                removed += len(members)
            self.delete_collection(collection_uuid)
            deleted += 1
            existing.pop(key, None)
            log.info(
                "Deleted Storyteller collection %s (smaller overlapping Bindery series)",
                name,
            )

        skipped = sum(1 for _n, _c, matched in series_matches if not matched)
        if created or added or removed or deleted or numbered:
            log.info(
                "Bindery series -> Storyteller collections: created %s, added %s, "
                "removed %s, deleted %s smaller series, numbered %s, skipped %s empty",
                created,
                added,
                removed,
                deleted,
                numbered,
                skipped,
            )
        else:
            log.debug(
                "Bindery series -> Storyteller collections: no changes (%s series with no Storyteller match)",
                skipped,
            )
        return created + added + removed + deleted + numbered

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

    @staticmethod
    def _media_present(media: Any) -> bool:
        if not isinstance(media, dict) or not media.get("filepath"):
            return False
        return not bool(media.get("missing"))

    def start_processing(self, book_uuid: str, restart: str | None = None) -> None:
        """POST /api/v2/books/{uuid}/process — queue readaloud alignment."""
        path = f"/api/v2/books/{book_uuid}/process"
        if restart:
            path = f"{path}?restart={urllib.parse.quote(restart)}"
        self._request("POST", path, timeout=60)

    def find_readaloud_candidates(
        self, books: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """Books with both formats that still need a readaloud created."""
        books = books if books is not None else self.list_books()
        candidates: list[dict[str, Any]] = []
        for book in books:
            if not self._media_present(book.get("ebook")):
                continue
            if not self._media_present(book.get("audiobook")):
                continue
            readaloud = book.get("readaloud")
            if not readaloud:
                candidates.append(book)
                continue
            status = str(readaloud.get("status") or "").upper()
            # Already done or actively working — leave alone.
            if status in {"ALIGNED", "PROCESSING", "QUEUED", "RUNNING"}:
                # ALIGNED but file gone → allow a full restart.
                if status == "ALIGNED" and (
                    readaloud.get("missing") or not readaloud.get("filepath")
                ):
                    candidates.append(book)
                continue
            # STOPPED / FAILED / etc. → retry.
            if status in {"STOPPED", "FAILED", "ERROR", "CANCELLED", ""}:
                candidates.append(book)
        return candidates

    def start_readalouds(self) -> int:
        """Queue Storyteller readaloud processing for dual-format books."""
        if not self.readaloud_enabled:
            return 0
        limit = max(1, int(os.environ.get("STORYTELLER_READALOUD_LIMIT", "2")))
        candidates = self.find_readaloud_candidates()
        if not candidates:
            log.debug("Storyteller readaloud: no dual-format candidates")
            return 0

        log.info(
            "Storyteller readaloud: %s candidate(s), starting up to %s",
            len(candidates),
            limit,
        )
        started = 0
        for book in candidates[:limit]:
            uuid = book.get("uuid")
            title = book.get("title") or uuid
            if not uuid:
                continue
            readaloud = book.get("readaloud") or {}
            status = str(readaloud.get("status") or "").upper()
            # Fresh start vs retry a previously stopped/failed/broken job.
            restart = None
            if status in {"STOPPED", "FAILED", "ERROR", "CANCELLED"} or (
                status == "ALIGNED"
                and (readaloud.get("missing") or not readaloud.get("filepath"))
            ):
                restart = "full"
            try:
                self.start_processing(str(uuid), restart=restart)
                started += 1
                log.info(
                    "Queued Storyteller readaloud for %s (%s%s)",
                    title,
                    uuid,
                    f", restart={restart}" if restart else "",
                )
            except Exception:
                log.exception(
                    "Failed starting Storyteller readaloud for %s (%s)",
                    title,
                    uuid,
                )
        if started:
            log.info("Storyteller readaloud queued %s book(s)", started)
        return started
