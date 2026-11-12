"""Core tables (PRD §6). Only M1 tables are defined; later milestones add theirs."""
from __future__ import annotations

from datetime import date, datetime
from datetime import date as date_
from typing import Any
from enum import StrEnum

from sqlalchemy import Column, JSON, UniqueConstraint
from sqlmodel import Field, SQLModel


class Status(StrEnum):
    active = "active"
    missing = "missing"      # absent from >=1 complete crawl, not yet considered removed
    removed = "removed"      # likely sold / withdrawn


class EventType(StrEnum):
    new = "new"
    price_change = "price_change"
    missing = "missing"
    removed = "removed"
    relisted = "relisted"


class Property(SQLModel, table=True):
    """A physical apartment. 1:1 with listing until dedupe (M6) merges cross-site duplicates."""
    id: int | None = Field(default=None, primary_key=True)
    zone: str | None = Field(default=None, index=True)
    surface_m2: float | None = None
    rooms: int | None = None
    bedrooms: int | None = None
    lat: float | None = None
    lon: float | None = None
    geo_precision: str | None = None      # page | address | zone  (best -> worst)
    dist_corniche_m: float | None = None
    first_seen: datetime
    last_seen: datetime
    status: Status = Status.active


class Listing(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: int | None = Field(default=None, primary_key=True)
    property_id: int = Field(foreign_key="property.id", index=True)
    source: str = Field(index=True)
    external_id: str
    url: str
    title: str | None = None
    agency: str | None = None
    location_raw: str | None = None
    zone: str | None = None
    price_mad: int | None = None
    surface_m2: float | None = None
    rooms: int | None = None
    bedrooms: int | None = None
    bathrooms: int | None = None
    description: str | None = None
    description_hash: str | None = None   # extraction re-runs only when this changes
    extracted_hash: str | None = None     # description_hash the last extraction ran on
    address_raw: str | None = None
    page_lat: float | None = None         # coordinates embedded in the detail page, if any
    page_lon: float | None = None
    photos: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    raw_html_path: str | None = None
    detail_fetched_at: datetime | None = None
    first_seen: datetime
    last_seen: datetime
    status: Status = Status.active
    missing_runs: int = 0
    relist_count: int = 0


class ListingSnapshot(SQLModel, table=True):
    """One row per listing per calendar day. Same-day reruns overwrite (idempotency)."""
    __table_args__ = (UniqueConstraint("listing_id", "run_date"),)

    id: int | None = Field(default=None, primary_key=True)
    listing_id: int = Field(foreign_key="listing.id", index=True)
    run_id: int = Field(foreign_key="run.id")
    run_date: date
    price_mad: int | None = None
    status: Status


class ListingEvent(SQLModel, table=True):
    """Change log consumed by alerts (M5): new, price drops, removals, relists."""
    __table_args__ = (UniqueConstraint("listing_id", "run_date", "type"),)

    id: int | None = Field(default=None, primary_key=True)
    listing_id: int = Field(foreign_key="listing.id", index=True)
    run_id: int = Field(foreign_key="run.id")
    run_date: date
    type: EventType
    old_value: int | None = None
    new_value: int | None = None


class Run(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    started_at: datetime
    finished_at: datetime | None = None
    status: str = "running"   # running | ok | partial | failed
    stats: dict = Field(default_factory=dict, sa_column=Column(JSON))


class PropertyFeature(SQLModel, table=True):
    """Extracted attribute. One row per (property, field, extractor); null value = not found."""
    __table_args__ = (UniqueConstraint("property_id", "field", "extracted_by"),)

    id: int | None = Field(default=None, primary_key=True)
    property_id: int = Field(foreign_key="property.id", index=True)
    source_listing_id: int = Field(foreign_key="listing.id")
    field: str
    value: Any = Field(default=None, sa_column=Column(JSON))
    confidence: float = 0.0
    extracted_by: str                     # rules | llm:<model>
    source_span: str | None = None        # verbatim evidence from the description
    extracted_at: datetime


class FxRate(SQLModel, table=True):
    date: date_ = Field(primary_key=True)
    eur_mad: float
    source: str


class Simulation(SQLModel, table=True):
    """Latest purchase simulation per property and parameter set (PRD F6)."""
    __table_args__ = (UniqueConstraint("property_id", "params_hash"),)

    id: int | None = Field(default=None, primary_key=True)
    property_id: int = Field(foreign_key="property.id", index=True)
    params_hash: str
    price_mad: int
    ok: bool
    outputs: dict = Field(default_factory=dict, sa_column=Column(JSON))
    computed_at: datetime


class ZoneIndex(SQLModel, table=True):
    """Daily asking-price/m² stats per zone (one price per property, outliers removed)."""
    zone: str = Field(primary_key=True)
    date: date_ = Field(primary_key=True)
    median_m2: float
    p25: float
    p75: float
    count: int
    reliable: bool = False
    trend_30d: float | None = None   # median vs the latest index >= 30 days older
    trend_90d: float | None = None


class Score(SQLModel, table=True):
    """Explainable score, one row per property per day (history kept)."""
    __table_args__ = (UniqueConstraint("property_id", "date"),)

    id: int | None = Field(default=None, primary_key=True)
    property_id: int = Field(foreign_key="property.id", index=True)
    date: date_
    computed_at: datetime
    total: float
    in_profile: bool
    breakdown: dict = Field(default_factory=dict, sa_column=Column(JSON))
