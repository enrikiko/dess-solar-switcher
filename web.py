"""Persistent, local dashboard for the DessMonitor solar switch scheduler."""

from __future__ import annotations

import html
import math
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from astral import LocationInfo
from astral.sun import sun

DATABASE = Path("/data/schedule.db")
FUTURE_DAYS = 30


def env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None or not value.strip():
        raise ValueError(f"{name} must be set")
    return value.strip()


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self) -> None:
        DATABASE.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(DATABASE, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            # app.py creates this table too. This keeps startup order irrelevant.
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                  id INTEGER PRIMARY KEY, local_date TEXT NOT NULL, kind TEXT NOT NULL,
                  scheduled_at TEXT NOT NULL, desired_value TEXT NOT NULL,
                  state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                  next_attempt_at TEXT, last_error TEXT, last_response TEXT, completed_at TEXT,
                  UNIQUE(local_date, kind)
                )
            """)
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
                )
            """)
            self.db.commit()

    def offsets(self) -> tuple[float, float]:
        defaults = (float(env("AFTER_SUNRISE_HOURS", "5")), float(env("BEFORE_SUNSET_HOURS", "3")))
        with self.lock:
            saved = dict(self.db.execute(
                "SELECT key, value FROM settings WHERE key IN (?, ?)",
                ("after_sunrise_hours", "before_sunset_hours"),
            ))
        return (
            float(saved.get("after_sunrise_hours", defaults[0])),
            float(saved.get("before_sunset_hours", defaults[1])),
        )

    def update_offsets(self, after: float, before: float) -> None:
        now = iso(datetime.now(timezone.utc))
        with self.lock:
            self.db.executemany("""
                INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """, [
                ("after_sunrise_hours", str(after), now),
                ("before_sunset_hours", str(before), now),
            ])
            self.db.commit()

    def put_job(self, day: str, kind: str, when: datetime, desired: str) -> None:
        with self.lock:
            self.db.execute("""
                INSERT INTO jobs (local_date, kind, scheduled_at, desired_value)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(local_date, kind) DO UPDATE SET
                  scheduled_at=excluded.scheduled_at, desired_value=excluded.desired_value
                WHERE jobs.state='pending'
            """, (day, kind, iso(when), desired))
            self.db.commit()

    def rows(self, future: bool) -> list[sqlite3.Row]:
        now = iso(datetime.now(timezone.utc))
        operator, direction = (">=", "ASC") if future else ("<", "DESC")
        with self.lock:
            return list(self.db.execute(
                f"SELECT * FROM jobs WHERE scheduled_at {operator} ? ORDER BY scheduled_at {direction} LIMIT ?",
                (now, FUTURE_DAYS * 2 if future else -1),
            ))


def reschedule(store: Store, timezone_name: str) -> None:
    timezone_info = ZoneInfo(timezone_name)
    location = LocationInfo(
        "inverter", "", timezone_name, float(env("LATITUDE")), float(env("LONGITUDE"))
    )
    after, before = store.offsets()
    solar_value = env("DAYLIGHT_VALUE", "12336")
    utility_value = env("NIGHT_VALUE", "12338")
    today = datetime.now(timezone.utc).astimezone(timezone_info).date()
    for day_offset in range(FUTURE_DAYS):
        local_day = today + timedelta(days=day_offset)
        times = sun(location.observer, date=local_day, tzinfo=timezone_info)
        store.put_job(
            local_day.isoformat(), "daylight",
            times["sunrise"] + timedelta(hours=after), solar_value,
        )
        store.put_job(
            local_day.isoformat(), "night",
            times["sunset"] - timedelta(hours=before), utility_value,
        )


def switch_text(row: sqlite3.Row, after: float, before: float) -> str:
    names = {"12336": "Utility Solar Bat", "12338": "Solar Bat Utility"}
    priority = names.get(row["desired_value"], row["desired_value"])
    if row["kind"] == "daylight":
        return f"{after:g}h after sunrise -> {priority}"
    if row["kind"] == "night":
        return f"{before:g}h before sunset -> {priority}"
    return f"{row['kind']} -> {priority}"


def render_rows(rows: list[sqlite3.Row], zone: ZoneInfo, after: float, before: float) -> str:
    if not rows:
        return '<tr><td colspan="5">No switches recorded.</td></tr>'
    result = []
    for row in rows:
        time_text = datetime.fromisoformat(row["scheduled_at"]).astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")
        error = html.escape((row["last_error"] or "-")[:180])
        state = html.escape(row["state"])
        result.append(
            "<tr>"
            f"<td>{html.escape(time_text)}</td>"
            f"<td>{html.escape(switch_text(row, after, before))}</td>"
            f'<td class="{state}">{state}</td>'
            f"<td>{row['attempts']}</td><td>{error}</td></tr>"
        )
    return "".join(result)


def page(store: Store, timezone_name: str, message: str = "") -> bytes:
    zone = ZoneInfo(timezone_name)
    after, before = store.offsets()
    notice = f'<p class="notice">{html.escape(message)}</p>' if message else ""
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DessMonitor solar scheduler</title><style>
body{{font-family:system-ui,sans-serif;max-width:1200px;margin:2rem auto;padding:0 1rem;background:#f6f8fa;color:#1f2937}}
section{{background:white;border:1px solid #d8dee5;border-radius:9px;padding:1rem;margin:1rem 0}}
form{{display:flex;gap:1rem;align-items:end;flex-wrap:wrap}}label{{display:grid;gap:.25rem;font-weight:600}}
input{{font:inherit;padding:.4rem;width:8rem}}button{{font:inherit;padding:.5rem .85rem;background:#0969da;color:white;border:0;border-radius:5px}}
table{{width:100%;border-collapse:collapse;font-size:.9rem}}th,td{{padding:.55rem;border-bottom:1px solid #e5e7eb;text-align:left;vertical-align:top}}
.succeeded{{color:#157347;font-weight:600}}.failed{{color:#b42318;font-weight:600}}.pending{{color:#9a6700;font-weight:600}}
.notice{{background:#e9f7ef;padding:.7rem;border-radius:5px}}.sub{{color:#667085}}
</style></head><body>
<h1>DessMonitor solar switches</h1><p class="sub">Times are shown in {html.escape(timezone_name)}. The next {FUTURE_DAYS} days are planned.</p>
{notice}
<section><h2>Schedule settings</h2><form method="post" action="/settings">
<label>Hours after sunrise<input name="after_sunrise_hours" type="number" min="0" max="24" step="0.25" value="{after:g}" required></label>
<label>Hours before sunset<input name="before_sunset_hours" type="number" min="0" max="24" step="0.25" value="{before:g}" required></label>
<button type="submit">Save and reschedule</button></form></section>
<section><h2>Upcoming switches</h2><table><thead><tr><th>Time</th><th>Switch</th><th>Status</th><th>Attempts</th><th>Last error</th></tr></thead>
<tbody>{render_rows(store.rows(True), zone, after, before)}</tbody></table></section>
<section><h2>Past switches</h2><table><thead><tr><th>Time</th><th>Switch</th><th>Status</th><th>Attempts</th><th>Last error</th></tr></thead>
<tbody>{render_rows(store.rows(False), zone, after, before)}</tbody></table></section>
</body></html>"""
    return document.encode("utf-8")


def dashboard_handler(store: Store, timezone_name: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def html_response(self, status: int, content: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/health":
                self.send_response(204)
                self.end_headers()
            elif path == "/":
                self.html_response(200, page(store, timezone_name))
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/settings":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2048:
                    raise ValueError("Invalid form data")
                form = parse_qs(self.rfile.read(length).decode("utf-8"), strict_parsing=True)
                after = float(form["after_sunrise_hours"][0])
                before = float(form["before_sunset_hours"][0])
                if not all(math.isfinite(value) and 0 <= value <= 24 for value in (after, before)):
                    raise ValueError("Each offset must be between 0 and 24")
                store.update_offsets(after, before)
                reschedule(store, timezone_name)
            except (KeyError, UnicodeDecodeError, ValueError) as exc:
                self.html_response(400, page(store, timezone_name, f"Settings were not saved: {exc}"))
                return
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

        def log_message(self, fmt: str, *args: object) -> None:
            print(f"dashboard: {self.address_string()} - {fmt % args}", flush=True)

    return Handler


def refresh_loop(store: Store, timezone_name: str) -> None:
    while True:
        try:
            reschedule(store, timezone_name)
        except Exception as exc:
            print(f"dashboard planner error: {exc}", flush=True)
        threading.Event().wait(900)


def main() -> None:
    timezone_name = env("TIMEZONE")
    store = Store()
    reschedule(store, timezone_name)
    threading.Thread(target=refresh_loop, args=(store, timezone_name), daemon=True).start()
    host, port = env("WEB_HOST", "0.0.0.0"), int(env("WEB_PORT", "8080"))
    print(f"Dashboard listening on http://{host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), dashboard_handler(store, timezone_name)).serve_forever()


if __name__ == "__main__":
    main()
