"""Switch a DessMonitor inverter priority at solar-relative times.

The state database contains scheduled runs, attempt counts and responses so a
container restart cannot silently repeat a successful switch or forget retries.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from astral import LocationInfo
from astral.sun import sun


LOG = logging.getLogger("dess-solar-switcher")
DATABASE = Path("/data/schedule.db")
PRIORITY_ID = "bse_output_source_priority"


def env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None or not value.strip():
        raise ValueError(f"{name} must be set")
    return value.strip()


@dataclass(frozen=True)
class Config:
    latitude: float
    longitude: float
    timezone: ZoneInfo
    username: str
    password: str
    company_key: str
    base_url: str
    pn: str
    sn: str
    devcode: str
    devaddr: str
    after_sunrise_hours: float
    before_sunset_hours: float
    daylight_value: str
    night_value: str
    poll_seconds: int
    timeout_seconds: int
    max_attempts: int
    retry_base_seconds: int

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            latitude=float(env("LATITUDE")), longitude=float(env("LONGITUDE")),
            timezone=ZoneInfo(env("TIMEZONE")), username=env("DESS_USERNAME"),
            password=env("DESS_PASSWORD"), company_key=env("DESS_COMPANY_KEY"),
            base_url=env("DESS_BASE_URL", "https://web.dessmonitor.com/public/").rstrip("/") + "/",
            pn=env("DESS_PN"), sn=env("DESS_SN"), devcode=env("DESS_DEVCODE"), devaddr=env("DESS_DEVADDR"),
            after_sunrise_hours=float(env("AFTER_SUNRISE_HOURS", "4")),
            before_sunset_hours=float(env("BEFORE_SUNSET_HOURS", "3")),
            daylight_value=env("DAYLIGHT_VALUE", "12338"), night_value=env("NIGHT_VALUE", "12336"),
            poll_seconds=int(env("POLL_SECONDS", "30")), timeout_seconds=int(env("HTTP_TIMEOUT_SECONDS", "120")),
            max_attempts=int(env("MAX_ATTEMPTS", "5")), retry_base_seconds=int(env("RETRY_BASE_SECONDS", "60")),
        )


class State:
    def __init__(self, database: Path) -> None:
        database.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(database)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
              id INTEGER PRIMARY KEY, local_date TEXT NOT NULL, kind TEXT NOT NULL,
              scheduled_at TEXT NOT NULL, desired_value TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
              next_attempt_at TEXT, last_error TEXT, last_response TEXT, completed_at TEXT,
              UNIQUE(local_date, kind)
            )
        """)
        self.db.commit()

    def schedule(self, local_day: date, kind: str, when: datetime, desired_value: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO jobs (local_date, kind, scheduled_at, desired_value) VALUES (?, ?, ?, ?)",
            (local_day.isoformat(), kind, iso(when), desired_value),
        )
        self.db.commit()

    def due_jobs(self, now: datetime) -> list[sqlite3.Row]:
        return list(self.db.execute("""
            SELECT * FROM jobs
            WHERE state = 'pending' AND scheduled_at <= ?
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY scheduled_at
        """, (iso(now), iso(now))))

    def success(self, job_id: int, response: dict[str, Any]) -> None:
        now = iso(datetime.now(timezone.utc))
        self.db.execute("""
            UPDATE jobs SET state='succeeded', attempts=attempts+1, last_response=?,
              completed_at=?, next_attempt_at=NULL, last_error=NULL WHERE id=?
        """, (json.dumps(response), now, job_id))
        self.db.commit()

    def failure(self, job_id: int, attempt_number: int, error: str, response: dict[str, Any] | None, config: Config) -> None:
        if attempt_number >= config.max_attempts:
            state, next_attempt = "failed", None
        else:
            delay = min(config.retry_base_seconds * (2 ** (attempt_number - 1)), 3600)
            state, next_attempt = "pending", iso(datetime.now(timezone.utc) + timedelta(seconds=delay))
        self.db.execute("""
            UPDATE jobs SET state=?, attempts=?, next_attempt_at=?, last_error=?, last_response=? WHERE id=?
        """, (state, attempt_number, next_attempt, error[:1000], json.dumps(response) if response else None, job_id))
        self.db.commit()


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


class DessMonitor:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.session = requests.Session()
        self.token: str | None = None
        self.secret: str | None = None
        self.expires_at: datetime | None = None

    @staticmethod
    def sha1(value: str) -> str:
        return hashlib.sha1(value.encode("utf-8")).hexdigest()

    def _get(self, query: list[tuple[str, str]]) -> dict[str, Any]:
        response = self.session.get(self.config.base_url, params=query, timeout=self.config.timeout_seconds)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise RuntimeError("DessMonitor returned a non-object JSON response")
        if result.get("err") != 0:
            raise RuntimeError(f"DessMonitor error {result.get('err')}: {result.get('desc')}")
        return result

    def authenticate(self) -> None:
        c, salt = self.config, str(int(time.time() * 1000))
        tail = [
            ("action", "authSource"), ("usr", c.username), ("source", "1"),
            ("company-key", c.company_key),
        ]
        signed_tail = "".join(f"&{key}={value}" for key, value in tail)
        sign = self.sha1(salt + self.sha1(c.password) + signed_tail)
        result = self._get([("sign", sign), ("salt", salt), *tail])
        dat = result.get("dat") or {}
        self.token, self.secret = dat.get("token"), dat.get("secret")
        if not self.token or not self.secret:
            raise RuntimeError("Authentication response did not contain token and secret")
        self.expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(dat.get("expire", 300)) - 15)
        LOG.info("Authenticated with DessMonitor")

    def call(self, action: str, parameters: list[tuple[str, str]]) -> dict[str, Any]:
        if not self.token or not self.secret or not self.expires_at or datetime.now(timezone.utc) >= self.expires_at:
            self.authenticate()
        salt = str(int(time.time() * 1000))
        tail = [("action", action), *parameters]
        signed_tail = "".join(f"&{key}={value}" for key, value in tail)
        sign = self.sha1(salt + self.secret + self.token + signed_tail)
        try:
            return self._get([("sign", sign), ("salt", salt), ("token", self.token), *tail])
        except RuntimeError as exc:
            # A server-side token expiry can happen before the advertised expiry.
            if "AUTH" not in str(exc).upper() and "TOKEN" not in str(exc).upper():
                raise
            self.token = self.secret = self.expires_at = None
            self.authenticate()
            return self.call(action, parameters)

    def priority(self) -> dict[str, Any]:
        c = self.config
        return self.call("queryDeviceCtrlValue", [
            ("source", "1"), ("pn", c.pn), ("sn", c.sn), ("devcode", c.devcode),
            ("devaddr", c.devaddr), ("id", PRIORITY_ID), ("i18n", "en_US"),
        ])

    def set_priority(self, value: str) -> dict[str, Any]:
        c = self.config
        return self.call("ctrlDevice", [
            ("source", "1"), ("pn", c.pn), ("sn", c.sn), ("devcode", c.devcode),
            ("devaddr", c.devaddr), ("id", PRIORITY_ID), ("val", value), ("i18n", "en_US"),
        ])


def plan_jobs(state: State, config: Config, now: datetime) -> None:
    location = LocationInfo("inverter", "", config.timezone.key, config.latitude, config.longitude)
    local_today = now.astimezone(config.timezone).date()
    for local_day in (local_today, local_today + timedelta(days=1)):
        times = sun(location.observer, date=local_day, tzinfo=config.timezone)
        state.schedule(local_day, "daylight", times["sunrise"] + timedelta(hours=config.after_sunrise_hours), config.daylight_value)
        state.schedule(local_day, "night", times["sunset"] - timedelta(hours=config.before_sunset_hours), config.night_value)


def run_job(job: sqlite3.Row, api: DessMonitor, state: State, config: Config) -> None:
    attempt = int(job["attempts"]) + 1
    try:
        before = api.priority()
        result = api.set_priority(job["desired_value"])
        state.success(job["id"], {"before": before, "set": result})
        LOG.info("Completed %s for %s (attempt %s)", job["kind"], job["local_date"], attempt)
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        LOG.warning("%s failed on attempt %s: %s", job["kind"], attempt, exc)
        state.failure(job["id"], attempt, str(exc), None, config)


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.from_env()
    except (ValueError, KeyError) as exc:
        LOG.error("Invalid configuration: %s", exc)
        sys.exit(2)
    state, api = State(DATABASE), DessMonitor(config)
    LOG.info("Scheduler started: sunrise +%sh, sunset -%sh, timezone=%s", config.after_sunrise_hours, config.before_sunset_hours, config.timezone.key)
    while True:
        now = datetime.now(timezone.utc)
        try:
            plan_jobs(state, config, now)
            for job in state.due_jobs(now):
                run_job(job, api, state, config)
        except Exception:  # Keep the scheduler alive for unexpected planning errors.
            LOG.exception("Scheduler loop failed; will retry on next poll")
        time.sleep(config.poll_seconds)


if __name__ == "__main__":
    main()
