from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter

from . import __version__
from .config import Config
from .db import StateDB
from .emby import EmbyClient
from .security import configure_logging, read_secret
from .worker import Worker


LOG = logging.getLogger(__name__)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="emby-theme-worker")
    root.add_argument("--config", default=os.environ.get("EMBY_THEME_CONFIG", "/config/config.yaml"))
    root.add_argument("--verbose", action="store_true")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    preview = commands.add_parser("preview")
    preview.add_argument("--json", action="store_true")
    preview.add_argument("--item")
    probe = commands.add_parser("probe")
    probe.add_argument("--item", required=True)
    probe.add_argument("--json", action="store_true")
    run = commands.add_parser("run")
    run.add_argument("--item")
    run.add_argument("--limit", type=int)
    status = commands.add_parser("status")
    status.add_argument("--json", action="store_true")
    migrate = commands.add_parser("migrate-local")
    migrate.add_argument("--dry-run", action="store_true")
    migrate.add_argument("--type", choices=("Movie", "Series"))
    migrate.add_argument("--limit", type=int, default=25)
    migrate.add_argument("--item")
    intro = commands.add_parser("intro")
    intro_commands = intro.add_subparsers(dest="intro_command", required=True)
    for name in ("status", "enable", "run"):
        child = intro_commands.add_parser(name)
        child.add_argument("--library", required=True)
        if name == "status":
            child.add_argument("--json", action="store_true")
    commands.add_parser("health")
    commands.add_parser("serve")
    return root


def load(path: str) -> tuple[Config, StateDB]:
    config = Config.load(path)
    config.providers.cookies = {name: value for name, value in config.providers.cookies.items() if Path(value).exists()}
    db = StateDB(config.database_path)
    db.initialize()
    return config, db


def make_worker(config: Config, db: StateDB) -> Worker:
    key = read_secret(config.emby_api_key_file)
    emby = EmbyClient(config.emby_url, key or "", config.allowed_path, config.providers.timeout_seconds)
    return Worker(config, db, emby)


def doctor(config: Config, db: StateDB) -> dict:
    checks: dict[str, dict] = {}
    try:
        key = read_secret(config.emby_api_key_file)
        checks["emby_api_key"] = {"ok": bool(key), "path": config.emby_api_key_file}
    except Exception as exc:
        key = None
        checks["emby_api_key"] = {"ok": False, "error": exc.__class__.__name__}
    try:
        with sqlite3.connect(db.path) as conn:
            conn.execute("SELECT 1").fetchone()
        checks["sqlite"] = {"ok": True, "path": str(db.path)}
    except Exception as exc:
        checks["sqlite"] = {"ok": False, "error": exc.__class__.__name__}
    allowed = Path(config.allowed_path)
    checks["media_volume"] = {
        "ok": allowed.is_dir() and os.access(allowed, os.R_OK | os.W_OK | os.X_OK),
        "path": str(allowed),
        "adult_mounted": Path("/Adult").exists(),
    }
    for binary in ("ffmpeg", "ffprobe", "deno"):
        path = shutil.which(binary)
        checks[binary] = {"ok": bool(path), "path": path}
    try:
        import yt_dlp
        checks["yt_dlp"] = {"ok": True, "version": yt_dlp.version.__version__}
    except Exception as exc:
        checks["yt_dlp"] = {"ok": False, "error": exc.__class__.__name__}
    if key:
        try:
            emby = EmbyClient(config.emby_url, key, config.allowed_path, config.providers.timeout_seconds)
            info = emby.system_info()
            checks["emby"] = {"ok": True, "version": info.get("Version"), "server": info.get("ServerName")}
            emby.close()
        except Exception as exc:
            checks["emby"] = {"ok": False, "error": exc.__class__.__name__}
    return {"ok": all(check.get("ok", False) for check in checks.values()), "checks": checks}


def health(config: Config, db: StateDB) -> dict:
    db_ok = False
    try:
        with sqlite3.connect(db.path, timeout=2) as conn:
            db_ok = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    except Exception:
        pass
    heartbeat = db.get_meta("scheduler_heartbeat")
    scheduler_ok = True
    if heartbeat:
        scheduler_ok = (datetime.now(UTC) - datetime.fromisoformat(heartbeat)).total_seconds() < 300
    if db.run_active():
        scheduler_ok = True
    return {"ok": db_ok and scheduler_ok, "sqlite": db_ok, "scheduler": scheduler_ok}


def serve(config: Config, db: StateDB) -> None:
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    tz = ZoneInfo(config.timezone)
    next_run = croniter(config.schedule, datetime.now(tz)).get_next(datetime)
    LOG.info("scheduler started; next run %s", next_run.isoformat())
    while not stopping:
        now = datetime.now(tz)
        db.set_meta("scheduler_heartbeat", datetime.now(UTC).isoformat())
        if now >= next_run:
            worker = make_worker(config, db)
            try:
                result = worker.run()
                LOG.info("scheduled run complete attempts=%s successes=%s", result["attempts"], result["successes"])
            except Exception:
                LOG.exception("scheduled run failed")
            finally:
                worker.close()
            next_run = croniter(config.schedule, now).get_next(datetime)
        time.sleep(30)
    LOG.info("scheduler stopped")


def emit(payload: dict, json_output: bool = True) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        for key, value in payload.items():
            print(f"{key}: {value}")


def emby_intro_status(worker: Worker, library_id: str) -> dict:
    return worker.emby.intro_status(library_id)


def enable_intro(worker: Worker, library_id: str) -> dict:
    folder = worker.emby.virtual_folder(library_id)
    backup_dir = Path(worker.config.database_path).parent / "library-options"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir / f"{library_id}-{stamp}.json"
    backup.write_text(json.dumps(folder, ensure_ascii=False, indent=2), encoding="utf-8")
    updated = worker.emby.enable_intro_detection(library_id)
    return {
        "library_id": library_id,
        "name": updated.get("Name"),
        "backup": str(backup),
        "marker_detection": (updated.get("LibraryOptions") or {}).get("EnableMarkerDetection"),
        "marker_during_scan": (updated.get("LibraryOptions") or {}).get("EnableMarkerDetectionDuringLibraryScan"),
    }


def run_intro(worker: Worker, library_id: str) -> dict:
    worker.emby.virtual_folder(library_id)
    tasks = []
    for key in ("IntroFingerprintExtractTask", "markers"):
        tasks.append(worker.emby.run_task(key, worker.config.refresh.library_scan_timeout_seconds, worker.config.refresh.task_poll_seconds))
    return {"library_id": library_id, "tasks": tasks, "status": worker.emby.intro_status(library_id)}


def main() -> None:
    args = parser().parse_args()
    configure_logging(args.verbose)
    try:
        config, db = load(args.config)
        if args.command == "doctor":
            result = doctor(config, db)
            emit(result)
            raise SystemExit(0 if result["ok"] else 1)
        if args.command == "health":
            result = health(config, db)
            emit(result)
            raise SystemExit(0 if result["ok"] else 1)
        if args.command == "status":
            result = db.status()
            worker = make_worker(config, db)
            try:
                result["intro_libraries"] = [worker.emby.intro_status(library_id) for library_id in config.intro_library_ids]
            finally:
                worker.close()
            emit(result, args.json)
            return
        if args.command == "serve":
            serve(config, db)
            return
        worker = make_worker(config, db)
        try:
            if args.command == "preview":
                emit(worker.preview(args.item), args.json)
            elif args.command == "probe":
                emit(worker.probe(args.item), args.json)
            elif args.command == "run":
                emit(worker.run(item_id=args.item, limit=args.limit))
            elif args.command == "migrate-local":
                emit(worker.migrate_local(dry_run=args.dry_run, item_type=args.type, limit=args.limit, item_id=args.item))
            elif args.command == "intro":
                if args.intro_command == "status":
                    emit(emby_intro_status(worker, args.library), args.json)
                elif args.intro_command == "enable":
                    emit(enable_intro(worker, args.library))
                elif args.intro_command == "run":
                    emit(run_intro(worker, args.library))
        finally:
            worker.close()
    except SystemExit:
        raise
    except Exception as exc:
        LOG.error("command failed: %s", exc.__class__.__name__)
        if args.verbose:
            LOG.exception("details")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
