# Native Fabric interactive compute evaluation

Version 0.20.0 tested the library's three existing daily-production rewrites on a real Fabric FTL64 trial capacity. Data and business rules are the independent fictional `production-lab` fixture: 728 days, ten sites, ten products, 71,241 actual rows and 145,600 plan rows, with missing observations, BLANK products, corrections, eligibility filters and two budget versions. The same refreshed Import model and helper definitions serve both alternatives. No original report data is included.

## Outcome

| Full replay metric | Original median | Candidate median | Reduction | Observation |
|---|---:|---:|---:|---|
| Native interactive CU-seconds | 23.872 | 16.520 | 30.8% | Observed reduction |
| Recorded total CPU, milliseconds | 1,492.0 | 1,032.5 | 30.8% | Observed reduction |
| Engine duration, milliseconds | 1,535.5 | 1,081.0 | 29.6% | Inconclusive: sample ranges overlap |
| Client elapsed, milliseconds | 10,897.4 | 10,122.1 | 7.1% | Inconclusive |

Across all eight paired replays, native query charges total 186.576 original versus 132.688 candidate CU-seconds (28.9% lower). Original replay CU ranges are 19.280–24.752; candidate ranges are 15.280–19.008. These are observed native charges, not a conversion from CPU or latency.

Expanded production-rate matrices show a 43.6% lower median (2.880 → 1.624 CU-s); expanded stock-coverage matrices show 32.2% lower (3.872 → 2.624 CU-s). Both have nonoverlapping ranges. Other individual interactions either have overlapping samples or are entirely BLANK and are not promoted as established wins.

## Method

- Run the generated root proposals for production rate, stock coverage and target variance. The library specializes reachable helpers and collapses guarded additive daily iterators; original helpers remain unchanged. Both paths use the same query-scoped alias and capture-tag variable shape.
- Fix TODAY at 2024-09-15 on both alternatives. Retain the fixture's dynamic actual/plan selection, ratios, stock snapshots and explicit BLANK semantics.
- For each measure, replay opening a region-by-period matrix, filtering one region, expanding site/family rows and an empty site selection. Queries contain native matrix subtotal/grand-total rows. This is twelve separate matrix-query interactions per replay, not a browser page with all three measures combined into one shared query.
- Pass 7,614 strict DAX equality and separate BLANK checks before measuring. Recheck loaded table row counts and a single successful refresh afterwards.
- Exclude one prime per alternative/context. Run eight measured pairs, alternating AB/BA order with seeded scenario ordering: 192 measured queries, 24 excluded primes.
- Use an unused DEFINE variable for the unique capture tag. Workspace Monitoring removes comments, so a preceding comment-only exploratory capture cannot establish per-sample attribution and is excluded.
- Match exact query tokens to semantic QueryBegin, QueryEnd and ExecutionMetrics. Join native capacity operation events by item/operation ID, deduplicate reexports, and convert native `capacityUnitMs` to seconds exactly once. CPU and CU coverage is complete for every measured query; no failed/throttled measured operations were observed.
- Require at least five pairs, tested-row equality, complete evidence, more than 10% lower median and nonoverlapping ranges for the descriptive reduction label. This is not a statistical significance test.

`results.json` contains sanitized metric summaries and replay pairs. Raw tenant logs, endpoints, resource IDs, model captures and local artifacts stay outside the repository. The release includes a sanitized expandable HTML report.

These findings demonstrate query-level compute savings on this synthetic Fabric workload. They do not predict production savings, establish universal DAX equivalence, reproduce browser cache behavior, or measure net capacity including model loading, refresh, monitoring and external sources. No preaggregation or extra source table was added by these rewrites. The earlier public-report audits remain unchanged: they did not establish clear speedups or native CU savings.
