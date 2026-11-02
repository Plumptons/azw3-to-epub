"""Delete watched episode/movie files and unmonitor them in Sonarr/Radarr.

Opt-in. Defaults: disabled, and dry-run even when enabled until explicitly
turned off. Matching prefers file path (with optional path rewrite), then
provider IDs (TVDB series + S/E for Sonarr; TMDB/IMDB for Radarr).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from arr_client import ArrFileMatch, RadarrClient, SonarrClient
from jellyfin_client import JellyfinClient

log = logging.getLogger("azw3-to-epub.watched-cleanup")


def _truthy(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() in {"1", "true", "yes"}


def _parse_types(raw: str) -> tuple[str, ...]:
    allowed = {"Episode", "Movie"}
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    chosen = tuple(p for p in parts if p in allowed)
    return chosen or ("Episode", "Movie")


def _parse_played_at(item: dict[str, Any]) -> datetime | None:
    user_data = item.get("UserData") if isinstance(item.get("UserData"), dict) else {}
    raw = (
        user_data.get("LastPlayedDate")
        or item.get("DatePlayed")
        or ""
    )
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class WatchedCleanup:
    def __init__(self, jellyfin: JellyfinClient | None = None) -> None:
        self.enabled = _truthy("WATCHED_CLEANUP", "false")
        self.dry_run = _truthy("WATCHED_CLEANUP_DRY_RUN", "true")
        self.interval = max(60.0, float(os.environ.get("WATCHED_CLEANUP_INTERVAL", "3600")))
        self.grace_hours = max(
            0.0, float(os.environ.get("WATCHED_CLEANUP_GRACE_HOURS", "24"))
        )
        self.types = _parse_types(
            os.environ.get("WATCHED_CLEANUP_TYPES", "Episode,Movie")
        )
        self.limit = max(
            1, int(os.environ.get("WATCHED_CLEANUP_ITEM_LIMIT", "200"))
        )
        # Skip Jellyfin favorites (episode/movie, and parent series for episodes)
        self.skip_favorites = _truthy("WATCHED_CLEANUP_SKIP_FAVORITES", "true")
        raw_users = os.environ.get(
            "WATCHED_CLEANUP_USER_NAMES",
            os.environ.get("JELLYFIN_USER_NAMES", ""),
        ).strip()
        self.user_names = {
            u.strip().lower() for u in raw_users.split(",") if u.strip()
        }
        state_path = os.environ.get(
            "WATCHED_CLEANUP_STATE_FILE",
            "/media/books/.azw3-to-epub-watched-cleanup-state.json",
        )
        self.state_path = Path(state_path)
        self.jellyfin = jellyfin or JellyfinClient()
        self.sonarr = SonarrClient()
        self.radarr = RadarrClient()
        self._processed: set[str] = set()
        self._series_tvdb: dict[str, str] = {}
        self._series_favorite: dict[str, bool] = {}
        self._load_state()

    @property
    def active(self) -> bool:
        return (
            self.enabled
            and self.jellyfin.configured
            and (self.sonarr.enabled or self.radarr.enabled)
        )

    def _load_state(self) -> None:
        try:
            if not self.state_path.is_file():
                return
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            ids = data.get("processed") if isinstance(data, dict) else None
            if isinstance(ids, list):
                self._processed = {str(x) for x in ids if x}
        except Exception:
            log.warning("Could not load watched-cleanup state", exc_info=True)

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            # Cap growth
            ordered = sorted(self._processed)
            if len(ordered) > 5000:
                ordered = ordered[-5000:]
                self._processed = set(ordered)
            payload = {
                "processed": ordered,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self.state_path.write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
        except Exception:
            log.warning("Could not save watched-cleanup state", exc_info=True)

    def _grace_ok(self, item: dict[str, Any]) -> bool:
        if self.grace_hours <= 0:
            return True
        played = _parse_played_at(item)
        if played is None:
            # No date — require grace by skipping until UserData has a stamp
            return False
        age_h = (datetime.now(timezone.utc) - played.astimezone(timezone.utc)).total_seconds() / 3600.0
        return age_h >= self.grace_hours

    @staticmethod
    def _is_favorite(item: dict[str, Any]) -> bool:
        user_data = item.get("UserData") if isinstance(item.get("UserData"), dict) else {}
        return bool(user_data.get("IsFavorite"))

    def _series_is_favorite(self, user_id: str, item: dict[str, Any]) -> bool:
        series_id = str(item.get("SeriesId") or "").strip()
        if not series_id:
            series = item.get("Series") if isinstance(item.get("Series"), dict) else {}
            series_id = str(series.get("Id") or "").strip()
        if not series_id:
            return False
        cache_key = f"{user_id}:{series_id}"
        if cache_key in self._series_favorite:
            return self._series_favorite[cache_key]
        try:
            meta = self.jellyfin.get_user_item(user_id, series_id)
        except Exception:
            log.debug(
                "Could not fetch Jellyfin series favorite state %s",
                series_id,
                exc_info=True,
            )
            self._series_favorite[cache_key] = False
            return False
        favored = bool(meta and self._is_favorite(meta))
        self._series_favorite[cache_key] = favored
        return favored

    def _should_skip_favorite(
        self, user_id: str, item: dict[str, Any]
    ) -> tuple[bool, str]:
        """Return (skip, reason). Favorites are never deleted/unmonitored."""
        if not self.skip_favorites:
            return False, ""
        if self._is_favorite(item):
            return True, "item favorited"
        if str(item.get("Type") or "") == "Episode" and self._series_is_favorite(
            user_id, item
        ):
            return True, "series favorited"
        return False, ""

    def _series_tvdb_id(self, item: dict[str, Any]) -> str | None:
        series_providers = (
            item.get("SeriesProviderIds")
            if isinstance(item.get("SeriesProviderIds"), dict)
            else {}
        )
        if series_providers.get("Tvdb"):
            return str(series_providers["Tvdb"])
        series = item.get("Series") if isinstance(item.get("Series"), dict) else {}
        if isinstance(series.get("ProviderIds"), dict) and series["ProviderIds"].get("Tvdb"):
            return str(series["ProviderIds"]["Tvdb"])
        series_id = str(item.get("SeriesId") or series.get("Id") or "").strip()
        if not series_id:
            return None
        if series_id in self._series_tvdb:
            return self._series_tvdb[series_id] or None
        try:
            meta = self.jellyfin.get_item(series_id)
        except Exception:
            log.debug("Could not fetch Jellyfin series %s", series_id, exc_info=True)
            self._series_tvdb[series_id] = ""
            return None
        if not meta:
            self._series_tvdb[series_id] = ""
            return None
        providers = (
            meta.get("ProviderIds")
            if isinstance(meta.get("ProviderIds"), dict)
            else {}
        )
        tvdb = str(providers.get("Tvdb") or "")
        self._series_tvdb[series_id] = tvdb
        return tvdb or None

    def _match(self, item: dict[str, Any]) -> ArrFileMatch | None:
        item_type = str(item.get("Type") or "")
        path = str(item.get("Path") or "").strip()
        providers = item.get("ProviderIds") if isinstance(item.get("ProviderIds"), dict) else {}

        if item_type == "Episode" and self.sonarr.enabled:
            if path:
                hit = self.sonarr.find_by_path(path)
                if hit:
                    return hit
            tvdb = self._series_tvdb_id(item)
            season = item.get("ParentIndexNumber")
            episode = item.get("IndexNumber")
            if tvdb is not None and season is not None and episode is not None:
                return self.sonarr.find_by_ids(
                    tvdb_series_id=str(tvdb),
                    season=int(season),
                    episode=int(episode),
                )
            return None

        if item_type == "Movie" and self.radarr.enabled:
            if path:
                hit = self.radarr.find_by_path(path)
                if hit:
                    return hit
            return self.radarr.find_by_ids(
                tmdb_id=str(providers["Tmdb"]) if providers.get("Tmdb") else None,
                imdb_id=str(providers["Imdb"]) if providers.get("Imdb") else None,
            )
        return None

    def _apply(self, match: ArrFileMatch) -> None:
        client = self.sonarr if match.kind == "episode" else self.radarr
        action = "Would delete+unmonitor" if self.dry_run else "Delete+unmonitor"
        log.info(
            "%s [%s] %s (item=%s file=%s path=%s monitored=%s)",
            action,
            match.arr_name,
            match.title,
            match.item_id,
            match.file_id,
            match.path or "?",
            match.monitored,
        )
        if self.dry_run:
            return
        # File first, then unmonitor — series stays monitored.
        client.delete_file(match.file_id)
        client.unmonitor(match.item_id)
        client.invalidate()

    def run_once(self) -> int:
        """Process played items. Returns count of matched actions (incl. dry-run)."""
        if not self.active:
            return 0

        users = self.jellyfin.resolve_user_ids(self.user_names or None)
        if not users:
            log.warning("Watched cleanup: no Jellyfin users matched filter")
            return 0

        # Refresh *arr indexes once per sweep (isolate failures so one *arr
        # with a bad key does not abort the other).
        sonarr_ok = False
        radarr_ok = False
        if self.sonarr.enabled and "Episode" in self.types:
            try:
                self.sonarr.invalidate()
                self.sonarr.build_path_index()
                sonarr_ok = True
            except Exception:
                log.exception(
                    "Watched cleanup: Sonarr index failed — skipping episodes this sweep"
                )
        if self.radarr.enabled and "Movie" in self.types:
            try:
                self.radarr.invalidate()
                self.radarr.build_path_index()
                radarr_ok = True
            except Exception:
                log.exception(
                    "Watched cleanup: Radarr index failed — skipping movies this sweep"
                )
        if not sonarr_ok and not radarr_ok:
            log.error("Watched cleanup: neither Sonarr nor Radarr indexable")
            return 0

        self._series_favorite.clear()
        actions = 0
        skipped_fav = 0
        for user_id, user_name in users:
            try:
                items = self.jellyfin.list_played_items(
                    user_id, include_types=self.types, limit=self.limit
                )
            except Exception:
                log.exception("Watched cleanup: Jellyfin list failed for %s", user_name)
                continue
            for item in items:
                jelly_id = str(item.get("Id") or "").strip()
                if not jelly_id:
                    continue
                state_key = f"{user_id}:{jelly_id}"
                if state_key in self._processed:
                    continue
                if not _truthy_item_played(item):
                    continue
                if not self._grace_ok(item):
                    continue
                item_type = str(item.get("Type") or "")
                if item_type == "Episode" and not sonarr_ok:
                    continue
                if item_type == "Movie" and not radarr_ok:
                    continue
                skip_fav, fav_reason = self._should_skip_favorite(user_id, item)
                if skip_fav:
                    skipped_fav += 1
                    title = str(item.get("Name") or jelly_id)
                    series = str(item.get("SeriesName") or "")
                    label = f"{series} — {title}" if series else title
                    log.info(
                        "Watched cleanup: skip favorite (%s): %s",
                        fav_reason,
                        label,
                    )
                    # Do not mark processed — unfavorite later can clean up.
                    continue
                match = self._match(item)
                if match is None:
                    title = str(item.get("Name") or jelly_id)
                    series = str(item.get("SeriesName") or "")
                    label = f"{series} — {title}" if series else title
                    log.debug(
                        "Watched cleanup: no *arr match for %s (%s)",
                        label,
                        item.get("Type"),
                    )
                    continue
                try:
                    self._apply(match)
                    actions += 1
                    self._processed.add(state_key)
                except Exception:
                    log.exception(
                        "Watched cleanup failed for %s", match.title
                    )

        if skipped_fav:
            log.info(
                "Watched cleanup: skipped %s favorited item(s)", skipped_fav
            )

        if actions:
            self._save_state()
        return actions


def _truthy_item_played(item: dict[str, Any]) -> bool:
    user_data = item.get("UserData") if isinstance(item.get("UserData"), dict) else {}
    if "Played" in user_data:
        return bool(user_data.get("Played"))
    # Filters=IsPlayed should already constrain; be permissive if UserData missing
    return True


def watched_cleanup_loop(jellyfin: JellyfinClient | None = None) -> None:
    cleaner = WatchedCleanup(jellyfin=jellyfin)
    if not cleaner.enabled:
        return
    if not cleaner.jellyfin.configured:
        log.warning(
            "WATCHED_CLEANUP=true but Jellyfin URL/API key missing — loop idle"
        )
        return
    if not (cleaner.sonarr.enabled or cleaner.radarr.enabled):
        log.warning(
            "WATCHED_CLEANUP=true but Sonarr/Radarr URL+API key missing — loop idle"
        )
        return

    log.info(
        "Watched cleanup enabled (dry_run=%s interval=%ss grace=%sh types=%s "
        "skip_favorites=%s sonarr=%s radarr=%s)",
        cleaner.dry_run,
        int(cleaner.interval),
        cleaner.grace_hours,
        ",".join(cleaner.types),
        cleaner.skip_favorites,
        bool(cleaner.sonarr.enabled),
        bool(cleaner.radarr.enabled),
    )
    if cleaner.dry_run:
        log.warning(
            "WATCHED_CLEANUP_DRY_RUN=true — logging only; set to false to delete/unmonitor"
        )

    while True:
        try:
            n = cleaner.run_once()
            if n:
                log.info("Watched cleanup sweep: %s action(s)", n)
        except Exception:
            log.exception("Watched cleanup sweep failed")
        time.sleep(cleaner.interval)
