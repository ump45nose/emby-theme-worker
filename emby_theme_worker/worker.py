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
                "anime": ["animethemes", "themerrdb", "plex_tv", "televisiontunes", "fuzzy", "intro_markers"],
                "series": ["themerrdb", "plex_tv", "televisiontunes", "fuzzy", "intro_markers"],
                "movie": ["themerrdb", "fuzzy"],
            },
            "items": [
                {"id": item.id, "name": item.name, "type": item.item_type, "year": item.year, "anime": item.is_anime}
                for item in eligible
            ],
        }

    def probe(self, item_id: str) -> dict:
        item = self.emby.get_item(item_id)
        exact = self._resolve_exact(item)
        fuzzy = [] if exact else self._resolve_fuzzy(item)
        return {
            "item": {"id": item.id, "name": item.name, "type": item.item_type, "year": item.year},
            "exact": [candidate.public_dict() for candidate in exact],
            "fuzzy": [candidate.public_dict() for candidate in fuzzy],
            "eligible": [candidate.public_dict() for candidate in exact + fuzzy if candidate.exact or candidate.score >= self.config.providers.threshold],
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
                # A prior full scan may have missed this sidecar.  Retry the
                # item's documented Default -> FullRefresh route before adding
                # it to another (expensive) library-wide scan.
                if self.emby.refresh_and_verify(item.id):
                    self.db.provider_success("resolver", state.get("source_provider") or "local_opening")
                    self.db.record_item(
                        item, "complete", provider=state.get("source_provider"), score=state.get("score"),
                        output_path=str(target), output_sha256=state["output_sha256"],
                    )
                    return {"item_id": item.id, "status": "complete", "provider": state.get("source_provider")}
                self.db.record_item(
                    item, "pending_refresh", provider=state.get("source_provider"), score=state.get("score"),
                    output_path=str(target), output_sha256=state["output_sha256"],
                )
                return {"item_id": item.id, "status": "pending_refresh", "path": str(target)}
            self.db.record_item(item, "skipped_existing_unindexed", output_path=str(target), output_sha256=sha256(target))
            return {"item_id": item.id, "status": "skipped_existing_unindexed"}

        errors: list[str] = []
        exact = self._resolve_exact(item)
        result = self._try_candidates(item, exact, errors, route="exact")
        if result:
            return result

        # An exact database match identifies the desired theme, but it does not
        # guarantee that the referenced transport (usually YouTube) is usable.
        # Search the independent providers after a transport failure instead of
        # skipping directly to local opening extraction.
        fuzzy = self._resolve_fuzzy(item)
        eligible = [candidate for candidate in fuzzy if candidate.score >= self.config.providers.threshold]
        result = self._try_candidates(item, eligible, errors, route="fuzzy")
        if result:
            return result

        try:
            return self._from_local(item)
        except Exception as exc:
            errors.append(f"local:{exc.__class__.__name__}")
            error_class = "low_score" if fuzzy and not eligible else "not_found"
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

    def _record_candidates(self, item: MediaItem, candidates: list[Candidate]) -> None:
        for candidate in candidates:
            url_hash = hashlib.sha256(candidate.source_url.encode()).hexdigest()
            self.db.record_candidate(item.id, candidate, url_hash)

    def _resolve(self, item: MediaItem) -> tuple[list[Candidate], list[Candidate]]:
        return self._resolve_exact(item), self._resolve_fuzzy(item)

    def _resolve_exact(self, item: MediaItem) -> list[Candidate]:
        exact = self.providers.exact(item)
        for candidate in exact:
            candidate.score = 100
        self._record_candidates(item, exact)
        return exact

    def _resolve_fuzzy(self, item: MediaItem) -> list[Candidate]:
        fuzzy = score_all(self.providers.fuzzy(item), item)
        self._record_candidates(item, fuzzy)
        return fuzzy

    def _try_candidates(
        self,
        item: MediaItem,
        candidates: list[Candidate],
        errors: list[str],
        *,
        route: str,
    ) -> dict | None:
        for candidate in candidates:
            transport = candidate.transport or "direct_http"
            if not self.db.provider_available("transport", transport):
                errors.append(f"{transport}:CircuitOpen")
                continue
            LOG.info(
                "candidate attempt item=%s route=%s provider=%s transport=%s score=%s",
                item.id, route, candidate.provider, transport, candidate.score,
            )
            try:
                return self._from_candidate(item, candidate)
            except Exception as exc:
                errors.append(f"{transport}:{exc.__class__.__name__}")
                self.db.provider_failure("transport", transport, exc.__class__.__name__)
                LOG.warning(
                    "candidate failed item=%s route=%s provider=%s transport=%s error=%s",
                    item.id, route, candidate.provider, transport, exc.__class__.__name__,
                )
        return None

    def _from_candidate(self, item: MediaItem, candidate: Candidate) -> dict:
        with temporary_directory() as work:
            workdir = Path(work)
            source = self.audio.fetch_candidate(candidate, workdir)
            prepared = workdir / "prepared.mp3"
            self.audio.normalize_online(source, prepared)
            info = self.audio.validate(prepared)
            return self._commit(item, prepared, candidate.resolver or candidate.provider, candidate.transport or "direct_http", candidate.score, info)

    def _from_local(self, item: MediaItem) -> dict:
        if item.item_type == "Movie":
            raise ValueError("movie local extraction is disabled")
        intro = self.emby.representative_intro(item.id)
        if not intro:
            raise FileNotFoundError("no valid IntroStart/IntroEnd markers")
        with temporary_directory() as work:
            prepared = Path(work) / "prepared.mp3"
            self.audio.extract_intro(intro["path"], prepared, intro["start"], intro["end"])
            info = self.audio.validate(prepared, min_duration=28, max_duration=62)
            return self._commit(item, prepared, "intro_markers", "local_media", 85, info)

    def _commit(self, item: MediaItem, prepared: Path, provider: str, transport: str, score: int, info: dict) -> dict:
        if self.emby.theme_visible(item.id):
            self.db.record_item(item, "skipped_existing")
            return {"item_id": item.id, "status": "skipped_existing"}
        created, target, digest = self.audio.atomic_commit(prepared, item.directory)
        if not created:
            self.db.record_item(item, "skipped_existing", output_path=str(target), output_sha256=digest)
            return {"item_id": item.id, "status": "skipped_existing"}
        if self.db.get_meta("theme_registration_mode") != "full_scan":
            if self.emby.refresh_and_verify(item.id):
                self.db.provider_success("resolver", provider)
                self.db.provider_success("transport", transport)
                self.db.record_item(item, "complete", provider=provider, score=score, output_path=str(target), output_sha256=digest)
                return {"item_id": item.id, "status": "complete", "provider": provider, "score": score, "path": str(target), "sha256": digest, "duration": info["duration"]}
            self.db.set_meta("theme_registration_mode", "full_scan")
        self.db.provider_success("resolver", provider)
        self.db.provider_success("transport", transport)
        self.db.record_item(item, "pending_refresh", provider=provider, score=score, output_path=str(target), output_sha256=digest)
        return {"item_id": item.id, "status": "pending_refresh", "provider": provider, "score": score, "path": str(target), "sha256": digest, "duration": info["duration"]}

    def migrate_local(self, *, dry_run: bool, item_type: str | None = None, limit: int = 25, item_id: str | None = None) -> dict:
        if item_id:
            state = self.db.item_state(item_id)
            rows = [state] if state and state.get("source_provider") == "local_opening" else []
        else:
            rows = self.db.local_opening_items(item_type)[:limit]
        results: list[dict] = []
        for state in rows:
            try:
                item = self.emby.get_item(str(state["emby_id"]))
                target = item.directory / "theme.mp3"
                expected = str(state["output_sha256"])
                if not target.exists() or sha256(target) != expected:
                    results.append({"item_id": item.id, "status": "changed_or_missing"})
                    self.db.defer_local_migration(item.id)
                    continue
                exact = self._resolve_exact(item)
                if dry_run:
                    fuzzy = [] if exact else self._resolve_fuzzy(item)
                    candidates = exact + [c for c in fuzzy if c.score >= self.config.providers.replacement_threshold]
                    if candidates:
                        results.append({"item_id": item.id, "name": item.name, "status": "ready", "candidate": candidates[0].public_dict()})
                    else:
                        results.append({"item_id": item.id, "status": "no_better_candidate"})
                        self.db.defer_local_migration(item.id)
                    continue
                errors: list[str] = []
                completed = self._try_replacement_candidates(item, state, exact, errors, "exact")
                if not completed:
                    fuzzy = [c for c in self._resolve_fuzzy(item) if c.score >= self.config.providers.replacement_threshold]
                    completed = self._try_replacement_candidates(item, state, fuzzy, errors, "fuzzy")
                if completed:
                    results.append(completed)
                else:
                    results.append({"item_id": item.id, "status": "failed", "errors": errors})
                    self.db.defer_local_migration(item.id)
            except Exception as exc:
                results.append({"item_id": str(state["emby_id"]), "status": "failed", "error": exc.__class__.__name__})
                self.db.defer_local_migration(str(state["emby_id"]))
        return {"dry_run": dry_run, "attempted": len(rows), "results": results}

    def _try_replacement_candidates(self, item: MediaItem, state: dict, candidates: list[Candidate], errors: list[str], route: str) -> dict | None:
        for candidate in candidates:
            transport = candidate.transport or "direct_http"
            if not self.db.provider_available("transport", transport):
                errors.append(f"{route}:{transport}:CircuitOpen")
                continue
            try:
                return self._replace_local(item, state, candidate)
            except Exception as exc:
                self.db.provider_failure("transport", transport, exc.__class__.__name__)
                errors.append(f"{route}:{transport}:{exc.__class__.__name__}")
        return None

    def _replace_local(self, item: MediaItem, state: dict, candidate: Candidate) -> dict:
        expected = str(state["output_sha256"])
        replacement_id = self.db.start_replacement(item.id, candidate, expected, candidate.score)
        target = item.directory / "theme.mp3"
        backup = Path(self.config.replacement_backup_path) / item.id / f"{expected}.mp3"
        replaced = False
        try:
            with temporary_directory() as work:
                workdir = Path(work)
                source = self.audio.fetch_candidate(candidate, workdir)
                prepared = workdir / "prepared.mp3"
                self.audio.normalize_online(source, prepared)
                info = self.audio.validate(prepared)
                digest, backup_path = self.audio.atomic_replace_owned(prepared, target, expected, backup)
                replaced = True
            if not self.emby.refresh_and_verify(item.id):
                raise RuntimeError("replacement not visible through ThemeSongs")
            provider = candidate.resolver or candidate.provider
            transport = candidate.transport or "direct_http"
            self.db.provider_success("resolver", provider)
            self.db.provider_success("transport", transport)
            self.db.record_item(item, "complete", provider=provider, score=candidate.score, output_path=str(target), output_sha256=digest)
            self.db.finish_replacement(replacement_id, "complete", new_sha256=digest, backup_path=backup_path)
            return {"item_id": item.id, "status": "complete", "provider": provider, "transport": transport, "sha256": digest, "duration": info["duration"], "backup": backup_path}
        except Exception as exc:
            if replaced and backup.exists():
                self.audio.restore_replacement(target, backup)
            self.db.finish_replacement(replacement_id, "rolled_back" if replaced else "failed", backup_path=str(backup) if backup.exists() else None, error=exc.__class__.__name__)
            raise

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
                # Do not claim completion without ThemeSongs readback.  These
                # files remain on disk for audit/manual intervention, but a
                # bounded registration failure must not restart bootstrap
                # indefinitely.
                self.db.record_registration_failure(
                    item_id,
                    self.db.backoff_until(days=self.config.backoff["registration_days"]),
                )
                LOG.warning("theme remains unindexed after refresh and library scan item=%s", item_id)
                continue
            item = self.emby.get_item(item_id)
            state = self.db.item_state(item_id) or {}
            self.db.record_item(
                item, "complete", provider=state.get("source_provider"), score=state.get("score"),
                output_path=state.get("output_path"), output_sha256=state.get("output_sha256"),
            )
            completed += 1
        return "complete", completed
