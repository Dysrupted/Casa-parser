"""Explainable 0-100 score (PRD F5). Pure function over a `ScoreInput`; no DB access here."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ZoneStats:
    p25: float
    median: float
    p75: float
    count: int
    reliable: bool


@dataclass
class ScoreInput:
    zone: str | None
    price_mad: int | None
    surface_m2: float | None
    zone_stats: ZoneStats | None
    dist_corniche_m: float | None
    geo_precision: str | None
    features: dict = field(default_factory=dict)   # resolved field -> value
    days_on_market: int = 0
    price_drop_pct: float = 0.0                    # cumulative drop over lookback (0.08 = -8 %)
    relist_count: int = 0
    has_photos: bool = False
    has_description: bool = False
    current_year: int = 2026

    @property
    def price_m2(self) -> float | None:
        if self.price_mad and self.surface_m2:
            return self.price_mad / self.surface_m2
        return None


@dataclass
class Factor:
    name: str
    weight: float          # share of 100
    value: float | None    # 0-1; None = could not compute (neutral used)
    points: float
    reason: str


@dataclass
class ScoreResult:
    total: float
    in_profile: bool
    profile_notes: list[str]
    factors: list[Factor]
    penalty: float
    penalty_reasons: list[str]
    flags: list[str]
    price_vs_zone: float | None

    def top_reasons(self, n: int = 3) -> list[str]:
        """Reasons of the factors that contributed most, for alerts."""
        ranked = sorted((f for f in self.factors if f.value is not None and f.value > 0),
                        key=lambda f: -f.points)
        return [f.reason for f in ranked[:n]]

    def to_dict(self) -> dict:
        return {"total": self.total, "in_profile": self.in_profile, "profile_notes": self.profile_notes,
                "price_vs_zone": self.price_vs_zone, "penalty": self.penalty,
                "penalty_reasons": self.penalty_reasons, "flags": self.flags,
                "factors": [f.__dict__ for f in self.factors], "top_reasons": self.top_reasons()}


def interp(x: float, xs: list[float], ys: list[float]) -> float:
    """Piecewise-linear interpolation, clamped at both ends. xs ascending."""
    if x <= xs[0]:
        return ys[0]
    for (x0, y0), (x1, y1) in zip(zip(xs, ys), zip(xs[1:], ys[1:])):
        if x <= x1:
            return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return ys[-1]


def _zone_label(z: str | None) -> str:
    return (z or "?").replace("_", " ").title()


def _feature_presence(inp: ScoreInput, cfg: dict) -> dict[str, float | None]:
    """Each nice-to-have -> 1/partial/0, or None if unknown."""
    f, fc, bc = inp.features, cfg["factors"]["features"], cfg["factors"]["building"]
    out: dict[str, float | None] = {}
    sv = f.get("sea_view")
    out["sea_view"] = None if sv is None else {"full": 1.0, "partial": fc["partial_sea_view"]}.get(sv, 0.0)
    for k in ("parking", "elevator", "concierge"):
        out[k] = None if f.get(k) is None else float(bool(f[k]))
    t = f.get("terrace_m2")
    out["terrace"] = None if t is None else float(t >= fc["min_terrace_m2"])
    age = _age(inp, f)
    if age is not None:
        out["recent_building"] = float(age <= bc["recent_max_age_years"])
    elif f.get("condition") == "new":
        out["recent_building"] = 1.0
    else:
        out["recent_building"] = None
    return out


def _age(inp: ScoreInput, f: dict) -> int | None:
    if f.get("building_year"):
        return max(0, inp.current_year - int(f["building_year"]))
    if f.get("building_age_years") is not None:
        return int(f["building_age_years"])
    return None


def check_profile(inp: ScoreInput, profile) -> tuple[bool, list[str]]:
    notes, ok = [], True
    s = profile.surface_m2
    if inp.surface_m2 is None:
        notes.append("surface unknown")
    elif (s.min is not None and inp.surface_m2 < s.min) or (s.max is not None and inp.surface_m2 > s.max):
        ok = False
        notes.append(f"surface {inp.surface_m2:g} m² outside {s.min:g}-{s.max:g}")
    if profile.max_price_mad is not None and inp.price_mad and inp.price_mad > profile.max_price_mad:
        ok = False
        notes.append(f"price above max {profile.max_price_mad:,.0f} MAD")
    if inp.zone not in profile.zones:
        ok = False
        notes.append("zone not in profile")
    return ok, notes


def score(inp: ScoreInput, cfg: dict, profile) -> ScoreResult:
    W = sum(cfg["weights"].values())
    neutral = cfg["missing_factor_value"]
    factors: list[Factor] = []
    flags: list[str] = []

    def add(name: str, value: float | None, reason: str) -> None:
        w = cfg["weights"][name] / W * 100
        v = neutral if value is None else max(0.0, min(1.0, value))
        factors.append(Factor(name, round(w, 2), None if value is None else round(v, 3), round(w * v, 2), reason))

    # 1. Price vs zone
    pvz = None
    zs, pm2 = inp.zone_stats, inp.price_m2
    if zs and pm2 and zs.reliable:
        a = cfg["factors"]["price_vs_zone"]["anchors"]
        pvz = round(pm2 / zs.median, 3)
        if zs.p75 > zs.p25:
            v = interp(pm2, [zs.p25, zs.median, zs.p75], [a["p25"], a["median"], a["p75"]])
        else:  # degenerate distribution (all equal): at the median = median score, not best-in-zone
            v = a["median"] if pm2 == zs.median else (a["p25"] if pm2 < zs.median else a["p75"])
        pct = (pvz - 1) * 100
        add("price_vs_zone", v, f"{abs(pct):.0f}% {'below' if pct < 0 else 'above'} {_zone_label(inp.zone)} "
                                f"median ({pm2:,.0f} vs {zs.median:,.0f} MAD/m²)")
    else:
        if zs and pm2:
            pvz = round(pm2 / zs.median, 3)
        why = "no price/surface" if not pm2 else f"zone index unreliable (n={zs.count if zs else 0})"
        add("price_vs_zone", None, f"price vs zone: {why}")

    # 2. Zone preference
    zw = profile.zones.get(inp.zone)
    add("zone_preference", zw, f"zone {_zone_label(inp.zone)} (preference {zw if zw is not None else '-'})")

    # 3. Beach distance
    bd = cfg["factors"]["beach_distance"]
    if inp.dist_corniche_m is not None:
        v = interp(inp.dist_corniche_m, [bd["full_score_within_m"], bd["zero_score_beyond_m"]], [1.0, 0.0])
        approx = " (zone-level estimate)" if inp.geo_precision == "zone" else ""
        add("beach_distance", v, f"{inp.dist_corniche_m:,.0f} m from the Corniche{approx}")
    else:
        add("beach_distance", None, "distance to Corniche unknown")

    # 4. Features (nice-to-have)
    fw = cfg["factors"]["features"]["weights"]
    pres = _feature_presence(inp, cfg)
    wanted = {k: w for k, w in fw.items() if k in profile.nice_to_have or k == "elevator"}
    got = sum(w * (pres.get(k) or 0) for k, w in wanted.items())
    have = [k.replace("_", " ") + (" (partial)" if k == "sea_view" and 0 < (pres[k] or 0) < 1 else "")
            for k in wanted if pres.get(k)]
    add("features", got / sum(wanted.values()) if wanted else None,
        ", ".join(have) if have else "no wanted features confirmed")

    # 5. Building
    bc = cfg["factors"]["building"]
    parts, bits = [], []
    if (cond := inp.features.get("condition")) in bc["condition"]:
        parts.append(bc["condition"][cond])
        bits.append(cond.replace("_", " "))
    if (age := _age(inp, inp.features)) is not None:
        parts.append(interp(age, [0, bc["old_age_years"]], [1.0, 0.0]))
        bits.append(f"~{age} years old")
    add("building", sum(parts) / len(parts) if parts else None,
        "building: " + (", ".join(bits) if bits else "age/condition unknown"))

    # 6. Market signals
    mc = cfg["factors"]["market_signals"]
    drop = interp(inp.price_drop_pct, [0, mc["price_drop_full_score_pct"]], [0, 1])
    dom = interp(inp.days_on_market, [0, mc["dom_full_score_days"]], [0, 1])
    v = mc["dom_weight"] * dom + (1 - mc["dom_weight"]) * drop
    sig = []
    if inp.price_drop_pct > 0:
        sig.append(f"price cut {inp.price_drop_pct:.0%} in {mc['price_drop_lookback_days']} d")
    sig.append(f"{inp.days_on_market} days on market")
    add("market_signals", v, ", ".join(sig))
    if inp.relist_count > mc["relist_suspicious_count"]:
        flags.append(f"relisted {inp.relist_count}× (possibly stale/fake)")

    # Penalty: data quality
    checks = {"price": bool(inp.price_mad), "surface": bool(inp.surface_m2), "photos": inp.has_photos,
              "description": inp.has_description, "geo_precise": inp.geo_precision in ("page", "address")}
    active = cfg["penalties"]["checks"]
    failed = [c for c in active if not checks[c]]
    penalty = round(cfg["penalties"]["data_quality_max"] * len(failed) / len(active), 2) if active else 0

    # Profile fit + must-haves
    ok, notes = check_profile(inp, profile)
    for m in profile.must_have:
        val = pres.get(m, inp.features.get(m))
        if val is None:
            notes.append(f"must-have '{m}' unverified")
        elif not val:
            ok = False
            notes.append(f"must-have '{m}' missing")

    total = max(0.0, min(100.0, sum(f.points for f in factors) - penalty))
    return ScoreResult(round(total, 1), ok, notes, factors, penalty,
                       [f"missing {c}" for c in failed], flags, pvz)
