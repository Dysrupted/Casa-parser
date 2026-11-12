"""Simulation parameters. All rates come from config (the workbook), none from code."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class CostLine(BaseModel):
    base: Literal["price", "loan"]
    rate: float | None = None
    fixed_mad: float | None = 0
    min_mad: float | None = None
    max_mad: float | None = None
    vat_rate: float | None = 0
    applies: Literal["always", "agency_listing", "never"] = "always"


class Insurance(BaseModel):
    mode: Literal["initial_capital", "outstanding_capital"]
    annual_rate: float | None = None


class Financing(BaseModel):
    down_payment_pct: float | None = Field(default=None, ge=0, le=1)
    finance_acquisition_costs: bool = False
    annual_rate: float | None = Field(default=None, ge=0)
    duration_years: int | None = Field(default=None, gt=0)
    insurance: Insurance
    bank_fees_mad: float | None = None


class FxParams(BaseModel):
    sensitivity_pct: float = Field(ge=0, lt=1)


class Rounding(BaseModel):
    decimals: int = 0


class FinancingParams(BaseModel):
    acquisition_costs: dict[str, CostLine]
    financing: Financing
    fx: FxParams
    rounding: Rounding = Rounding()

    def missing(self) -> list[str]:
        """Parameters that must be filled before the simulation can run."""
        out = []
        for name, line in self.acquisition_costs.items():
            if line.applies == "never":
                continue
            for f in ("rate", "fixed_mad", "vat_rate"):
                if getattr(line, f) is None:
                    out.append(f"acquisition_costs.{name}.{f}")
        f = self.financing
        for k in ("down_payment_pct", "annual_rate", "duration_years", "bank_fees_mad"):
            if getattr(f, k) is None:
                out.append(f"financing.{k}")
        if f.insurance.annual_rate is None:
            out.append("financing.insurance.annual_rate")
        return out

    def hash(self) -> str:
        return hashlib.sha1(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()[:16]


def load_params(path: Path) -> FinancingParams:
    with open(path, encoding="utf-8") as fh:
        return FinancingParams(**yaml.safe_load(fh))
