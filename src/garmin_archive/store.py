"""PostgreSQL storage for the raw archive."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from importlib.resources import files
from typing import Any

import psycopg
from psycopg.types.json import Jsonb


def is_empty(payload: Any) -> bool:
    """True for responses that carry no data at all."""
    return payload is None or payload == {} or payload == [] or payload == ""


def _sha256_json(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class Store:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    @classmethod
    def connect(cls, database_url: str) -> "Store":
        return cls(psycopg.connect(database_url, autocommit=False))

    def close(self) -> None:
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    def apply_schema(self) -> None:
        sql = files("garmin_archive").joinpath("schema.sql").read_text()
        with self.conn.cursor() as cur:
            cur.execute(sql)
        self.conn.commit()

    # --- daily data -----------------------------------------------------

    def save_daily(self, source: str, day: date, payload: Any) -> None:
        """Insert or update one day. An empty response never replaces stored data."""
        value = None if is_empty(payload) else Jsonb(payload)
        self.conn.execute(
            """
            INSERT INTO archive.daily (source, day, payload)
            VALUES (%s, %s, %s)
            ON CONFLICT (source, day) DO UPDATE
               SET payload = EXCLUDED.payload, fetched_at = now()
             WHERE EXCLUDED.payload IS NOT NULL OR archive.daily.payload IS NULL
            """,
            (source, day, value),
        )

    def stored_sources(self, day: date) -> set[str]:
        rows = self.conn.execute(
            "SELECT source FROM archive.daily WHERE day = %s", (day,)
        ).fetchall()
        return {r[0] for r in rows}

    # --- account snapshots ----------------------------------------------

    def save_snapshot(self, source: str, payload: Any) -> bool:
        """Add a row if the content changed since the latest one. Returns True if added."""
        if is_empty(payload):
            return False
        digest = _sha256_json(payload)
        latest = self.conn.execute(
            "SELECT sha256 FROM archive.snapshot WHERE source = %s ORDER BY id DESC LIMIT 1",
            (source,),
        ).fetchone()
        if latest and latest[0] == digest:
            return False
        self.conn.execute(
            "INSERT INTO archive.snapshot (source, sha256, payload) VALUES (%s, %s, %s)",
            (source, digest, Jsonb(payload)),
        )
        return True

    # --- activities -----------------------------------------------------

    def save_activity_summary(self, summary: dict) -> None:
        activity_type = (summary.get("activityType") or {}).get("typeKey")
        start_gmt = summary.get("startTimeGMT")
        self.conn.execute(
            """
            INSERT INTO archive.activity (activity_id, start_time, activity_type, summary)
            VALUES (%s, %s::timestamp AT TIME ZONE 'UTC', %s, %s)
            ON CONFLICT (activity_id) DO UPDATE
               SET start_time = EXCLUDED.start_time,
                   activity_type = EXCLUDED.activity_type,
                   summary = EXCLUDED.summary,
                   fetched_at = now()
            """,
            (int(summary["activityId"]), start_gmt, activity_type, Jsonb(summary)),
        )

    def save_activity_details(self, activity_id: int, details: Any) -> None:
        if is_empty(details):
            return
        self.conn.execute(
            "UPDATE archive.activity SET details = %s WHERE activity_id = %s",
            (Jsonb(details), activity_id),
        )

    def save_activity_files(self, activity_id: int, files_: dict[str, bytes]) -> None:
        for name, content in files_.items():
            self.conn.execute(
                """
                INSERT INTO archive.activity_file (activity_id, filename, content, sha256)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (activity_id, filename) DO UPDATE
                   SET content = EXCLUDED.content, sha256 = EXCLUDED.sha256, fetched_at = now()
                """,
                (activity_id, name, content, hashlib.sha256(content).hexdigest()),
            )
        self.set_file_status(activity_id, "ok" if files_ else "none")

    def set_file_status(self, activity_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE archive.activity SET file_status = %s WHERE activity_id = %s",
            (status, activity_id),
        )

    def activities_needing_work(self, limit: int) -> list[tuple[int, bool, bool, bool]]:
        """Activities missing details or a file attempt.

        Returns (id, needs_details, needs_file, is_manual) tuples, newest first.
        """
        rows = self.conn.execute(
            """
            SELECT activity_id, details IS NULL, file_status IS NULL,
                   coalesce((summary->>'manualActivity')::boolean, false)
              FROM archive.activity
             WHERE details IS NULL OR file_status IS NULL
             ORDER BY start_time DESC NULLS LAST
             LIMIT %s
            """,
            (limit,),
        ).fetchall()
        return [(r[0], r[1], r[2], r[3]) for r in rows]

    # --- run log --------------------------------------------------------

    def start_run(self, command: str) -> int:
        row = self.conn.execute(
            "INSERT INTO archive.sync_run (command) VALUES (%s) RETURNING id", (command,)
        ).fetchone()
        self.conn.commit()
        return row[0]

    def finish_run(self, run_id: int, status: str, detail: dict) -> None:
        self.conn.execute(
            "UPDATE archive.sync_run SET finished_at = now(), status = %s, detail = %s WHERE id = %s",
            (status, Jsonb(detail), run_id),
        )
        self.conn.commit()

    # --- reporting ------------------------------------------------------

    def summary(self) -> dict:
        c = self.conn
        return {
            "daily": c.execute(
                """
                SELECT source, count(*) FILTER (WHERE payload IS NOT NULL), min(day), max(day)
                  FROM archive.daily GROUP BY source ORDER BY source
                """
            ).fetchall(),
            "activities": c.execute(
                """
                SELECT count(*), count(details), count(*) FILTER (WHERE file_status = 'ok'),
                       min(start_time)::date, max(start_time)::date
                  FROM archive.activity
                """
            ).fetchone(),
            "files_bytes": c.execute(
                "SELECT coalesce(sum(length(content)), 0) FROM archive.activity_file"
            ).fetchone()[0],
            "runs": c.execute(
                """
                SELECT id, command, started_at, finished_at, status, detail
                  FROM archive.sync_run ORDER BY id DESC LIMIT 5
                """
            ).fetchall(),
        }
