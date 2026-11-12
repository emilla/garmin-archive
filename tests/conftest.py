import io
import os
import zipfile

import psycopg
import pytest
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from garmin_archive.client import Caller
from garmin_archive.store import Store

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def store():
    """A Store on an emptied test database. Never point this at real data."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    conn = psycopg.connect(TEST_DATABASE_URL)
    conn.execute("DROP SCHEMA IF EXISTS archive CASCADE")
    conn.commit()
    s = Store(conn)
    s.apply_schema()
    yield s
    conn.close()


@pytest.fixture
def caller():
    return Caller(delay=0, backoff=(0, 0), sleep=lambda s: None)


def fit_zip(name="12345_ACTIVITY.fit", content=b"\x0e\x10\x00\x00\x00\x00\x00\x00.FITdata"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, content)
    return buf.getvalue()


def not_found():
    return GarminConnectConnectionError("API client error (404): Not Found")


class FakeGarmin:
    """Stands in for garminconnect.Garmin with canned responses.

    daily maps (method name) -> {date string: payload or exception}.
    """

    ActivityDownloadFormat = type("F", (), {"ORIGINAL": "original"})

    def __init__(self, daily=None, activities=None, details=None, downloads=None):
        self.daily = daily or {}
        self.activities = activities or []
        self.details = details or {}
        self.downloads = downloads or {}
        self.calls = []

    def _day(self, method, d):
        self.calls.append((method, d))
        value = self.daily.get(method, {}).get(d)
        if isinstance(value, Exception):
            raise value
        return value

    def __getattr__(self, name):
        if name.startswith("get_"):
            def method(*args, **kwargs):
                d = args[0] if args else None
                return self._day(name, d)
            return method
        raise AttributeError(name)

    def get_activities(self, start, limit):
        self.calls.append(("get_activities", start))
        return self.activities[start:start + limit]

    def get_activity(self, activity_id):
        self.calls.append(("get_activity", activity_id))
        value = self.details.get(activity_id)
        if isinstance(value, Exception):
            raise value
        return value

    def download_activity(self, activity_id, dl_fmt=None):
        self.calls.append(("download_activity", activity_id))
        value = self.downloads.get(activity_id)
        if isinstance(value, Exception):
            raise value
        return value


__all__ = [
    "FakeGarmin",
    "GarminConnectAuthenticationError",
    "GarminConnectTooManyRequestsError",
    "fit_zip",
    "not_found",
]
