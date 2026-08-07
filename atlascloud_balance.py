"""Fetch AtlasCloud balance and save the response to a dated results file."""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from requests.exceptions import ConnectionError, HTTPError, RequestException, Timeout

DEFAULT_API_URL = "https://api.atlascloud.ai/public/v1/balance"
RESULTS_DIR = Path("results")
API_KEY_ENV = "ATLASCLOUD_API_KEY"
API_URL_ENV = "ATLASCLOUD_API_URL"


def load_environment() -> None:
    load_dotenv()


def get_api_key() -> str:
    api_key = os.getenv(API_KEY_ENV)
    if not api_key:
        raise RuntimeError(
            f"Environment variable {API_KEY_ENV} is required. "
            "Create a .env file with your AtlasCloud API key."
        )
    return api_key


def get_api_url() -> str:
    return os.getenv(API_URL_ENV, DEFAULT_API_URL)


def fetch_balance(api_url: str, api_key: str) -> requests.Response:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json, text/html, text/plain",
    }

    try:
        response = requests.get(api_url, headers=headers, timeout=30)
        response.raise_for_status()
        return response
    except Timeout as exc:
        raise RuntimeError("Request timed out while connecting to AtlasCloud.") from exc
    except ConnectionError as exc:
        raise RuntimeError(
            "Unable to connect to AtlasCloud. "
            "Check your network connection and DNS settings."
        ) from exc
    except HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else "unknown"
        raise RuntimeError(
            f"AtlasCloud API returned HTTP {status_code}. "
            "Verify your API key and endpoint."
        ) from exc
    except RequestException as exc:
        raise RuntimeError("An error occurred while requesting AtlasCloud balance.") from exc


def write_results(response: requests.Response, output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    with output_file.open("w", encoding="utf-8") as handle:
        handle.write(f"AtlasCloud balance fetched on {timestamp}\n")
        handle.write(f"URL: {response.url}\n")
        handle.write(f"Status: {response.status_code}\n")
        handle.write(f"Content-Type: {response.headers.get('Content-Type', 'unknown')}\n\n")

        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            json.dump(response.json(), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        else:
            handle.write(response.text)


def main() -> int:
    load_environment()
    api_key = get_api_key()
    api_url = get_api_url()

    print(f"Fetching AtlasCloud balance from {api_url}")
    try:
        response = fetch_balance(api_url, api_key)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    output_file = RESULTS_DIR / f"balance_{datetime.now(timezone.utc):%Y.%m.%d}.txt"
    write_results(response, output_file)

    print(f"Wrote balance output to {output_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
