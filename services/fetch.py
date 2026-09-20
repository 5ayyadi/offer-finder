"""The one choke point every outbound request to Kleinanzeigen goes through.

V1 keeps this simple on purpose: a single acquire()/record_result() gate
with jittered pacing and a hard per-run cap, plain "stop after N failures"
instead of a persisted exponential-backoff state machine, and a flat
on-disk HTML cache. Not built here (deferred to V2): a budget that
survives process restarts, exponential backoff curves, a formal
kill-switch object. See the plan's V2 roadmap for the fuller design.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

PageType = Literal["search", "detail"]
FetchOutcome = Literal["ok", "throttled", "suspicious", "blocked"]

_MIN_PLAUSIBLE_BODY_LENGTH = 1000
_CHALLENGE_MARKERS = ("captcha", "kein roboter", "bot detection", "access denied")


class BudgetExhausted(Exception):
    """Raised when a run has already made MAX_REQUESTS_PER_RUN requests."""


class TooManyFailures(Exception):
    """Raised when MAX_CONSECUTIVE_FAILURES bad responses happened in a row —
    the caller should stop the run entirely and notify the user rather than
    keep hammering a site that looks like it's blocking us."""


class KillSwitchActive(Exception):
    """Raised immediately when KILL_SWITCH=true, regardless of cache state."""


class CacheMiss(Exception):
    """Raised in DRY_RUN mode when a URL isn't already cached."""


class FetchFailed(Exception):
    """A single request came back throttled/suspicious/blocked, but the
    failure cap hasn't been hit yet — this one fetch just didn't yield
    usable HTML."""

    def __init__(self, outcome: FetchOutcome):
        super().__init__(f"fetch failed with outcome={outcome}")
        self.outcome = outcome


def classify_response(status_code: int, html: str) -> FetchOutcome:
    if status_code == 429:
        return "throttled"
    lowered = html.lower()
    if any(marker in lowered for marker in _CHALLENGE_MARKERS):
        return "blocked"
    if len(html) < _MIN_PLAUSIBLE_BODY_LENGTH:
        return "suspicious"
    return "ok"


class RequestGate:
    """The single choke point. Every fetch must call acquire() before the
    network call and record_result() after."""

    def __init__(
        self,
        max_requests_per_run: int,
        delay_floor_search_s: float,
        delay_floor_detail_s: float,
        jitter_s: float,
        max_consecutive_failures: int,
        sleep_fn: Callable[[float], None] = time.sleep,
        random_fn: Callable[[float, float], float] = random.uniform,
    ):
        self._max_requests_per_run = max_requests_per_run
        self._delay_floor = {"search": delay_floor_search_s, "detail": delay_floor_detail_s}
        self._jitter_s = jitter_s
        self._max_consecutive_failures = max_consecutive_failures
        self._sleep_fn = sleep_fn
        self._random_fn = random_fn

        self.requests_this_run = 0
        self.consecutive_failures = 0
        self._last_request_at: float | None = None

    def acquire(self, page_type: PageType) -> None:
        if self.requests_this_run >= self._max_requests_per_run:
            raise BudgetExhausted(
                f"reached MAX_REQUESTS_PER_RUN={self._max_requests_per_run}"
            )

        if self._last_request_at is not None:
            delay = self._delay_floor[page_type] + self._random_fn(0.0, self._jitter_s)
            self._sleep_fn(delay)

        self.requests_this_run += 1
        self._last_request_at = time.monotonic()

    def record_result(self, outcome: FetchOutcome) -> None:
        if outcome == "ok":
            self.consecutive_failures = 0
            return

        self.consecutive_failures += 1
        if self.consecutive_failures >= self._max_consecutive_failures:
            raise TooManyFailures(
                f"{self.consecutive_failures} consecutive failures (outcome={outcome})"
            )


def _cache_path(cache_dir: str, url: str) -> Path:
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return Path(cache_dir) / f"{key}.html"


def _cache_read(cache_dir: str, url: str) -> str | None:
    path = _cache_path(cache_dir, url)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _cache_write(cache_dir: str, url: str, html: str) -> None:
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    _cache_path(cache_dir, url).write_text(html, encoding="utf-8")


_CHROME_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}


def _curl_cffi_http_get(url: str) -> tuple[int, str]:
    from curl_cffi import requests as curl_requests

    response = curl_requests.get(url, headers=_CHROME_HEADERS, impersonate="chrome124", timeout=30)
    return response.status_code, response.text


def fetch_page(
    url: str,
    page_type: PageType,
    gate: RequestGate,
    cache_dir: str,
    dry_run: bool,
    kill_switch: bool,
    http_get: Callable[[str], tuple[int, str]] = _curl_cffi_http_get,
) -> str:
    if kill_switch:
        raise KillSwitchActive("KILL_SWITCH is active — refusing to fetch anything")

    cached = _cache_read(cache_dir, url)
    if cached is not None:
        return cached

    if dry_run:
        raise CacheMiss(f"DRY_RUN is active and {url} is not cached")

    gate.acquire(page_type)
    status_code, html = http_get(url)
    outcome = classify_response(status_code, html)
    gate.record_result(outcome)

    if outcome != "ok":
        raise FetchFailed(outcome)

    _cache_write(cache_dir, url, html)
    return html


class SeenIds:
    """A flat JSON-file-backed set of listing IDs already processed, so a
    listing is never re-fetched once seen — independent of cache TTL."""

    def __init__(self, path: str):
        self._path = Path(path)
        if self._path.exists():
            self._ids: set[str] = set(json.loads(self._path.read_text(encoding="utf-8")))
        else:
            self._ids = set()

    def has(self, listing_id: str) -> bool:
        return listing_id in self._ids

    def add(self, listing_id: str) -> None:
        self._ids.add(listing_id)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(sorted(self._ids)), encoding="utf-8")
