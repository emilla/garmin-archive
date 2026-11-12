"""Command line: login, sync, backfill, status."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta

from . import client as gc
from .sources import select_daily_sources
from .store import Store
from .sync import Stats, archive_activities, archive_day, archive_snapshots, days_back

log = logging.getLogger("garmin_archive")

DEFAULT_ENV_FILE = "/etc/garmin-archive.env"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_AUTH = 2
EXIT_INTERRUPTED = 130


def load_env_file(path: str) -> None:
    """Read KEY=VALUE lines into the environment without overriding what is set."""
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except (FileNotFoundError, PermissionError):
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def open_store() -> Store:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit(
            f"DATABASE_URL is not set. Put it in {DEFAULT_ENV_FILE} or the environment."
        )
    store = Store.connect(url)
    store.apply_schema()
    return store


def parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got '{value}'")


def run_logged(store: Store, command: str, body) -> int:
    """Run body(stats) and record the outcome in archive.sync_run."""
    stats = Stats()
    run_id = store.start_run(command)
    status, code = "failed", EXIT_FAILED
    try:
        body(stats)
        status, code = ("partial" if stats.errors else "ok"), EXIT_OK
    except gc.AuthExpired as e:
        stats.errors.append(f"login expired: {e}")
        log.error("Garmin login is missing or expired. Run `garmin-archive login`.")
        code = EXIT_AUTH
    except gc.RateLimited as e:
        stats.errors.append(str(e))
        log.error("%s. The next run continues where this one stopped.", e)
    except KeyboardInterrupt:
        status, code = "interrupted", EXIT_INTERRUPTED
        log.warning("Interrupted. Run the same command again to continue.")
    except Exception as e:
        stats.errors.append(f"{type(e).__name__}: {e}")
        log.exception("run failed")
    finally:
        store.conn.rollback()
        store.finish_run(run_id, status, stats.as_dict())
    detail = stats.as_dict()
    log.info(
        "%s %s: %d saved, %d empty, %d already stored, %d activities listed, "
        "%d activity details, %d activity files, %d errors",
        command, status, detail["saved"], detail["empty"], detail["skipped"],
        detail["activities_listed"], detail["activity_details"], detail["activity_files"],
        detail["error_count"],
    )
    return code


def cmd_login(args) -> int:
    path = gc.token_dir()
    name = gc.interactive_login(path)
    print(f"Logged in as {name}. Tokens saved to {path}")
    return EXIT_OK


def cmd_sync(args) -> int:
    sources = select_daily_sources(args.sources)
    store = open_store()
    caller = gc.Caller(delay=args.delay)

    def body(stats: Stats) -> None:
        garmin = gc.load_client(gc.token_dir())
        today = date.today()
        oldest = today - timedelta(days=args.days - 1)
        archive_activities(garmin, caller, store, oldest, stats, backlog_limit=args.backlog)
        archive_snapshots(garmin, caller, store, stats)
        for day in days_back(today, oldest):
            archive_day(garmin, caller, store, day, sources, stats, skip_existing=False)

    try:
        return run_logged(store, "sync", body)
    finally:
        store.close()


def cmd_backfill(args) -> int:
    sources = select_daily_sources(args.sources)
    until = args.until or date.today() - timedelta(days=1)
    if args.since > until:
        raise SystemExit("--since must be on or before --until")
    store = open_store()
    caller = gc.Caller(delay=args.delay)

    def body(stats: Stats) -> None:
        garmin = gc.load_client(gc.token_dir())
        log.info("Listing activities since %s", args.since)
        archive_activities(garmin, caller, store, args.since, stats, backlog_limit=1_000_000)
        days = days_back(until, args.since)
        log.info("Fetching %d days x %d sources, newest first", len(days), len(sources))
        for i, day in enumerate(days, 1):
            before = (stats.saved, stats.empty, len(stats.errors))
            archive_day(garmin, caller, store, day, sources, stats, skip_existing=not args.refetch)
            log.info(
                "%s (%d/%d): %d saved, %d empty, %d errors",
                day, i, len(days), stats.saved - before[0], stats.empty - before[1],
                len(stats.errors) - before[2],
            )

    try:
        return run_logged(store, f"backfill {args.since}..{until}", body)
    finally:
        store.close()


def cmd_status(args) -> int:
    store = open_store()
    try:
        s = store.summary()
    finally:
        store.close()
    print("Daily data (source: days with data, first day, last day)")
    for source, count, first, last in s["daily"]:
        print(f"  {source:22} {count:6}  {first}  {last}")
    total, details, files, first, last = s["activities"]
    print(f"\nActivities: {total} ({details} with details, {files} with files), {first} to {last}")
    print(f"Activity files: {s['files_bytes'] / 1_000_000:.1f} MB")
    print("\nRecent runs")
    for run_id, command, started, finished, status, detail in s["runs"]:
        errors = (detail or {}).get("error_count", 0)
        print(f"  #{run_id} {started:%Y-%m-%d %H:%M} {status:11} {command} ({errors} errors)")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="garmin-archive", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true", help="show library debug output")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="log in to Garmin Connect (asks for email, password, MFA)")

    def add_fetch_options(sp):
        sp.add_argument(
            "--delay", type=float,
            default=float(os.environ.get("GARMIN_ARCHIVE_DELAY", "1.0")),
            help="seconds between Garmin requests (default 1.0)",
        )
        sp.add_argument(
            "--sources", default=os.environ.get("GARMIN_ARCHIVE_SOURCES"),
            help="comma-separated daily sources; 'default' means the standard set",
        )

    sp = sub.add_parser("sync", help="refresh recent days, activities and account data")
    sp.add_argument("--days", type=int, default=7, help="days to refresh, counting today (default 7)")
    sp.add_argument(
        "--backlog", type=int, default=200,
        help="max activities to fill in with details and files per run (default 200)",
    )
    add_fetch_options(sp)

    bp = sub.add_parser("backfill", help="load history; safe to stop and rerun")
    bp.add_argument("--since", type=parse_day, required=True, help="first day, YYYY-MM-DD")
    bp.add_argument("--until", type=parse_day, help="last day, YYYY-MM-DD (default yesterday)")
    bp.add_argument("--refetch", action="store_true", help="fetch days that are already stored")
    add_fetch_options(bp)

    sub.add_parser("status", help="show what is in the archive and recent runs")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    if not args.verbose:
        # The library logs a full traceback for every failed request, including
        # expected 404s. Our own messages cover what matters.
        logging.getLogger("garminconnect").setLevel(logging.CRITICAL)
    load_env_file(os.environ.get("GARMIN_ARCHIVE_ENV_FILE", DEFAULT_ENV_FILE))
    handlers = {
        "login": cmd_login,
        "sync": cmd_sync,
        "backfill": cmd_backfill,
        "status": cmd_status,
    }
    try:
        return handlers[args.command](args)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
