from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

from .models import Candidate, MediaItem


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  mode TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  successes INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  error TEXT
);
CREATE TABLE IF NOT EXISTS items (
  emby_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  item_type TEXT NOT NULL,
  path TEXT NOT NULL,
  provider_ids TEXT NOT NULL,
  status TEXT NOT NULL,
  source_provider TEXT,
  score INTEGER,
  output_path TEXT,
  output_sha256 TEXT,
  last_error_class TEXT,
  last_error TEXT,
  failure_count INTEGER NOT NULL DEFAULT 0,
  retry_after TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  emby_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  title TEXT NOT NULL,
  source_url_hash TEXT NOT NULL,
  score INTEGER NOT NULL,
  exact INTEGER NOT NULL,
  metadata TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_health (
  provider TEXT PRIMARY KEY,
  failures INTEGER NOT NULL DEFAULT 0,
  circuit_until TEXT,
  last_error TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_health_v2 (
  stage TEXT NOT NULL,
  provider TEXT NOT NULL,
  failures INTEGER NOT NULL DEFAULT 0,
  circuit_until TEXT,
  last_error TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(stage, provider)
);
CREATE TABLE IF NOT EXISTS replacements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  emby_id TEXT NOT NULL,
  old_provider TEXT NOT NULL,
  new_resolver TEXT NOT NULL,
  new_transport TEXT NOT NULL,
  old_sha256 TEXT NOT NULL,
  new_sha256 TEXT,
  backup_path TEXT,
  score INTEGER NOT NULL,
  status TEXT NOT NULL,
  error TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat()


class StateDB:
    def __init__(self, path: str):
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
            if "failure_count" not in columns:
                conn.execute("ALTER TABLE items ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0")
            candidate_columns = {row[1] for row in conn.execute("PRAGMA table_info(candidates)")}
            for name in ("resolver", "transport"):
                if name not in candidate_columns:
                    conn.execute(f"ALTER TABLE candidates ADD COLUMN {name} TEXT")
            pipeline_version = conn.execute("SELECT value FROM meta WHERE key='provider_pipeline_version'").fetchone()
            if not pipeline_version or pipeline_version[0] != "2":
                conn.execute(
                    "UPDATE items SET retry_after=NULL WHERE status='failed' "
                    "AND last_error_class IN ('network','not_found','low_score')"
                )
                conn.execute(
                    "INSERT INTO meta(key,value) VALUES('provider_pipeline_version','2') "
                    "ON CONFLICT(key) DO UPDATE SET value='2'"
                )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def start_run(self, mode: str) -> int:
        with self.connect() as conn:
            stamp = now()
            conn.execute(
                "UPDATE runs SET finished_at=?,status='interrupted',"
                "error=COALESCE(error,'superseded by a new run') WHERE status='running'",
                (stamp,),
            )
            cur = conn.execute(
                "INSERT INTO runs(started_at,mode,status) VALUES(?,?,?)",
                (stamp, mode, "running"),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, attempts: int, successes: int, status: str, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE runs SET finished_at=?,attempts=?,successes=?,status=?,error=? WHERE id=?",
                (now(), attempts, successes, status, error, run_id),
            )

    def record_item(
        self,
        item: MediaItem,
        status: str,
        *,
        provider: str | None = None,
        score: int | None = None,
        output_path: str | None = None,
        output_sha256: str | None = None,
        error_class: str | None = None,
        error: str | None = None,
        retry_after: str | None = None,
    ) -> None:
        previous_failures = self.failure_count(item.id)
        failure_count = previous_failures + 1 if status == "failed" else 0
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO items(emby_id,name,item_type,path,provider_ids,status,source_provider,score,
                  output_path,output_sha256,last_error_class,last_error,failure_count,retry_after,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(emby_id) DO UPDATE SET
                  name=excluded.name,item_type=excluded.item_type,path=excluded.path,
                  provider_ids=excluded.provider_ids,status=excluded.status,
                  source_provider=excluded.source_provider,score=excluded.score,
                  output_path=excluded.output_path,output_sha256=excluded.output_sha256,
                  last_error_class=excluded.last_error_class,last_error=excluded.last_error,
                  failure_count=excluded.failure_count,retry_after=excluded.retry_after,updated_at=excluded.updated_at
                """,
                (
                    item.id, item.name, item.item_type, item.path, json.dumps(item.provider_ids), status,
                    provider, score, output_path, output_sha256, error_class, error, failure_count, retry_after, now(),
                ),
            )

    def failure_count(self, item_id: str) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT failure_count FROM items WHERE emby_id=?", (item_id,)).fetchone()
        return int(row["failure_count"]) if row else 0

    def item_state(self, item_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM items WHERE emby_id=?", (item_id,)).fetchone()
        return dict(row) if row else None

    def pending_item_ids(self) -> list[str]:
        with self.connect() as conn:
            return [str(row["emby_id"]) for row in conn.execute("SELECT emby_id FROM items WHERE status='pending_refresh' ORDER BY updated_at")]

    def record_registration_failure(self, item_id: str, retry_after: str) -> None:
        """Keep an unindexed output auditable without blocking bootstrap forever."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE items SET status='failed',last_error_class='registration',"
                "last_error='theme not visible after item refresh and library scan',"
                "failure_count=failure_count+1,retry_after=?,updated_at=? WHERE emby_id=?",
                (retry_after, now(), item_id),
            )

    def run_active(self) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT 1 FROM runs WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone()
        return bool(row)

    def record_candidate(self, item_id: str, candidate: Candidate, url_hash: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO candidates(emby_id,provider,title,source_url_hash,score,exact,metadata,created_at,resolver,transport) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (item_id, candidate.provider, candidate.title, url_hash, candidate.score, int(candidate.exact), json.dumps(candidate.metadata), now(), candidate.resolver, candidate.transport),
            )

    def local_opening_items(self, item_type: str | None = None) -> list[dict]:
        sql = "SELECT * FROM items WHERE source_provider='local_opening' AND output_sha256 IS NOT NULL"
        args: tuple[str, ...] = ()
        if item_type:
            sql += " AND item_type=?"
            args = (item_type,)
        sql += " ORDER BY updated_at,emby_id"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, args)]

    def defer_local_migration(self, item_id: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE items SET updated_at=? WHERE emby_id=?", (now(), item_id))

    def start_replacement(self, item_id: str, candidate: Candidate, old_sha256: str, score: int) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO replacements(emby_id,old_provider,new_resolver,new_transport,old_sha256,score,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (item_id, "local_opening", candidate.resolver or candidate.provider, candidate.transport or "direct_http", old_sha256, score, "running", now()),
            )
            return int(cur.lastrowid)

    def finish_replacement(self, replacement_id: int, status: str, *, new_sha256: str | None = None, backup_path: str | None = None, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE replacements SET status=?,new_sha256=?,backup_path=?,error=?,finished_at=? WHERE id=?",
                (status, new_sha256, backup_path, error, now(), replacement_id),
            )

    def is_due(self, item_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT status,retry_after FROM items WHERE emby_id=?", (item_id,)).fetchone()
        if not row:
            return True
        if row["status"] in {"complete", "skipped_existing", "skipped_existing_unindexed"}:
            return False
        if not row["retry_after"]:
            return True
        return datetime.fromisoformat(row["retry_after"]) <= datetime.now(UTC)

    def backoff_until(self, *, hours: int = 0, days: int = 0) -> str:
        return (datetime.now(UTC) + timedelta(hours=hours, days=days)).isoformat()

    def provider_available(self, stage: str, provider: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT circuit_until FROM provider_health_v2 WHERE stage=? AND provider=?", (stage, provider)).fetchone()
        return not row or not row["circuit_until"] or datetime.fromisoformat(row["circuit_until"]) <= datetime.now(UTC)

    def provider_success(self, stage: str, provider: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO provider_health_v2(stage,provider,failures,updated_at) VALUES(?,?,0,?) "
                "ON CONFLICT(stage,provider) DO UPDATE SET failures=0,circuit_until=NULL,last_error=NULL,updated_at=excluded.updated_at",
                (stage, provider, now()),
            )

    def provider_failure(self, stage: str, provider: str, error: str) -> None:
        with self.connect() as conn:
            row = conn.execute("SELECT failures FROM provider_health_v2 WHERE stage=? AND provider=?", (stage, provider)).fetchone()
            failures = (int(row["failures"]) if row else 0) + 1
            circuit_hours = (6, 24, 72, 168)[min(max(failures - 3, 0), 3)]
            circuit = self.backoff_until(hours=circuit_hours) if failures >= 3 else None
            conn.execute(
                "INSERT INTO provider_health_v2(stage,provider,failures,circuit_until,last_error,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(stage,provider) DO UPDATE SET failures=excluded.failures,circuit_until=excluded.circuit_until,last_error=excluded.last_error,updated_at=excluded.updated_at",
                (stage, provider, failures, circuit, error[:500], now()),
            )

    def status(self) -> dict:
        with self.connect() as conn:
            counts = {r["status"]: r["n"] for r in conn.execute("SELECT status,COUNT(*) n FROM items GROUP BY status")}
            last = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
            circuits = [dict(r) for r in conn.execute("SELECT * FROM provider_health_v2 WHERE circuit_until IS NOT NULL ORDER BY stage,provider")]
            failures = {r["last_error_class"] or "unknown": r["n"] for r in conn.execute("SELECT last_error_class,COUNT(*) n FROM items WHERE status='failed' GROUP BY last_error_class")}
            resolution = [dict(r) for r in conn.execute("SELECT COALESCE(resolver,provider) resolver,COALESCE(transport,'unknown') transport,COUNT(*) candidates,COUNT(DISTINCT emby_id) items FROM candidates GROUP BY 1,2 ORDER BY candidates DESC")]
            replacements = {r["status"]: r["n"] for r in conn.execute("SELECT status,COUNT(*) n FROM replacements GROUP BY status")}
        return {
            "bootstrap_complete": self.get_meta("bootstrap_complete", "false") == "true",
            "items": counts,
            "failures": failures,
            "last_run": dict(last) if last else None,
            "provider_circuits": circuits,
            "resolver_transport": resolution,
            "replacements": replacements,
        }
