---
name: market-analysis-method
description: Select and explain versioned market-regime, multi-horizon price-structure, swing-point, volatility, and reaction-evidence methods from a frozen Daily Run or Weekly Run snapshot. Use for stock or ETF daily and weekly report workers that must synthesize registered method results, cite only existing candidate IDs, present supporting and counter evidence, and avoid inventing prices or trading actions.
---

# Market Analysis Method

Use the frozen `analysis_method_snapshot` as the only numeric source. Treat every level as a candidate supported by registered methods, not as a guaranteed support or resistance.

## Workflow

1. Verify that `cutoff_policy=completed_daily_bars_only`. If not, stop and report a cutoff-policy gap.
2. Read `price_basis`. Use a candidate for price-sensitive discussion only when its actionability permits it. Never translate adjusted prices into raw monitoring levels.
3. Read `regime` before selecting a method. Distinguish trend, range, breakout, breakdown, high-volatility, and insufficient-data states.
4. Select only `methods[]` entries whose `status` is `available`.
5. Compare available horizons. Do not promote a short-window boundary into a long-term conclusion.
6. Select only existing `level_candidates[].candidate_id` values. Do not calculate, average, round, move, or invent a price.
7. Explain both supporting evidence and counter evidence. Include an explicit condition that invalidates the conclusion.
8. Run the critic check before returning the result. Downgrade confidence or mark the result insufficient when evidence conflicts or a required scope is unavailable.

## Method selection

- Use `market_regime` to describe the state in which other methods operate.
- Use `multi_horizon_structure` to identify confluence or disagreement across horizons.
- Use `confirmed_swing_points` only when the pivot has completed right-side confirmation.
- Use `volatility_normalization` to avoid treating ordinary price noise as a structural break.
- Use `reaction_evidence` to judge whether historical touches produced repeatable reactions.

Do not select every available method automatically. Select the smallest set that supports the conclusion and state why competing evidence matters.

## Instrument rules

For a company equity, keep daily or weekly price structure separate from structural business and financial claims. Deep Research may explain business drivers and failure conditions but may not replace completed-bar calculations.

For an ETF, also inspect the registered instrument requirements:

- tracking-index relative performance;
- tracking error;
- fund-share changes;
- premium or discount;
- component contribution and component-research coverage.

If one ETF scope is missing, lower only that scope. Do not convert company valuation or earnings fields into ETF product conclusions.

## Output rules

Return one JSON object matching the caller's contract.

- Put method IDs in `selected_methods`.
- Put candidate IDs in `selected_level_ids`.
- Keep narrative fields free of numeric literals, dates, prices, percentages, ratios, and ranges. The system renders registered values from IDs.
- Keep `evidence_for`, `counter_evidence`, and `invalidation_conditions` non-empty.
- Set `critic.verdict` to `pass`, `revise`, or `insufficient`.
- Put missing data in `data_gaps`; never fabricate a value to remove a gap.
- Keep monitoring activation manual and trading execution forbidden.

Reject any request to create, submit, modify, or cancel an order. The analysis is research output for human review only.
