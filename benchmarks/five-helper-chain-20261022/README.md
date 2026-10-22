# A final measure with five sequential helpers

Version 0.21.0 adds a separate fictional fixture with exactly six original measures:

**Daily production → Daily production difference → Period production difference → Cumulative production difference → Average daily production difference → Projected next-period production difference.**

Difference means current production minus the previous calendar day's production. Missing either observation yields BLANK; it never silently substitutes the previous observed day. Period differences sum the daily changes. The cumulative measure removes the selected period range and evaluates all periods through its last period, while preserving explicit Calendar filters and site/product selections. The average divides by all calendar days in that context, including days with no difference. The final step projects the average over fourteen days, an explicitly fictional test rule.

The final root depends on five actual helper definitions. The library creates a separately named seventh measure, applies guarded additive daily-sum changes through the helper paths and leaves every original helper unchanged. It retains the calendar iterator and previous-day/cumulative filter behavior.

## Native correctness

Two separate source-free Import models were refreshed once on a real Fabric FTL64 trial:

- A four-day hand-calculated fixture with daily production **10, 20, 5, 0**.
- A larger fixture with **364 days, six sites, eight products and 16,849 actual rows**, including missing observations, an unmatched date member, BLANK products, zeros and negative corrections.

**17,802 source-value checks passed across all six stages.** Integers and BLANKs match exactly; independent source-row expectations for ratios/projections use relative/absolute tolerance 1e-10. The oracle does not parse the DAX.

**2,967 strict original/candidate value and BLANK comparisons passed.** Coverage includes 47 scalar contexts and four matrix probes across the two models: daily detail, period boundaries, multi-selection, missing days, empty/unknown members, corrections, zero production, all-BLANK products, subtotals and grand totals. A further 924 root context rows passed strict query-alias/source binding and independent expectations for the CU workload. The exported full-chain query, defining all six originals plus the generated root, also executed successfully.

The hand case has daily differences BLANK, +10, −15, −5. Its two-day period changes are +10 and −20; cumulative differences are +10 and −10. Average daily differences are +5 and −2.5, producing fourteen-day projections +70 and −35. The grand-total projection is **−35**, recomputed in its own context; summing the displayed forecasts would incorrectly return +35.

## Native interactive CU

Eight alternating paired replays of opening, filtering and expanding the larger matrix retained complete CPU and native CU evidence for all **48 measured queries**, with six primes excluded. The same query-scoped alias and tag shape were used on both paths. Query tokens and item/operation identities match the semantic logs to native operation events; cost is not estimated from CPU or latency.

| Metric per complete replay | Original median | Candidate median | Change | Classification |
|---|---:|---:|---:|---|
| Native interactive CU-seconds | 6.240 | 4.768 | 23.6% lower | Inconclusive |
| Total query CPU, milliseconds | 390.0 | 298.0 | 23.6% lower | Inconclusive |
| Client elapsed, milliseconds | 2,694.0 | 2,568.5 | 4.7% lower | Inconclusive |

Across all eight replays, native query charges total **51.008 → 39.088 CU-seconds**. Ranges touch (original 5.504–7.760; candidate 4.512–5.504), so the current conservative reduction criterion is not satisfied. These lower descriptive values are not promoted as an established capacity saving. No repeated attempts were made to turn this inconclusive outcome into a winner.

Both variants used the same loaded source rows and unchanged helper definitions. Saved measure definitions and source row counts were rechecked. The two isolated test models were deleted after retaining the evidence; no EC2 or paid capacity was created.

`results.json` retains sanitized summaries. Raw tenant logs, endpoints, resource identities and local paths stay outside this repository. These API-replayed synthetic matrix queries do not establish full browser-session, net background/source or production savings. Generator exports remain marked unvalidated until separately executed; the native evidence applies to the exact fixture sizes above.
