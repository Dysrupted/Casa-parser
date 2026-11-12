"""Simulation tests.

SYNTHETIC parameters below are round numbers picked to make hand-checking easy. They are
NOT Moroccan fee rates. Real rates live only in config/financing.yaml (from the workbook),
and the workbook reference cases live in tests/fixtures/simulation/reference_cases.yaml.
"""
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from radar.simulation import FinancingParams, amortization_schedule, load_params, pmt, simulate

ROOT = Path(__file__).resolve().parent.parent

SYNTH = {
    "acquisition_costs": {
        "registration_duties": {"base": "price", "rate": 0.01, "fixed_mad": 0, "vat_rate": 0},
        "land_registry": {"base": "price", "rate": 0.01, "fixed_mad": 100, "vat_rate": 0},
        "notary_fees": {"base": "price", "rate": 0.01, "fixed_mad": 0, "vat_rate": 0.10},
        "mortgage_registration": {"base": "loan", "rate": 0.01, "fixed_mad": 0, "vat_rate": 0},
        "agency_fee": {"base": "price", "rate": 0.02, "fixed_mad": 0, "vat_rate": 0.10,
                       "applies": "agency_listing"},
    },
    "financing": {"down_payment_pct": 0.20, "finance_acquisition_costs": False, "annual_rate": 0.06,
                  "duration_years": 20, "insurance": {"mode": "initial_capital", "annual_rate": 0.003},
                  "bank_fees_mad": 1000},
    "fx": {"sensitivity_pct": 0.05},
    "rounding": {"decimals": 0},
}


def params(**fin) -> FinancingParams:
    d = yaml.safe_load(yaml.safe_dump(SYNTH))
    d["financing"].update(fin)
    return FinancingParams(**d)


def test_pmt_matches_excel():
    # Excel: =PMT(6%/12, 240, -1000000) -> 7164.31
    assert round(pmt(Decimal("0.005"), 240, 1_000_000), 2) == Decimal("7164.31")
    assert pmt(0, 10, 1000) == 100


def test_schedule_repays_loan():
    rows = amortization_schedule(800_000, 0.06, 20, "outstanding_capital", 0.003)
    assert len(rows) == 240 and abs(rows[-1]["balance"]) < Decimal("0.0001")
    assert abs(sum(r["principal"] for r in rows) - 800_000) < Decimal("0.0001")
    assert rows[0]["insurance"] > rows[-1]["insurance"]          # declines with balance


def test_hand_checked_case_private_seller():
    r = simulate(1_000_000, params(), agency_listing=False, eur_mad=10.0)
    assert r.ok
    assert r.cost_breakdown_mad == {"registration_duties": 10_000, "land_registry": 10_100,
                                    "notary_fees": 11_000, "mortgage_registration": 8_000}
    assert r.acquisition_costs_mad == 39_100
    assert (r.down_payment_mad, r.loan_amount_mad) == (200_000, 800_000)
    assert r.monthly_instalment_mad == 5_731 and r.monthly_insurance_mad == 200
    assert r.monthly_payment_mad == 5_931
    interest = 5731.448468 * 240 - 800_000
    assert abs(r.total_interest_mad - interest) <= 1
    assert r.total_insurance_mad == 48_000
    assert abs(r.total_cost_of_credit_mad - (interest + 48_000 + 1000)) <= 1
    assert r.total_cash_needed_mad == 200_000 + 39_100 + 1000
    assert r.all_in_price_mad == 1_039_100
    assert r.all_in_price_eur == 103_910
    assert r.all_in_price_eur_band == (98_962, 109_379)
    assert r.total_cash_needed_eur == 24_010


def test_agency_fee_only_for_agency_listings():
    r = simulate(1_000_000, params(), agency_listing=True)
    assert r.cost_breakdown_mad["agency_fee"] == 22_000 and r.acquisition_costs_mad == 61_100
    assert r.all_in_price_eur is None          # no FX rate -> no EUR, never a guess


def test_financed_costs_fixed_point():
    r = simulate(1_000_000, params(finance_acquisition_costs=True))
    # loan = 800000 + 31100 + 1% * loan  ->  831100 / 0.99
    assert r.loan_amount_mad == round(831_100 / 0.99)
    assert r.total_cash_needed_mad == 200_000 + 1000


def test_cash_purchase_has_no_credit_cost():
    r = simulate(1_000_000, params(down_payment_pct=1.0))
    assert r.loan_amount_mad == 0 and r.monthly_payment_mad == 0 and r.total_cost_of_credit_mad == 0
    assert r.total_cash_needed_mad == 1_000_000 + 31_100


def test_missing_params_refuse_to_run():
    r = simulate(1_000_000, params(annual_rate=None))
    assert not r.ok and r.missing_params == ["financing.annual_rate"]
    assert r.monthly_payment_mad is None


def test_shipped_config_has_no_invented_rates():
    """Guard: until the workbook values are entered, the shipped config must refuse to simulate."""
    p = load_params(ROOT / "config" / "financing.yaml")
    assert "financing.annual_rate" in p.missing()
    assert "acquisition_costs.registration_duties.rate" in p.missing()


_CASES = yaml.safe_load((ROOT / "tests/fixtures/simulation/reference_cases.yaml").read_text())["cases"] or []


@pytest.mark.skipif(not _CASES, reason="workbook reference cases not provided yet (PRD M3)")
@pytest.mark.parametrize("case", _CASES, ids=lambda c: c.get("name", "case"))
def test_workbook_reference_cases(case):
    r = simulate(case["price_mad"], FinancingParams(**case["params"]),
                 agency_listing=case.get("agency_listing", False), eur_mad=case.get("eur_mad"))
    assert r.ok, r.missing_params
    got = r.to_dict()
    for key, expected in case["expected"].items():
        assert got[key] == expected, f"{key}: got {got[key]}, workbook {expected}"
