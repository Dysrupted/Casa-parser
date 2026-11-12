"""Typed loading of YAML config. All tunables live in config/*.yaml, never in code."""
from __future__ import annotations

import os
import re
import unicodedata
from functools import cached_property
from pathlib import Path

import yaml
from dotenv import load_dotenv
from typing import Literal

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
    api: float
    photo: float = 2160


class HttpConfig(BaseModel):
    user_agent: str
    min_delay_s: float
    max_delay_s: float
    timeout_s: float
    max_retries: int
    retry_backoff_s: float
    max_retry_after_s: float = 120
    cache_ttl_hours: CacheTTL

    @model_validator(mode="after")
    def _polite(self) -> "HttpConfig":
        if self.min_delay_s < MIN_POLITE_DELAY_S:
            raise ValueError(f"http.min_delay_s must be >= {MIN_POLITE_DELAY_S}")
        if self.max_delay_s < self.min_delay_s:
            raise ValueError("http.max_delay_s must be >= min_delay_s")
        return self


class SaleFilter(BaseModel):
    min_price_mad: float = 0
    exclude_title_regex: str | None = None


class PipelineConfig(BaseModel):
    missing_runs_before_removed: int = Field(ge=1)
    refresh_detail_days: float = Field(gt=0)
    timezone: str = "Africa/Casablanca"  # defines the 'day' for snapshots/events
    sale_filter: SaleFilter = SaleFilter()


class SourceConfig(BaseModel):
    enabled: bool = True
    base_url: str
    search_urls: list[str]
    pagination: str
    max_pages: int = Field(ge=1)
    fetch_details: bool = True
    max_details_per_run: int | None = None            # cap slow detail fetches (site quotas)
    fetcher: Literal["http", "playwright"] = "http"   # playwright only for JS-rendered sites
    id_pattern: str = r"(\d{5,})"                       # regex on the listing URL; group 1 = external id
    selectors: dict[str, list[str]] = {}
    embedded_json: dict | None = None                   # JSON-embedded sites (e.g. __NEXT_DATA__)


class SourcesFile(BaseModel):
    http: HttpConfig
    pipeline: PipelineConfig
    sources: dict[str, SourceConfig]


class LlmConfig(BaseModel):
    enabled: bool
    provider: str
    model: str
    effort: str
    max_tokens: int
    max_extractions_per_run: int = Field(ge=0)
    min_confidence: float = Field(ge=0, le=1)


LatLon = tuple[float, float]


class BBox(BaseModel):
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

    def contains(self, p: LatLon) -> bool:
        return self.lat_min <= p[0] <= self.lat_max and self.lon_min <= p[1] <= self.lon_max


class NominatimConfig(BaseModel):
    enabled: bool
    url: str


class GeoConfig(BaseModel):
    zone_centroids: dict[str, LatLon]
    corniche_polyline: list[LatLon] = Field(min_length=2)
    bbox: BBox
    nominatim: NominatimConfig


class FxConfig(BaseModel):
    url: str
    json_path: list[str]
    max_staleness_days: int = Field(ge=0)
    sanity_range: tuple[float, float]
    manual_rate: float | None = None


class EnrichFile(BaseModel):
    llm: LlmConfig
    geo: GeoConfig
    fx: FxConfig


class QuietHours(BaseModel):
    start: str
    end: str

    def contains(self, hhmm: str) -> bool:
        """True if local time `HH:MM` is inside the window (handles windows crossing midnight)."""
        if self.start == self.end:
            return False
        if self.start < self.end:
            return self.start <= hhmm < self.end
        return hhmm >= self.start or hhmm < self.end


class AlertTriggers(BaseModel):
    new_high_score: bool = True
    price_drop: bool = True
    watch_removed: bool = True
    run_errors: bool = True


class AlertsConfig(BaseModel):
    enabled: bool
    mode: Literal["realtime", "digest"]
    score_threshold: float = Field(ge=0, le=100)
    price_drop_pct: float = Field(gt=0, lt=1)
    triggers: AlertTriggers
    quiet_hours: QuietHours | None = None
    max_alerts_per_run: int = Field(ge=1)
    max_send_attempts: int = Field(ge=1)


class DedupeCandidate(BaseModel):
    same_zone: bool = True
    surface_tolerance_m2: float = Field(ge=0)
    price_tolerance_pct: float = Field(ge=0, lt=1)


class DedupeConfirm(BaseModel):
    phash_max_distance: int = Field(ge=0, le=64)
    photos_per_listing: int = Field(ge=0)
    description_min_similarity: float = Field(ge=0, le=1)
    description_min_chars: int = Field(ge=0)


class DedupeConfig(BaseModel):
    enabled: bool
    candidate: DedupeCandidate
    confirm: DedupeConfirm
    recheck_rejected_after_days: int = Field(ge=1)


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_text(s: str) -> str:
    """Lowercase, accent-free, punctuation -> space ("Aïn-Diab, Casablanca" -> "ain diab casablanca")."""
    return " ".join(re.sub(r"[^\w]+", " ", strip_accents(s).lower()).split())


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
        enrich = EnrichFile(**self._load("enrich.yaml"))
        self.llm, self.geo, self.fx = enrich.llm, enrich.geo, enrich.fx
        from radar.simulation.params import FinancingParams  # local: avoid import cycle
        self.financing = FinancingParams(**self._load("financing.yaml"))
        self.scoring = self._load("scoring.yaml")
        self.alerts = AlertsConfig(**self._load("alerts.yaml"))
        self.dedupe = DedupeConfig(**self._load("dedupe.yaml"))
        self.telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN") or None
        self.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID") or None
        _validate_scoring(self.scoring)

    def _load(self, name: str) -> dict:
        with open(self.config_dir / name, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'radar.db'}"

    @property
    def target_zones(self) -> set[str]:
        return set(self.profile.zones)


def _validate_scoring(cfg: dict) -> None:
    """Fail fast on a malformed scoring.yaml (kept as a dict for easy tuning)."""
    need = {"price_vs_zone", "zone_preference", "beach_distance", "features", "building", "market_signals"}
    if set(cfg.get("weights", {})) != need:
        raise ValueError(f"scoring.weights must define exactly {sorted(need)}")
    if any(w < 0 for w in cfg["weights"].values()) or sum(cfg["weights"].values()) <= 0:
        raise ValueError("scoring.weights must be non-negative with a positive sum")
    if set(cfg["market_index"]["trend_days"]) - {30, 90}:
        raise ValueError("market_index.trend_days supports 30 and 90 (zone_index columns)")
