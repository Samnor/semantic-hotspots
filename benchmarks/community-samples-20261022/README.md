# Twenty additional public Power BI reports

This extension adds **20 community reports, 667 explicit measures**, without reusing any of the previous ten PBIX files; all twenty have distinct explicit-measure fingerprints. Source files are pinned by immutable Git revision, Git blob SHA-1 and downloaded SHA-256 in [sources.json](sources.json). The 11 repositories have verified MIT licenses at the pinned revisions. Original author/license notices remain in local downloads; PBIX files, source data, complete source DAX and raw cell results are not redistributed here.

| Report | Explicit measures attempted | Every baseline probe succeeds |
|---|---:|---:|
| [Agents Performance - Dashboard](https://github.com/Dashboard-Design/Power-BI-Design-Files/blob/3c56a12f6481d24acdf6c01442a496d020f76e55/Full%20Dashboards/Agents%20Performance%20-%20Dashboard/Agents%20Performance%20-%20Dashboard.pbix) | 102 | 102 |
| [Ecommerce Conversion Dashboard](https://github.com/Dashboard-Design/Power-BI-Design-Files/blob/3c56a12f6481d24acdf6c01442a496d020f76e55/Full%20Dashboards/Ecommerce%20Conversion%20Dashboard/Ecommerce%20Conversion%20Dashboard.pbix) | 33 | 33 |
| [Exceutive Sales Report - Toast Notifictaion](https://github.com/Dashboard-Design/Power-BI-Design-Files/blob/3c56a12f6481d24acdf6c01442a496d020f76e55/Full%20Dashboards/Exceutive%20Sales%20Report/Exceutive%20Sales%20Report%20-%20Toast%20Notifictaion.pbix) | 52 | 47 |
| [GeoSales Dashboard - Azure Map](https://github.com/Dashboard-Design/Power-BI-Design-Files/blob/3c56a12f6481d24acdf6c01442a496d020f76e55/Full%20Dashboards/GeoSales%20Dashboard%20-%20Azure%20Map/GeoSales%20Dashboard%20-%20Azure%20Map.pbix) | 43 | 43 |
| [IT_Support_Ticket_Desk](https://github.com/Dashboard-Design/Power-BI-Design-Files/blob/3c56a12f6481d24acdf6c01442a496d020f76e55/Full%20Dashboards/IT%20Support%20Performance%20Dashboard/IT_Support_Ticket_Desk.pbix) | 26 | 26 |
| [bank_loan_data_insights](https://github.com/virajbhutada/InsightForge/blob/ee899a822e05377803d0cfa805bfe6a7958bac53/Bank%20Loan%20Insights/powerBI/bank_loan_data_insights.pbix) | 28 | 28 |
| [Global_Univ_Analysis](https://github.com/virajbhutada/InsightForge/blob/ee899a822e05377803d0cfa805bfe6a7958bac53/Global%20Universities%27%20Success%20Analysis/powerBI/Global_Univ_Analysis.pbix) | 4 | 4 |
| [HR_Insights_&_Trends](https://github.com/virajbhutada/InsightForge/blob/ee899a822e05377803d0cfa805bfe6a7958bac53/HR%20Workforce%20Analysis/analysis/HR_Insights_%26_Trends.pbix) | 1 | 1 |
| [movie_rental_analysis](https://github.com/virajbhutada/InsightForge/blob/ee899a822e05377803d0cfa805bfe6a7958bac53/Movie%20Rental%20Store%20Analysis/powerBI/movie_rental_analysis.pbix) | 5 | 5 |
| [Road_Accident_Analysis](https://github.com/virajbhutada/InsightForge/blob/ee899a822e05377803d0cfa805bfe6a7958bac53/U.K.%20Road%20Accident%20Analysis/Road_Accident_Analysis.pbix) | 6 | 6 |
| [US-healthcare-dynamics](https://github.com/virajbhutada/InsightForge/blob/ee899a822e05377803d0cfa805bfe6a7958bac53/U.S.%20Healthcare%20Industry%20Dynamics/analytics/US-healthcare-dynamics.pbix) | 32 | 32 |
| [City Sustainability Scorecard (World Champs 26)](https://github.com/gusbavia/city-sustainability-scorecard/blob/87020be37c73b8b5e34c91407919987e55e75780/City%20Sustainability%20Scorecard%20%28World%20Champs%2026%29.pbix) | 223 | 223 |
| [The World of Peaches (World Champs 26)](https://github.com/gusbavia/the-world-of-peaches/blob/0692068e53acf75550c0290824f0fc0aa6090a8a/The%20World%20of%20Peaches%20%28World%20Champs%2026%29.pbix) | 45 | 44 |
| [Healthcare_Data_Analysis](https://github.com/gopiashokan/Healthcare-Data-Analysis-using-PowerBI/blob/10e3d59f3d6e3a0a8349d25d503d025824d56d23/Healthcare_Data_Analysis.pbix) | 2 | 2 |
| [T20_WorldCup_Analysis](https://github.com/shivanshka/Cricket_Analytics/blob/2cc4010796c2298ad90f7fceab656b2b8839a851/T20_WorldCup_Analysis.pbix) | 27 | 27 |
| [Inventory Management Dashboard _Final](https://github.com/BasseyIsrael/Inventory-Management-System/blob/84a183a2a8f8d05b9b22148a5e495bf99ff29131/Dashboards/Inventory%20Management%20Dashboard%20_Final.pbix) | 10 | 10 |
| [Data_Jobs_2024_3.0](https://github.com/dyneth02/Data-Jobs-2024-3.0-PowerBI-Project/blob/fbb69612ff3f5acb260f64280e47d5427d54357e/Data_Jobs_2024_3.0.pbix) | 1 | 1 |
| [Customer Churn Dashboard](https://github.com/DataVisualizationExpert/Customer-Churn-Analysis-using-Power-BI/blob/5990fa65dde98c2a76aa293cac70c84606d54b47/Customer%20Churn%20Dashboard.pbix) | 13 | 13 |
| [dash](https://github.com/haroldeustaquio/Analysis-of-Oncological-Diseases-in-Peru/blob/effa5a705953003c2e0aa728caff2cdf117597c2/Dashboard/dash.pbix) | 8 | 8 |
| [Crime_Statistics_2024](https://github.com/shaun-mia/Power-BI-Data-Analysis-Project/blob/ded7c418b6e746f9e55af69cdf4352af1018e0c5/Bangladesh%20Crime%20Data%20Analysis_2024/Crime_Statistics_2024.pbix) | 6 | 6 |
| **Total** | **667** | **661** |

## Outcomes and iteration

The old release produced no automatic proposals for these models. Two new guarded proposals now pass exact finite value and BLANK checks: a constant-one SUMX in the IT support ratio, and a row-invariant direct COUNT in the movie-rental report. Their source-connected follow-up includes **79 candidate comparison rows**, plus separate original-alias comparisons. All **10 follow-up timing contexts are inconclusive** under the stated descriptive rule. No clear speedup or general optimality is established, and unchanged measures are retained.

- `OPT008` retains the complete table expression while changing `SUMX(t,1)` or `COUNTX(t,1)` into `COUNTROWS(t)`. Empty input remains BLANK.
- `OPT009` replaces a physical-table SUMX of a directly repeated same-table COUNT with integer count multiplication. No measure callback, CALCULATE or enclosing row context is allowed; moved column references are qualified to preserve binding. The engine may already normalize this form.
- Single-underscore VAR names, numeric source spans such as `1.`, `.75` and `1e-3`, COUNTX row scopes and DATEDIFF intervals increase supported root expressions from **288 to 303**, and retained logical structure from **392 to 421**. Unknown syntax/context remains guarded.
- New experiment bundles time both paths through the same query-scoped alias and validate the original alias against the saved measure separately. Failed/missing binding evidence withholds timing.
- Experiment winner selection now requires a median change above both 10% and 1 ms and nonoverlapping ranges in every completed phase. Small/overlapping gains and legacy unmatched query scopes remain inconclusive.
- Desktop returned one definition with `COUNT` changed to `count`. A case-aware repeat completed all eight measures in that report. Helper recovery now tolerates function/keyword casing differences while string literals and changed constants remain conflicts.
- A GeoSales SVG result exceeded the CSV reader's default 128 KiB field limit. Repeating that report with a bounded larger reader completed all 43 measures. The library's validation reader now accepts large quoted text/SVG cells with a bounded field limit.

Six source measures fail some probes: five in the executive-sales report refer to absent parameter tables, and one World of Peaches trend measure supplies BLANK arguments to GENERATESERIES in three contexts. Their **28 context errors** are retained. No source definition was invented or silently repaired. Static parser limitations, refresh-expression/virtual-column binding and calculation-group guards are distinct from these actual runtime errors.

## Protocol

Original embedded data were opened without refreshing sources or substituting mocks in Power BI Desktop 2.159.883.0 on Windows Server 2022, using Python 3.14 and the official Microsoft ADOMD client. File checksums and every explicit loaded measure definition were checked, allowing identifier/function casing differences while retaining string literals exactly. PBIXRay 0.15.5 supplied the offline metadata projection; the core library itself does not read binary PBIX.

Every measure was attempted at a scalar total, an empty-largest-table filter, up to two dimension slices of 12 members and a 4-by-4 matrix grid with subtotal rows, where suitable axes were available. Baselines use one excluded prime and three warm samples; individual queries have a three-second timeout. Automatically chosen axes can be disconnected tables or hidden auto-date fields. Those exploratory probes are recorded, not claimed as the report's actual visual workload.

Both candidates were repeated against **explicit source-connected axes**: cluster/country for IT support, category/rating for films. The empty filter targets the actual iterator source. Candidate and original-formula aliases each must match the saved measure using strict DAX `==` and separate ISBLANK flags. Nonblank evidence is required before timing. Both paths define the same alias and output shape; two priming queries are excluded before **15 alternating AB/BA warm pairs** per context.

A faster/slower finding requires nonoverlapping sample ranges and a median change exceeding both 10% and 1 ms. This is a conservative descriptive rule, not a significance test. Timings include execution and complete result reading/conversion; they are not isolated measure CPU, FE/SE time, cold-cache performance, source-refresh costs, RLS coverage or production predictions. Source checksums were rechecked after the candidate follow-up.

[results.json](results.json) includes every measure, context, failure, candidate check summary, source-expression hashes and retained timing samples. Original source-connected checks and failed exploratory attempts are distinguished in the local evidence report. [screening.json](screening.json) records the ordered selection, exclusions with no explicit measures and unused reserves; selection did not depend on finding a speedup.

## Reproduce and develop further

Download and checksum the pinned reports, keep each embedded model unchanged, and export a full PBIP/TMDL or BIM projection from Desktop. Preserve parameter/calculated tables, hidden measures, formats and calculation groups. Analyze every explicit measure with `analyze(model)` and `create_measure(model, measure.id, analysis=analysis)`; retain unchanged outcomes. Open one report at a time and explicitly select its local server/catalog for read-only ADOMD comparisons.

Use the recorded context specifications to reconstruct the ROW/SUMMARIZECOLUMNS probes. Compare both proposed and original query-scoped aliases against the saved measure before matched timing. Include meaningful nonblank results, empty source selections and subtotal rows, prime both paths, alternate order and retain all samples. A passing finite result is not a universal equivalence proof.

The next coverage work should address typed SELECTEDVALUE/ALLSELECTED and iterator support, SVG/string structure, and refresh-phase virtual-column binding. Their semantics must be established before increasing automatic rewrite coverage. A broader rule registry alone would not establish faster measures.
