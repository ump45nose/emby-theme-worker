from __future__ import annotations

import re
import unicodedata
from collections import Counter

from rapidfuzz.fuzz import partial_ratio, ratio

from .models import Candidate, MediaItem


POSITIVE = re.compile(r"\b(main theme|theme song|official theme|opening|op\s*1|ost|soundtrack|主题曲|片头曲|原声)\b", re.I)
NEGATIVE = {
    "cover": 30,
    "remix": 35,
    "live": 20,
    "karaoke": 40,
    "instrumental": 20,
    "trailer": 35,
    "reaction": 50,
    "playlist": 30,
    "翻唱": 35,
    "混剪": 35,
    "现场": 20,
    "伴奏": 35,
}


def normalize(text: str | None) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return " ".join(re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff]+", " ", text).split())


def score_candidate(candidate: Candidate, item: MediaItem, consensus: int = 1) -> int:
    if candidate.exact:
        candidate.score = 100
        return 100
    titles = [normalize(item.name), normalize(item.original_title)]
    title = normalize(candidate.title)
    title_score = max((partial_ratio(title, expected) for expected in titles if expected), default=0)
    score = round(title_score * 0.45)
    if item.year and candidate.year:
        score += 10 if abs(item.year - candidate.year) <= 1 else -15
    if candidate.media_type and candidate.media_type.casefold() == item.item_type.casefold():
        score += 5
    if POSITIVE.search(candidate.title):
        score += 15
    if candidate.duration is not None and 30 <= candidate.duration <= 600:
        score += 10
    score += min(15, max(0, consensus - 1) * 5)
    lowered = candidate.title.casefold()
    for term, penalty in NEGATIVE.items():
        if term in lowered:
            score -= penalty
    candidate.score = max(0, min(100, score))
    return candidate.score


def score_all(candidates: list[Candidate], item: MediaItem) -> list[Candidate]:
    tokens = [normalize(c.title) for c in candidates]
    provider_counts: Counter[str] = Counter()
    for token in tokens:
        providers = {c.provider for c in candidates if ratio(token, normalize(c.title)) >= 85}
        provider_counts[token] = len(providers)
    for candidate, token in zip(candidates, tokens, strict=True):
        score_candidate(candidate, item, provider_counts[token])
    return sorted(candidates, key=lambda c: (c.exact, c.score), reverse=True)
