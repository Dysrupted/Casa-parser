import httpx
import pytest
import respx

from radar.config import HttpConfig
from radar.http import FetchError, PoliteFetcher, RobotsDisallowed

BASE = "https://example.ma"


def cfg(**kw):
    d = dict(user_agent="CasaRadar/test", min_delay_s=3, max_delay_s=5, timeout_s=5, max_retries=1,
             retry_backoff_s=1, cache_ttl_hours={"search": 6, "detail": 168, "robots": 24, "api": 720})
    d.update(kw)
    return HttpConfig(**d)


class FakeTime:
    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def fetcher(tmp_path, ft, **kw):
    return PoliteFetcher(cfg(**kw), tmp_path, sleep=ft.sleep, clock=ft.clock)


def test_delay_floor_enforced():
    with pytest.raises(ValueError):
        cfg(min_delay_s=1)


@respx.mock
def test_robots_disallow_and_delay_and_cache(tmp_path):
    respx.get(f"{BASE}/robots.txt").respond(200, text="User-agent: *\nDisallow: /private\n")
    page = respx.get(f"{BASE}/list").respond(200, text="<html>ok</html>")
    ft = FakeTime()
    f = fetcher(tmp_path, ft)

    assert f.get(f"{BASE}/list", "search") == "<html>ok</html>"
    assert ft.sleeps and all(s >= 3 for s in ft.sleeps)   # waited after robots.txt request
    with pytest.raises(RobotsDisallowed):
        f.get(f"{BASE}/private/x", "detail")
    n = f.network_requests
    assert f.get(f"{BASE}/list", "search") == "<html>ok</html>"  # cache hit
    assert f.network_requests == n and page.call_count == 1


@respx.mock
def test_missing_robots_allows(tmp_path):
    respx.get(f"{BASE}/robots.txt").respond(404)
    respx.get(f"{BASE}/a").respond(200, text="a")
    assert fetcher(tmp_path, FakeTime()).get(f"{BASE}/a", "search") == "a"


@respx.mock
def test_robots_error_fails_closed(tmp_path):
    respx.get(f"{BASE}/robots.txt").respond(403)
    respx.get(f"{BASE}/a").respond(200, text="a")
    with pytest.raises(RobotsDisallowed):
        fetcher(tmp_path, FakeTime()).get(f"{BASE}/a", "search")


@respx.mock
def test_retry_on_5xx_then_fail(tmp_path):
    respx.get(f"{BASE}/robots.txt").respond(404)
    route = respx.get(f"{BASE}/a").mock(side_effect=[httpx.Response(503), httpx.Response(503)])
    with pytest.raises(FetchError):
        fetcher(tmp_path, FakeTime()).get(f"{BASE}/a", "search")
    assert route.call_count == 2
