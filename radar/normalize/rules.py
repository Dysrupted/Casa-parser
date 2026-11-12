"""Deterministic (regex) feature extraction for French listing text. Runs before the LLM.

High precision, modest recall. Each hit returns (value, confidence, verbatim span).
"""
from __future__ import annotations

import re
from typing import Any

Hit = tuple[Any, float, str]
_F = re.I | re.U

_NEG = r"(?:sans|pas d['e]|pas de|aucun(?:e)?)\s+"


def _bool(text: str, pos: str, conf: float = 0.9) -> Hit | None:
    if m := re.search(_NEG + pos, text, _F):
        return False, conf, m.group(0)
    if m := re.search(rf"\b{pos}", text, _F):
        return True, conf, m.group(0)
    return None


def _int(s: str) -> int:
    return int(re.sub(r"[^\d]", "", s))


def extract_rules(text: str | None, agency: str | None = None) -> dict[str, Hit]:
    t = text or ""
    out: dict[str, Hit] = {}

    if h := _bool(t, r"ascenseurs?\b"):
        out["elevator"] = h
    if h := _bool(t, r"(?:parkings?|garages?|box|place de stationnement)\b"):
        out["parking"] = h
    if h := _bool(t, r"(?:concierge|gardien(?:nage)?|s[ée]curit[ée] 24)"):
        out["concierge"] = h

    if m := re.search(r"vue\s+(?:sur\s+(?:la\s+)?)?(?:mer|oc[ée]an)(?:\s+(partielle|lat[ée]rale|imprenable|panoramique|d[ée]gag[ée]e))?", t, _F):
        partial = bool(m.group(1)) and m.group(1).lower().startswith(("partiel", "lat"))
        out["sea_view"] = ("partial" if partial else "full", 0.85, m.group(0))
    elif m := re.search(r"(?:aper[çc]u|vue partielle)\s+(?:sur\s+(?:la\s+)?)?mer", t, _F):
        out["sea_view"] = ("partial", 0.85, m.group(0))

    if m := re.search(r"terrasse\s*(?:de|d'environ|:)?\s*(\d+(?:[.,]\d+)?)\s*m(?:²|2)", t, _F):
        out["terrace_m2"] = (float(m.group(1).replace(",", ".")), 0.9, m.group(0))

    if m := re.search(r"\b(rez[- ]de[- ]chauss[ée]e|rdc)\b", t, _F):
        out["floor"] = (0, 0.9, m.group(0))
    elif m := re.search(r"\b(\d{1,2})\s*(?:er|[èe]me|e|°)\s+[ée]tage", t, _F):
        out["floor"] = (int(m.group(1)), 0.9, m.group(0))
    if m := re.search(r"[ée]tage\s+sur\s+(\d{1,2})\b", t, _F) or re.search(
            r"immeuble\s+(?:de\s+)?(\d{1,2})\s+[ée]tages", t, _F):
        out["total_floors"] = (int(m.group(1)), 0.85, m.group(0))

    if m := re.search(r"syndic[^\d]{0,25}(\d[\d\s.]{1,7}\d|\d+)\s*(?:dh|mad|dirhams?)(\s*(?:/|par)\s*(mois|an|ann[ée]e))?", t, _F):
        amount, period = _int(m.group(1)), (m.group(3) or "").lower()
        if period.startswith("an"):
            out["syndic_fees_mad"] = (round(amount / 12), 0.8, m.group(0))
        else:
            out["syndic_fees_mad"] = (amount, 0.9 if period else 0.6, m.group(0))

    if m := re.search(r"(?:construit|construction|livr[ée]e?)\s+en\s+((?:19|20)\d{2})\b", t, _F):
        out["building_year"] = (int(m.group(1)), 0.9, m.group(0))

    # Order matters: "refait à neuf" must not read as "neuf".
    if m := re.search(r"([àa] r[ée]nover|travaux [àa] pr[ée]voir|[àa] rafra[iî]chir)", t, _F):
        out["condition"] = ("to_renovate", 0.85, m.group(0))
    elif m := re.search(r"(refait [àa] neuf|enti[èe]rement r[ée]nov[ée]e?|r[ée]nov[ée]e?\b|(?:tr[èe]s )?bon [ée]tat)", t, _F):
        out["condition"] = ("good", 0.8, m.group(0))
    elif m := re.search(r"\b(jamais habit[ée]e?|r[ée]sidence neuve|neuf|neuve)\b", t, _F):
        out["condition"] = ("new", 0.75, m.group(0))

    if m := re.search(r"orient[ée]e?\s+(?:plein\s+)?((?:sud|nord)(?:[- ](?:est|ouest))?|est|ouest)\b", t, _F):
        out["orientation"] = (m.group(1).lower(), 0.85, m.group(0))

    if m := re.search(r"\b(particulier|sans interm[ée]diaire|pas d'agence)\b", t, _F):
        out["agency_or_private"] = ("private", 0.8, m.group(0))
    elif agency:
        out["agency_or_private"] = ("agency", 0.8, agency)  # span = listing's agency field
    return out
