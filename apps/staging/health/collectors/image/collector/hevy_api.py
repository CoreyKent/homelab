"""Read-only client for the Hevy public API (https://api.hevyapp.com, `api-key` header).

Design rules (23/11/2026, `20261123-hevy-collector`):
- GET only. Hevy's personal API key can also create and change workouts, so this module has
  exactly one HTTP call site, `_send`, and it issues GET. tests/test_hevy_api.py fails if any
  other verb appears in this file.
- The key never appears in an exception, a log line or `ops.run_log`: errors carry the status
  and path only, and `scrub()` removes the key from any text that might reach storage.
- Transient failures (timeouts, connection errors, 429, 5xx) are retried with backoff. Hevy's
  429 carries no Retry-After, so the delays are fixed. 401 and other 4xx fail at once.
- Pagination is capped, so a wrong `page_count` cannot loop the job.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import time
from typing import Any

import requests

BASE_URL = "https://api.hevyapp.com"
TIMEOUT_S = 30
PAGE_SIZE = 10  # Hevy refuses pageSize > 10 with a 400.
PAGE_CAP = 500  # 5,000 workouts; far beyond any real account, and a hard stop for a bad page_count.
RETRY_DELAYS_S = (2, 4, 8)  # one first attempt, then up to three retries


class HevyError(Exception):
    """Base class. Messages never contain the API key."""


class HevyAuthError(HevyError):
    """401: the key is missing, wrong, revoked, or the account lost Hevy Pro."""


class HevyNotFound(HevyError):
    """404 for a single resource (e.g. a custom exercise template that was deleted)."""


class HevyAPIError(HevyError):
    def __init__(self, status: int | None, path: str, detail: str = ""):
        self.status = status
        self.path = path
        what = f"HTTP {status}" if status is not None else (detail or "no response")
        super().__init__(f"Hevy GET {path} failed: {what}")


@dataclass(frozen=True)
class Response:
    status: int
    body: Any  # parsed JSON, or None when the body was not JSON


Transport = Callable[[str, dict[str, Any], dict[str, str], float], Response]


def _requests_transport() -> Transport:
    session = requests.Session()

    def send(url: str, params: dict[str, Any], headers: dict[str, str], timeout: float) -> Response:
        r = session.get(url, params=params, headers=headers, timeout=timeout)
        try:
            body = r.json()
        except ValueError:
            body = None
        return Response(r.status_code, body)

    return send


@dataclass(frozen=True)
class WorkoutSnapshot:
    workouts: list[dict]
    pages_fetched: int
    page_count: int
    complete: bool  # every page from 1 to page_count was fetched


class HevyClient:
    def __init__(
        self,
        api_key: str,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        base_url: str = BASE_URL,
    ):
        if not api_key:
            raise HevyAuthError("HEVY_API_KEY is empty")
        self._key = api_key
        self._transport = transport or _requests_transport()
        self._sleep = sleep
        self._base = base_url.rstrip("/")

    def scrub(self, text: str) -> str:
        """Remove the key from text that may be logged or stored (belt and braces)."""
        return text.replace(self._key, "<hevy-key>") if self._key else text

    def _send(self, path: str, params: dict[str, Any]) -> Response:
        headers = {"api-key": self._key, "accept": "application/json"}
        return self._transport(self._base + path, params, headers, TIMEOUT_S)

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        params = dict(params or {})
        attempts = len(RETRY_DELAYS_S) + 1
        last: HevyAPIError | None = None
        for attempt in range(attempts):
            if attempt:
                self._sleep(RETRY_DELAYS_S[attempt - 1])
            try:
                resp = self._send(path, params)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last = HevyAPIError(None, path, type(exc).__name__)
                continue
            if resp.status == 200:
                return resp.body
            if resp.status == 401:
                raise HevyAuthError(f"Hevy rejected the API key on GET {path} (HTTP 401)")
            if resp.status == 404:
                raise HevyNotFound(f"Hevy GET {path}: HTTP 404")
            if resp.status == 429 or resp.status >= 500:
                last = HevyAPIError(resp.status, path)
                continue
            raise HevyAPIError(resp.status, path)
        assert last is not None
        raise last

    def workout_count(self) -> int:
        body = self.get("/v1/workouts/count")
        n = body.get("workout_count") if isinstance(body, dict) else None
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            raise HevyAPIError(200, "/v1/workouts/count", "unexpected body")
        return n

    def all_workouts(self, page_cap: int = PAGE_CAP) -> WorkoutSnapshot:
        """Page through /v1/workouts. `complete` is False if the cap stopped the walk."""
        workouts: list[dict] = []
        page = 1
        page_count = 1
        pages = 0
        while page <= page_count:
            if pages >= page_cap:
                return WorkoutSnapshot(workouts, pages, page_count, complete=False)
            body = self.get("/v1/workouts", {"page": page, "pageSize": PAGE_SIZE})
            if not isinstance(body, dict) or not isinstance(body.get("workouts"), list):
                raise HevyAPIError(200, "/v1/workouts", "unexpected body")
            pc = body.get("page_count")
            if not isinstance(pc, int) or isinstance(pc, bool) or pc < 0:
                raise HevyAPIError(200, "/v1/workouts", "bad page_count")
            if page == 1:
                page_count = pc
            workouts.extend(body["workouts"])
            pages += 1
            page += 1
        return WorkoutSnapshot(workouts, pages, page_count, complete=True)

    def exercise_template(self, template_id: str) -> dict:
        body = self.get(f"/v1/exercise_templates/{requests.utils.quote(template_id, safe='')}")
        if not isinstance(body, dict) or body.get("id") != template_id:
            raise HevyAPIError(200, "/v1/exercise_templates/{id}", "unexpected body")
        return body
