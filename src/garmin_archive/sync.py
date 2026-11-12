"""Fetch data from Garmin and write it to the archive."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable

from garminconnect import Garmin

from .client import (
    Caller,
    GarminConnectConnectionError,
    extract_files,
    is_not_found,
)
from .sources import SNAPSHOT_SOURCES, DailyFetch
from .store import Store, is_empty

log = logging.getLogger(__name__)

ACTIVITY_PAGE_SIZE = 100


@dataclass
class Stats:
    saved: int = 0
    empty: int = 0
    skipped: int = 0
    activities_listed: int = 0
    activity_details: int = 0
    activity_files: int = 0
    errors: list[str] = field(default_factory=list)

    def error(self, message: str) -> None:
        log.warning(message)
        self.errors.append(message)

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "errors"}
        d["error_count"] = len(self.errors)
        d["errors"] = self.errors[:50]
        return d


def _fetch(caller: Caller, fn: Callable[[], Any], what: str, stats: Stats) -> tuple[bool, Any]:
    """Returns (ok, payload). A 404 counts as success with no data."""
    try:
        return True, caller(fn)
    except GarminConnectConnectionError as e:
        if is_not_found(e):
            return True, None
        stats.error(f"{what}: {e}")
        return False, None


def archive_day(
    garmin: Garmin,
    caller: Caller,
    store: Store,
    day: date,
    sources: dict[str, DailyFetch],
    stats: Stats,
    skip_existing: bool,
) -> None:
    existing = store.stored_sources(day) if skip_existing else set()
    for name, fetch in sources.items():
        if name in existing:
            stats.skipped += 1
            continue
        ok, payload = _fetch(caller, lambda: fetch(garmin, day.isoformat()), f"{name} {day}", stats)
        if not ok:
            continue
        store.save_daily(name, day, payload)
        if is_empty(payload):
            stats.empty += 1
        else:
            stats.saved += 1
    store.commit()


def archive_snapshots(garmin: Garmin, caller: Caller, store: Store, stats: Stats) -> None:
    for name, fetch in SNAPSHOT_SOURCES.items():
        ok, payload = _fetch(caller, lambda: fetch(garmin), name, stats)
        if ok and store.save_snapshot(name, payload):
            stats.saved += 1
    store.commit()


def list_activities(garmin: Garmin, caller: Caller, since: date) -> list[dict]:
    """All activities starting on or after `since`, newest first."""
    found: list[dict] = []
    offset = 0
    while True:
        page = caller(lambda: garmin.get_activities(offset, ACTIVITY_PAGE_SIZE)) or []
        if not isinstance(page, list):
            page = page.get("activityList", []) if isinstance(page, dict) else []
        for activity in page:
            start = str(activity.get("startTimeLocal") or activity.get("startTimeGMT") or "")
            if start[:10] < since.isoformat():
                return found
            found.append(activity)
        if len(page) < ACTIVITY_PAGE_SIZE:
            return found
        offset += ACTIVITY_PAGE_SIZE


def archive_activities(
    garmin: Garmin,
    caller: Caller,
    store: Store,
    since: date,
    stats: Stats,
    backlog_limit: int,
) -> None:
    """Store summaries for recent activities, then fill in details and files."""
    for summary in list_activities(garmin, caller, since):
        store.save_activity_summary(summary)
        stats.activities_listed += 1
    store.commit()

    for activity_id, needs_details, needs_file, manual in store.activities_needing_work(backlog_limit):
        if needs_details:
            ok, details = _fetch(
                caller, lambda: garmin.get_activity(str(activity_id)), f"activity {activity_id}", stats
            )
            if ok:
                store.save_activity_details(activity_id, details)
                stats.activity_details += 1
        if needs_file:
            if manual:
                store.set_file_status(activity_id, "none")
            else:
                ok, download = _fetch(
                    caller,
                    lambda: garmin.download_activity(
                        str(activity_id), dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL
                    ),
                    f"activity file {activity_id}",
                    stats,
                )
                if ok:
                    files = extract_files(download) if download else {}
                    store.save_activity_files(activity_id, files)
                    if files:
                        stats.activity_files += 1
        store.commit()


def days_back(newest: date, oldest: date) -> list[date]:
    """Days from newest down to oldest, inclusive."""
    count = (newest - oldest).days + 1
    return [newest - timedelta(days=i) for i in range(max(count, 0))]
