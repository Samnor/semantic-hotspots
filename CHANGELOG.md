# Changelog

## 0.6.0

- Export a new PBIP project containing exactly one page and one baseline/candidate comparison matrix.
- Add a separate Import semantic model with embedded, portable synthetic CSV sources and a refresh-time relationship-key summary candidate.
- Recompose supported scalar measures from summary states while retaining original selected measure chains for comparison.
- Write separate detail/subtotal/total validation and A/B query files, explicit BLANK flags, fingerprints and a change manifest.
- Withhold unsupported filter/drill/security/fact contexts; preserve source inputs and commit generated output only after successful staging.
- Validate generated project/report connection documents against versioned Microsoft JSON schemas in CI. Desktop opening, refresh and actual DAX execution remain unverified.

## 0.5.0

- Add explicit matrix contracts and common PBIR pivotTable projection extraction.
- Analyze value/filter chains, shared dependencies, hierarchy/drill context and partial coverage.
- Suggest relationship-preserving and exploratory matrix grains with component-state rollups, retained iterator/distinct keys and conservative blockers.
- Generate representative multi-measure detail/subtotal/grand-total queries and optional grouping-count probes.
- Include matrix workloads in synthetic bundles and link measured benchmarks back to matrix grain reports.
- Attach exact matching baseline workload timings without claiming pre-aggregation improvements.
- Add fictional matrix fixtures and cross-platform regression coverage. Unsupported visual semantics remain explicit.

## 0.4.0

- Add a bound, typed logical subset with explicit row/filter context and opaque unsupported expressions.
- Propose guarded numeric SUMX simplification and row-local nested FILTER fusion, with isolated edits and bounded combinations.
- Include before/after DAX, logical plans, dependencies and guard evidence in offline reports and JSON.
- Prepare query-scoped synthetic experiments; validate strict values and BLANK flags before A/B timing.
- Rank observed baseline query durations first while keeping tested-case equality, incomplete coverage, mixed timings and all-BLANK cases distinct.
- Retain query fingerprints, runner outputs and raw CSV evidence without modifying source or loaded models.
- Add fictional rewrite fixtures and mocked engine failure/correctness/coverage tests. Actual DAX engine execution remains a separate integration step.

## 0.3.1

Initial public release.

- Parse exported semantic models and construct measure dependency graphs.
- Rank review priorities and optionally associate offline runtime telemetry.
- Generate offline HTML reports with progressively detailed findings and candidate fixes.
- Generate reproducible synthetic tables, a separate Import model and DAX workloads.
- Run or collect DAX Studio benchmarks and report measured workload durations.
- Include fictional retail examples, cross-platform tests and portable Python distributions.
