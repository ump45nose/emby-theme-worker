from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from urllib.parse import quote, urljoin

import httpx
from .db import StateDB
from .models import Candidate, MediaItem
from .scoring import normalize
from .ytdlp_runner import search as ytdlp_search


LOG = logging.getLogger(__name__)
UA = "emby-theme-worker/0.1 (+private-home-instance)"


class ProviderBlocked(RuntimeError):
    pass


class ProviderParserError(RuntimeError):
    pass


class Providers:
    def __init__(
        self,
        enabled: list[str],
        timeout: int,
        concurrency: int,
        db: StateDB,
        cookies: dict[str, str],
        ytdlp_search_timeout: int = 60,
    ):
        self.enabled = set(enabled)
        self.timeout = timeout
        self.concurrency = concurrency
        self.db = db
        self.cookies = cookies
        self.ytdlp_search_timeout = ytdlp_search_timeout
        self.http = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": UA})

    def close(self) -> None:
        self.http.close()

    def _enabled(self, name: str) -> bool:
        return name in self.enabled and self.db.provider_available("resolver", name)

    def exact(self, item: MediaItem) -> list[Candidate]:
        order: list[tuple[str, object]] = []
        found: list[Candidate] = []
        if item.is_anime:
            order.append(("animethemes", self.animethemes))
        order.append(("themerrdb", self.themerrdb))
        if item.item_type == "Series":
            order.append(("plex_tv", self.plex_tv))
            order.append(("televisiontunes", self.televisiontunes))
        for name, method in order:
            if not self._enabled(name):
                continue
            try:
                candidate = method(item)  # type: ignore[operator]
                if candidate:
                    if isinstance(candidate, list):
                        results = candidate
                    else:
                        results = [candidate]
                    self.db.provider_success("resolver", name)
                    found.extend(results)
                else:
                    self.db.provider_success("resolver", name)
            except Exception as exc:
                LOG.warning("exact provider %s failed: %s", name, exc.__class__.__name__)
                self.db.provider_failure("resolver", name, exc.__class__.__name__)
        return found

    def fuzzy(self, item: MediaItem) -> list[Candidate]:
        methods = {
            "archive": self.archive,
            "youtube": self.youtube,
            "bilibili": self.bilibili,
            "netease": self.netease,
            "qqmusic": self.qqmusic,
        }
        results: list[Candidate] = []
        selected = {name: fn for name, fn in methods.items() if self._enabled(name)}
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {pool.submit(fn, item): name for name, fn in selected.items()}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    found = future.result()
                    results.extend(found)
                    self.db.provider_success("resolver", name)
                except Exception as exc:  # providers are isolated by design
                    LOG.warning("provider %s failed: %s", name, exc.__class__.__name__)
                    self.db.provider_failure("resolver", name, exc.__class__.__name__)
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
                            return Candidate("animethemes", f"{query_text} OP1", source, exact=True, year=anime.get("year"), media_type="Series", transport="direct_http")
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
        return Candidate(
            "themerrdb", f"{item.original_title or item.name} main theme", source,
            exact=True, year=item.year, media_type=item.item_type,
            metadata={"transport": "youtube/yt-dlp"},
            transport="youtube",
        )

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
        return Candidate("plex_tv", f"{item.original_title or item.name} TV theme", url, exact=True, year=item.year, media_type="Series", transport="direct_http")

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
            found.append(Candidate("archive", str(doc.get("title") or identifier), source, year=_year(doc.get("year")), metadata={"license": rights[:200]}, transport="direct_http"))
        return found

    def televisiontunes(self, item: MediaItem) -> list[Candidate]:
        query = item.original_title or item.name
        response = self.http.get("https://www.televisiontunes.co.uk/", params={"s": query})
        self._usable_html(response)
        expected = normalize(query)
        links: list[str] = []
        for href, label in re.findall(r'href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', response.text, re.I | re.S):
            url = urljoin(str(response.url), href)
            if "televisiontunes.co.uk/" not in url or "/themes/" in url or "/search/" in url:
                continue
            label_text = normalize(re.sub(r"<[^>]+>", " ", unescape(label)))
            slug_text = normalize(url.rstrip("/").rsplit("/", 1)[-1].replace("-", " "))
            if expected and expected in {label_text, slug_text} and url not in links:
                links.append(url)
        found: list[Candidate] = []
        for detail_url in links[:5]:
            detail = self.http.get(detail_url)
            self._usable_html(detail)
            audio = re.findall(r'contentUrl["\']?\s*:\s*["\']([^"\']+)', detail.text, re.I)
            if not audio:
                audio = re.findall(r'<source[^>]+src=["\']([^"\']+)', detail.text, re.I)
            for source in audio[:1]:
                found.append(Candidate(
                    "televisiontunes", f"{query} television theme", urljoin(str(detail.url), unescape(source)),
                    exact=True, year=item.year, media_type="Series", transport="direct_http",
                ))
        if links and not found:
            raise ProviderParserError("matching detail page contained no audio URL")
        return found

    @staticmethod
    def _usable_html(response: httpx.Response) -> None:
        if response.status_code in {401, 403, 429}:
            raise ProviderBlocked(f"HTTP {response.status_code}")
        response.raise_for_status()
        lowered = response.text.casefold()
        if "just a moment" in lowered or "cf-chl-" in lowered:
            raise ProviderBlocked("anti-bot challenge")

    def tunefind(self, item: MediaItem) -> list[Candidate]:
        raise ProviderBlocked("official Tunefind API credentials are required")

    def _ytdlp_search(self, provider: str, prefix: str, item: MediaItem) -> list[Candidate]:
        cookie = self.cookies.get(provider)
        info = ytdlp_search(
            f"{prefix}5:{self._query(item)}",
            cookie_file=cookie,
            socket_timeout_seconds=self.timeout,
            hard_timeout_seconds=self.ytdlp_search_timeout,
        )
        candidates: list[Candidate] = []
        for entry in (info or {}).get("entries") or []:
            if not entry:
                continue
            source = entry.get("webpage_url") or entry.get("url")
            if source and not str(source).startswith("http") and provider == "youtube":
                source = f"https://www.youtube.com/watch?v={source}"
            if source:
                candidates.append(Candidate(
                    provider, entry.get("title") or self._query(item), str(source),
                    duration=entry.get("duration"), metadata={"transport": f"{provider}/yt-dlp"}, transport=provider,
                ))
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
            Candidate(
                "netease", f"{row.get('name','')} - {'/'.join(a.get('name','') for a in row.get('artists',[]))}",
                f"https://music.163.com/#/song?id={row.get('id')}",
                duration=(row.get("duration") or 0) / 1000,
                metadata={"catalog_id": str(row.get("id", "")), "transport": "netease/yt-dlp"}, transport="netease",
            )
            for row in rows if row.get("id")
        ]

    def qqmusic(self, item: MediaItem) -> list[Candidate]:
        params = {"w": self._query(item), "format": "json", "p": 1, "n": 5}
        payload = self.http.get("https://c.y.qq.com/soso/fcgi-bin/client_search_cp", params=params, headers={"Referer": "https://y.qq.com/"}).raise_for_status().json()
        rows = payload.get("data", {}).get("song", {}).get("list", [])
        return [
            Candidate(
                "qqmusic", f"{row.get('songname','')} - {'/'.join(a.get('name','') for a in row.get('singer',[]))}",
                f"https://y.qq.com/n/ryqq/songDetail/{row.get('songmid')}",
                duration=row.get("interval"),
                metadata={"catalog_id": row.get("songmid"), "transport": "qqmusic/yt-dlp"}, transport="qqmusic",
            )
            for row in rows if row.get("songmid")
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
