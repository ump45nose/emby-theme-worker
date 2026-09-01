from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import pytest

from emby_theme_worker.audio import AudioProcessor, sha256
from emby_theme_worker.config import Config
from emby_theme_worker.db import StateDB
from emby_theme_worker.models import Candidate, MediaItem
from emby_theme_worker.scoring import score_candidate
from emby_theme_worker.security import RedactingFilter, contained, redact, safe_http_url_from_strm
from emby_theme_worker.worker import Worker
from emby_theme_worker.ytdlp_runner import YtDlpTimeout, _run


def item(path: Path, item_type: str = "Movie") -> MediaItem:
    return MediaItem("1", "Example Show", "Example Show", 2025, item_type, str(path), {"Tmdb": "1"}, ["Drama"])


def test_config_validation(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("allowed_path: /Media\nschedule: '0 3 * * *'\n", encoding="utf-8")
    assert Config.load(config_file).allowed_path == "/Media"
    config_file.write_text("allowed_path: relative\n", encoding="utf-8")
    with pytest.raises(ValueError):
        Config.load(config_file)


def test_ytdlp_wall_clock_timeout() -> None:
    started = time.monotonic()
    with pytest.raises(YtDlpTimeout):
        _run([sys.executable, "-c", "import time; time.sleep(10)"], 1)
    assert time.monotonic() - started < 3


def test_path_containment() -> None:
    assert contained("/Media/Movies/A", "/Media")
    assert not contained("/MediaAdult/A", "/Media")
    assert not contained("/Adult/A", "/Media")


def test_log_redaction() -> None:
    message = "GET https://example.test/path?q=secret&token=abc cookie=def"
    output = redact(message)
    assert "secret" not in output
    assert "abc" not in output
    assert "def" not in output
    record = logging.LogRecord("test", logging.INFO, __file__, 1, message, (), None)
    assert RedactingFilter().filter(record)
    assert "abc" not in record.msg


def test_candidate_scoring() -> None:
    media = item(Path("/Media/Example/film.mkv"))
    good = Candidate("youtube", "Example Show 2025 Official Main Theme Soundtrack", "https://example.test/a", year=2025, media_type="Movie", duration=180)
    cover = Candidate("youtube", "Example Show 2025 Main Theme Cover Remix Live", "https://example.test/b", year=2025, media_type="Movie", duration=180)
    assert score_candidate(good, media) >= 75
    assert score_candidate(cover, media) < score_candidate(good, media)


def test_strm_single_absolute_url() -> None:
    assert safe_http_url_from_strm("https://example.test/video?id=secret\n").startswith("https://")
    with pytest.raises(ValueError):
        safe_http_url_from_strm("https://a.test/1\nhttps://b.test/2")
    with pytest.raises(ValueError):
        safe_http_url_from_strm("/local/file.mkv")


def test_atomic_no_overwrite(tmp_path: Path) -> None:
    processor = AudioProcessor(45, "192k", 30, {})
    target_dir = tmp_path / "movie"
    target_dir.mkdir()
    first = tmp_path / "first.mp3"
    second = tmp_path / "second.mp3"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    created, target, digest = processor.atomic_commit(first, target_dir)
    assert created and target.read_bytes() == b"first" and digest == sha256(target)
    created, _, digest2 = processor.atomic_commit(second, target_dir)
    assert not created and target.read_bytes() == b"first" and digest2 == digest
    assert not (target_dir / ".theme.mp3.lock").exists()


def test_existing_theme_is_skipped(tmp_path: Path) -> None:
    media_dir = tmp_path / "Series"
    media_dir.mkdir()
    theme = media_dir / "theme.mp3"
    theme.write_bytes(b"existing-theme")
    before = sha256(theme)

    class FakeEmby:
        def theme_visible(self, _item_id: str) -> bool:
            return True

        def close(self) -> None:
            pass

    config = Config(database_path=str(tmp_path / "state.db"), allowed_path=str(tmp_path))
    db = StateDB(config.database_path)
    db.initialize()
    worker = Worker(config, db, FakeEmby())  # type: ignore[arg-type]
    try:
        result = worker.process(item(media_dir, "Series"))
    finally:
        worker.close()
    assert result["status"] == "skipped_existing"
    assert sha256(theme) == before
