from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from urllib.parse import quote, urljoin

import httpx
import yt_dlp

from .db import StateDB
from .models import Candidate, MediaItem
from .scoring import normalize


LOG = logging.getLogger(__name__)
UA = "emby-theme-worker/0.1 (+private-home-instance)"


class Providers:
    def __init__(self, enabled: list[str], timeout: int, concurrency: int, db: StateDB, cookies: dict[str, str]):
        self.enabled = set(enabled)
        self.timeout = timeout
        self.concurrency = concurrency
        self.db = db
        self.cookies = cookies
        self.http = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": UA})

    def close(self) -> None:
        self.http.close()

    def _enabled(self, name: str) -> bool:
        return name in self.enabled and self.db.provider_available(name)

    def exact(self, item: MediaItem) -> list[Candidate]:
        order: list[tuple[str, object]] = []
        if item.is_anime:
            order.append(("animethemes", self.animethemes))
        order.append(("themerrdb", self.themerrdb))
        if item.item_type == "Series":
            order.append(("plex_tv", self.plex_tv))
        for name, method in order:
            if not self._enabled(name):
                continue
            try:
                candidate = method(item)  # type: ignore[operator]
                self.db.provider_success(name)
                if candidate:
                    return [candidate]
            except Exception as exc:
                LOG.warning("exact provider %s failed: %s", name, exc.__class__.__name__)
                self.db.provider_failure(name, exc.__class__.__name__)
        return []

    def fuzzy(self, item: MediaItem) -> list[Candidate]:
        methods = {
            "archive": self.archive,
            "televisiontunes": self.televisiontunes,
            "tunefind": self.tunefind,
            "youtube": self.youtube,
            "bilibili": self.bilibili,
            "netease": self.netease,
            "qqmusic": self.qqmusic,
        }
        results: list[Candidate] = []
        selected = {name: fn for name, fn in methods.items() if self._enabled(name)}
        if item.item_type != "Series":
            selected.pop("televisiontunes", None)
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {pool.submit(fn, item): name for name, fn in selected.items()}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    found = future.result()
                    results.extend(found)
                    self.db.provider_success(name)
                except Exception as exc:  # providers are isolated by design
                    LOG.warning("provider %s failed: %s", name, exc.__class__.__name__)
                    self.db.provider_failure(name, exc.__class__.__name__)
        return results

    def animethemes(self, item: MediaItem) -> Candidate | None:
        query_text = item.original_title or item.name
        query = """
        query($search: String!) {
          search(search: $search, first: 8) {
            anime {
              year slug title { romaji english native }
              animethemes { type sequence slug animethemeentries { videos { nodes { link audio { link } } } } }
            }
          }
        }
        """
        payload = self.http.post(
            "https://graphql.animethemes.moe",
            json={"query": query, "variables": {"search": query_text}},
        ).raise_for_status().json()
        if payload.get("errors"):
            raise RuntimeError("AnimeThemes GraphQL error")
        expected = {normalize(item.name), normalize(item.original_title)} - {""}
        for anime in payload.get("data", {}).get("search", {}).get("anime", []):
            titles = anime.get("title") or {}
            actual = {normalize(titles.get(key)) for key in ("romaji", "english", "native")} - {""}
            if expected.isdisjoint(actual):
                continue
            if item.year and anime.get("year") and abs(int(item.year) - int(anime["year"])) > 1:
                continue
            themes = sorted(
                (theme for theme in anime.get("animethemes", []) if theme.get("type") == "OP"),
                key=lambda theme: int(theme.get("sequence") or 999),
            )
            for theme in themes:
                if int(theme.get("sequence") or 0) != 1:
                    continue
                for entry in theme.get("animethemeentries", []):
                    for video in (entry.get("videos") or {}).get("nodes", []):
                        source = (video.get("audio") or {}).get("link") or video.get("link")
                        if source:
                            return Candidate("animethemes", f"{query_text} OP1", source, exact=True, year=anime.get("year"), media_type="Series")
        return None

    def themerrdb(self, item: MediaItem) -> Candidate | None:
        tmdb = item.provider_ids.get("Tmdb") or item.provider_ids.get("TMDb") or item.provider_ids.get("TheMovieDb")
        if not tmdb:
            return None
        media = "movies" if item.item_type == "Movie" else "tv_shows"
        url = f"https://app.lizardbyte.dev/ThemerrDB/{media}/themoviedb/{quote(tmdb)}.json"
        response = self.http.get(url)
        if response.status_code == 404:
            return None
        payload = response.raise_for_status().json()
        source = payload.get("youtube_theme_url")
        if not source:
            return None
        return Candidate("themerrdb", f"{item.original_title or item.name} main theme", source, exact=True, year=item.year, media_type=item.item_type)

    def plex_tv(self, item: MediaItem) -> Candidate | None:
        tvdb = item.provider_ids.get("Tvdb") or item.provider_ids.get("TVDb")
        if not tvdb:
            return None
        url = f"https://tvthemes.plexapp.com/{quote(tvdb)}.mp3"
        response = self.http.head(url)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        if "audio" not in response.headers.get("content-type", ""):
            return None
        return Candidate("plex_tv", f"{item.original_title or item.name} TV theme", url, exact=True, year=item.year, media_type="Series")

    def _query(self, item: MediaItem) -> str:
        title = item.original_title or item.name
        year = f" {item.year}" if item.year else ""
        intent = " opening theme OP1" if item.is_anime else " main theme soundtrack"
        return f"{title}{year}{intent}"

    def archive(self, item: MediaItem) -> list[Candidate]:
        query = f'title:("{self._query(item)}") AND mediatype:(audio)'
        params = [("q", query), ("output", "json"), ("rows", "5")]
        for field in ("identifier", "title", "year", "licenseurl"):
            params.append(("fl[]", field))
        payload = self.http.get("https://archive.org/advancedsearch.php", params=params).raise_for_status().json()
        found: list[Candidate] = []
        for doc in payload.get("response", {}).get("docs", []):
            license_url = str(doc.get("licenseurl") or "").casefold()
            if not any(term in license_url for term in ("creativecommons.org", "publicdomain")):
                continue
            identifier = doc.get("identifier")
            metadata = self.http.get(f"https://archive.org/metadata/{quote(str(identifier))}").raise_for_status().json()
            md = metadata.get("metadata", {})
            rights = " ".join(str(md.get(k) or "") for k in ("licenseurl", "rights")).casefold()
            if not any(term in rights for term in ("creativecommons.org", "public domain", "publicdomain")):
                continue
            media_file = next(
                (f for f in metadata.get("files", []) if str(f.get("name", "")).lower().endswith((".mp3", ".ogg", ".flac")) and f.get("source") == "original"),
                None,
            )
            if not media_file:
                continue
            source = f"https://archive.org/download/{quote(str(identifier))}/{quote(str(media_file['name']))}"
            found.append(Candidate("archive", str(doc.get("title") or identifier), source, year=_year(doc.get("year")), metadata={"license": rights[:200]}))
        return found

    def televisiontunes(self, item: MediaItem) -> list[Candidate]:
        response = self.http.get("https://www.televisiontunes.com/search.php", params={"q": item.original_title or item.name})
        if response.status_code >= 400:
            return []
        links = re.findall(r'href=["\']([^"\']+\.mp3[^"\']*)["\']', response.text, re.I)
        return [Candidate("televisiontunes", f"{item.original_title or item.name} television theme", urljoin(str(response.url), link)) for link in links[:5]]

    def tunefind(self, item: MediaItem) -> list[Candidate]:
        response = self.http.get("https://www.tunefind.com/search/site", params={"q": item.original_title or item.name})
        if response.status_code >= 400:
            return []
        text = re.sub(r"<[^>]+>", " ", response.text)
        titles = [unescape(t).strip() for t in re.findall(r"([\w\s'’:&.-]{4,80}(?:Theme|Soundtrack|Opening|Main Title)[\w\s'’:&.-]{0,80})", text, re.I)]
        return [Candidate("tunefind", title, f"ytsearch1:{title}", metadata={"search_only": True}) for title in titles[:5]]

    def _ytdlp_search(self, provider: str, prefix: str, item: MediaItem) -> list[Candidate]:
        opts: dict = {"quiet": True, "no_warnings": True, "extract_flat": True, "playlistend": 5, "socket_timeout": self.timeout, "cachedir": False}
        cookie = self.cookies.get(provider)
        if cookie:
            opts["cookiefile"] = cookie
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"{prefix}5:{self._query(item)}", download=False)
        candidates: list[Candidate] = []
        for entry in (info or {}).get("entries") or []:
            if not entry:
                continue
            source = entry.get("webpage_url") or entry.get("url")
            if source and not str(source).startswith("http") and provider == "youtube":
                source = f"https://www.youtube.com/watch?v={source}"
            if source:
                candidates.append(Candidate(provider, entry.get("title") or self._query(item), str(source), duration=entry.get("duration")))
        return candidates

    def youtube(self, item: MediaItem) -> list[Candidate]:
        return self._ytdlp_search("youtube", "ytsearch", item)

    def bilibili(self, item: MediaItem) -> list[Candidate]:
        try:
            return self._ytdlp_search("bilibili", "bilisearch", item)
        except Exception:
            payload = self.http.get("https://api.bilibili.com/x/web-interface/search/type", params={"search_type": "video", "keyword": self._query(item), "page": 1}).raise_for_status().json()
            return [
                Candidate("bilibili", re.sub(r"<[^>]+>", "", row.get("title") or ""), f"https://www.bilibili.com/video/{row['bvid']}", duration=_duration(row.get("duration")))
                for row in payload.get("data", {}).get("result", [])[:5] if row.get("bvid")
            ]

    def netease(self, item: MediaItem) -> list[Candidate]:
        payload = self.http.get("https://music.163.com/api/search/get", params={"s": self._query(item), "type": 1, "limit": 5, "offset": 0}).raise_for_status().json()
        rows = payload.get("result", {}).get("songs", [])
        return [
            Candidate("netease", f"{row.get('name','')} - {'/'.join(a.get('name','') for a in row.get('artists',[]))}", f"ytsearch1:{row.get('name','')} {' '.join(a.get('name','') for a in row.get('artists',[]))}", duration=(row.get("duration") or 0) / 1000, metadata={"search_only": True, "catalog_id": str(row.get("id", ""))})
            for row in rows
        ]

    def qqmusic(self, item: MediaItem) -> list[Candidate]:
        params = {"w": self._query(item), "format": "json", "p": 1, "n": 5}
        payload = self.http.get("https://c.y.qq.com/soso/fcgi-bin/client_search_cp", params=params, headers={"Referer": "https://y.qq.com/"}).raise_for_status().json()
        rows = payload.get("data", {}).get("song", {}).get("list", [])
        return [
            Candidate("qqmusic", f"{row.get('songname','')} - {'/'.join(a.get('name','') for a in row.get('singer',[]))}", f"ytsearch1:{row.get('songname','')} {' '.join(a.get('name','') for a in row.get('singer',[]))}", duration=row.get("interval"), metadata={"search_only": True, "catalog_id": row.get("songmid")})
            for row in rows
        ]


def _year(value: object) -> int | None:
    match = re.search(r"\d{4}", str(value or ""))
    return int(match.group()) if match else None


def _duration(value: object) -> float | None:
    if not value:
        return None
    parts = str(value).split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        return float(value)
    except ValueError:
        return None
