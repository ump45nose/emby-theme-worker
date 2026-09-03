from __future__ import annotations

import time
from pathlib import Path
from typing import Iterator

import httpx

from .models import MediaItem
from .security import contained


class EmbyClient:
    def __init__(self, base_url: str, api_key: str, allowed_path: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/") + "/emby"
        self.allowed_path = allowed_path
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"X-Emby-Token": api_key, "Accept": "application/json"},
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self.client.close()

    def system_info(self) -> dict:
        return self.client.get("/System/Info").raise_for_status().json()

    def _to_item(self, raw: dict) -> MediaItem:
        return MediaItem(
            id=str(raw["Id"]),
            name=raw.get("Name") or "",
            original_title=raw.get("OriginalTitle"),
            year=raw.get("ProductionYear"),
            item_type=raw.get("Type") or "",
            path=raw.get("Path") or "",
            provider_ids={str(k): str(v) for k, v in (raw.get("ProviderIds") or {}).items()},
            genres=list(raw.get("Genres") or []),
        )

    def get_item(self, item_id: str) -> MediaItem:
        payload = self.client.get(
            "/Items",
            params={"Ids": item_id, "Recursive": True, "Fields": "Path,ProviderIds,Genres,OriginalTitle"},
        ).raise_for_status().json()
        rows = payload.get("Items", [])
        if not rows:
            raise ValueError(f"item {item_id} was not found")
        raw = rows[0]
        item = self._to_item(raw)
        if item.item_type not in {"Movie", "Series"}:
            raise ValueError(f"item {item_id} is not Movie or Series")
        if not contained(item.path, self.allowed_path):
            raise ValueError(f"item {item_id} is outside allowed path")
        return item

    def iter_items(self, *, has_theme: bool, parent_id: str | None = None, page_size: int = 200) -> Iterator[MediaItem]:
        start = 0
        while True:
            params: dict[str, str | int | bool] = {
                "Recursive": True,
                "IncludeItemTypes": "Movie,Series",
                "HasThemeSong": has_theme,
                "Fields": "Path,ProviderIds,Genres,OriginalTitle",
                "StartIndex": start,
                "Limit": page_size,
                "SortBy": "SortName",
            }
            if parent_id:
                params["ParentId"] = parent_id
            payload = self.client.get("/Items", params=params).raise_for_status().json()
            batch = payload.get("Items", [])
            for raw in batch:
                item = self._to_item(raw)
                if item.path and contained(item.path, self.allowed_path):
                    yield item
            start += len(batch)
            if not batch or start >= int(payload.get("TotalRecordCount", start)):
                break

    def iter_missing(self, *, parent_id: str | None = None, page_size: int = 200) -> Iterator[MediaItem]:
        yield from self.iter_items(has_theme=False, parent_id=parent_id, page_size=page_size)

    def iter_themed(self, *, parent_id: str | None = None, page_size: int = 200) -> Iterator[MediaItem]:
        yield from self.iter_items(has_theme=True, parent_id=parent_id, page_size=page_size)

    def theme_visible(self, item_id: str) -> bool:
        payload = self.client.get(f"/Items/{item_id}/ThemeSongs").raise_for_status().json()
        return bool(payload.get("Items"))

    def has_theme(self, item: MediaItem) -> bool:
        return (item.directory / "theme.mp3").exists() or self.theme_visible(item.id)

    def earliest_episode_media(self, series_id: str) -> str | None:
        params = {
            "ParentId": series_id,
            "Recursive": True,
            "IncludeItemTypes": "Episode",
            "Fields": "Path",
            "SortBy": "SortName",
            "SortOrder": "Ascending",
            "Limit": 20,
            "IsMissing": False,
        }
        for raw in self.client.get("/Items", params=params).raise_for_status().json().get("Items", []):
            path = raw.get("Path") or ""
            if path and contained(path, self.allowed_path):
                return path
        return None

    def refresh_and_verify(self, item_id: str, attempts: int = 8, delay: float = 2.0) -> bool:
        common = {"Recursive": False, "ImageRefreshMode": "Default", "ReplaceAllImages": False, "ReplaceAllMetadata": False}
        for mode in ("Default", "FullRefresh"):
            params = dict(common, MetadataRefreshMode=mode)
            try:
                self.client.post(f"/Items/{item_id}/Refresh", params=params).raise_for_status()
            except httpx.HTTPError:
                # A slow Emby refresh must not abort the whole bootstrap.  The
                # caller will retain the output for a later library scan.
                continue
            for _ in range(attempts):
                try:
                    payload = self.client.get(f"/Items/{item_id}/ThemeSongs").raise_for_status().json()
                except httpx.HTTPError:
                    continue
                if payload.get("Items"):
                    return True
                time.sleep(delay)
        return False

    def trigger_library_scan(self) -> None:
        self.client.post("/Library/Refresh").raise_for_status()

    def wait_library_scan(self, timeout_seconds: int, poll_seconds: int) -> bool:
        deadline = time.monotonic() + timeout_seconds
        seen_running = False
        idle_polls = 0
        while time.monotonic() < deadline:
            try:
                tasks = self.client.get("/ScheduledTasks").raise_for_status().json()
                task = next((row for row in tasks if row.get("Key") == "RefreshLibrary"), None)
                if not task:
                    raise RuntimeError("RefreshLibrary scheduled task not found")
                if task.get("State") == "Running":
                    seen_running = True
                    idle_polls = 0
                else:
                    idle_polls += 1
                    if seen_running or idle_polls >= 2:
                        return True
            except httpx.HTTPError:
                idle_polls = 0
            time.sleep(poll_seconds)
        return False
