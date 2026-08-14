"""Authenticate with DessMonitor and print the current output priority.

Run with: python3 test_credentials.py
The script reads the project's .env file (or environment variables) and never
prints the returned token or secret.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen


BASE_URL = os.getenv("DESS_BASE_URL", "https://web.dessmonitor.com/public/").rstrip("/") + "/"
PRIORITY_ID = "bse_output_source_priority"


def load_dotenv() -> None:
    """Load simple KEY=VALUE lines without needing another Python package."""
    dotenv = Path(__file__).with_name(".env")
    if not dotenv.exists():
        return
    for line in dotenv.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing {name}. Add it to .env.")
    return value


def sha1(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def api_get(query: list[tuple[str, str]]) -> dict[str, Any]:
    url = BASE_URL + "?" + urlencode(query)
    with urlopen(url, timeout=120) as response:  # nosec B310: URL is a configured HTTPS API endpoint
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("err") != 0:
        raise RuntimeError(f"DessMonitor error {payload.get('err')}: {payload.get('desc')}")
    return payload


def signed_tail(parameters: list[tuple[str, str]]) -> str:
    # Parameter order is required by DessMonitor's SHA-1 signature.
    return "".join(f"&{key}={value}" for key, value in parameters)


def main() -> None:
    load_dotenv()
    username = required("DESS_USERNAME")
    password = required("DESS_PASSWORD")
    company_key = required("DESS_COMPANY_KEY")
    salt = str(int(time.time() * 1000))

    auth_parameters = [
        ("action", "authSource"),
        ("usr", username),
        ("source", "1"),
        ("company-key", company_key),
    ]
    auth_signature = sha1(salt + sha1(password) + signed_tail(auth_parameters))
    auth = api_get([("sign", auth_signature), ("salt", salt), *auth_parameters])
    credentials = auth.get("dat") or {}
    token, secret = credentials.get("token"), credentials.get("secret")
    if not token or not secret:
        raise RuntimeError("Authentication worked but did not return a token and secret.")
    print(f"Authentication succeeded for {credentials.get('usr', username)}; token expires in {credentials.get('expire')} seconds.")

    status_parameters = [
        ("action", "queryDeviceCtrlValue"),
        ("source", "1"),
        ("pn", required("DESS_PN")),
        ("sn", required("DESS_SN")),
        ("devcode", required("DESS_DEVCODE")),
        ("devaddr", required("DESS_DEVADDR")),
        ("id", PRIORITY_ID),
        ("i18n", "en_US"),
    ]
    salt = str(int(time.time() * 1000))
    status_signature = sha1(salt + secret + token + signed_tail(status_parameters))
    status = api_get([("sign", status_signature), ("salt", salt), ("token", token), *status_parameters])
    current = status.get("dat") or {}
    print(f"Current {current.get('name', PRIORITY_ID)}: {current.get('val', '<not returned>')}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Credential/status test failed: {exc}", file=sys.stderr)
        sys.exit(1)
