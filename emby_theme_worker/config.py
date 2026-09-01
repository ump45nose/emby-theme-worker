from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class Limits:
    bootstrap_attempts: int = 0
    regular_attempts: int = 100
    regular_successes: int = 25
    network_concurrency: int = 2
    media_concurrency: int = 1


@dataclass(slots=True)
class ProviderSettings:
    enabled: list[str] = field(default_factory=lambda: [
        "animethemes", "themerrdb", "plex_tv", "archive", "televisiontunes",
        "tunefind", "youtube", "bilibili", "netease", "qqmusic",
    ])
    timeout_seconds: int = 30
    ytdlp_search_timeout_seconds: int = 60
    ytdlp_download_timeout_seconds: int = 180
    threshold: int = 75
    cookies: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class RefreshSettings:
    full_scan_after_scheduled_run: bool = True
    library_scan_timeout_seconds: int = 7200
    task_poll_seconds: int = 10


@dataclass(slots=True)
class Config:
    emby_url: str = "http://127.0.0.1:28096"
    emby_api_key_file: str = "/run/secrets/emby_api_key"
    allowed_path: str = "/Media"
    database_path: str = "/data/worker.db"
    timezone: str = "Asia/Taipei"
    schedule: str = "0 3 * * *"
    target_seconds: int = 45
    output_bitrate: str = "192k"
    limits: Limits = field(default_factory=Limits)
    providers: ProviderSettings = field(default_factory=ProviderSettings)
    refresh: RefreshSettings = field(default_factory=RefreshSettings)
    backoff: dict[str, int] = field(default_factory=lambda: {
        "not_found_days": 30,
        "low_score_days": 7,
        "network_hours": 6,
    })

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        limits = Limits(**raw.pop("limits", {}))
        providers = ProviderSettings(**raw.pop("providers", {}))
        refresh = RefreshSettings(**raw.pop("refresh", {}))
        config = cls(limits=limits, providers=providers, refresh=refresh, **raw)
        config.validate()
        return config

    def validate(self) -> None:
        if not self.emby_url.startswith(("http://", "https://")):
            raise ValueError("emby_url must be HTTP(S)")
        if not Path(self.allowed_path).is_absolute():
            raise ValueError("allowed_path must be absolute")
        if self.target_seconds < 15 or self.target_seconds > 180:
            raise ValueError("target_seconds must be between 15 and 180")
        if not 0 <= self.providers.threshold <= 100:
            raise ValueError("providers.threshold must be between 0 and 100")
        if self.providers.ytdlp_search_timeout_seconds < 5:
            raise ValueError("providers.ytdlp_search_timeout_seconds must be at least 5")
        if self.providers.ytdlp_download_timeout_seconds < 15:
            raise ValueError("providers.ytdlp_download_timeout_seconds must be at least 15")
        if len(self.schedule.split()) != 5:
            raise ValueError("schedule must be a five-field cron expression")
