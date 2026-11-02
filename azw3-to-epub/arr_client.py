"""Minimal Sonarr / Radarr clients for watched-cleanup (file delete + unmonitor)."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("azw3-to-epub.arr")


def _truthy(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() in {"1", "true", "yes"}


def normalize_path(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").lower()


def map_jellyfin_path(path: str) -> str:
    """Optional path rewrite so Jellyfin paths match *arr paths."""
    raw_from = os.environ.get("WATCHED_CLEANUP_PATH_FROM", "").strip()
    raw_to = os.environ.get("WATCHED_CLEANUP_PATH_TO", "").strip()
    if not raw_from:
        return path.replace("\\", "/")
    src = path.replace("\\", "/")
    prefix = raw_from.replace("\\", "/").rstrip("/")
    dest = raw_to.replace("\\", "/").rstrip("/")
    if src == prefix or src.lower() == prefix.lower():
        return dest or src
    if src.lower().startswith(prefix.lower() + "/"):
        return dest + src[len(prefix) :]
    return src


@dataclass
class ArrFileMatch:
    kind: str  # "episode" | "movie"
    title: str
    arr_name: str
    item_id: int  # episode id or movie id
    file_id: int
    path: str
    monitored: bool


class ArrApi:
    def __init__(self, name: str, base_url: str, api_key: str) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.enabled = bool(self.base_url and self.api_key)

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | list[Any] | None = None,
        timeout: float = 60,
    ) -> Any:
        url = f"{self.base_url}{path}"
        data = None
        headers = {
            "Accept": "application/json",
            "X-Api-Key": self.api_key,
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if not raw or resp.status == 204:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"{self.name} {method} {path} -> HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"{self.name} {method} {path} unreachable: {exc.reason}"
            ) from exc


class SonarrClient(ArrApi):
    def __init__(self) -> None:
        super().__init__(
            "Sonarr",
            os.environ.get("SONARR_URL", "").strip(),
            os.environ.get("SONARR_API_KEY", "").strip(),
        )
        self._series_cache: list[dict[str, Any]] | None = None
        self._path_index: dict[str, ArrFileMatch] | None = None

    def invalidate(self) -> None:
        self._series_cache = None
        self._path_index = None

    def list_series(self) -> list[dict[str, Any]]:
        if self._series_cache is None:
            payload = self._request("GET", "/api/v3/series")
            self._series_cache = payload if isinstance(payload, list) else []
        return self._series_cache

    def build_path_index(self) -> dict[str, ArrFileMatch]:
        if self._path_index is not None:
            return self._path_index
        index: dict[str, ArrFileMatch] = {}
        for series in self.list_series():
            series_id = series.get("id")
            series_title = str(series.get("title") or "?")
            if not isinstance(series_id, int):
                continue
            files = self._request("GET", f"/api/v3/episodefile?seriesId={series_id}")
            if not isinstance(files, list):
                continue
            # Map file id -> episode for monitored flag / title
            episodes = self._request("GET", f"/api/v3/episode?seriesId={series_id}")
            ep_by_file: dict[int, dict[str, Any]] = {}
            if isinstance(episodes, list):
                for ep in episodes:
                    efid = ep.get("episodeFileId")
                    if isinstance(efid, int) and efid > 0:
                        ep_by_file[efid] = ep
            for ef in files:
                path = str(ef.get("path") or "")
                file_id = ef.get("id")
                if not path or not isinstance(file_id, int):
                    continue
                ep = ep_by_file.get(file_id, {})
                ep_id = ep.get("id")
                if not isinstance(ep_id, int):
                    continue
                season = ep.get("seasonNumber")
                number = ep.get("episodeNumber")
                ep_title = str(ep.get("title") or "")
                label = f"{series_title} S{int(season or 0):02d}E{int(number or 0):02d}"
                if ep_title:
                    label = f"{label} — {ep_title}"
                match = ArrFileMatch(
                    kind="episode",
                    title=label,
                    arr_name=self.name,
                    item_id=ep_id,
                    file_id=file_id,
                    path=path,
                    monitored=bool(ep.get("monitored", True)),
                )
                index[normalize_path(path)] = match
        self._path_index = index
        log.info("%s path index: %s episode file(s)", self.name, len(index))
        return index

    def find_by_path(self, jellyfin_path: str) -> ArrFileMatch | None:
        mapped = map_jellyfin_path(jellyfin_path)
        return self.build_path_index().get(normalize_path(mapped))

    def find_by_ids(
        self,
        *,
        tvdb_series_id: str | None,
        season: int | None,
        episode: int | None,
    ) -> ArrFileMatch | None:
        if not tvdb_series_id or season is None or episode is None:
            return None
        want = str(tvdb_series_id).strip()
        for series in self.list_series():
            ids = series.get("tvdbId")
            if str(ids) != want:
                continue
            series_id = series.get("id")
            series_title = str(series.get("title") or "?")
            if not isinstance(series_id, int):
                return None
            eps = self._request(
                "GET",
                f"/api/v3/episode?seriesId={series_id}&seasonNumber={int(season)}",
            )
            if not isinstance(eps, list):
                return None
            for ep in eps:
                if int(ep.get("episodeNumber") or -1) != int(episode):
                    continue
                ep_id = ep.get("id")
                file_id = ep.get("episodeFileId")
                if not isinstance(ep_id, int) or not isinstance(file_id, int) or file_id <= 0:
                    return None
                files = self._request(
                    "GET", f"/api/v3/episodefile?seriesId={series_id}"
                )
                path = ""
                if isinstance(files, list):
                    for ef in files:
                        if ef.get("id") == file_id:
                            path = str(ef.get("path") or "")
                            break
                ep_title = str(ep.get("title") or "")
                label = f"{series_title} S{int(season):02d}E{int(episode):02d}"
                if ep_title:
                    label = f"{label} — {ep_title}"
                return ArrFileMatch(
                    kind="episode",
                    title=label,
                    arr_name=self.name,
                    item_id=ep_id,
                    file_id=file_id,
                    path=path,
                    monitored=bool(ep.get("monitored", True)),
                )
        return None

    def unmonitor(self, episode_id: int) -> None:
        self._request(
            "PUT",
            "/api/v3/episode/monitor",
            {"episodeIds": [episode_id], "monitored": False},
        )

    def delete_file(self, file_id: int) -> None:
        self._request("DELETE", f"/api/v3/episodefile/{file_id}")


class RadarrClient(ArrApi):
    def __init__(self) -> None:
        super().__init__(
            "Radarr",
            os.environ.get("RADARR_URL", "").strip(),
            os.environ.get("RADARR_API_KEY", "").strip(),
        )
        self._movies_cache: list[dict[str, Any]] | None = None
        self._path_index: dict[str, ArrFileMatch] | None = None

    def invalidate(self) -> None:
        self._movies_cache = None
        self._path_index = None

    def list_movies(self) -> list[dict[str, Any]]:
        if self._movies_cache is None:
            payload = self._request("GET", "/api/v3/movie")
            self._movies_cache = payload if isinstance(payload, list) else []
        return self._movies_cache

    def build_path_index(self) -> dict[str, ArrFileMatch]:
        if self._path_index is not None:
            return self._path_index
        index: dict[str, ArrFileMatch] = {}
        for movie in self.list_movies():
            movie_id = movie.get("id")
            title = str(movie.get("title") or "?")
            year = movie.get("year")
            label = f"{title} ({year})" if year else title
            mf = movie.get("movieFile") or {}
            if not isinstance(mf, dict):
                continue
            path = str(mf.get("path") or "")
            file_id = mf.get("id")
            if not path or not isinstance(movie_id, int) or not isinstance(file_id, int):
                continue
            match = ArrFileMatch(
                kind="movie",
                title=label,
                arr_name=self.name,
                item_id=movie_id,
                file_id=file_id,
                path=path,
                monitored=bool(movie.get("monitored", True)),
            )
            index[normalize_path(path)] = match
        self._path_index = index
        log.info("%s path index: %s movie file(s)", self.name, len(index))
        return index

    def find_by_path(self, jellyfin_path: str) -> ArrFileMatch | None:
        mapped = map_jellyfin_path(jellyfin_path)
        return self.build_path_index().get(normalize_path(mapped))

    def find_by_ids(
        self,
        *,
        tmdb_id: str | None = None,
        imdb_id: str | None = None,
    ) -> ArrFileMatch | None:
        want_tmdb = str(tmdb_id).strip() if tmdb_id else ""
        want_imdb = str(imdb_id).strip().lower() if imdb_id else ""
        if not want_tmdb and not want_imdb:
            return None
        for movie in self.list_movies():
            mid = str(movie.get("tmdbId") or "")
            iid = str(movie.get("imdbId") or "").lower()
            if want_tmdb and mid == want_tmdb:
                pass
            elif want_imdb and iid and iid == want_imdb:
                pass
            else:
                continue
            movie_id = movie.get("id")
            mf = movie.get("movieFile") or {}
            if not isinstance(movie_id, int) or not isinstance(mf, dict):
                return None
            file_id = mf.get("id")
            path = str(mf.get("path") or "")
            if not isinstance(file_id, int) or file_id <= 0:
                return None
            title = str(movie.get("title") or "?")
            year = movie.get("year")
            label = f"{title} ({year})" if year else title
            return ArrFileMatch(
                kind="movie",
                title=label,
                arr_name=self.name,
                item_id=movie_id,
                file_id=file_id,
                path=path,
                monitored=bool(movie.get("monitored", True)),
            )
        return None

    def unmonitor(self, movie_id: int) -> None:
        self._request(
            "PUT",
            "/api/v3/movie/editor",
            {"movieIds": [movie_id], "monitored": False},
        )

    def delete_file(self, file_id: int) -> None:
        self._request("DELETE", f"/api/v3/moviefile/{file_id}")
