"""Tests that need no database."""

from datetime import date

import pytest
from conftest import GarminConnectAuthenticationError, GarminConnectTooManyRequestsError, fit_zip

from garmin_archive.cli import build_parser, load_env_file
from garmin_archive.client import AuthExpired, Caller, RateLimited, extract_files, is_not_found
from garmin_archive.sources import DAILY_SOURCES, select_daily_sources
from garmin_archive.store import is_empty
from garmin_archive.sync import days_back


def test_is_empty():
    assert is_empty(None) and is_empty({}) and is_empty([]) and is_empty("")
    assert not is_empty({"a": None}) and not is_empty([0]) and not is_empty(0)


def test_days_back_is_inclusive_and_newest_first():
    assert days_back(date(2026, 3, 2), date(2026, 2, 27)) == [
        date(2026, 3, 2), date(2026, 3, 1), date(2026, 2, 28), date(2026, 2, 27)
    ]
    assert days_back(date(2026, 1, 1), date(2026, 1, 2)) == []


def test_select_sources():
    assert select_daily_sources(None) == DAILY_SOURCES
    assert list(select_daily_sources("sleep,hrv")) == ["sleep", "hrv"]
    assert "menstrual" in select_daily_sources("default,menstrual")
    with pytest.raises(ValueError, match="unknown source 'nope'"):
        select_daily_sources("sleep,nope")


def test_extract_files_from_zip_and_bare_fit():
    assert extract_files(fit_zip("a.fit", b"x")) == {"a.fit": b"x"}
    bare = b"\x0e\x10\x00\x00\x00\x00\x00\x00.FIT" + b"rest"
    assert extract_files(bare) == {"activity.fit": bare}
    assert extract_files(b"<html>") == {"activity.bin": b"<html>"}
    assert extract_files(b"") == {}


def test_is_not_found():
    from conftest import not_found
    assert is_not_found(not_found())
    assert not is_not_found(Exception("API client error (400): bad"))


def test_caller_spaces_requests():
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    caller = Caller(delay=2.0, sleep=sleep, clock=lambda: now[0])
    caller(lambda: 1)
    now[0] += 0.5
    caller(lambda: 2)
    assert slept == [1.5]


def test_caller_retries_after_rate_limit_then_gives_up():
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise GarminConnectTooManyRequestsError("429")
        return "ok"

    slept = []
    caller = Caller(delay=0, backoff=(10, 20, 30), sleep=slept.append)
    assert caller(flaky) == "ok"
    assert slept == [10, 20]

    caller = Caller(delay=0, backoff=(1,), sleep=lambda s: None)
    with pytest.raises(RateLimited):
        caller(lambda: (_ for _ in ()).throw(GarminConnectTooManyRequestsError("429")))


def test_caller_turns_401_into_auth_expired():
    caller = Caller(delay=0)
    with pytest.raises(AuthExpired):
        caller(lambda: (_ for _ in ()).throw(GarminConnectAuthenticationError("401")))


def test_load_env_file_does_not_override(tmp_path, monkeypatch):
    f = tmp_path / "env"
    f.write_text("# comment\nDATABASE_URL='postgresql://u:p@h/db'\nexport GARMIN_ARCHIVE_DELAY=0.5\nKEEP=file\n")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("GARMIN_ARCHIVE_DELAY", raising=False)
    monkeypatch.setenv("KEEP", "env")
    load_env_file(str(f))
    import os
    assert os.environ["DATABASE_URL"] == "postgresql://u:p@h/db"
    assert os.environ["GARMIN_ARCHIVE_DELAY"] == "0.5"
    assert os.environ["KEEP"] == "env"
    load_env_file(str(tmp_path / "missing"))


def test_parser_requires_since_for_backfill():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["backfill"])
    args = build_parser().parse_args(["backfill", "--since", "2020-01-31"])
    assert args.since == date(2020, 1, 31)


def test_every_source_calls_a_real_library_method():
    from garminconnect import Garmin

    from garmin_archive.sources import ALL_DAILY_SOURCES, SNAPSHOT_SOURCES

    class Recorder:
        def __init__(self):
            self.names = []

        def __getattr__(self, name):
            self.names.append(name)
            return lambda *a, **k: {"userProfileNumber": 1}

    r = Recorder()
    for fetch in ALL_DAILY_SOURCES.values():
        fetch(r, "2026-01-01")
    for fetch in SNAPSHOT_SOURCES.values():
        fetch(r)
    missing = [n for n in r.names if not hasattr(Garmin, n)]
    assert r.names and missing == []
