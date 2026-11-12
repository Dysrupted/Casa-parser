"""Typed loading of YAML config. All tunables live in config/*.yaml, never in code."""
from __future__ import annotations

import os
import unicodedata
from functools import cached_property
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

MIN_POLITE_DELAY_S = 3.0  # PRD F1 floor; config may only go above it.


class Range(BaseModel):
    min: float | None = None
    max: float | None = None


class SearchProfile(BaseModel):
    city: str
    surface_m2: Range
    zones: dict[str, float]
    property_type: str
    max_price_mad: float | None = None
    must_have: list[str] = []
    nice_to_have: list[str] = []


class CacheTTL(BaseModel):
    search: float
    detail: float
    robots: float


class HttpConfig(BaseModel):
    user_agent: str
    min_delay_s: float
    max_delay_s: float
    timeout_s: float
    max_retries: int
    retry_backoff_s: float
    cache_ttl_hours: CacheTTL

    @model_validator(mode="after")
    def _polite(self) -> "HttpConfig":
        if self.min_delay_s < MIN_POLITE_DELAY_S:
            raise ValueError(f"http.min_delay_s must be >= {MIN_POLITE_DELAY_S}")
        if self.max_delay_s < self.min_delay_s:
            raise ValueError("http.max_delay_s must be >= min_delay_s")
        return self


class PipelineConfig(BaseModel):
    missing_runs_before_removed: int = Field(ge=1)
    refresh_detail_days: float = Field(gt=0)
    timezone: str = "Africa/Casablanca"  # defines the 'day' for snapshots/events


class SourceConfig(BaseModel):
    enabled: bool = True
    base_url: str
    search_urls: list[str]
    pagination: str
    max_pages: int = Field(ge=1)
    fetch_details: bool = True
    selectors: dict[str, list[str]]


class SourcesFile(BaseModel):
    http: HttpConfig
    pipeline: PipelineConfig
    sources: dict[str, SourceConfig]


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_text(s: str) -> str:
    return " ".join(strip_accents(s).lower().replace("-", " ").split())


class ZoneMap(BaseModel):
    aliases: dict[str, list[str]]

    @field_validator("aliases")
    @classmethod
    def _non_empty(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        if not v:
            raise ValueError("zones.yaml is empty")
        return v

    @cached_property
    def _ordered(self) -> list[tuple[str, str]]:
        pairs = [(norm_text(a), z) for z, al in self.aliases.items() for a in [z.replace("_", " "), *al]]
        return sorted(set(pairs), key=lambda p: -len(p[0]))

    def resolve(self, location: str | None) -> str | None:
        """Map a free-text location ("Aïn Diab à Casablanca") to a canonical zone, or None."""
        if not location:
            return None
        text = f" {norm_text(location)} "
        for alias, zone in self._ordered:
            if f" {alias} " in text:
                return zone
        return None


class Settings:
    def __init__(self, config_dir: Path | None = None, data_dir: Path | None = None):
        load_dotenv()
        self.config_dir = Path(config_dir or os.environ.get("RADAR_CONFIG_DIR") or "config")
        self.data_dir = Path(data_dir or os.environ.get("RADAR_DATA_DIR") or "data")
        self.profile = SearchProfile(**self._load("search_profile.yaml"))
        src = SourcesFile(**self._load("sources.yaml"))
        self.http, self.pipeline, self.sources = src.http, src.pipeline, src.sources
        contact = os.environ.get("RADAR_CONTACT")
        if contact:
            self.http.user_agent = f"{self.http.user_agent} contact:{contact}"
        self.zones = ZoneMap(aliases=self._load("zones.yaml"))

    def _load(self, name: str) -> dict:
        with open(self.config_dir / name, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'radar.db'}"

    @property
    def target_zones(self) -> set[str]:
        return set(self.profile.zones)
