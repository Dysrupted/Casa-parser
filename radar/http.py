"""Polite HTTP fetching: robots.txt, randomized >=3s delay, disk cache, retries."""
from __future__ import annotations

import hashlib
import logging
import random
import time
from pathlib import Path
from typing import Callable, Literal, Protocol
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from radar.config import HttpConfig

log = logging.getLogger(__name__)
Kind = Literal["search", "detail", "robots", "api", "photo"]


class FetchError(Exception):
    pass


class RobotsDisallowed(FetchError):
    pass


class Fetcher(Protocol):
    def get(self, url: str, kind: Kind) -> str: ...


class PoliteFetcher:
    """The only component allowed to touch the network.

    Delay is applied only before real network requests (cache hits are free), measured
    from the end of the previous request per host.
    """

    def __init__(
        self,
        cfg: HttpConfig,
        cache_dir: Path,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ):
        self.cfg = cfg
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.client = client or httpx.Client(
            headers={"User-Agent": cfg.user_agent, "Accept-Language": "fr-FR,fr;q=0.9"},
            timeout=cfg.timeout_s,
            follow_redirects=True,
        )
        self._sleep, self._clock = sleep, clock
        self._rng = rng or random.Random()
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser] = {}
        self.network_requests = 0

    # -- public -----------------------------------------------------------------
    def get(self, url: str, kind: Kind) -> str:
        cached = self._cache_read(url, kind)
        if cached is not None:
            return cached
        # robots.txt governs crawling; JSON APIs (geocoding, FX) are governed by their own
        # usage policies, which we honour via delay + cache + identification.
        if kind not in ("robots", "api") and not self._allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows (or could not be read) for {url}")
        text = self._network_get(url)
        self._cache_write(url, text)
        return text

    def get_bytes(self, url: str, kind: Kind = "photo") -> bytes:
        """Binary fetch (photos for perceptual hashing). Same politeness + cache rules."""
        p = self._cache_path(url).with_suffix(".bin")
        ttl_s = getattr(self.cfg.cache_ttl_hours, kind) * 3600
        if p.exists() and time.time() - p.stat().st_mtime < ttl_s:
            return p.read_bytes()
        if not self._allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows (or could not be read) for {url}")
        data = self._network_get(url, binary=True)
        p.write_bytes(data)
        return data

    # -- robots -----------------------------------------------------------------
    def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        rp = self._robots.get(host)
        if rp is None:
            rp = RobotFileParser()
            try:
                rp.parse(self.get(f"{host}/robots.txt", "robots").splitlines())
            except _NotFound:
                rp.parse([])  # no robots.txt => everything allowed
            except FetchError as e:
                log.warning("robots.txt unavailable for %s (%s): failing closed", host, e)
                rp.disallow_all = True
            self._robots[host] = rp
        return rp.can_fetch(self.cfg.user_agent, url)

    # -- network ----------------------------------------------------------------
    def _wait_turn(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is None:
            return
        delay = self._rng.uniform(self.cfg.min_delay_s, self.cfg.max_delay_s)
        remaining = delay - (self._clock() - last)
        if remaining > 0:
            self._sleep(remaining)

    def _network_get(self, url: str, binary: bool = False):
        host = urlsplit(url).netloc
        last_exc: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            self._wait_turn(host)
            try:
                self.network_requests += 1
                resp = self.client.get(url)
            except httpx.HTTPError as e:
                last_exc = e
            else:
                if resp.status_code == 404:
                    raise _NotFound(url)
                if resp.status_code == 200:
                    return resp.content if binary else resp.text
                last_exc = FetchError(f"HTTP {resp.status_code} for {url}")
                if resp.status_code < 500 and resp.status_code != 429:
                    break  # 4xx other than 429: retrying is pointless and impolite
            finally:
                self._last_request[host] = self._clock()
            if attempt < self.cfg.max_retries:
                self._sleep(self.cfg.retry_backoff_s * (attempt + 1))
        raise FetchError(str(last_exc)) from last_exc

    # -- cache ------------------------------------------------------------------
    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / f"{hashlib.sha1(url.encode()).hexdigest()}.html"

    def _cache_read(self, url: str, kind: Kind) -> str | None:
        p = self._cache_path(url)
        ttl_s = getattr(self.cfg.cache_ttl_hours, kind) * 3600
        if p.exists() and time.time() - p.stat().st_mtime < ttl_s:
            return p.read_text(encoding="utf-8")
        return None

    def _cache_write(self, url: str, text: str) -> None:
        self._cache_path(url).write_text(text, encoding="utf-8")


class _NotFound(FetchError):
    pass


class PlaywrightFetcher(PoliteFetcher):
    """Renders JS pages in headless Chromium. Same robots/delay/cache rules; HTML pages only."""

    def __init__(self, *a, wait_until: str = "networkidle", **kw):
        super().__init__(*a, **kw)
        self._wait_until, self._pw, self._browser = wait_until, None, None

    def _page_html(self, url: str) -> str:
        if self._browser is None:
            from playwright.sync_api import sync_playwright  # optional dependency

            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
        page = self._browser.new_page(user_agent=self.cfg.user_agent)
        try:
            resp = page.goto(url, wait_until=self._wait_until, timeout=self.cfg.timeout_s * 1000)
            if resp is None or resp.status != 200:
                raise FetchError(f"HTTP {resp.status if resp else '?'} for {url}")
            return page.content()
        finally:
            page.close()

    def _network_get(self, url: str, binary: bool = False):
        if binary or url.endswith("/robots.txt"):
            return super()._network_get(url, binary)
        host = urlsplit(url).netloc
        self._wait_turn(host)
        try:
            self.network_requests += 1
            return self._page_html(url)
        except FetchError:
            raise
        except Exception as e:
            raise FetchError(f"playwright: {type(e).__name__}: {e}") from e
        finally:
            self._last_request[host] = self._clock()

    def close(self) -> None:
        if self._browser:
            self._browser.close()
            self._pw.stop()
