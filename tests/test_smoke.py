from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import pytest
import httpx

from emby_theme_worker.audio import AudioProcessor, sha256
from emby_theme_worker.config import Config
from emby_theme_worker.db import StateDB
from emby_theme_worker.emby import EmbyClient
from emby_theme_worker.models import Candidate, MediaItem
from emby_theme_worker.providers import Providers
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
    incomplete = Candidate("youtube", "Example Show Main Theme", "https://example.test/c")
    assert score_candidate(incomplete, media) < 80


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


def test_atomic_owned_replacement_and_restore(tmp_path: Path) -> None:
    processor = AudioProcessor(45, "192k", 30, {})
    target = tmp_path / "theme.mp3"
    prepared = tmp_path / "new.mp3"
    backup = tmp_path / "backups" / "old.mp3"
    target.write_bytes(b"old")
    prepared.write_bytes(b"new")
    new_hash, backup_path = processor.atomic_replace_owned(prepared, target, sha256(target), backup)
    assert target.read_bytes() == b"new" and backup.read_bytes() == b"old"
    assert new_hash == sha256(target) and backup_path == str(backup)
    processor.restore_replacement(target, backup)
    assert target.read_bytes() == b"old"


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


def test_exact_transport_failure_falls_back_to_fuzzy(tmp_path: Path) -> None:
    media_dir = tmp_path / "Movie"
    media_dir.mkdir()
    media = item(media_dir)

    class FakeEmby:
        def theme_visible(self, _item_id: str) -> bool:
            return False

    class FakeProviders:
        def exact(self, _item: MediaItem) -> list[Candidate]:
            return [Candidate("themerrdb", "Exact theme", "https://youtube.test/exact", exact=True)]

        def fuzzy(self, _item: MediaItem) -> list[Candidate]:
            return [Candidate("archive", "Example Show 2025 Official Main Theme Soundtrack", "https://archive.test/theme.mp3", year=2025, media_type="Movie", duration=180)]

    config = Config(database_path=str(tmp_path / "state.db"), allowed_path=str(tmp_path))
    db = StateDB(config.database_path)
    db.initialize()
    worker = object.__new__(Worker)
    worker.config = config
    worker.db = db
    worker.emby = FakeEmby()
    worker.providers = FakeProviders()
    attempted: list[str] = []

    def from_candidate(_item: MediaItem, candidate: Candidate) -> dict:
        attempted.append(candidate.provider)
        if candidate.provider == "themerrdb":
            raise TimeoutError("exact transport unavailable")
        return {"item_id": media.id, "status": "complete", "provider": candidate.provider}

    worker._from_candidate = from_candidate  # type: ignore[method-assign]
    result = worker.process(media)
    assert result["provider"] == "archive"
    assert attempted == ["themerrdb", "archive"]


def test_provider_health_isolated_by_stage(tmp_path: Path) -> None:
    db = StateDB(str(tmp_path / "state.db"))
    db.initialize()
    for _ in range(3):
        db.provider_failure("transport", "youtube", "Timeout")
    assert not db.provider_available("transport", "youtube")
    assert db.provider_available("resolver", "themerrdb")


def test_exact_collects_all_resolvers(tmp_path: Path) -> None:
    db = StateDB(str(tmp_path / "state.db"))
    db.initialize()
    providers = Providers(["themerrdb", "plex_tv", "televisiontunes"], 5, 1, db, {})
    providers.themerrdb = lambda _item: Candidate("themerrdb", "a", "https://a", exact=True, transport="youtube")  # type: ignore[method-assign]
    providers.plex_tv = lambda _item: Candidate("plex_tv", "b", "https://b", exact=True)  # type: ignore[method-assign]
    providers.televisiontunes = lambda _item: [Candidate("televisiontunes", "c", "https://c", exact=True)]  # type: ignore[method-assign]
    try:
        assert [c.provider for c in providers.exact(item(tmp_path, "Series"))] == ["themerrdb", "plex_tv", "televisiontunes"]
    finally:
        providers.close()


def test_televisiontunes_two_stage_parser(tmp_path: Path) -> None:
    db = StateDB(str(tmp_path / "state.db"))
    db.initialize()
    providers = Providers(["televisiontunes"], 5, 1, db, {})
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200, text='<a href="https://www.televisiontunes.co.uk/example-show">Example Show</a>')
        return httpx.Response(200, text='{"contentUrl":"https://www.televisiontunes.co.uk/themes/Example.wav"}')
    providers.http.close()
    providers.http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    try:
        found = providers.televisiontunes(item(tmp_path, "Series"))
        assert len(found) == 1 and found[0].exact and found[0].source_url.endswith("Example.wav")
    finally:
        providers.close()


def test_movie_local_extraction_is_disabled(tmp_path: Path) -> None:
    worker = object.__new__(Worker)
    with pytest.raises(ValueError, match="disabled"):
        worker._from_local(item(tmp_path, "Movie"))


def test_unregistered_pending_output_becomes_registration_failure(tmp_path: Path) -> None:
    media_dir = tmp_path / "Movie"
    media_dir.mkdir()
    media = item(media_dir)
    config = Config(database_path=str(tmp_path / "state.db"), allowed_path=str(tmp_path))
    db = StateDB(config.database_path)
    db.initialize()
    db.record_item(media, "pending_refresh", provider="local_opening", output_path=str(media_dir / "theme.mp3"), output_sha256="a" * 64)

    class FakeEmby:
        def trigger_library_scan(self) -> None:
            pass

        def wait_library_scan(self, _timeout: int, _poll: int) -> bool:
            return True

        def theme_visible(self, _item_id: str) -> bool:
            return False

    worker = object.__new__(Worker)
    worker.config = config
    worker.db = db
    worker.emby = FakeEmby()
    status, completed = worker._register_pending()
    state = db.item_state(media.id)
    assert status == "complete"
    assert completed == 0
    assert state and state["status"] == "failed" and state["last_error_class"] == "registration"
    assert db.pending_item_ids() == []
    assert not db.is_due(media.id)


def test_item_refresh_timeout_does_not_abort_registration() -> None:
    class TimeoutClient:
        def post(self, *_args: object, **_kwargs: object) -> object:
            raise httpx.ReadTimeout("timed out")

    client = object.__new__(EmbyClient)
    client.client = TimeoutClient()
    assert client.refresh_and_verify("1") is False
