
import pytest

from offer_finder.services.fetch import (
    BudgetExhausted,
    CacheMiss,
    FetchFailed,
    KillSwitchActive,
    RequestGate,
    TooManyFailures,
    classify_response,
    fetch_page,
)

# --- blocking detection --------------------------------------------------------


def test_classify_response_normal_page_is_ok():
    assert classify_response(200, "x" * 5000) == "ok"


def test_classify_response_429_is_throttled():
    assert classify_response(429, "rate limited") == "throttled"


def test_classify_response_suspiciously_short_body():
    assert classify_response(200, "short") == "suspicious"


def test_classify_response_challenge_page_is_blocked():
    html = "x" * 3000 + "Bitte bestätige, dass du kein Roboter bist (captcha)"
    assert classify_response(200, html) == "blocked"


# --- RequestGate: budget + failure cap (no real sleeping) --------------------


def _gate(**overrides):
    defaults = {
        "max_requests_per_run": 3,
        "delay_floor_search_s": 0.0,
        "delay_floor_detail_s": 0.0,
        "jitter_s": 0.0,
        "max_consecutive_failures": 2,
        "sleep_fn": lambda _seconds: None,
    }
    defaults.update(overrides)
    return RequestGate(**defaults)


def test_gate_allows_requests_up_to_the_cap():
    gate = _gate(max_requests_per_run=2)
    gate.acquire("search")
    gate.acquire("search")
    with pytest.raises(BudgetExhausted):
        gate.acquire("search")


def test_gate_paces_between_requests_using_injected_sleep():
    sleep_calls: list[float] = []
    gate = _gate(
        max_requests_per_run=5,
        delay_floor_search_s=2.0,
        jitter_s=0.0,
        sleep_fn=sleep_calls.append,
    )
    gate.acquire("search")  # first request: no prior request, no pacing sleep expected
    gate.acquire("search")  # second request: must pace at least the floor
    assert sleep_calls == [2.0]


def test_gate_detail_floor_is_slower_than_search_floor():
    sleep_calls: list[float] = []
    gate = _gate(
        max_requests_per_run=5,
        delay_floor_search_s=1.0,
        delay_floor_detail_s=3.0,
        jitter_s=0.0,
        sleep_fn=sleep_calls.append,
    )
    gate.acquire("detail")
    gate.acquire("detail")
    assert sleep_calls == [3.0]


def test_gate_trips_after_max_consecutive_failures():
    gate = _gate(max_consecutive_failures=2)
    gate.record_result("throttled")
    with pytest.raises(TooManyFailures):
        gate.record_result("throttled")


def test_gate_resets_failure_count_on_success():
    gate = _gate(max_consecutive_failures=2)
    gate.record_result("throttled")
    gate.record_result("ok")
    gate.record_result("throttled")  # should not trip — counter was reset
    assert gate.consecutive_failures == 1


# --- fetch_page: dry-run / kill-switch / cache --------------------------------


def test_fetch_page_dry_run_returns_cached_content(tmp_path):
    cache_dir = str(tmp_path)
    gate = _gate()
    from offer_finder.services.fetch import _cache_write

    _cache_write(cache_dir, "https://example.invalid/foo", "<html>cached</html>")

    html = fetch_page(
        "https://example.invalid/foo", "search", gate, cache_dir=cache_dir, dry_run=True, kill_switch=False
    )
    assert html == "<html>cached</html>"


def test_fetch_page_dry_run_raises_cache_miss(tmp_path):
    gate = _gate()
    with pytest.raises(CacheMiss):
        fetch_page(
            "https://example.invalid/missing", "search", gate,
            cache_dir=str(tmp_path), dry_run=True, kill_switch=False,
        )


def test_fetch_page_kill_switch_raises_even_with_cache(tmp_path):
    cache_dir = str(tmp_path)
    gate = _gate()
    from offer_finder.services.fetch import _cache_write

    _cache_write(cache_dir, "https://example.invalid/foo", "<html>cached</html>")

    with pytest.raises(KillSwitchActive):
        fetch_page(
            "https://example.invalid/foo", "search", gate,
            cache_dir=cache_dir, dry_run=False, kill_switch=True,
        )


def test_fetch_page_live_success_writes_cache_and_returns_html(tmp_path):
    gate = _gate()

    def fake_http_get(url: str) -> tuple[int, str]:
        return 200, "<html>" + "x" * 5000 + "</html>"

    html = fetch_page(
        "https://example.invalid/live", "detail", gate,
        cache_dir=str(tmp_path), dry_run=False, kill_switch=False, http_get=fake_http_get,
    )
    assert "x" * 100 in html

    # second call should be served from cache, not hit http_get again
    calls = []

    def counting_http_get(url: str) -> tuple[int, str]:
        calls.append(url)
        return 200, "<html>should not be used</html>"

    html2 = fetch_page(
        "https://example.invalid/live", "detail", gate,
        cache_dir=str(tmp_path), dry_run=False, kill_switch=False, http_get=counting_http_get,
    )
    assert html2 == html
    assert calls == []


def test_fetch_page_raises_fetch_failed_on_bad_response(tmp_path):
    gate = _gate()

    def fake_http_get(url: str) -> tuple[int, str]:
        return 429, "rate limited"

    with pytest.raises(FetchFailed):
        fetch_page(
            "https://example.invalid/blocked", "search", gate,
            cache_dir=str(tmp_path), dry_run=False, kill_switch=False, http_get=fake_http_get,
        )


# --- cache TTL -------------------------------------------------------------------


def _stale_cache(cache_dir: str, url: str, age_s: float) -> None:
    import os
    import time

    from offer_finder.services.fetch import _cache_path, _cache_write

    _cache_write(cache_dir, url, "<html>old</html>")
    old = time.time() - age_s
    os.utime(_cache_path(cache_dir, url), (old, old))


def test_fetch_page_refetches_when_cache_is_older_than_ttl(tmp_path):
    url = "https://example.invalid/search"
    _stale_cache(str(tmp_path), url, age_s=3600)

    html = fetch_page(
        url, "search", _gate(), cache_dir=str(tmp_path), dry_run=False, kill_switch=False,
        http_get=lambda _url: (200, "<html>" + "new" * 1000 + "</html>"), cache_ttl_s=900,
    )
    assert "new" in html


def test_fetch_page_uses_fresh_cache_within_ttl(tmp_path):
    url = "https://example.invalid/search"
    _stale_cache(str(tmp_path), url, age_s=60)

    html = fetch_page(
        url, "search", _gate(), cache_dir=str(tmp_path), dry_run=False, kill_switch=False,
        http_get=lambda _url: (200, "should not be fetched"), cache_ttl_s=900,
    )
    assert html == "<html>old</html>"


def test_fetch_page_dry_run_serves_stale_cache_rather_than_touching_network(tmp_path):
    url = "https://example.invalid/search"
    _stale_cache(str(tmp_path), url, age_s=3600)

    html = fetch_page(
        url, "search", _gate(), cache_dir=str(tmp_path), dry_run=True, kill_switch=False, cache_ttl_s=900,
    )
    assert html == "<html>old</html>"
