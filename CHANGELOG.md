# Changelog

## 0.21.0

- Add a separate six-stage daily-production fixture whose final measure has exactly five transitive helpers: production, previous-calendar-day difference, period difference, cumulative difference, average daily difference and a fictional 14-day projection.
- Export one generated replacement root, unchanged helpers, one comparison matrix, paired validation queries and original/proposed DAGs through `production-chain-lab` and Python APIs. Keep this fixture separate from the existing 24-measure lab.
- Add independent source-row arithmetic and hand-calculated regressions for period boundaries, missing dates, BLANKs, zero production, corrections, cumulative context and nonadditive totals. Native evaluation results are documented separately from generator tests.

## 0.20.0

- Add a compute-first comparison API and CLI with native interactive CU-seconds as the primary outcome, and CPU, engine duration and client elapsed as independent supporting metrics. Never convert CPU or latency to CU charges.
- Join captured interactions to exact query tokens and native operation identities, using in-query tags that survive Workspace Monitoring comment removal. Accept raw capacity CloudEvents or explicit normalized operation records.
- Deduplicate exported operation charges, reject conflicting evidence, retain failed/throttled costs, exclude declared warm-ups, and withhold savings when expected pairs, correctness, binding or telemetry are incomplete. Recorded zeros remain distinct from missing observations.
- Rank expandable HTML evidence by original CU consumption and compare whole predeclared replays, including all listed queries per interaction. Keep background materialization, external sources, monitoring and production extrapolations outside query-level savings claims.

## 0.19.0

- Add narrowly guarded constant-one counting iterators and row-invariant direct-count proposals. Preserve empty-input BLANK behavior, qualify moved column bindings, and retain context-transition and outer-row-context guards.
- Allow function/keyword casing differences when anchoring helper recovery; string literals and changed constants remain conflicts.
- Retain single-underscore VAR identifiers, decimal/scientific numeric source spans, COUNTX row scopes and DATEDIFF interval structure in the partial logical representation.
- Prepare experiment timing paths with the same query-scoped alias. Validate the original alias against the saved measure separately; alias mismatches or missing binding evidence withhold timings and performance claims.
- Require a median improvement above both 10% and 1 ms with nonoverlapping ranges in every completed phase before selecting an experiment winner. Small/overlapping gains and legacy unmatched query scopes remain inconclusive.
- Read large quoted text/SVG validation cells with a bounded 16 MiB CSV field limit instead of treating the default 128 KiB transport limit as a DAX failure.
- Extend public-model evaluation with 20 additional reports, including representative matrix subtotal probes. Keep unchanged, failed and inconclusive cases visible; no speedup follows from syntax simplification alone.

## 0.18.1

- Qualify a bound bare iterator column when removing SUMX, so SUM cannot rebind the name to a different column in the measure home table. Add a cross-table name-collision regression. Existing corpus proposals are unchanged.

## 0.18.0

- Bind unqualified home-table columns only in supported column argument positions; recognize AVERAGE, COUNT/COUNTA, DISTINCTCOUNT and single-argument ALL without allowing naked scalar column references.
- Add guarded exact-numeric IF/division alternatives using DIVIDE or a fresh denominator VAR. Retain floating-point operands after two real-engine counterexample rounds; emit a specific guard explanation.
- Normalize literal DAX string format expressions into preserved static formats across BIM, TMDL and Desktop recovery. Keep contextual/malformed format expressions guarded and recover missing matching helper formatting metadata.
- Record a pinned ten-report, 250-measure public sample audit with matched query-scoped paths, strict value/BLANK checks and alternating warm timings. No clear performance improvement was established; keep failed candidates and unchanged measures visible in aggregate evidence.

## 0.17.0

- Tighten OPT003 to exact numeric stored columns after a real-engine floating-point counterexample; retain BLANK filters for double or unestablished scalar values.
- Add guarded OPT004 to collapse a context-transitioned additive grain iterator over exact same-table sums; retain nonadditive, floating-point and relationship guards.
- Add bounded OPT005 helper-path specialization for a selected root. Preserve implicit context transition with explicit CALCULATE, qualify moved model references, and leave every original helper unchanged. Carry local-rule evidence and expanded-helper identities into the new-measure package.
- Add `production-lab` and Python APIs for coherent fictional daily actual/plan data, eight tables, 24 measures, three long-chain proposals, paired DAGs, fixed-date validation packages and one Power BI comparison matrix.
- Exercise missing observations, all-BLANK and empty slices, zeros, corrections, budget versions, snapshot grain and nonadditive totals with an independent arithmetic oracle.

## 0.16.0

- Preserve live TODAY() in production proposals; export fixed-date query-scoped overrides for both paths and date-dependent helper measures, including per-date visual workloads and synthetic rewrite experiments.
- Propose independently guarded local rewrites while known helper definitions remain missing; keep their calls opaque and withhold validation until real definitions are available.
- Extend OPT003 through filtered table variables while preserving lexical bindings and other consumers. Allow local rules around missing column-type metadata without inferring surrounding semantics.
- Recover actual reachable definitions from explicit matching BIM/TMDL exports or one explicitly selected local Desktop catalog. Reject conflicting roots/definitions and ambiguous names; do not edit source files or the engine. Add Studio recovery actions, date selection, public APIs and CLI options.

## 0.15.0

- Bind table variables and physical/temporary row columns through VALUES, ADDCOLUMNS, paired-name SELECTCOLUMNS, FILTER and SUMX. Keep lexical lifetimes, row/filter contexts, source spans and projected schemas explicit.
- Link temporary-column uses to their defining expressions in original/new DAGs; do not infer physical lineage for added columns.
- Apply existing guarded rewrites inside recognized extension expressions. Add OPT003 for a blank-only filter before SUMX over the same stored column of a table variable.
- Recognize ISEMPTY, MAX/MIN, YEAR/DATE, SWITCH and TODAY structure. Date-dependent chains, unknown helpers/schemas and ambiguous references retain their guards; no speedup is inferred.

## 0.14.2

- Label the Power BI Desktop measure definition separately from a DAX Studio query preview.
- Include preview-query.dax with a quoted query-scoped DEFINE MEASURE and EVALUATE ROW, plus HOW-TO-USE.txt, in every created-measure package.
- Keep query previews separate from guarded validation; they do not save model measures, establish equivalence, or bypass optimizer blockers.

## 0.14.1

- Identify dependency blockers individually, separating scope uncertainty, unresolved references, missing definitions, syntax-scan failures and detected cycles.
- Include the unqualified reference and source location in logical-binding failures, and identify the function, owning measure and source line for controlled-evaluation guards.
- Retain all rewrite/validation guards while making unchanged-copy results and custom-proposal validation reasons specific.

## 0.14.0

- Parse scoped VAR/RETURN bindings and shared variable references in computation DAGs without inlining or moving their definitions.
- Allow existing guarded rewrites inside recognized scalar variables, IF/NOT/ISBLANK/BLANK, DIVIDE with literal alternatives, and CALCULATE with REMOVEFILTERS. Table variables remain outside automatic rewrite coverage.
- Separate informational reference notes from dependency blockers when VAR scopes are bound. Missing/ambiguous references, unsupported scopes and cycles still block validation.
- Explain unchanged copies above both Studio results and paired DAGs, and identify unsupported operations with source offsets.

## 0.13.0

- Make the primary workflow Select measure -> optional visual checkboxes -> Create new measure, with copy/download output and advanced proposal details collapsed.
- Create a collision-free new measure definition, measure JSON, before/after DAGs, offline report and downloadable package without modifying the original model or helpers.
- Prefer a bounded combined guarded rewrite; explicitly label an unchanged copy when no supported rewrite applies. Missing definitions block creation; incomplete chains withhold validation queries.
- Retain multiple selected visuals as independent contexts and export separate representative queries without merging their filters or baking them into DAX.
- Add create_measure / CreatedMeasure to the library, a create-measure CLI command with repeatable --visual, and plural contexts in root-only review. Preserve the single-context API.

## 0.12.0

- Limit Studio’s optional visual picker to matrices that directly display the selected measure, with the same binding check on the server. Select one visual or continue without any.
- Preserve report/page names and literal PBIR visual titles; disambiguate duplicate labels with stable IDs. Add optional report_name, page_name and visual_id display metadata to matrix specifications.
- Replace the raw context preview with readable axes, filters and totals, plus an explicit Remove visual context action and helpful no-matching-visual state.
- Keep local measure/search changes intact across polling instead of restoring a previous completed selection. Include a matching iterator matrix in the fictional demo.

## 0.11.0

- Make Studio measure-first, deferring rewrite checks until selection and moving matrices into an optional landing-visual choice with no automatic selection.
- Retain landing axes, filters, totals/drill and unsupported flags while targeting only the selected measure; export representative context queries and root-scoped summary-table PBIP alternatives.
- Reset stale generated projects and model selection when context changes, and keep measure analysis available when optional matrix imports fail.
- Add explicit landing context options to optimize-measure and selected-measure scoping to project. Incomplete/cyclic custom proposals retain graphs but withhold validation probes.

## 0.10.0

- Add a searchable Optimize one measure flow in Studio, with direct actions on ranked rows and no matrix requirement.
- Show root-only proposals, original DAX/helper lineage, paired DAGs and copy/download actions; explain when no supported automatic rule applies.
- Compare user-authored proposals for the selected root and download explicit scalar-total value/BLANK probes without executing queries or changing model definitions.
- Add optimize-measure, review_measure and measure/table scoping in plan_rewrites. Reuse completed analysis in Studio and include a fictional SUMX-to-SUM demo measure.

## 0.9.1

- Replace the generic Studio busy message with actual analysis stages, current objects/measures, known item counts, stage/total elapsed time and a persistent completion/failure summary.
- Distinguish work checkpoints from browser connectivity; show indeterminate progress when totals are unknown and support explicit retry after analysis failures.
- Reuse the same model/matrix analysis for Studio eligibility checks, preserving export guards without repeating full analysis per matrix.
- Add observer/lifecycle regressions and verify desktop/mobile progress, connection recovery and corrected-input retry in a browser.

## 0.9.0

- Explain original and proposed measure computation DAGs side by side, including helper measures, parsed operators, exact DAX, shared dependencies and structural differences.
- Separate query-time dependencies from calculated-table/column refresh lineage so materialization work remains visible; unknown syntax/bindings stay explicit and no speedup is inferred.
- Export reusable original/proposed JSON, text and Graphviz DOT alongside offline HTML. Add `compare-dags` and public Python APIs for root-expression or separate-model comparisons.
- Include comparison reports automatically in guarded rewrites, rewrite experiments and generated one-matrix PBIP showcases, with a direct Studio link.
- Add fictional regression coverage; this feature executes no DAX and changes no source models.

## 0.8.0

- Add dependency-free local Studio: workspace file browsing, bundled fictional demo, ranked findings, optional native Performance Analyzer v1 import, matrix context review/builder, guarded generation, ZIP download and Windows Desktop opening.
- Discover loaded models with matching-definition hints while requiring explicit selection. Add an explicit checksum-verified installer for the pinned official x64 Windows ADOMD .NET Framework client; client DLLs remain outside distributions.
- Add showcase-run and a Python API to verify generated definitions/physical counts, check every prefix context and BLANK flag, withhold all timing for failed or all-BLANK pairs, then time separate baseline/candidate paths.
- Retain canonical query/model hashes, raw results, progress and failure reports; recheck loaded definitions/row counts and project files after timing. Exclude one explicit warm-up, alternate workload order across contexts, and label overlapping/noisy results inconclusive.
- Protect the loopback interface with a per-session token, same-origin POST checks and bounded workspace/artifact browsing. Native recording timings remain shared DAX query contexts, never isolated measure cost or CPU/FE/SE.
- Add invented regression coverage and Windows/Linux install checks. This new guided orchestrator has not yet been rerun against a live Desktop engine; the earlier bounded 0.7.1 integration evidence still applies to the exporter/client foundations.

## 0.7.1

- Discover local catalogs through ADOMD XMLA schema discovery instead of routing schema SQL through DAX execution.
- Fix generated showcase summary grouping-column bindings, PBIR content version and embedded-model enhanced metadata so the comparison matrix opens and refreshes in Desktop.
- Preserve LF query bytes on Windows so generated query fingerprints match the files on disk.
- Manually validate one fictional showcase on Windows Server 2022 with Power BI Desktop 2.159.883.0, Python 3.14 and the official ADOMD 19.118.3 client: all six prefix contexts pass exact value/BLANK comparisons. Separate warm timings are mixed; no performance improvement is established. This bounded check does not establish compatibility for every model or replace engine integration tests.

## 0.7.0

- Make DAX Studio optional: benchmark and guarded rewrite runners default to a direct local Windows ADOMD client when no dscmd path is supplied.
- Add the optional pip engine extra using Python.NET, best-effort Desktop endpoint/catalog discovery, and single-result DAX-to-CSV execution.
- Isolate .NET work in timed subprocesses and commit complete CSV outputs without overwriting existing results.
- Label client elapsed timings explicitly; never promote them to CPU/FE/SE traces. Direct mode does not clear caches or deploy/refresh models.
- Retain the dscmd backend for tracing and explicit cold-cache tests. Add fictional client/evidence regressions and Windows client-load/serialization CI; live Desktop/DAX integration remains unverified.

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
