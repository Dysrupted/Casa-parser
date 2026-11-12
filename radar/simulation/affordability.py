"""What it takes to buy: income needed for a price, and the highest price a buyer can reach.

Pure functions over the simulation engine (Decimal, no I/O). Two constraints, both from config:
  cash    cash at signing (down payment + acquisition costs + bank fees) <= savings, with the down
          payment at least the scenario's minimum (the lender's loan-to-value cap);
  income  monthly payment + existing debts <= max_debt_ratio x monthly net income.
Amounts are compared after output rounding, so a result always agrees with `simulate()`.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from radar.simulation.engine import SimulationResult, _d, core, simulate
from radar.simulation.params import FinancingParams

D = Decimal
_MAX_DOUBLINGS = 64      # search guard: a price beyond savings x 2^64 means "no upper bound"


def required_income(price_mad, params: FinancingParams, scenario: str | None = None,
                    agency_listing: bool = False, eur_mad: float | None = None) -> dict:
    """Monthly net income needed to carry the loan for `price_mad` at the debt-ratio cap."""
    r = simulate(price_mad, params, agency_listing, eur_mad, scenario=scenario)
    return {"ok": r.ok, "missing_params": r.missing_params, "scenario": r.scenario,
            "price_mad": r.price_mad, "monthly_payment_mad": r.monthly_payment_mad,
            "max_debt_ratio": r.max_debt_ratio, "required_income_mad": r.required_income_mad,
            "required_income_eur": r.required_income_eur, "total_cash_needed_mad": r.total_cash_needed_mad}


def compare_scenarios(price_mad, params: FinancingParams, agency_listing: bool = False,
                      eur_mad: float | None = None) -> list[dict]:
    """One row per configured scenario for the same price (the default scenario first)."""
    names = params.scenario_names() or [None]
    names.sort(key=lambda n: n != params.default_scenario)
    rows = []
    for n in names:
        r = simulate(price_mad, params, agency_listing, eur_mad, scenario=n)
        f = params.for_scenario(n).financing
        rows.append({
            "scenario": n, "label": f.label or n or "default", "profile": f.profile,
            "is_default": n == params.default_scenario, "ok": r.ok, "missing_params": r.missing_params,
            "annual_rate": f.annual_rate, "annual_rate_effective": r.annual_rate_effective,
            "duration_years": f.duration_years, "down_payment_pct": f.down_payment_pct,
            "min_down_payment_pct": f.min_down_payment_pct, "insurance_annual_rate": f.insurance.annual_rate,
            "down_payment_mad": r.down_payment_mad, "loan_amount_mad": r.loan_amount_mad,
            "acquisition_costs_mad": r.acquisition_costs_mad, "total_cash_needed_mad": r.total_cash_needed_mad,
            "total_cash_needed_eur": r.total_cash_needed_eur, "monthly_payment_mad": r.monthly_payment_mad,
            "required_income_mad": r.required_income_mad, "required_income_eur": r.required_income_eur,
            "total_cost_of_credit_mad": r.total_cost_of_credit_mad, "all_in_cost_mad": r.all_in_cost_mad,
        })
    return rows


@dataclass
class MaxPrice:
    ok: bool
    missing_params: list[str] = field(default_factory=list)
    scenario: str | None = None
    max_price_mad: int | None = None
    binding: str | None = None                 # "cash" | "income": what stops one more dirham of price
    min_down_payment_active: bool | None = None  # True = borrowing as much as the lender allows
    savings_mad: float | None = None
    payment_budget_mad: float | None = None    # max monthly payment the income supports (None = no cap)
    down_payment_mad: float | None = None
    simulation: SimulationResult | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def payment_budget_mad(params: FinancingParams, income_eur, eur_mad) -> Decimal | None:
    """Largest monthly payment allowed: max_debt_ratio x income - existing debts (MAD)."""
    a = params.affordability
    if income_eur is None:
        return None
    fx = _d(eur_mad)
    return _d(a.max_debt_ratio) * _d(income_eur) * fx - _d(a.existing_monthly_debt_eur) * fx


def max_price(params: FinancingParams, savings_mad, income_eur=None, eur_mad: float | None = None,
              scenario: str | None = None, agency_listing: bool = False) -> MaxPrice:
    """Highest whole-dirham price reachable with `savings_mad` cash and `income_eur` monthly net income.

    The down payment is free to exceed the scenario minimum: the search puts all the savings that
    are left after costs into it, which is what minimises the monthly payment for a given price.
    `income_eur=None` drops the income constraint (cash only).
    """
    name = scenario or params.default_scenario
    p = params.for_scenario(scenario)
    missing = p.missing()
    if income_eur is not None:
        if p.affordability.max_debt_ratio is None:
            missing.append("affordability.max_debt_ratio")
        if not eur_mad:
            missing.append("eur_mad")
    if missing:
        return MaxPrice(ok=False, missing_params=missing, scenario=name)
    q = D(1).scaleb(-p.rounding.decimals)
    rnd = lambda x: x.quantize(q, rounding=ROUND_HALF_UP)
    savings = _d(savings_mad)
    budget = payment_budget_mad(p, income_eur, eur_mad)
    min_down = _d(p.financing.min_down_payment_pct)

    def cash(price: int, down: int) -> Decimal:
        return rnd(core(price, p, agency_listing, down)["cash"])

    def fit(price: int) -> tuple[str | None, int | None]:
        """(None, down) when `price` is reachable, else (failing constraint, None)."""
        if cash(price, price) <= savings:           # bought outright: no loan, no payment
            return None, price
        lo = int((price * min_down).to_integral_value(rounding=ROUND_CEILING))
        if lo >= price or cash(price, lo) > savings:
            return "cash", None
        hi = price - 1                              # cash() rises with the down payment on [lo, price-1]
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if cash(price, mid) <= savings:
                lo = mid
            else:
                hi = mid - 1
        if budget is not None and rnd(core(price, p, agency_listing, lo)["monthly"]) > budget:
            return "income", None
        return None, lo

    if fit(0)[0] is not None:
        return MaxPrice(ok=True, scenario=name, max_price_mad=0, binding="cash", savings_mad=float(savings),
                        payment_budget_mad=float(budget) if budget is not None else None)
    lo, hi = 0, max(1, int(savings))
    for _ in range(_MAX_DOUBLINGS):                 # grow until out of reach
        if fit(hi)[0] is not None:
            break
        lo, hi = hi, hi * 2
    else:
        return MaxPrice(ok=False, missing_params=["no upper bound: nothing limits the loan"], scenario=name)
    while hi - lo > 1:                              # invariant: lo reachable, hi not
        mid = (lo + hi) // 2
        if fit(mid)[0] is None:
            lo = mid
        else:
            hi = mid
    _, down = fit(lo)
    binding, _ = fit(lo + 1)
    sim = simulate(lo, params, agency_listing, eur_mad, scenario=scenario, down_payment_mad=down)
    floor = int((lo * min_down).to_integral_value(rounding=ROUND_CEILING))
    return MaxPrice(ok=True, scenario=name, max_price_mad=lo, binding=binding,
                    min_down_payment_active=down == floor and down < lo, savings_mad=float(savings),
                    payment_budget_mad=float(rnd(budget)) if budget is not None else None,
                    down_payment_mad=float(down), simulation=sim)
