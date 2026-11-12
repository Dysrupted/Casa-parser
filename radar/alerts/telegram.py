"""Minimal Telegram Bot API client (sendMessage / sendPhoto) + message formatting."""
from __future__ import annotations

import html
import logging

import httpx

log = logging.getLogger(__name__)
API = "https://api.telegram.org"
CAPTION_MAX = 1024
MESSAGE_MAX = 4096


class TelegramError(Exception):
    pass


class TelegramClient:
    def __init__(self, token: str, chat_id: str, client: httpx.Client | None = None):
        self.base, self.chat_id = f"{API}/bot{token}", chat_id
        self.http = client or httpx.Client(timeout=20)

    def _call(self, method: str, **body) -> dict:
        try:
            r = self.http.post(f"{self.base}/{method}", json={"chat_id": self.chat_id, **body})
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise TelegramError(f"{method}: {type(e).__name__}") from e  # never log the token-bearing URL
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description', r.status_code)}")
        return data

    def send(self, text: str, photo: str | None = None) -> None:
        if photo and len(text) <= CAPTION_MAX:
            try:
                self._call("sendPhoto", photo=photo, caption=text, parse_mode="HTML")
                return
            except TelegramError as e:  # dead photo URL etc. -> fall back to text
                log.warning("sendPhoto failed (%s); sending text", e)
        self._call("sendMessage", text=text[:MESSAGE_MAX], parse_mode="HTML",
                   link_preview_options={"is_disabled": photo is None})


def _mad(x) -> str:
    return f"{x:,.0f}".replace(",", " ") + " MAD" if x is not None else "?"


def _eur(x) -> str:
    return f"{x:,.0f}".replace(",", " ") + " €" if x is not None else ""


HEADERS = {"new_high_score": "🏠 New match", "price_drop": "📉 Price drop", "watch_removed": "❌ Watched listing removed",
           "run_errors": "⚠️ Casa Radar run issues", "test": "✅ Casa Radar test alert"}


def format_alert(a: dict) -> str:
    e = html.escape
    if a["type"] in ("run_errors", "test"):
        lines = [f"<b>{HEADERS[a['type']]}</b>"] + [f"• {e(m)}" for m in a.get("messages", [])]
        return "\n".join(lines)
    head = HEADERS.get(a["type"], a["type"])
    lines = [f"<b>{head}</b> · {e(a.get('zone_label') or '?')} · score <b>{a.get('score', '?')}</b>",
             e(a.get("title") or "")]
    price = _mad(a.get("price_mad"))
    if a["type"] == "price_drop" and a.get("old_price_mad"):
        pct = (a["old_price_mad"] - a["price_mad"]) / a["old_price_mad"]
        price = f"<s>{_mad(a['old_price_mad'])}</s> → <b>{_mad(a['price_mad'])}</b> (−{pct:.0%})"
    eur = f" ({_eur(a['price_eur'])})" if a.get("price_eur") else ""
    lines.append(f"{price}{eur} · {a.get('surface_m2') or '?'} m²")
    if a.get("price_m2"):
        pvz = f" · {a['price_vs_zone']:.2f}× zone median" if a.get("price_vs_zone") else ""
        lines.append(f"{a['price_m2']:,.0f} MAD/m²{pvz}".replace(",", " "))
    for r in a.get("reasons", [])[:3]:
        lines.append(f"+ {e(r)}")
    for r in a.get("warnings", [])[:2]:
        lines.append(f"− {e(r)}")
    if a.get("cash_needed_mad"):
        lines.append(f"💰 Cash needed {_mad(a['cash_needed_mad'])} · {_mad(a.get('monthly_payment_mad'))}/month")
    lines.append(f'<a href="{e(a.get("url") or "")}">Open listing</a>')
    return "\n".join(lines)


def format_digest(alerts: list[dict]) -> str:
    e = html.escape
    lines = [f"<b>🗞 Casa Radar digest · {len(alerts)} alerts</b>"]
    for a in alerts:
        if a["type"] in ("run_errors", "test"):
            lines.append(f"⚠️ {len(a.get('messages', []))} run issue(s)")
            continue
        tag = {"new_high_score": "🏠", "price_drop": "📉", "watch_removed": "❌"}.get(a["type"], "•")
        lines.append(f'{tag} <a href="{e(a.get("url") or "")}">{e(a.get("zone_label") or "?")} '
                     f'{_mad(a.get("price_mad"))}</a> · {a.get("surface_m2") or "?"} m² · score {a.get("score", "?")}')
    return "\n".join(lines)[:MESSAGE_MAX]
