"""robots.txt with wildcard support (RFC 9309 / Google semantics).

urllib.robotparser ignores `*` and `$` inside paths, so e.g. Mubawab's `Disallow: /*:` (which
forbids its `:p:N` pagination) would be silently allowed. This matcher implements:
group selection by product token (longest match, else `*`), `*`/`$` patterns, longest rule wins,
Allow wins ties, and `Crawl-delay`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit


@dataclass
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[bool, str]] = field(default_factory=list)   # (allow, pattern)
    crawl_delay: float | None = None


def _compile(pattern: str) -> re.Pattern:
    anchored = pattern.endswith("$")
    body = re.escape(pattern[:-1] if anchored else pattern).replace(r"\*", ".*")
    return re.compile(body + ("$" if anchored else ""))


class Robots:
    def __init__(self, text: str = "", allow_all: bool = False, disallow_all: bool = False):
        self.allow_all, self.disallow_all = allow_all, disallow_all
        self.groups: list[_Group] = []
        cur: _Group | None = None
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, val = (x.strip() for x in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if cur is None or not last_was_agent:
                    cur = _Group()
                    self.groups.append(cur)
                cur.agents.append(val.lower())
                last_was_agent = True
                continue
            last_was_agent = False
            if cur is None:
                continue
            if key in ("allow", "disallow"):
                if val:  # empty Disallow = allow everything
                    cur.rules.append((key == "allow", val))
            elif key == "crawl-delay":
                try:
                    cur.crawl_delay = float(val)
                except ValueError:
                    pass

    def _group(self, user_agent: str) -> _Group | None:
        token = user_agent.split("/")[0].strip().lower()
        best, best_len = None, -1
        for g in self.groups:
            for a in g.agents:
                if a != "*" and a in token and len(a) > best_len:
                    best, best_len = g, len(a)
        if best is not None:
            return best
        return next((g for g in self.groups if "*" in g.agents), None)

    def can_fetch(self, user_agent: str, url: str) -> bool:
        if self.disallow_all:
            return False
        if self.allow_all:
            return True
        g = self._group(user_agent)
        if g is None:
            return True
        parts = urlsplit(url)
        path = unquote(parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        verdict, best = True, -1
        for allow, pat in g.rules:
            if _compile(pat).match(path) and (len(pat) > best or (len(pat) == best and allow)):
                verdict, best = allow, len(pat)
        return verdict

    def crawl_delay(self, user_agent: str) -> float | None:
        g = self._group(user_agent)
        return g.crawl_delay if g else None
