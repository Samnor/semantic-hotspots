# Changelog

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
