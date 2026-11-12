"""Pure purchase-cost simulation (port target: Simulation_achat_immo_MNM.xlsx).

Decimal arithmetic throughout; rounding (Excel ROUND = half away from zero) only on outputs.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from radar.simulation.params import CostLine, FinancingParams

D = Decimal


def _d(x) -> Decimal:
    return x if isinstance(x, Decimal) else D(str(x))


def pmt(rate_per_period, n_periods: int, principal) -> Decimal:
    """Excel PMT (positive sign): constant payment repaying `principal` over n periods."""
    r, p = _d(rate_per_period), _d(principal)
    if n_periods <= 0:
        raise ValueError("n_periods must be > 0")
    if r == 0:
        return p / n_periods
    return p * r / (1 - (1 + r) ** -n_periods)


def amortization_schedule(principal, annual_rate, years: int, insurance_mode: str,
                          insurance_annual_rate) -> list[dict]:
    """Month-by-month rows: interest, principal, insurance, balance (unrounded Decimals)."""
    n, r = years * 12, _d(annual_rate) / 12
    ins_r = _d(insurance_annual_rate) / 12
    pay = pmt(r, n, principal)
    bal, initial, rows = _d(principal), _d(principal), []
    for m in range(1, n + 1):
        interest = bal * r
        princ = pay - interest
        insurance = (initial if insurance_mode == "initial_capital" else bal) * ins_r
        bal -= princ
        rows.append({"month": m, "payment": pay, "interest": interest, "principal": princ,
                     "insurance": insurance, "balance": max(bal, D(0))})
    return rows


@dataclass
class SimulationResult:
    ok: bool
    missing_params: list[str] = field(default_factory=list)
    price_mad: float | None = None
    cost_breakdown_mad: dict[str, float] = field(default_factory=dict)
    acquisition_costs_mad: float | None = None
    down_payment_mad: float | None = None
    loan_amount_mad: float | None = None
    monthly_payment_mad: float | None = None        # loan instalment + insurance
    monthly_instalment_mad: float | None = None     # loan only
    monthly_insurance_mad: float | None = None      # month 1 (constant in initial_capital mode)
    total_interest_mad: float | None = None
    total_insurance_mad: float | None = None
    total_cost_of_credit_mad: float | None = None   # interest + insurance + bank fees
    total_cash_needed_mad: float | None = None      # cash at signing
    all_in_price_mad: float | None = None           # price + acquisition costs
    all_in_cost_mad: float | None = None            # all-in price + cost of credit
    eur_mad: float | None = None
    all_in_price_eur: float | None = None
    all_in_price_eur_band: tuple[float, float] | None = None   # (EUR strong, EUR weak)
    total_cash_needed_eur: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _line_amount(line: CostLine, price: Decimal, loan: Decimal) -> Decimal:
    base = price if line.base == "price" else loan
    amt = _d(line.rate) * base + _d(line.fixed_mad or 0)
    if line.min_mad is not None:
        amt = max(amt, _d(line.min_mad))
    if line.max_mad is not None:
        amt = min(amt, _d(line.max_mad))
    return amt * (1 + _d(line.vat_rate or 0))


def simulate(price_mad, params: FinancingParams, agency_listing: bool = False,
             eur_mad: float | None = None) -> SimulationResult:
    if (missing := params.missing()):
        return SimulationResult(ok=False, missing_params=missing, price_mad=float(price_mad))
    q = D(1).scaleb(-params.rounding.decimals)

    def rnd(x: Decimal) -> float:
        return float(x.quantize(q, rounding=ROUND_HALF_UP))

    f = params.financing
    price = _d(price_mad)
    down = price * _d(f.down_payment_pct)
    base_loan = price - down

    lines = {k: l for k, l in params.acquisition_costs.items()
             if l.applies == "always" or (l.applies == "agency_listing" and agency_listing)}
    # Loan-based costs depend on the loan; if costs are financed, the loan depends on them.
    # Fixed point: loan = base_loan + costs(loan). Converges fast (loan-based rates are tiny).
    loan = base_loan
    for _ in range(50):
        costs = {k: _line_amount(l, price, loan) for k, l in lines.items()}
        new_loan = base_loan + sum(costs.values(), D(0)) if f.finance_acquisition_costs else base_loan
        if abs(new_loan - loan) < D("0.000001"):
            break
        loan = new_loan
    total_costs = sum(costs.values(), D(0))

    sched = amortization_schedule(loan, f.annual_rate, f.duration_years, f.insurance.mode,
                                  f.insurance.annual_rate) if loan > 0 else []
    instalment = sched[0]["payment"] if sched else D(0)
    ins_m1 = sched[0]["insurance"] if sched else D(0)
    tot_interest = sum((r["interest"] for r in sched), D(0))
    tot_ins = sum((r["insurance"] for r in sched), D(0))
    bank = _d(f.bank_fees_mad)
    credit_cost = tot_interest + tot_ins + (bank if loan > 0 else 0)
    cash = down + (0 if f.finance_acquisition_costs else total_costs) + (bank if loan > 0 else 0)
    all_in = price + total_costs

    res = SimulationResult(
        ok=True, price_mad=rnd(price),
        cost_breakdown_mad={k: rnd(v) for k, v in costs.items()},
        acquisition_costs_mad=rnd(total_costs), down_payment_mad=rnd(down), loan_amount_mad=rnd(loan),
        monthly_payment_mad=rnd(instalment + ins_m1), monthly_instalment_mad=rnd(instalment),
        monthly_insurance_mad=rnd(ins_m1), total_interest_mad=rnd(tot_interest),
        total_insurance_mad=rnd(tot_ins), total_cost_of_credit_mad=rnd(credit_cost),
        total_cash_needed_mad=rnd(cash), all_in_price_mad=rnd(all_in),
        all_in_cost_mad=rnd(all_in + credit_cost),
    )
    if eur_mad:
        fx, band = _d(eur_mad), _d(params.fx.sensitivity_pct)
        res.eur_mad = eur_mad
        res.all_in_price_eur = rnd(all_in / fx)
        res.all_in_price_eur_band = (rnd(all_in / (fx * (1 + band))), rnd(all_in / (fx * (1 - band))))
        res.total_cash_needed_eur = rnd(cash / fx)
    return res
