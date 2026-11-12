"""Mubawab.ma — server-rendered HTML (httpx + selectolax, no Playwright)."""
from radar.sources.html_cards import HtmlCardSource


class MubawabSource(HtmlCardSource):
    name = "mubawab"
