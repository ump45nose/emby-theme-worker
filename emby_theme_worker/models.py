from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class MediaItem:
    id: str
    name: str
    original_title: str | None
    year: int | None
    item_type: str
    path: str
    provider_ids: dict[str, str] = field(default_factory=dict)
    genres: list[str] = field(default_factory=list)

    @property
    def directory(self) -> Path:
        path = Path(self.path)
        return path if self.item_type == "Series" or path.is_dir() else path.parent

    @property
    def is_anime(self) -> bool:
        genre_text = " ".join(self.genres).casefold()
        return "anime" in genre_text or "animation" in genre_text or "动画" in genre_text or "动漫" in genre_text


@dataclass(slots=True)
class Candidate:
    provider: str
    title: str
    source_url: str
    score: int = 0
    exact: bool = False
    year: int | None = None
    media_type: str | None = None
    duration: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "title": self.title,
            "score": self.score,
            "exact": self.exact,
            "year": self.year,
            "duration": self.duration,
        }
