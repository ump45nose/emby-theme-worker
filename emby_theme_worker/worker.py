from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from .audio import AudioProcessor, sha256, temporary_directory
from .config import Config
from .db import StateDB
from .emby import EmbyClient
from .models import Candidate, MediaItem
from .providers import Providers
from .scoring import score_all
from .security import redact


LOG = logging.getLogger(__name__)


class Worker:
    def __init__(self, config: Config, db: StateDB, emby: EmbyClient):
        self.config = config
        self.db = db
        self.emby = emby
        self.providers = Providers(
            config.providers.enabled,
            config.providers.timeout_seconds,
            config.limits.network_concurrency,
            db,
            config.providers.cookies,
            config.providers.ytdlp_search_timeout_seconds,
        )
        self.audio = AudioProcessor(
            config.target_seconds,
            config.output_bitrate,
            config.providers.timeout_seconds,
            config.providers.cookies,
            config.providers.ytdlp_download_timeout_seconds,
        )

    def close(self) -> None:
        self.providers.close()
        self.emby.close()

    def preview(self, item_id: str | None = None) -> dict:
        items = [self.emby.get_item(item_id)] if item_id else list(self.emby.iter_missing())
        eligible: list[MediaItem] = []
        disk_theme_skips = 0
        for item in items:
            if (item.directory / "theme.mp3").exists():
                disk_theme_skips += 1
            else:
                eligible.append(item)
        emby_theme_count = int(self.emby.theme_visible(items[0].id)) if item_id and items else len(list(self.emby.iter_themed()))
        return {
            "allowed_path": self.config.allowed_path,
            "adult_paths_mounted": False,
            "eligible_missing": len(eligible),
            "existing_theme_skips": emby_theme_count + disk_theme_skips,
            "emby_theme_count": emby_theme_count,
            "unindexed_disk_theme_count": disk_theme_skips,
            "provider_routes": {
                "anime": ["animethemes", "themerrdb", "plex_tv", "fuzzy", "local_opening"],
                "series": ["themerrdb", "plex_tv", "fuzzy", "local_opening"],
                "movie": ["themerrdb", "fuzzy", "local_opening"],
            },
            "items": [
                {"id": item.id, "name": item.name, "type": item.item_type, "year": item.year, "anime": item.is_anime}
                for item in eligible
            ],
        }

    def run(self, *, item_id: str | None = None, limit: int | None = None) -> dict:
        bootstrap = self.db.get_meta("bootstrap_complete", "false") != "true"
        mode = "manual_item" if item_id else ("bootstrap" if bootstrap else "scheduled")
        run_id = self.db.start_run(mode)
        attempts = successes = 0
        completed_scan = True
        library_scan = "not_requested"
        try:
            items = [self.emby.get_item(item_id)] if item_id else self.emby.iter_missing()
            attempt_cap = limit
            success_cap: int | None = None
            if item_id is None and limit is None:
                if bootstrap:
                    attempt_cap = self.config.limits.bootstrap_attempts or None
                else:
                    attempt_cap = self.config.limits.regular_attempts
                    success_cap = self.config.limits.regular_successes
            for item in items:
                if attempt_cap is not None and attempt_cap > 0 and attempts >= attempt_cap:
                    completed_scan = False
                    break
                if success_cap is not None and success_cap > 0 and successes >= success_cap:
                    completed_scan = False
                    break
                # An explicit operator retry is authoritative. Scheduled scans
                # still honor per-item backoff and terminal skip states.
                if item_id is None and not self.db.is_due(item.id):
                    continue
                attempts += 1
                result = self.process(item)
                if result["status"] == "complete":
                    successes += 1
            if item_id is None and self.db.pending_item_ids() and self.config.refresh.full_scan_after_scheduled_run:
                library_scan, registered = self._register_pending()
                successes += registered
            pending = len(self.db.pending_item_ids())
            if item_id is None and bootstrap and completed_scan and pending == 0:
                self.db.set_meta("bootstrap_complete", "true")
                self.db.set_meta("bootstrap_completed_at", __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat())
            self.db.finish_run(run_id, attempts, successes, "complete")
            return {
                "run_id": run_id,
                "mode": mode,
                "attempts": attempts,
                "successes": successes,
                "scan_complete": completed_scan,
                "library_scan": library_scan,
                "pending_refresh": pending,
            }
        except Exception as exc:
            self.db.finish_run(run_id, attempts, successes, "failed", redact(exc))
            raise

    def process(self, item: MediaItem) -> dict:
        LOG.info("processing item=%s type=%s", item.id, item.item_type)
        target = item.directory / "theme.mp3"
        state = self.db.item_state(item.id)
        if self.emby.theme_visible(item.id):
            if state and state.get("output_sha256") and target.exists() and sha256(target) == state["output_sha256"]:
                self.db.record_item(
                    item, "complete", provider=state.get("source_provider"), score=state.get("score"),
                    output_path=str(target), output_sha256=state["output_sha256"],
                )
                return {"item_id": item.id, "status": "complete", "provider": state.get("source_provider")}
            self.db.record_item(item, "skipped_existing")
            return {"item_id": item.id, "status": "skipped_existing"}
        if target.exists():
            owned = bool(
                state
                and state.get("output_sha256")
                and state.get("status") in {"pending_refresh", "failed"}
                and sha256(target) == state["output_sha256"]
            )
            if owned:
                self.db.set_meta("theme_registration_mode", "full_scan")
                self.db.record_item(
                    item, "pending_refresh", provider=state.get("source_provider"), score=state.get("score"),
                    output_path=str(target), output_sha256=state["output_sha256"],
                )
                return {"item_id": item.id, "status": "pending_refresh", "path": str(target)}
            self.db.record_item(item, "skipped_existing_unindexed", output_path=str(target), output_sha256=sha256(target))
            return {"item_id": item.id, "status": "skipped_existing_unindexed"}

        exact = self.providers.exact(item)
        for candidate in exact:
            candidate.score = 100
        fuzzy: list[Candidate] = [] if exact else score_all(self.providers.fuzzy(item), item)
        candidates = exact + [c for c in fuzzy if c.score >= self.config.providers.threshold]
        for candidate in exact + fuzzy:
            url_hash = hashlib.sha256(candidate.source_url.encode()).hexdigest()
            self.db.record_candidate(item.id, candidate, url_hash)

        errors: list[str] = []
        for candidate in candidates:
            try:
                return self._from_candidate(item, candidate)
            except Exception as exc:
                errors.append(f"{candidate.provider}:{exc.__class__.__name__}")
                self.db.provider_failure(candidate.provider, exc.__class__.__name__)
                LOG.warning("candidate failed item=%s provider=%s error=%s", item.id, candidate.provider, exc.__class__.__name__)

        try:
            return self._from_local(item)
        except Exception as exc:
            errors.append(f"local:{exc.__class__.__name__}")
            error_class = "low_score" if fuzzy and not candidates else "not_found"
            if any("HTTP" in err or "Timeout" in err for err in errors):
                error_class = "network"
            if error_class == "network":
                failures = self.db.failure_count(item.id)
                hours = (self.config.backoff["network_hours"], 24, 72, 168)[min(failures, 3)]
                retry = self.db.backoff_until(hours=hours)
            elif error_class == "low_score":
                retry = self.db.backoff_until(days=self.config.backoff["low_score_days"])
            else:
                retry = self.db.backoff_until(days=self.config.backoff["not_found_days"])
            self.db.record_item(item, "failed", error_class=error_class, error=";".join(errors), retry_after=retry)
            return {"item_id": item.id, "status": "failed", "error_class": error_class, "errors": errors}

    def _from_candidate(self, item: MediaItem, candidate: Candidate) -> dict:
        with temporary_directory() as work:
            workdir = Path(work)
            source = self.audio.fetch_candidate(candidate, workdir)
            prepared = workdir / "prepared.mp3"
            self.audio.normalize_online(source, prepared)
            info = self.audio.validate(prepared)
            return self._commit(item, prepared, candidate.provider, candidate.score, info)

    def _from_local(self, item: MediaItem) -> dict:
        media_path = item.path if item.item_type == "Movie" else self.emby.earliest_episode_media(item.id)
        if not media_path:
            raise FileNotFoundError("no local media for opening extraction")
        with temporary_directory() as work:
            prepared = Path(work) / "prepared.mp3"
            self.audio.extract_opening(media_path, prepared)
            info = self.audio.validate(prepared)
            return self._commit(item, prepared, "local_opening", 0, info)

    def _commit(self, item: MediaItem, prepared: Path, provider: str, score: int, info: dict) -> dict:
        if self.emby.theme_visible(item.id):
            self.db.record_item(item, "skipped_existing")
            return {"item_id": item.id, "status": "skipped_existing"}
        created, target, digest = self.audio.atomic_commit(prepared, item.directory)
        if not created:
            self.db.record_item(item, "skipped_existing", output_path=str(target), output_sha256=digest)
            return {"item_id": item.id, "status": "skipped_existing"}
        if self.db.get_meta("theme_registration_mode") != "full_scan":
            if self.emby.refresh_and_verify(item.id):
                self.db.provider_success(provider)
                self.db.record_item(item, "complete", provider=provider, score=score, output_path=str(target), output_sha256=digest)
                return {"item_id": item.id, "status": "complete", "provider": provider, "score": score, "path": str(target), "sha256": digest, "duration": info["duration"]}
            self.db.set_meta("theme_registration_mode", "full_scan")
        self.db.provider_success(provider)
        self.db.record_item(item, "pending_refresh", provider=provider, score=score, output_path=str(target), output_sha256=digest)
        return {"item_id": item.id, "status": "pending_refresh", "provider": provider, "score": score, "path": str(target), "sha256": digest, "duration": info["duration"]}

    def _register_pending(self) -> tuple[str, int]:
        pending = self.db.pending_item_ids()
        if not pending:
            return "not_requested", 0
        LOG.info("requesting one Emby media-library scan for %s pending theme(s)", len(pending))
        self.emby.trigger_library_scan()
        finished = self.emby.wait_library_scan(
            self.config.refresh.library_scan_timeout_seconds,
            self.config.refresh.task_poll_seconds,
        )
        if not finished:
            return "timeout", 0
        completed = 0
        for item_id in pending:
            if not self.emby.theme_visible(item_id):
                continue
            item = self.emby.get_item(item_id)
            state = self.db.item_state(item_id) or {}
            self.db.record_item(
                item, "complete", provider=state.get("source_provider"), score=state.get("score"),
                output_path=state.get("output_path"), output_sha256=state.get("output_sha256"),
            )
            completed += 1
        return "complete", completed
