"""Garmin Connect login and a rate-limited way to call it."""

from __future__ import annotations

import getpass
import io
import logging
import os
import re
import time
import zipfile
from typing import Any, Callable

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

log = logging.getLogger(__name__)

DEFAULT_TOKEN_DIR = "~/.garmin-archive/tokens"


class AuthExpired(Exception):
    """Garmin rejected the saved tokens. Run `garmin-archive login` again."""


class RateLimited(Exception):
    """Garmin kept answering 429 after all retries."""


def token_dir() -> str:
    return os.path.expanduser(os.environ.get("GARMIN_ARCHIVE_TOKENS") or DEFAULT_TOKEN_DIR)


def interactive_login(path: str) -> str:
    """Log in with email, password and MFA, and save tokens to path."""
    email = os.environ.get("GARMIN_EMAIL") or input("Garmin email: ").strip()
    password = os.environ.get("GARMIN_PASSWORD") or getpass.getpass("Garmin password: ")
    garmin = Garmin(email=email, password=password, return_on_mfa=True)
    status, state = garmin.login()
    if status == "needs_mfa":
        garmin.resume_login(state, input("MFA code: ").strip())
    os.makedirs(path, mode=0o700, exist_ok=True)
    garmin.client.dump(path)
    return garmin.get_full_name() or "unknown"


def load_client(path: str) -> Garmin:
    """Log in from saved tokens. Rotated tokens are written back to path."""
    if not os.path.isdir(path):
        raise AuthExpired(f"no saved tokens at {path}")
    garmin = Garmin()
    try:
        garmin.login(path)
    except (GarminConnectAuthenticationError, FileNotFoundError, ValueError) as e:
        raise AuthExpired(str(e)) from e
    return garmin


def is_not_found(error: Exception) -> bool:
    return bool(re.search(r"\(404\)", str(error)))


class Caller:
    """Calls Garmin with a minimum gap between requests and backs off on 429."""

    def __init__(
        self,
        delay: float = 1.0,
        backoff: tuple[float, ...] = (60, 180, 600),
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.delay = delay
        self.backoff = backoff
        self.sleep = sleep
        self.clock = clock
        self._last = float("-inf")
        self.calls = 0

    def __call__(self, fn: Callable[[], Any]) -> Any:
        for attempt in range(len(self.backoff) + 1):
            wait = self._last + self.delay - self.clock()
            if wait > 0:
                self.sleep(wait)
            self._last = self.clock()
            self.calls += 1
            try:
                return fn()
            except GarminConnectAuthenticationError as e:
                raise AuthExpired(str(e)) from e
            except GarminConnectTooManyRequestsError:
                if attempt == len(self.backoff):
                    raise RateLimited("Garmin is rate limiting requests; try again later")
                pause = self.backoff[attempt]
                log.warning("rate limited by Garmin, waiting %.0f s", pause)
                self.sleep(pause)
        raise AssertionError("unreachable")


def extract_files(download: bytes) -> dict[str, bytes]:
    """Unpack an 'original' activity download, which is a zip of the source files."""
    if not download:
        return {}
    try:
        with zipfile.ZipFile(io.BytesIO(download)) as zf:
            return {
                info.filename: zf.read(info)
                for info in zf.infolist()
                if not info.is_dir()
            }
    except zipfile.BadZipFile:
        # Some downloads are a bare FIT file; its header carries ".FIT" at bytes 8-11.
        name = "activity.fit" if download[8:12] == b".FIT" else "activity.bin"
        return {name: download}


__all__ = [
    "AuthExpired",
    "Caller",
    "GarminConnectConnectionError",
    "RateLimited",
    "extract_files",
    "interactive_login",
    "is_not_found",
    "load_client",
    "token_dir",
]
