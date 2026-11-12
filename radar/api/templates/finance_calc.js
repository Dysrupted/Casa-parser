// Purchase calculator: the same formula as radar/simulation/engine.py (core + required income),
// in plain floating point. tests/test_affordability.py runs both on the same cases and requires
// identical results to the dirham. Keep the two in step.
//   cfg: { lines: [{key, base, rate, tiers, fixed, min, max, vat, applies}], max_debt_ratio,
//          existing_monthly_debt_eur, decimals }
//   inp: { price, down_pct, rate, interest_vat, years, insurance_rate, bank_fees, finance_costs,
//          agency, eur_mad }
function financeCalc(cfg, inp) {
  const k = Math.pow(10, cfg.decimals || 0);
  const rnd = (x) => Math.round(x * k) / k;           // Excel ROUND for positive amounts
  const lineAmount = (l, price, loan) => {
    const base = l.base === 'price' ? price : loan;
    let amt = 0;
    if (l.tiers) {
      let floor = 0;
      for (const t of l.tiers) {                      // progressive: each rate on its own slice
        const top = t.up_to == null ? base : Math.min(base, t.up_to);
        if (top > floor) { amt += (top - floor) * t.rate; floor = top; }
      }
    } else amt = l.rate * base;
    amt += l.fixed || 0;
    if (l.min != null) amt = Math.max(amt, l.min);
    if (l.max != null) amt = Math.min(amt, l.max);
    return amt * (1 + (l.vat || 0));
  };
  const price = inp.price, down = price * inp.down_pct, baseLoan = price - down;
  const lines = cfg.lines.filter((l) => l.applies === 'always' || (l.applies === 'agency_listing' && inp.agency));
  let loan = baseLoan, costs = {}, total = 0;
  for (let i = 0; i < 50; i++) {                      // fixed point when costs are financed
    costs = {}; total = 0;
    for (const l of lines) { costs[l.key] = lineAmount(l, price, loan); total += costs[l.key]; }
    const next = inp.finance_costs ? baseLoan + total : baseLoan;
    if (Math.abs(next - loan) < 1e-6) break;
    loan = next;
  }
  const r = inp.rate * (1 + (inp.interest_vat || 0)) / 12, n = inp.years * 12;
  const instalment = loan > 0 ? (r === 0 ? loan / n : loan * r / (1 - Math.pow(1 + r, -n))) : 0;
  const insurance = loan > 0 ? loan * inp.insurance_rate / 12 : 0;
  const bank = loan > 0 ? inp.bank_fees : 0;
  const cash = down + (inp.finance_costs ? 0 : total) + bank;
  const monthly = instalment + insurance;
  const debt = cfg.existing_monthly_debt_eur || 0;
  const income = cfg.max_debt_ratio && (!debt || inp.eur_mad)
    ? (monthly + debt * (inp.eur_mad || 0)) / cfg.max_debt_ratio : null;
  const out = {
    costs: Object.fromEntries(Object.entries(costs).map(([key, v]) => [key, rnd(v)])),
    acquisition_costs_mad: rnd(total), down_payment_mad: rnd(down), loan_amount_mad: rnd(loan),
    monthly_instalment_mad: rnd(instalment), monthly_insurance_mad: rnd(insurance),
    monthly_payment_mad: rnd(monthly), total_cash_needed_mad: rnd(cash),
    all_in_price_mad: rnd(price + total),
    required_income_mad: income == null ? null : rnd(income),
    required_income_eur: income == null || !inp.eur_mad ? null : rnd(income / inp.eur_mad),
    total_cash_needed_eur: inp.eur_mad ? rnd(cash / inp.eur_mad) : null,
  };
  return out;
}
