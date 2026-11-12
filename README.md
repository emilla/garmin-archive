# garmin-archive

Copies your Garmin Connect data into a PostgreSQL database every night, so you
keep it if the Garmin account is lost or Garmin stops serving it.

It uses the unofficial Garmin Connect API through the
[`garminconnect`](https://github.com/cyberjunky/python-garminconnect) library,
the same one the Garmin MCP server uses.

## What it stores

Everything goes into the `archive` schema, as the raw JSON Garmin returns.

| Table | Contents |
| --- | --- |
| `archive.daily` | One row per source per day: daily summary, heart rate, sleep, stress and Body Battery, HRV, respiration, SpO2, steps, floors, intensity minutes, training readiness and status, VO2 max, hydration, weigh-ins, blood pressure, food log. |
| `archive.activity` | One row per activity: the list summary and the full activity details. |
| `archive.activity_file` | The original file for each activity (usually `.fit`), as uploaded by the device. |
| `archive.snapshot` | Profile, settings, devices, gear, personal records, goals and badges. A new row is added only when something changes. |
| `archive.sync_run` | One row per run, with its status and any errors. |

A day with no data is stored with a `NULL` payload, so a backfill does not ask
for it again. An empty response from Garmin never replaces data that is already
stored.

Two more daily sources are available but off by default because they return
nothing unless the feature is used: `menstrual` and `lifestyle_logging`. Turn
them on with `GARMIN_ARCHIVE_SOURCES=default,menstrual` in the env file.

## Setup on the server

These steps assume a Debian or Ubuntu LXC container, logged in as root, that
can reach the database and the internet.

1. Install `git` and `uv`:

   ```
   apt-get install -y git curl
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

   Open a new shell afterwards so `uv` is on the PATH.

2. Install the collector:

   ```
   git clone https://github.com/emilla/garmin-archive /opt/garmin-archive
   cd /opt/garmin-archive
   uv sync --frozen --no-dev
   ln -s /opt/garmin-archive/.venv/bin/garmin-archive /usr/local/bin/garmin-archive
   ```

3. Create `/etc/garmin-archive.env` with the database address, readable only
   by root. Do not paste the address anywhere else, since it contains the
   password.

   ```
   echo "DATABASE_URL=$(sys db url garmin)" > /etc/garmin-archive.env
   chmod 600 /etc/garmin-archive.env
   ```

   If `sys` only runs on another machine, run it there and copy the line over.
   The commands below read this file on their own.

4. Log in to Garmin. It asks for your email, password and MFA code, and saves
   tokens to `/root/.garmin-archive/tokens`.

   ```
   garmin-archive login
   ```

   This login is separate from the one the MCP server on Railway uses. Do not
   copy tokens between them: Garmin replaces the refresh token each time it is
   used, so two services sharing one set of tokens log each other out.

5. Check that it works by archiving the last two days:

   ```
   garmin-archive sync --days 2
   garmin-archive status
   ```

6. Load your history. Pick a `--since` date on or before your first Garmin
   device. This takes hours, so run it as a background unit that survives
   logging out:

   ```
   systemd-run --unit=garmin-backfill /opt/garmin-archive/.venv/bin/garmin-archive backfill --since 2015-01-01
   journalctl -u garmin-backfill -f
   ```

   It fetches activities first, then days from newest to oldest. If it stops
   for any reason, run the same command again. It skips what is already
   stored.

7. When the backfill has finished, turn on the nightly sync:

   ```
   cp /opt/garmin-archive/deploy/garmin-archive.service /opt/garmin-archive/deploy/garmin-archive.timer /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now garmin-archive.timer
   ```

   Waiting avoids two jobs querying Garmin at once, which makes rate limiting
   more likely.

## Commands

| Command | What it does |
| --- | --- |
| `garmin-archive login` | Log in to Garmin and save tokens. |
| `garmin-archive sync` | Refresh the last 7 days (`--days`), new activities, and account data. Also fills in up to 200 activities still missing details or files (`--backlog`). |
| `garmin-archive backfill --since DATE` | Load history from DATE to yesterday (`--until`). Skips stored days unless `--refetch`. |
| `garmin-archive status` | Show what is stored and the last five runs. |

`sync` and `backfill` accept `--sources` to limit the daily sources and
`--delay` to set the gap between requests (default 1 second, also settable as
`GARMIN_ARCHIVE_DELAY`).

Exit codes: 0 when the run finished (status `ok`, or `partial` if some
requests failed; the next sync or a rerun of the backfill retries them), 1
when it stopped early, 2 when the Garmin login is missing or expired, 130 when
interrupted.

## When something goes wrong

- **Login expired.** The run fails with exit code 2 and the journal says to
  log in. Run `garmin-archive login` again. Expect this roughly every six
  months, or after a Garmin password change.
- **Rate limited.** The collector waits and retries, then stops. The next run
  continues where it stopped.
- **Checking the nightly run:** `systemctl status garmin-archive` and
  `journalctl -u garmin-archive`. The `archive.sync_run` table has the same
  information for a Grafana alert.

## Updating

```
cd /opt/garmin-archive && git pull && uv sync --frozen --no-dev
```

The schema is applied at the start of every run, so new tables appear on
their own.

## Tests

The database tests need an empty PostgreSQL database that the tests can wipe.
Never point them at the real archive.

```
TEST_DATABASE_URL=postgresql://user:pass@localhost/garmin_test uv run pytest
```

Without `TEST_DATABASE_URL`, only the tests that need no database run.
