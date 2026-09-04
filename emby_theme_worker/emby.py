from __future__ import annotations

import time
import statistics
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
            params={"Ids": item_id, "Recursive": True, "Fields": "Path,ProviderIds,Genres,OriginalTitle,ProductionYear"},
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
                "Fields": "Path,ProviderIds,Genres,OriginalTitle,ProductionYear",
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
                if item.path and contained(item.path, self.allowed_path) and (item.provider_ids or item.year):
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

    def intro_candidates(self, series_id: str) -> list[dict]:
        params = {
            "ParentId": series_id, "Recursive": True, "IncludeItemTypes": "Episode",
            "Fields": "Path,Chapters,ParentIndexNumber", "SortBy": "SortName", "SortOrder": "Ascending",
            "IsMissing": False, "Limit": 10000,
        }
        found: list[dict] = []
        for raw in self.client.get("/Items", params=params).raise_for_status().json().get("Items", []):
            if int(raw.get("ParentIndexNumber") or 0) == 0:
                continue
            path = raw.get("Path") or ""
            if not path or not contained(path, self.allowed_path):
                continue
            markers = {str(ch.get("MarkerType")): ch for ch in raw.get("Chapters") or [] if ch.get("MarkerType")}
            start = markers.get("IntroStart")
            end = markers.get("IntroEnd")
            if not start or not end:
                continue
            start_seconds = float(start.get("StartPositionTicks") or 0) / 10_000_000
            end_seconds = float(end.get("StartPositionTicks") or 0) / 10_000_000
            duration = end_seconds - start_seconds
            if duration <= 0:
                continue
            found.append({"episode_id": str(raw["Id"]), "path": path, "start": start_seconds, "end": end_seconds, "duration": duration})
        return found

    def representative_intro(self, series_id: str) -> dict | None:
        candidates = [row for row in self.intro_candidates(series_id) if row["duration"] >= 30]
        if not candidates:
            return None
        median = statistics.median(row["duration"] for row in candidates)
        return min(candidates, key=lambda row: abs(row["duration"] - median))

    def virtual_folder(self, library_id: str) -> dict:
        for folder in self.client.get("/Library/VirtualFolders").raise_for_status().json():
            if str(folder.get("ItemId") or "") == str(library_id):
                locations = folder.get("Locations") or []
                if not locations or not all(contained(path, self.allowed_path) for path in locations):
                    raise ValueError("library is outside allowed path")
                return folder
        raise ValueError(f"library {library_id} was not found")

    def enable_intro_detection(self, library_id: str) -> dict:
        folder = self.virtual_folder(library_id)
        options = dict(folder.get("LibraryOptions") or {})
        options["EnableMarkerDetection"] = True
        options["EnableMarkerDetectionDuringLibraryScan"] = False
        self.client.post("/Library/VirtualFolders/LibraryOptions", json={"Id": str(library_id), "LibraryOptions": options}).raise_for_status()
        updated = self.virtual_folder(library_id)
        updated_options = updated.get("LibraryOptions") or {}
        if not updated_options.get("EnableMarkerDetection") or updated_options.get("EnableMarkerDetectionDuringLibraryScan"):
            raise RuntimeError("intro detection settings did not read back")
        return updated

    def intro_status(self, library_id: str) -> dict:
        self.virtual_folder(library_id)
        params = {"ParentId": library_id, "Recursive": True, "IncludeItemTypes": "Episode", "Fields": "Chapters,SeriesId", "Limit": 10000}
        rows = self.client.get("/Items", params=params).raise_for_status().json().get("Items", [])
        marked = 0
        series: set[str] = set()
        durations: list[float] = []
        for raw in rows:
            markers = {str(ch.get("MarkerType")): ch for ch in raw.get("Chapters") or [] if ch.get("MarkerType")}
            if "IntroStart" in markers and "IntroEnd" in markers:
                start = float(markers["IntroStart"].get("StartPositionTicks") or 0) / 10_000_000
                end = float(markers["IntroEnd"].get("StartPositionTicks") or 0) / 10_000_000
                if end > start:
                    marked += 1
                    durations.append(end - start)
                    if raw.get("SeriesId"):
                        series.add(str(raw["SeriesId"]))
        return {
            "library_id": str(library_id), "episodes": len(rows), "marked_episodes": marked,
            "marked_series": len(series), "coverage_pct": round(marked * 100 / len(rows), 2) if rows else 0,
            "duration_min": min(durations) if durations else None,
            "duration_max": max(durations) if durations else None,
            "duration_median": statistics.median(durations) if durations else None,
        }

    def run_task(self, key: str, timeout_seconds: int, poll_seconds: int) -> dict:
        tasks = self.client.get("/ScheduledTasks").raise_for_status().json()
        task = next((row for row in tasks if row.get("Key") == key), None)
        if not task:
            raise ValueError(f"scheduled task {key} not found")
        task_id = str(task["Id"])
        previous_start = ((task.get("LastExecutionResult") or {}).get("StartTimeUtc"))
        self.client.post(f"/ScheduledTasks/Running/{task_id}").raise_for_status()
        deadline = time.monotonic() + timeout_seconds
        seen_running = False
        while time.monotonic() < deadline:
            current = next((row for row in self.client.get("/ScheduledTasks").raise_for_status().json() if str(row.get("Id")) == task_id), None)
            if current and current.get("State") == "Running":
                seen_running = True
            elif seen_running or ((current or {}).get("LastExecutionResult") or {}).get("StartTimeUtc") != previous_start:
                result = current.get("LastExecutionResult") if current else None
                status = (result or {}).get("Status")
                if status not in {"Completed", "CompletedWithError"}:
                    raise RuntimeError(f"scheduled task {key} ended with {status or 'unknown'}")
                return {"key": key, "id": task_id, "status": status}
            time.sleep(poll_seconds)
        raise TimeoutError(f"scheduled task {key} did not finish")

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
