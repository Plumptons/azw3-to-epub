"""Minimal Jellyfin client: detect active (non-paused) playback sessions."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger("azw3-to-epub.jellyfin")


def _truthy(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() in {"1", "true", "yes"}


class JellyfinClient:
    """Read Sessions to see whether anyone is actively watching/listening."""

    def __init__(self) -> None:
        self.base_url = os.environ.get("JELLYFIN_URL", "").rstrip("/")
        self.api_key = os.environ.get("JELLYFIN_API_KEY", "").strip()
        # Opt-in once URL+key are set; used to pause Storyteller readaloud.
        self.enabled = bool(self.base_url and self.api_key) and _truthy(
            "JELLYFIN_PAUSE_READALOUD", "true"
        )
        # Comma-separated usernames; empty = any user.
        raw_users = os.environ.get("JELLYFIN_USER_NAMES", "").strip()
        self.user_names = {
            u.strip().lower() for u in raw_users.split(",") if u.strip()
        }
        # If true, a paused NowPlayingItem still blocks readaloud.
        self.treat_paused_as_playing = _truthy(
            "JELLYFIN_TREAT_PAUSED_AS_PLAYING", "false"
        )
        self.check_interval = max(
            15.0, float(os.environ.get("JELLYFIN_PLAYBACK_CHECK_INTERVAL", "60"))
        )

    def _request(self, path: str, *, timeout: float = 15) -> Any:
        url = f"{self.base_url}{path}"
        headers = {
            "Accept": "application/json",
            # Jellyfin accepts either header style.
            "X-Emby-Token": self.api_key,
            "Authorization": f'MediaBrowser Token="{self.api_key}"',
        }
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Jellyfin GET {path} -> HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Jellyfin GET {path} unreachable: {exc.reason}"
            ) from exc

    def list_sessions(self) -> list[dict[str, Any]]:
        payload = self._request("/Sessions")
        if not isinstance(payload, list):
            return []
        return [s for s in payload if isinstance(s, dict)]

    @staticmethod
    def _item_label(item: dict[str, Any]) -> str:
        series = str(item.get("SeriesName") or "").strip()
        name = str(item.get("Name") or "").strip()
        if series and name:
            return f"{series} — {name}"
        return name or series or "unknown"

    def active_playback(self) -> list[dict[str, Any]]:
        """Sessions with NowPlayingItem that count as 'playing' for our gate."""
        active: list[dict[str, Any]] = []
        for session in self.list_sessions():
            item = session.get("NowPlayingItem")
            if not isinstance(item, dict):
                continue
            user = str(session.get("UserName") or "").strip()
            if self.user_names and user.lower() not in self.user_names:
                continue
            play_state = session.get("PlayState") or {}
            is_paused = bool(
                isinstance(play_state, dict) and play_state.get("IsPaused")
            )
            if is_paused and not self.treat_paused_as_playing:
                continue
            active.append(
                {
                    "user": user or "?",
                    "device": str(session.get("DeviceName") or "?"),
                    "client": str(session.get("Client") or "?"),
                    "title": self._item_label(item),
                    "paused": is_paused,
                }
            )
        return active

    def is_playing(self) -> tuple[bool, list[dict[str, Any]]]:
        """Return (playing, sessions). On API errors: fail open (False, [])."""
        if not self.enabled:
            return False, []
        try:
            sessions = self.active_playback()
            return bool(sessions), sessions
        except Exception:
            log.warning(
                "Jellyfin playback check failed; allowing readaloud to continue",
                exc_info=True,
            )
            return False, []
