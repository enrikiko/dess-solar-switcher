# DessMonitor solar-priority scheduler

This Docker Compose service schedules two DessMonitor `ctrlDevice` calls each
day, using the configured site location:

| Time | Output Source Priority | Device value |
| --- | --- | --- |
| 4 hours after sunrise | Solar Bat Utility | `12338` |
| 3 hours before sunset | Utility Solar Bat | `12336` |

Before every change it calls `queryDeviceCtrlValue` and saves both responses in
`data/schedule.db`.  It retries failed HTTP/API calls up to five times with
exponential backoff (configurable in `.env`). A completed change is never
replayed after a restart. If it was due while the service was offline, it runs
once on the next startup/poll.

## Run

1. Copy `.env.example` to `.env` and set the location, DessMonitor account,
   company key, and device identifiers. Keep `.env` private.
2. Start the service:

   ```sh
   docker compose up -d --build
   ```

3. Watch it:

   ```sh
   docker compose logs -f
   ```

The service requests a fresh API token and secret using `authSource` from the
configured username/password whenever needed. `DESS_COMPANY_KEY` is required by
the published DessMonitor authentication API; obtain it from DessMonitor if you
do not have one.

The app re-signs every request with the current timestamp, secret and token.
Do not paste an old copied control URL into the configuration: its signature
will not remain valid.

## Inspect persistent state

```sh
sqlite3 data/schedule.db 'SELECT local_date, kind, scheduled_at, state, attempts, last_error FROM jobs ORDER BY scheduled_at;'
```

To force a failed job to retry after correcting the underlying issue, update
its `state` to `pending` and set `next_attempt_at` to `NULL`. Successful jobs
are intentionally idempotent in the scheduler state.
