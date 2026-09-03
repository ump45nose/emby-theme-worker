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
                "INSERT INTO candidates(emby_id,provider,title,source_url_hash,score,exact,metadata,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (item_id, candidate.provider, candidate.title, url_hash, candidate.score, int(candidate.exact), json.dumps(candidate.metadata), now()),
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

    def provider_available(self, provider: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT circuit_until FROM provider_health WHERE provider=?", (provider,)).fetchone()
        return not row or not row["circuit_until"] or datetime.fromisoformat(row["circuit_until"]) <= datetime.now(UTC)

    def provider_success(self, provider: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO provider_health(provider,failures,updated_at) VALUES(?,0,?) "
                "ON CONFLICT(provider) DO UPDATE SET failures=0,circuit_until=NULL,last_error=NULL,updated_at=excluded.updated_at",
                (provider, now()),
            )

    def provider_failure(self, provider: str, error: str) -> None:
        with self.connect() as conn:
            row = conn.execute("SELECT failures FROM provider_health WHERE provider=?", (provider,)).fetchone()
            failures = (int(row["failures"]) if row else 0) + 1
            circuit_hours = (6, 24, 72, 168)[min(max(failures - 3, 0), 3)]
            circuit = self.backoff_until(hours=circuit_hours) if failures >= 3 else None
            conn.execute(
                "INSERT INTO provider_health(provider,failures,circuit_until,last_error,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(provider) DO UPDATE SET failures=excluded.failures,circuit_until=excluded.circuit_until,last_error=excluded.last_error,updated_at=excluded.updated_at",
                (provider, failures, circuit, error[:500], now()),
            )

    def status(self) -> dict:
        with self.connect() as conn:
            counts = {r["status"]: r["n"] for r in conn.execute("SELECT status,COUNT(*) n FROM items GROUP BY status")}
            last = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
            circuits = [dict(r) for r in conn.execute("SELECT * FROM provider_health WHERE circuit_until IS NOT NULL ORDER BY provider")]
            failures = {r["last_error_class"] or "unknown": r["n"] for r in conn.execute("SELECT last_error_class,COUNT(*) n FROM items WHERE status='failed' GROUP BY last_error_class")}
        return {
            "bootstrap_complete": self.get_meta("bootstrap_complete", "false") == "true",
            "items": counts,
            "failures": failures,
            "last_run": dict(last) if last else None,
            "provider_circuits": circuits,
        }
