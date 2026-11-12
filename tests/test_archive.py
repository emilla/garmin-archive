"""Tests against a real PostgreSQL database (TEST_DATABASE_URL)."""

from datetime import date

from conftest import FakeGarmin, GarminConnectAuthenticationError, fit_zip, not_found

import pytest
from garmin_archive import cli
from garmin_archive.client import AuthExpired
from garmin_archive.sync import Stats, archive_activities, archive_day, archive_snapshots

DAY = date(2026, 11, 10)
D = DAY.isoformat()


def rows(store, sql, *params):
    return store.conn.execute(sql, params).fetchall()


def test_schema_is_idempotent(store):
    store.apply_schema()
    store.apply_schema()


def test_empty_response_never_replaces_stored_data(store):
    store.save_daily("sleep", DAY, {"score": 80})
    store.save_daily("sleep", DAY, None)
    store.save_daily("sleep", DAY, {})
    assert rows(store, "SELECT payload FROM archive.daily") == [({"score": 80},)]
    store.save_daily("sleep", DAY, {"score": 82})
    assert rows(store, "SELECT payload FROM archive.daily") == [({"score": 82},)]


def test_empty_day_is_recorded_so_backfill_skips_it(store):
    store.save_daily("hrv", DAY, None)
    assert rows(store, "SELECT payload FROM archive.daily") == [(None,)]
    assert store.stored_sources(DAY) == {"hrv"}


def test_snapshot_adds_row_only_on_change(store):
    assert store.save_snapshot("devices", [{"id": 1}])
    assert not store.save_snapshot("devices", [{"id": 1}])
    assert store.save_snapshot("devices", [{"id": 1}, {"id": 2}])
    assert not store.save_snapshot("devices", [])
    assert rows(store, "SELECT count(*) FROM archive.snapshot") == [(2,)]


def test_archive_day_saves_each_source_and_continues_after_errors(store, caller):
    garmin = FakeGarmin(daily={
        "get_sleep_data": {D: {"dailySleepDTO": {"sleepTimeSeconds": 28000}}},
        "get_hrv_data": {D: not_found()},
    })
    sources = {
        "sleep": lambda g, d: g.get_sleep_data(d),
        "hrv": lambda g, d: g.get_hrv_data(d),
        "stress": lambda g, d: _raise_connection_error(),
        "steps": lambda g, d: g.get_steps_data(d),
    }
    stats = Stats()
    archive_day(garmin, caller, store, DAY, sources, stats, skip_existing=False)
    stored = dict(rows(store, "SELECT source, payload FROM archive.daily"))
    assert stored == {
        "sleep": {"dailySleepDTO": {"sleepTimeSeconds": 28000}},
        "hrv": None,
        "steps": None,
    }
    assert stats.saved == 1 and stats.empty == 2 and len(stats.errors) == 1


def _raise_connection_error():
    from garminconnect import GarminConnectConnectionError
    raise GarminConnectConnectionError("HTTP error: 500")


def test_archive_day_skips_existing_when_asked(store, caller):
    store.save_daily("sleep", DAY, {"x": 1})
    store.commit()
    garmin = FakeGarmin()
    sources = {"sleep": lambda g, d: g.get_sleep_data(d), "hrv": lambda g, d: g.get_hrv_data(d)}
    stats = Stats()
    archive_day(garmin, caller, store, DAY, sources, stats, skip_existing=True)
    assert garmin.calls == [("get_hrv_data", D)]
    assert stats.skipped == 1


def activity(i, start, manual=False):
    return {
        "activityId": i,
        "startTimeLocal": f"{start} 07:00:00",
        "startTimeGMT": f"{start} 06:00:00",
        "activityType": {"typeKey": "running"},
        "manualActivity": manual,
    }


def test_archive_activities_stores_summary_details_and_file(store, caller):
    garmin = FakeGarmin(
        activities=[activity(3, "2026-11-10"), activity(2, "2026-11-09", manual=True),
                    activity(1, "2026-10-01")],
        details={"3": {"summaryDTO": {"distance": 5000}}, "2": {"summaryDTO": {}}},
        downloads={"3": fit_zip("3_ACTIVITY.fit", b"FITBYTES")},
    )
    stats = Stats()
    archive_activities(garmin, caller, store, date(2026, 11, 1), stats, backlog_limit=100)

    acts = rows(store, "SELECT activity_id, activity_type, file_status, start_time::text "
                       "FROM archive.activity ORDER BY activity_id")
    assert acts == [(2, "running", "none", "2026-11-09 06:00:00+00"),
                    (3, "running", "ok", "2026-11-10 06:00:00+00")]
    assert rows(store, "SELECT activity_id, filename, content FROM archive.activity_file") == [
        (3, "3_ACTIVITY.fit", b"FITBYTES")]
    assert ("download_activity", "2") not in garmin.calls
    assert stats.activities_listed == 2 and stats.activity_files == 1


def test_activity_file_404_marks_none_and_errors_retry_next_run(store, caller):
    garmin = FakeGarmin(
        activities=[activity(5, "2026-11-10"), activity(6, "2026-11-10")],
        details={"5": {"a": 1}, "6": {"a": 1}},
        downloads={"5": not_found(), "6": _connection_error_500()},
    )
    archive_activities(garmin, caller, store, date(2026, 11, 1), Stats(), backlog_limit=100)
    status = dict(rows(store, "SELECT activity_id, file_status FROM archive.activity"))
    assert status == {5: "none", 6: None}

    garmin.downloads["6"] = fit_zip()
    archive_activities(garmin, caller, store, date(2026, 11, 1), Stats(), backlog_limit=100)
    status = dict(rows(store, "SELECT activity_id, file_status FROM archive.activity"))
    assert status == {5: "none", 6: "ok"}


def _connection_error_500():
    from garminconnect import GarminConnectConnectionError
    return GarminConnectConnectionError("Download error: 500")


def test_activity_listing_pages_until_older_than_since(store, caller, monkeypatch):
    import garmin_archive.sync as sync_mod
    monkeypatch.setattr(sync_mod, "ACTIVITY_PAGE_SIZE", 2)
    acts = [activity(10 - i, f"2026-11-{10 - i:02d}") for i in range(6)]
    garmin = FakeGarmin(activities=acts)
    found = sync_mod.list_activities(garmin, caller, date(2026, 11, 7))
    assert [a["activityId"] for a in found] == [10, 9, 8, 7]
    assert [c for c in garmin.calls if c[0] == "get_activities"] == [
        ("get_activities", 0), ("get_activities", 2), ("get_activities", 4)]


def test_snapshots(store, caller):
    garmin = FakeGarmin(daily={"get_devices": {None: [{"deviceId": 1}]}})
    archive_snapshots(garmin, caller, store, Stats())
    assert rows(store, "SELECT source, payload FROM archive.snapshot") == [
        ("devices", [{"deviceId": 1}])]


def test_run_logged_records_auth_failure(store):
    def body(stats):
        raise AuthExpired("401")
    code = cli.run_logged(store, "sync", body)
    assert code == cli.EXIT_AUTH
    (status, detail), = rows(store, "SELECT status, detail FROM archive.sync_run")
    assert status == "failed" and "login expired" in detail["errors"][0]


def test_run_logged_marks_partial_when_sources_failed(store):
    def body(stats):
        stats.error("sleep 2026-11-10: HTTP error: 500")
    assert cli.run_logged(store, "sync", body) == cli.EXIT_OK
    assert rows(store, "SELECT status FROM archive.sync_run") == [("partial",)]


def test_cli_sync_then_backfill_end_to_end(store, monkeypatch):
    from conftest import TEST_DATABASE_URL
    from garmin_archive.sources import DAILY_SOURCES

    garmin = FakeGarmin(
        daily={"get_sleep_data": {date.today().isoformat(): {"score": 75}}},
        activities=[activity(1, date.today().isoformat())],
        details={"1": {"summaryDTO": {}}},
        downloads={"1": fit_zip()},
    )
    monkeypatch.setattr(cli.gc, "load_client", lambda path: garmin)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("GARMIN_ARCHIVE_ENV_FILE", "/nonexistent")

    assert cli.main(["sync", "--days", "2", "--delay", "0"]) == cli.EXIT_OK
    assert rows(store, "SELECT count(*) FROM archive.daily") == [(2 * len(DAILY_SOURCES),)]
    assert rows(store, "SELECT file_status FROM archive.activity") == [("ok",)]

    garmin.calls.clear()
    since = date.fromordinal(date.today().toordinal() - 3).isoformat()
    assert cli.main(["backfill", "--since", since, "--delay", "0", "--sources", "sleep,hrv"]) == 0
    day_calls = [c for c in garmin.calls if c[0] in ("get_sleep_data", "get_hrv_data")]
    # Yesterday was stored by sync, so only the two older days are fetched.
    assert len(day_calls) == 4
    assert rows(store, "SELECT status FROM archive.sync_run ORDER BY id") == [("ok",), ("ok",)]
    assert cli.main(["status"]) == 0


def test_cli_reports_unknown_source(monkeypatch, capsys):
    monkeypatch.setenv("GARMIN_ARCHIVE_ENV_FILE", "/nonexistent")
    assert cli.main(["sync", "--sources", "nope"]) == cli.EXIT_FAILED
    assert "unknown source 'nope'" in capsys.readouterr().err
