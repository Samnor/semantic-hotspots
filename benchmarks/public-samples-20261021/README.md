# Ten public Power BI reports

This corpus audit uses Microsoft’s published PBIX samples at the immutable revision in [sources.json](sources.json). It analyzes every explicit measure, including hidden and parameter measures. Original PBIX files, embedded data and raw cell results are not redistributed. Preserve Microsoft/obviEnce notices and follow the [sample terms](https://learn.microsoft.com/en-us/power-bi/create-reports/sample-datasets#legal-guidelines-for-using-samples).

| Report | Explicit measures | All four baseline contexts executed |
|---|---:|---:|
| AdventureWorks Sales | 20 | 20 |
| Artificial Intelligence | 29 | 29 |
| Life Expectancy | 46 | 41 |
| Corporate Spend | 19 | 19 |
| Employee Hiring and History | 25 | 25 |
| COVID-19 US Tracking | 11 | 11 |
| Revenue Opportunities | 6 | 6 |
| Store Sales | 32 | 32 |
| Sales & Returns | 58 | 58 |
| Supply Chain | 4 | 4 |
| **Total** | **250** | **245** |

Competitive Marketing, Regional Sales and the DAX training file were screened out because they contain no explicit measures. Selection did not depend on finding performance improvements.

## Findings

The first iteration proposed nine changes. Two floating-point DIVIDE proposals failed strict equality. The second iteration retained the division operator and cached the denominator in a local variable; four floating-point proposals then failed strict equality. No tolerance was substituted for exact comparison. Both rule families now require exact numeric operands, so **all six floating-point division proposals are withdrawn**, including two that passed their finite second-round checks.

Three existing SUMX/helper proposals remain eligible in Artificial Intelligence. They passed their tested exact value/BLANK checks, but no clear performance improvement was established. The other measures remain unchanged; this does not establish that they are optimal.

Five Life Expectancy measures reference columns absent from the published model. The original engine queries fail in all four tested contexts. Definitions or replacement columns were not fabricated.

[results.json](results.json) retains all 250 measure outcomes, baseline costs, experimental timing samples, and which proposals remain eligible after the final numeric guards. It intentionally preserves rejected experiments. `outcome` describes the experiment; `retained_automatic_proposal` describes the final optimizer.

## Method

- Original downloaded PBIX files were checksum-verified and opened in Power BI Desktop **2.159.883.0** on a disposable Windows Server 2022 VM with Python 3.14 and the official Microsoft ADOMD client. Cached embedded rows were used without refreshing sources or substituting mock data.
- Read-only `INFO.VIEW.MEASURES()` results verified all extracted measure definitions. Literal format expressions were normalized into preserved static formats; nonliteral formatting remains guarded. Blank source definitions normalized to `BLANK()` by Desktop were recorded from the live model.
- Every measure was attempted at a scalar total, an empty dimension filter, and two actual dimension slices of up to 12 members. An empty dimension filter need not empty the fact table, especially for disconnected dimensions. These are representative probes, not captured report visual workloads or exhaustive matrix/RLS tests.
- Opening-page visuals settled for 15 seconds. Each baseline context had one excluded priming execution and seven recorded warm executions.
- Candidate-versus-saved and original-alias-versus-saved probes use strict DAX equality plus separate `ISBLANK` flags. Failed candidates were not timed. Nine second-round candidates were checked across 213 candidate rows; five passed every tested context. Original-alias checks are separate.
- Both timing paths declare the same query-scoped measure alias and output shape. Each context has 15 warm samples per path in alternating AB/BA order after both paths are primed. This corrects the first iteration’s asymmetric saved-versus-query-scoped comparison.
- A descriptive faster/slower finding requires nonoverlapping sample ranges and a median difference above both 10% and 1 ms. All 20 timed second-round contexts are inconclusive by this rule. This is not a statistical significance test.
- Timings include execution and complete result reading/conversion. They are not isolated measure CPU, server FE/SE time, cold-cache performance, production predictions, DirectQuery source costs or capacity estimates. Separate exact-integer total/empty calibration probes passed for both new rules; they are not additional original sample measures.

## Reproduction

Download the pinned URLs in `sources.json` and verify SHA-256. Open one report in Desktop, retain its embedded data, and export its full semantic model as PBIP/TMDL or BIM. The core library does not read binary PBIX; this audit used the separate PBIXRay 0.15.5 metadata extractor and checked its projection against the live model. In particular, retain calculated/parameter tables, hidden measures, calculation groups and format definitions.

Install this release with the optional Windows engine extra and run `python -m semantic_hotspots engine-setup` if the official ADOMD client is absent. Analyze the complete export:

```python
from pathlib import Path
from semantic_hotspots import load_model, analyze, create_measure

model = load_model("model.bim")
analysis = analyze(model)
analysis.write("audit")
for number, measure in enumerate(model.measures):
    if measure.expression:
        create_measure(model, measure.id, analysis=analysis).write(
            Path("proposals") / str(number)
        )
```

Explicitly select the matching local server and catalog with `desktop_sessions()`. The optional ADOMD backend runs DAX queries without DAX Studio. Before timing, compare each proposal to the saved measure with strict equality and explicit BLANK flags in every selected context. Use matching query-scoped original/candidate definitions, exclude priming runs, alternate order, retain all samples, and recheck definitions and source checksums. Do not promote a failed or slower candidate.
