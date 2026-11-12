"""Core tables (PRD §6). Only M1 tables are defined; later milestones add theirs."""
from __future__ import annotations

from datetime import date, datetime
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
