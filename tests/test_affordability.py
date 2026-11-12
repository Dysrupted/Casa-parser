"""Affordability tests: scenarios, loan-to-value cap, income needed, highest reachable price.

SYNTHETIC parameters (round numbers, NOT Moroccan rates) so every expectation can be checked by
hand; the arithmetic is written next to each assertion. Shipped-config behaviour is tested
separately at the end.
"""
import json
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from radar.api.queries import calc_config
from radar.simulation import (FinancingParams, compare_scenarios, load_params, max_price, pmt, required_income,
                              simulate)

ROOT = Path(__file__).resolve().parent.parent
CALC_JS = ROOT / "radar" / "api" / "templates" / "finance_calc.js"
PARITY = ROOT / "tests" / "fixtures" / "simulation" / "calc_parity.json"

SYNTH = {
    "acquisition_costs": {
        "registration_duties": {"base": "price", "rate": 0.01, "fixed_mad": 0, "vat_rate": 0},
        "land_registry": {"base": "price", "rate": 0.01, "fixed_mad": 100, "vat_rate": 0},
        "notary_fees": {"base": "price", "rate": 0.01, "fixed_mad": 0, "vat_rate": 0.10},
        "mortgage_registration": {"base": "loan", "rate": 0.01, "fixed_mad": 0, "vat_rate": 0},
        "agency_fee": {"base": "price", "rate": 0.02, "fixed_mad": 0, "vat_rate": 0.10,
                       "applies": "agency_listing"},
    },
    "financing": {"down_payment_pct": 0.20, "min_down_payment_pct": 0.20, "finance_acquisition_costs": False,
                  "annual_rate": 0.06, "duration_years": 20,
                  "insurance": {"mode": "initial_capital", "annual_rate": 0.003}, "bank_fees_mad": 1000},
    "default_scenario": "base",
    "scenarios": {
        "base": {"label": "Base", "profile": "mre"},
        "long": {"label": "Long", "profile": "mre", "duration_years": 25, "annual_rate": 0.05,
                 "insurance": {"annual_rate": 0.0024}},
        "resident": {"label": "Resident", "profile": "resident", "down_payment_pct": 0.10,
                     "min_down_payment_pct": 0},
    },
    "affordability": {"max_debt_ratio": 0.40, "existing_monthly_debt_eur": 0},
    "fx": {"sensitivity_pct": 0.05},
    "rounding": {"decimals": 0},
}
# Loan payment per dirham borrowed in SYNTH "base": PMT(0.5 %, 240) + insurance 0.3 %/12
#   = 0.0071643106 + 0.00025 = 0.0074143106
K_BASE = float(pmt(Decimal("0.005"), 240, 1)) + 0.003 / 12


def params(**over) -> FinancingParams:
    d = yaml.safe_load(yaml.safe_dump(SYNTH))
    for k, v in over.items():
        d[k] = d[k] | v if isinstance(v, dict) and isinstance(d.get(k), dict) else v
    return FinancingParams(**d)


# ---- scenarios

def test_default_scenario_is_used_and_named():
    r = simulate(1_000_000, params(), eur_mad=10.0)
    assert r.scenario == "base" and r.monthly_payment_mad == 5_931      # same as the engine's hand-checked case
    assert r.min_down_payment_ok is True and r.down_payment_pct == 0.2


def test_scenario_overrides_only_what_it_sets():
    r = simulate(1_000_000, params(), scenario="long")
    # loan 800 000 (down payment inherited); PMT(5 %/12, 300, 800 000) = 4 676.72;
    # insurance 800 000 x 0.24 % / 12 = 160 (mode inherited: initial capital)  ->  4 836.72
    assert r.scenario == "long" and r.loan_amount_mad == 800_000
    assert r.monthly_instalment_mad == 4_677 and r.monthly_insurance_mad == 160 and r.monthly_payment_mad == 4_837
    assert r.total_insurance_mad == 160 * 300


def test_unknown_scenario_is_an_error():
    with pytest.raises(KeyError):
        simulate(1_000_000, params(), scenario="nope")


def test_config_without_scenarios_keeps_working():
    d = yaml.safe_load(yaml.safe_dump(SYNTH))
    del d["scenarios"], d["default_scenario"], d["affordability"]
    r = simulate(1_000_000, FinancingParams(**d))
    assert r.ok and r.scenario is None and r.monthly_payment_mad == 5_931 and r.required_income_mad is None


def test_compare_scenarios_default_first():
    rows = compare_scenarios(1_000_000, params(), eur_mad=10.0)
    assert [r["scenario"] for r in rows] == ["base", "long", "resident"] and rows[0]["is_default"]
    # resident: 10 % down -> loan 900 000; costs 31 100 + 1 % x 900 000 = 40 100; cash 100 000 + 40 100 + 1 000
    assert rows[2]["loan_amount_mad"] == 900_000 and rows[2]["total_cash_needed_mad"] == 141_100
    assert rows[1]["monthly_payment_mad"] == 4_837


# ---- cost lines and rate mechanics added for the Moroccan defaults

def test_tax_on_interest_raises_the_rate_paid():
    r = simulate(1_000_000, params(financing={"interest_vat_rate": 0.10}))
    # rate paid = 6 % x 1.10 = 6.6 %; PMT(0.55 %, 240, 800 000) = 6 011.78; + 200 insurance
    assert r.annual_rate_effective == pytest.approx(0.066)
    assert r.monthly_instalment_mad == 6_012 and r.monthly_payment_mad == 6_212


def test_progressive_tiers():
    tiers = {"mortgage_registration": {"base": "loan", "fixed_mad": 0, "vat_rate": 0, "tiers": [
        {"up_to": 250_000, "rate": 0.005}, {"up_to": 5_000_000, "rate": 0.015}, {"up_to": None, "rate": 0.005}]}}
    reg = lambda price: simulate(price, params(acquisition_costs=tiers)).cost_breakdown_mad["mortgage_registration"]
    assert reg(125_000) == 500              # loan 100 000 x 0.5 %
    assert reg(1_000_000) == 9_500          # loan 800 000: 250 000 x 0.5 % + 550 000 x 1.5 % = 1 250 + 8 250
    assert reg(7_500_000) == 77_500         # loan 6 000 000: 1 250 + 4 750 000 x 1.5 % + 1 000 000 x 0.5 %
    assert params(acquisition_costs=tiers).missing() == []      # tiers stand in for `rate`


# ---- income needed

def test_required_income_hand_checked():
    r = required_income(1_000_000, params(), eur_mad=10.0)
    # payment 5 931.448468 / 0.40 = 14 828.62 MAD  ->  14 829;  / 10 = 1 482.86 EUR  ->  1 483
    assert r["monthly_payment_mad"] == 5_931 and r["required_income_mad"] == 14_829
    assert r["required_income_eur"] == 1_483 and r["max_debt_ratio"] == 0.40


def test_existing_debts_raise_the_income_needed():
    p = params(affordability={"existing_monthly_debt_eur": 100})
    # (5 931.448468 + 100 EUR x 10) / 0.40 = 17 328.62  ->  17 329 MAD, 1 733 EUR
    assert simulate(1_000_000, p, eur_mad=10.0).required_income_mad == 17_329
    assert simulate(1_000_000, p, eur_mad=10.0).required_income_eur == 1_733
    assert simulate(1_000_000, p).required_income_mad is None       # debts in EUR, no rate: no guess


def test_cash_purchase_needs_no_income():
    r = simulate(1_000_000, params(financing={"down_payment_pct": 1.0}), eur_mad=10.0)
    assert r.required_income_mad == 0 and r.monthly_payment_mad == 0


# ---- highest reachable price

def test_max_price_cash_binding_closed_form():
    # At the 20 % minimum down payment: cash = 0.2 P + (1 % + 1 % + 1.1 %) P + 100 + 1 % x 0.8 P + 1 000
    #                                        = 0.239 P + 1 100.  Savings 240 100  ->  P = 1 000 000 exactly.
    m = max_price(params(), 240_100, income_eur=5_000, eur_mad=10.0)
    assert m.ok and m.max_price_mad == 1_000_000 and m.binding == "cash" and m.min_down_payment_active
    assert m.down_payment_mad == 200_000 and m.simulation.total_cash_needed_mad == 240_100
    assert m.payment_budget_mad == 20_000           # 0.40 x 5 000 EUR x 10
    assert max_price(params(), 240_099, income_eur=5_000, eur_mad=10.0).max_price_mad < 1_000_000


def test_max_price_income_binding_closed_form():
    # Income 1 200 EUR x 10 x 0.40 = 4 800 MAD/month  ->  loan L = 4 800 / K_BASE = 647 397.
    # All savings go into the purchase: (P - L) + 3.1 % P + 100 + 1 % L + 1 000 = 300 000
    #   ->  P = (298 900 + 0.99 L) / 1.031 = 911 566 (payment rounding moves it by a few dirhams)
    m = max_price(params(), 300_000, income_eur=1_200, eur_mad=10.0)
    loan = 4_800 / K_BASE
    assert m.binding == "income" and not m.min_down_payment_active
    assert abs(m.max_price_mad - (298_900 + 0.99 * loan) / 1.031) < 75      # 0.5 MAD of payment ~ 67 MAD of loan
    assert m.simulation.monthly_payment_mad == 4_800
    assert 0 <= 300_000 - m.simulation.total_cash_needed_mad <= 1
    assert m.simulation.down_payment_pct > 0.20


def test_max_price_respects_ltv_cap():
    # Same buyer, two lenders: one requires 20 % down, the other nothing.
    capped = max_price(params(), 100_000, income_eur=10_000, eur_mad=10.0, scenario="base")
    free = max_price(params(), 100_000, income_eur=10_000, eur_mad=10.0, scenario="resident")
    # 20 % cap: 0.239 P + 1 100 <= 100 000  ->  P = 413 807.5  ->  413 807 or one below (whole-dirham down payment)
    assert capped.max_price_mad in (413_806, 413_807) and capped.binding == "cash"
    assert capped.simulation.down_payment_mad >= 0.20 * capped.max_price_mad
    assert capped.simulation.min_down_payment_ok
    assert free.max_price_mad > 2 * capped.max_price_mad            # with no cap the income decides
    assert free.simulation.down_payment_pct < 0.20


def test_max_price_without_income_is_cash_only():
    m = max_price(params(), 240_100)
    assert m.max_price_mad == 1_000_000 and m.binding == "cash" and m.payment_budget_mad is None


def test_max_price_buys_outright_when_income_is_zero():
    # No income: no loan. P + 3.1 % P + 100 <= 500 000 (no mortgage registration, no bank fees)
    #   ->  P = 499 900 / 1.031 = 484 869.06
    m = max_price(params(), 500_000, income_eur=0, eur_mad=10.0)
    assert m.max_price_mad == 484_869 and m.binding == "income" and m.simulation.loan_amount_mad == 0


def test_max_price_needs_what_it_needs():
    assert max_price(params(), 500_000, income_eur=3_000).missing_params == ["eur_mad"]
    assert not max_price(params(financing={"annual_rate": None}), 500_000).ok


@pytest.mark.parametrize("scenario", ["mre_typical", "mre_best", "mre_prudent", "resident"])
@pytest.mark.parametrize("savings,income", [(400_000, 2_500), (700_000, 4_000), (1_500_000, 3_000),
                                            (350_000, 9_000), (900_000, None)])
def test_inversion_round_trip_on_shipped_config(scenario, savings, income):
    """max_price -> simulate at that price: nothing is exceeded and the binding constraint is tight."""
    p = load_params(ROOT / "config" / "financing.yaml")
    eur = 10.85
    m = max_price(p, savings, income, eur, scenario=scenario)
    assert m.ok and m.max_price_mad > 0
    r = simulate(m.max_price_mad, p, False, eur, scenario=scenario, down_payment_mad=m.down_payment_mad)
    assert r.to_dict() == m.simulation.to_dict()
    assert r.total_cash_needed_mad <= savings and r.min_down_payment_ok
    if income is not None:
        assert r.monthly_payment_mad <= m.payment_budget_mad
    if m.binding == "cash":
        assert savings - r.total_cash_needed_mad <= 1
    else:
        assert m.payment_budget_mad - r.monthly_payment_mad <= 1
        assert savings - r.total_cash_needed_mad <= 1               # spare cash would lower the payment
    # one more dirham of savings never lowers the answer
    assert max_price(p, savings + 10_000, income, eur, scenario=scenario).max_price_mad >= m.max_price_mad


# ---- the page's JavaScript calculator must agree with Python to the dirham

def _parity():
    return json.loads(PARITY.read_text())


def _case_params(fx: dict, c: dict) -> FinancingParams:
    d = json.loads(json.dumps(fx["params"]))
    d["financing"] |= {"down_payment_pct": c["down_pct"], "annual_rate": c["rate"], "duration_years": c["years"],
                       "finance_acquisition_costs": c["finance_costs"]}
    return FinancingParams(**d)


KEYS = ["acquisition_costs_mad", "down_payment_mad", "loan_amount_mad", "monthly_instalment_mad",
        "monthly_insurance_mad", "monthly_payment_mad", "total_cash_needed_mad", "all_in_price_mad",
        "required_income_mad", "required_income_eur", "total_cash_needed_eur"]


def test_parity_fixture_matches_python():
    fx = _parity()
    assert len(fx["cases"]) >= 40
    for c in fx["cases"]:
        r = simulate(c["price"], _case_params(fx, c), c["agency"], c["eur_mad"]).to_dict()
        assert {k: r[k] for k in KEYS} == c["expected"], c
    # First case, by hand: price 1 000 000, 20 % down, 6 % + 10 % tax = 6.6 %, 20 years, private sale, EUR = 10.
    #   costs: 4 % = 40 000 · 1.5 % + 100 = 15 100 · max(1 %, 2 500) x 1.10 = 11 000 · 2 000
    #          · mortgage (loan 800 000) 1 250 + 550 000 x 1.5 % = 9 500
    #          · capped line min(0.2 %, 4 000) x 1.20 = 2 400                         -> 80 000
    #   cash 200 000 + 80 000 + 6 000 = 286 000 (28 600 EUR)
    #   PMT(0.55 %, 240, 800 000) = 6 011.78 · insurance 800 000 x 0.42 % / 12 = 280 -> payment 6 291.78
    #   income (6 291.78 + 150 EUR x 10) / 0.40 = 19 479.45 MAD -> 19 479; 1 947.9 EUR -> 1 948
    first = fx["cases"][0]["expected"]
    assert (first["acquisition_costs_mad"], first["total_cash_needed_mad"]) == (80_000, 286_000)
    assert (first["monthly_instalment_mad"], first["monthly_insurance_mad"]) == (6_012, 280)
    assert (first["monthly_payment_mad"], first["total_cash_needed_eur"]) == (6_292, 28_600)
    assert (first["required_income_mad"], first["required_income_eur"]) == (19_479, 1_948)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed; fixture still checked in Python")
def test_parity_fixture_matches_javascript():
    fx = _parity()
    base = FinancingParams(**fx["params"])
    f = base.financing
    inputs = [{"price": c["price"], "down_pct": c["down_pct"], "rate": c["rate"], "years": c["years"],
               "interest_vat": f.interest_vat_rate, "insurance_rate": f.insurance.annual_rate,
               "bank_fees": f.bank_fees_mad, "finance_costs": c["finance_costs"], "agency": c["agency"],
               "eur_mad": c["eur_mad"]} for c in fx["cases"]]
    script = (CALC_JS.read_text() + "\nconst j = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
              "\nconsole.log(JSON.stringify(j.inputs.map((i) => financeCalc(j.cfg, i))));")
    out = subprocess.run(["node", "-e", script], input=json.dumps({"cfg": calc_config(base), "inputs": inputs}),
                         capture_output=True, text=True, check=True, timeout=60).stdout
    for c, got in zip(fx["cases"], json.loads(out), strict=True):
        assert {k: got[k] for k in KEYS} == c["expected"], c


def test_site_embeds_the_tested_calculator():
    html = (ROOT / "radar" / "api" / "templates" / "site.html").read_text()
    assert '{% include "finance_calc.js" %}' in html and "function financeCalc(" in CALC_JS.read_text()
