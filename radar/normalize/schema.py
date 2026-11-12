"""Strict extraction schema (PRD F2). Every field: value | null, confidence, verbatim span.

`null` means "not stated in the text". It is never a guess. A value without a verifiable
span is discarded by `verify()`.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from radar.config import norm_text


class _Fact(BaseModel):
    confidence: float = Field(description="0-1. 0 when value is null.")
    source_span: str | None = Field(
        description="Exact substring copied verbatim from the description that states the value. "
                    "null when value is null.")


class BoolFact(_Fact):
    value: bool | None


class IntFact(_Fact):
    value: int | None


class FloatFact(_Fact):
    value: float | None


class StrFact(_Fact):
    value: str | None


class SeaViewFact(_Fact):
    value: Literal["full", "partial", "none"] | None = Field(
        description="'none' only if the text says there is no sea view; null if not mentioned.")


class ConditionFact(_Fact):
    value: Literal["new", "good", "to_renovate"] | None


class SellerFact(_Fact):
    value: Literal["agency", "private"] | None


class ExtractedFeatures(BaseModel):
    floor: IntFact = Field(description="Floor number of the apartment. Ground floor (RDC) = 0.")
    total_floors: IntFact = Field(description="Number of floors in the building.")
    sea_view: SeaViewFact
    parking: BoolFact = Field(description="Private parking / garage / box included.")
    elevator: BoolFact
    terrace_m2: FloatFact = Field(description="Terrace area in m². Only if a number is given.")
    building_year: IntFact = Field(description="Construction/delivery year, if stated.")
    building_age_years: IntFact = Field(description="Building age in years, if stated as an age.")
    condition: ConditionFact
    syndic_fees_mad: IntFact = Field(description="Monthly building/syndic fees in MAD.")
    orientation: StrFact = Field(description="Exposure, e.g. 'sud', 'sud-ouest'.")
    agency_or_private: SellerFact
    concierge: BoolFact = Field(description="Concierge / gardien / 24h security.")


FEATURE_FIELDS: tuple[str, ...] = tuple(ExtractedFeatures.model_fields)


def verify(feats: ExtractedFeatures, description: str) -> tuple[ExtractedFeatures, list[str]]:
    """Null out any value whose span is missing or not literally in the description."""
    hay = norm_text(description)
    rejected: list[str] = []
    for name in FEATURE_FIELDS:
        f: _Fact = getattr(feats, name)
        f.confidence = min(max(f.confidence, 0.0), 1.0)
        if f.value is None:  # type: ignore[attr-defined]
            f.confidence, f.source_span = 0.0, None
            continue
        if not f.source_span or norm_text(f.source_span) not in hay:
            rejected.append(name)
            f.value, f.confidence, f.source_span = None, 0.0, None  # type: ignore[attr-defined]
    return feats, rejected
