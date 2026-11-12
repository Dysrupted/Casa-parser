"""Feature extraction: rules first, then LLM, stored per extractor in `propertyfeature`."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime

from sqlmodel import Session, select

from radar.config import norm_text
from radar.llm.provider import LLMProvider
from radar.models import Listing, PropertyFeature
from radar.normalize.rules import extract_rules
from radar.normalize.schema import FEATURE_FIELDS, ExtractedFeatures, verify

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You extract facts from Moroccan real-estate listing descriptions \
(usually French, sometimes Arabic or English) into the given schema.

Rules:
- Report only what the text explicitly states. Never infer from price, zone or photos.
- If a fact is not stated, set value=null, confidence=0, source_span=null.
- source_span must be copied verbatim from the description (exact characters), short \
(a few words) and must contain the evidence for the value.
- confidence: 0.9+ when stated unambiguously, 0.5-0.8 when wording is vague.
- syndic_fees_mad is monthly: convert an annual amount by dividing by 12.
- floor: ground floor (rez-de-chaussée, RDC) is 0.
- sea_view 'none' only when the text says there is no sea view."""


def description_hash(text: str | None) -> str | None:
    return hashlib.sha1(norm_text(text).encode()).hexdigest() if text else None


def _upsert(db: Session, listing: Listing, field: str, value, conf: float, by: str,
            span: str | None, now: datetime) -> None:
    row = db.exec(select(PropertyFeature).where(
        PropertyFeature.property_id == listing.property_id, PropertyFeature.field == field,
        PropertyFeature.extracted_by == by)).first()
    row = row or PropertyFeature(property_id=listing.property_id, field=field, extracted_by=by,
                                 source_listing_id=listing.id, extracted_at=now)
    row.value, row.confidence, row.source_span = value, conf, span
    row.source_listing_id, row.extracted_at = listing.id, now
    db.add(row)


def apply_rules(db: Session, listing: Listing, now: datetime) -> int:
    hits = extract_rules(listing.description, listing.agency)
    for field in FEATURE_FIELDS:
        v, c, span = hits.get(field, (None, 0.0, None))
        _upsert(db, listing, field, v, c, "rules", span, now)
    return len(hits)


def apply_llm(db: Session, listing: Listing, provider: LLMProvider, now: datetime) -> list[str]:
    """Returns the list of fields rejected by span verification. Raises LLMError."""
    feats = provider.extract(ExtractedFeatures, SYSTEM_PROMPT, listing.description or "")
    feats, rejected = verify(feats, listing.description or "")
    if rejected:
        log.warning("listing %s: dropped unverifiable LLM values %s", listing.id, rejected)
    for field in FEATURE_FIELDS:
        f = getattr(feats, field)
        _upsert(db, listing, field, f.value, f.confidence, provider.name, f.source_span, now)
    listing.extracted_hash = listing.description_hash
    db.add(listing)
    return rejected


def resolved_features(db: Session, property_id: int, min_confidence: float) -> dict[str, PropertyFeature]:
    """Best non-null value per field (highest confidence >= threshold)."""
    best: dict[str, PropertyFeature] = {}
    for r in db.exec(select(PropertyFeature).where(PropertyFeature.property_id == property_id)):
        if r.value is None or r.confidence < min_confidence:
            continue
        if r.field not in best or r.confidence > best[r.field].confidence:
            best[r.field] = r
    return best
