"""FastAPI app: JSON API (Finance Cockpit, PRD F9) + phone-friendly HTML dashboard (PRD F8).

Auth (env):
  RADAR_DASHBOARD_PASSWORD  HTTP Basic for everything (any username)
  RADAR_API_TOKEN           Bearer token accepted on /api/* (machine clients)
  RADAR_SHARE_TOKEN         read-only HTML access via ?share=<token> (family view); never allows writes
With none set, the server must bind to localhost (enforced by `radar serve`).
"""
from __future__ import annotations

import base64
import hmac
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.engine import Engine
from sqlmodel import Session

from radar.api import queries as Q
from radar.config import Settings
from radar.models import NoteStatus, UserNote

TEMPLATES = Path(__file__).parent / "templates"


def svg_line(rows: list[dict], key: str, w: int = 320, h: int = 90, pad: int = 6) -> str:
    """Tiny inline SVG line chart (no JS, no CDN) of rows[i][key]."""
    pts = [(i, r.get(key)) for i, r in enumerate(rows) if r.get(key) is not None]
    if len(pts) < 2:
        return ""
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    lo, hi = min(ys), max(ys)
    span = (hi - lo) or 1
    n = max(xs) or 1
    coords = [(pad + (x / n) * (w - 2 * pad), h - pad - ((y - lo) / span) * (h - 2 * pad)) for x, y in pts]
    d = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(coords))
    return (f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="trend from {lo:,.0f} to {hi:,.0f}">'
            f'<path d="{d}" fill="none" stroke="currentColor" stroke-width="2"/>'
            f'<circle cx="{coords[-1][0]:.1f}" cy="{coords[-1][1]:.1f}" r="3" fill="currentColor"/></svg>')


class GoalIn(BaseModel):
    property_id: int | None = None
    manual_price_mad: int | None = None
    agency_listing: bool = False



class NoteIn(BaseModel):
    status: NoteStatus = NoteStatus.watching
    note: str | None = None
    reject_reason: str | None = None
    actual_sale_price_mad: int | None = None



def _eq(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


class Access:
    def __init__(self):
        self.password = os.environ.get("RADAR_DASHBOARD_PASSWORD") or None
        self.api_token = os.environ.get("RADAR_API_TOKEN") or None
        self.share_token = os.environ.get("RADAR_SHARE_TOKEN") or None

    @property
    def open(self) -> bool:
        return not (self.password or self.api_token or self.share_token)

    def level(self, request: Request) -> str | None:
        """'write', 'read' or None."""
        if self.open:
            return "write"
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic ") and self.password:
            try:
                _, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
            except (ValueError, UnicodeDecodeError):
                pw = ""
            if _eq(pw, self.password):
                return "write"
        if auth.startswith("Bearer ") and request.url.path.startswith("/api/") and _eq(auth[7:], self.api_token):
            return "write"
        share = request.query_params.get("share") or request.cookies.get("radar_share")
        if _eq(share, self.share_token) and not request.url.path.startswith("/api/"):
            return "read"
        return None


def create_app(settings: Settings, engine: Engine) -> FastAPI:
    app = FastAPI(title="Casa Radar", docs_url="/api/docs", openapi_url="/api/openapi.json")
    tpl = Jinja2Templates(directory=str(TEMPLATES))
    tpl.env.globals.update(svg_line=svg_line)
    tpl.env.filters["mad"] = lambda x: "—" if x is None else f"{x:,.0f}".replace(",", " ")
    tpl.env.filters["pct"] = lambda x: "—" if x is None else f"{x:+.1%}"
    access = Access()

    def db():
        with Session(engine) as session:
            yield session

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        lvl = access.level(request)
        if lvl is None:
            hdrs = {"WWW-Authenticate": 'Basic realm="Casa Radar"'} if access.password else {}
            return JSONResponse({"detail": "unauthorized"}, status_code=401, headers=hdrs)
        if lvl == "read" and request.method != "GET":
            return JSONResponse({"detail": "read-only share link"}, status_code=403)
        request.state.access = lvl
        resp = await call_next(request)
        if lvl == "read" and request.query_params.get("share"):
            resp.set_cookie("radar_share", request.query_params["share"], httponly=True, samesite="lax",
                            max_age=30 * 86400)
        return resp

    def page(request: Request, name: str, **ctx):
        ctx |= {"readonly": request.state.access == "read", "zones": list(settings.profile.zones),
                "now": datetime.now(timezone.utc)}
        return tpl.TemplateResponse(request, name, ctx)

    # ---------------------------------------------------------------- JSON API
    @app.get("/api/goal")
    def api_goal(session: Session = Depends(db)):
        return Q.goal(session, settings)

    @app.put("/api/goal")
    def api_set_goal(body: GoalIn, session: Session = Depends(db)):
        try:
            Q.set_goal(session, body.property_id, body.manual_price_mad, body.agency_listing)
        except ValueError as e:
            raise HTTPException(422, str(e))
        except LookupError as e:
            raise HTTPException(404, str(e))
        return Q.goal(session, settings)

    @app.get("/api/market")
    def api_market_all(session: Session = Depends(db)):
        return Q.market(session, settings)

    @app.get("/api/market/{zone}")
    def api_market(zone: str, session: Session = Depends(db)):
        if zone not in settings.zones.aliases and zone not in settings.profile.zones:
            raise HTTPException(404, f"unknown zone {zone}")
        return Q.market(session, settings, zone)[0]

    @app.get("/api/listings")
    def api_listings(zone: str | None = None, min_score: float | None = None, in_profile: bool | None = None,
                     watch: bool = False, sort: str = "score", limit: int = 200, session: Session = Depends(db)):
        return Q.listing_rows(session, settings, zone, min_score, in_profile, watch, sort=sort, limit=limit)

    @app.get("/api/properties/{pid}")
    def api_property(pid: int, session: Session = Depends(db)):
        d = Q.property_detail(session, settings, pid)
        if d is None:
            raise HTTPException(404)
        return d

    def _save_note(session: Session, pid: int, body: NoteIn) -> None:
        if Q.property_detail(session, settings, pid) is None:
            raise HTTPException(404)
        n = session.get(UserNote, pid) or UserNote(property_id=pid, updated_at=datetime.now(timezone.utc))
        n.status, n.note, n.reject_reason = body.status, body.note or None, body.reject_reason or None
        n.actual_sale_price_mad, n.updated_at = body.actual_sale_price_mad, datetime.now(timezone.utc)
        session.add(n)
        session.commit()

    @app.put("/api/properties/{pid}/note")
    def api_note(pid: int, body: NoteIn, session: Session = Depends(db)):
        _save_note(session, pid, body)
        return {"ok": True}

    @app.delete("/api/properties/{pid}/note")
    def api_unnote(pid: int, session: Session = Depends(db)):
        if (n := session.get(UserNote, pid)) is not None:
            session.delete(n)
            session.commit()
        return {"ok": True}

    # ---------------------------------------------------------------- HTML
    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, zone: str | None = None, min_score: str | None = None, all: bool = False,
             watch: bool = False, sort: str = "score", session: Session = Depends(db)):
        ms = float(min_score) if min_score not in (None, "") else None
        rows = Q.listing_rows(session, settings, zone or None, ms, None if all else True, watch, sort=sort)
        return page(request, "listings.html", rows=rows, f={"zone": zone or "", "min_score": min_score or "",
                                                          "all": all, "watch": watch, "sort": sort},
                    goal=Q.goal(session, settings))

    @app.get("/p/{pid}", response_class=HTMLResponse)
    def detail(request: Request, pid: int, session: Session = Depends(db)):
        d = Q.property_detail(session, settings, pid)
        if d is None:
            raise HTTPException(404)
        g = Q.goal(session, settings)
        return page(request, "detail.html", d=d, is_goal=g.get("property_id") == pid,
                    statuses=[s.value for s in NoteStatus])

    @app.post("/p/{pid}/note")
    def detail_note(pid: int, status: str = Form("watching"), note: str = Form(""), reject_reason: str = Form(""),
                    actual_sale_price_mad: str = Form(""), remove: str = Form(""), session: Session = Depends(db)):
        if remove:
            api_unnote(pid, session)
        else:
            _save_note(session, pid, NoteIn(status=NoteStatus(status), note=note, reject_reason=reject_reason,
                                            actual_sale_price_mad=int(actual_sale_price_mad)
                                            if actual_sale_price_mad.strip().isdigit() else None))
        return RedirectResponse(f"/p/{pid}", status_code=303)

    @app.post("/p/{pid}/goal")
    def detail_goal(pid: int, session: Session = Depends(db)):
        Q.set_goal(session, pid, None)
        return RedirectResponse(f"/p/{pid}", status_code=303)

    @app.get("/market", response_class=HTMLResponse)
    def market_page(request: Request, session: Session = Depends(db)):
        return page(request, "market.html", zones_data=Q.market(session, settings))

    @app.get("/healthz")
    def health():
        return {"ok": True}

    return app
