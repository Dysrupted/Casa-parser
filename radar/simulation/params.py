"""Simulation parameters. All rates come from config, none from code.

`financing` is the base loan block. `scenarios` are named partial overrides of it (rate, duration,
down payment, insurance, ...); `default_scenario` picks the one the pipeline and `simulate()` use.
A config without scenarios behaves exactly as before: the `financing` block is the scenario.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class Tier(BaseModel):
    """One bracket of a progressive scale: `rate` applies to the slice of the base up to `up_to`."""
    up_to: float | None = None      # None = no upper bound (last bracket)
    rate: float


class CostLine(BaseModel):
    label: str | None = None
    base: Literal["price", "loan"]
    rate: float | None = None
    tiers: list[Tier] | None = None     # progressive brackets; replaces `rate` when set
    fixed_mad: float | None = 0
    min_mad: float | None = None
    max_mad: float | None = None
    vat_rate: float | None = 0
    applies: Literal["always", "agency_listing", "never"] = "always"


class Insurance(BaseModel):
    mode: Literal["initial_capital", "outstanding_capital"]
    annual_rate: float | None = None


class Financing(BaseModel):
    label: str | None = None
    profile: Literal["mre", "resident"] | None = None
    down_payment_pct: float | None = Field(default=None, ge=0, le=1)
    min_down_payment_pct: float = Field(default=0, ge=0, le=1)   # LTV cap: loan <= (1 - min) x price
    finance_acquisition_costs: bool = False
    annual_rate: float | None = Field(default=None, ge=0)        # nominal, before tax on interest
    interest_vat_rate: float = Field(default=0, ge=0)            # tax added on top of the interest
    duration_years: int | None = Field(default=None, gt=0)
    insurance: Insurance
    bank_fees_mad: float | None = None


class InsuranceOverride(BaseModel):
    mode: Literal["initial_capital", "outstanding_capital"] | None = None
    annual_rate: float | None = None


class Scenario(BaseModel):
    """Named override of the `financing` block. Unset fields inherit from it."""
    label: str
    profile: Literal["mre", "resident"]
    down_payment_pct: float | None = Field(default=None, ge=0, le=1)
    min_down_payment_pct: float | None = Field(default=None, ge=0, le=1)
    finance_acquisition_costs: bool | None = None
    annual_rate: float | None = Field(default=None, ge=0)
    interest_vat_rate: float | None = Field(default=None, ge=0)
    duration_years: int | None = Field(default=None, gt=0)
    insurance: InsuranceOverride | None = None
    bank_fees_mad: float | None = None


class Affordability(BaseModel):
    max_debt_ratio: float | None = Field(default=None, gt=0, le=1)   # (loan payment + debts) / net income
    monthly_net_income_eur: float | None = None                      # buyer profile; None = not provided
    existing_monthly_debt_eur: float = 0


class Ownership(BaseModel):
    """Running costs, for display only (not part of the simulation)."""
    syndic_monthly_mad: tuple[float, float] | None = None
    communal_services_tax_rate: float | None = None


class Source(BaseModel):
    url: str
    date: str
    confidence: Literal["high", "medium", "low"]
    note: str | None = None


class FxParams(BaseModel):
    sensitivity_pct: float = Field(ge=0, lt=1)


class Rounding(BaseModel):
    decimals: int = 0


class FinancingParams(BaseModel):
    acquisition_costs: dict[str, CostLine]
    financing: Financing
    scenarios: dict[str, Scenario] = {}
    default_scenario: str | None = None
    affordability: Affordability = Affordability()
    ownership: Ownership = Ownership()
    sources: dict[str, Source] = {}     # dotted config path (or prefix) -> where the value comes from
    fx: FxParams
    rounding: Rounding = Rounding()

    def scenario_names(self) -> list[str]:
        return list(self.scenarios)

    def for_scenario(self, name: str | None = None) -> "FinancingParams":
        """Params whose `financing` block is the named scenario (default one when `name` is None)."""
        name = name or self.default_scenario
        if name is None:
            return self
        if name not in self.scenarios:
            raise KeyError(f"unknown scenario {name!r} (known: {', '.join(self.scenarios) or 'none'})")
        over = self.scenarios[name].model_dump(exclude_none=True)
        ins = self.financing.insurance.model_dump() | over.pop("insurance", {})
        fin = Financing(**(self.financing.model_dump() | over | {"insurance": ins}))
        return self.model_copy(update={"financing": fin, "default_scenario": None, "scenarios": {}})

    def missing(self) -> list[str]:
        """Parameters that must be filled before the simulation can run."""
        out = []
        for name, line in self.acquisition_costs.items():
            if line.applies == "never":
                continue
            for f in ("rate", "fixed_mad", "vat_rate"):
                if getattr(line, f) is None and not (f == "rate" and line.tiers):
                    out.append(f"acquisition_costs.{name}.{f}")
        f = self.for_scenario().financing
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
