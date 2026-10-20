# semantic-hotspots

An offline Python library and command-line tool for exploring Power BI semantic-model measure chains and prioritizing performance investigations.

**Pure Python, Python 3.10+, no runtime dependencies.** The universal wheel installs on Windows without Rust, Visual Studio, .NET, Power BI Desktop, or a compiler. Parsing, static analysis, reports and mock-data generation run locally without a cloud account or API key. The explicit benchmark command invokes a separately installed DAX Studio against an engine endpoint you choose; model deployment is not automatic.

## Install in a virtual environment

Python 3.10 or later is required. Create a virtual environment, then install the universal wheel from the GitHub release. On Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install "https://github.com/Samnor/semantic-hotspots/releases/download/v0.4.0/semantic_hotspots-0.4.0-py3-none-any.whl"
.\.venv\Scripts\python.exe -m semantic_hotspots --version
```

On macOS or Linux, use `.venv/bin/python` instead. No activation, compiler, Git installation, cloud account, or API key is needed to install the wheel. The package has no runtime dependencies and has not been published to PyPI.

For a source installation, with Git installed:

```shell
python -m pip install "git+https://github.com/Samnor/semantic-hotspots.git@v0.4.0"
```

To develop from a checkout, use `python -m pip install -e .` from the repository root. DAX execution requires a separately installed engine and DAX Studio; parsing, analysis, reports, and mock-data generation run locally without them.

## Analyze a semantic model

```powershell
py -m semantic_hotspots analyze "C:\Reports\Retail.SemanticModel" -o "C:\Reports\hotspots"
py -m semantic_hotspots analyze "C:\Reports\model.bim" --top 10 -o hotspots
```

Alternatively use the installed `semantic-hotspots` command. `python -m semantic_hotspots` avoids problems with the Windows Scripts directory not being on PATH.

Outputs:

- `report.html`: offline overview, highest-priority measures, searchable ranking and clickable drill-downs into fixes, chains, DAX and query evidence.
- `analysis.json`: ranking, score breakdown, graph, diagnostics and optional runtime evidence.
- `chains/*.json`: reusable dependency graph for every measure.
- `chain-index.json`: measure IDs mapped to portable filenames.

```powershell
py -m semantic_hotspots chain model.bim "Revenue" --table Sales -o chain.json
py -m semantic_hotspots chain model.bim "Revenue" --format text
py -m semantic_hotspots chain model.bim "Revenue" --format dot -o chain.dot
```

Graph edges run **consumer → dependency**. `evaluation_order` is dependency-first and is supplied only for an acyclic graph. Cycles are returned explicitly; they are never mislabeled as DAGs. A chain includes measures, columns, tables, referenced user-defined functions and applicable dynamic measure-format expressions. Inactive relationships are retained as metadata but excluded from potential active filter-path traversal.

## Read the HTML report

Open `report.html` in a browser. It works offline without a server or internet access.

1. **Overview:** model coverage, distinct findings, unique captured query totals (if supplied), and the first three priorities with a suggested investigation.
2. **Measure ranking:** descending native query-context CU cost when matched observations exist, otherwise recorded CPU, otherwise estimated review priority. Unknown values come last. Search by measure or table name.
3. **Measure details:** click any measure, then open findings and candidate fixes, its dependency chain, runtime evidence, DAX definition, or score/completeness details. Fixes include evidence and correctness checks. Dependency links jump to the referenced measure; query links reveal its directly requested measures and telemetry coverage.
4. **Coverage:** scoring assumptions and model/telemetry diagnostics.

Runtime costs are inclusive whole-query contexts and overlap across measures. The overview counts each query operation once. Partial measurements are labeled as recorded subtotals; unknown cost remains unknown. Cost bars scale to the largest known measure-context value, not a share of total cost. Static points are clearly labeled as estimated review priority, never measured cost.

The HTML embeds its styles and interactions. Keep `analysis.json`, `chain-index.json` and `chains/` beside it if you want its JSON download links to work. HTML navigation and all findings remain available if the HTML file is copied alone.

## Python API

```python
from semantic_hotspots import analyze, load_model

model = load_model(r"C:\Reports\Retail.SemanticModel")
result = analyze(model)

for measure in result.ranked[:10]:
    print(measure["name"], measure["risk_score"], measure["chain_complete"])

result.write("hotspots")
root = model.measure("Revenue", table="Sales")
chain = result.graph.to_dict(root.id)
dependency_first = result.graph.dependency_order(root.id)
```

`parse_model(dict)` reads in-memory BIM/TMSL or supported export envelopes. `analyze()` accepts a model object, a path, or a model dictionary. `ModelError` covers invalid/ambiguous inputs; `CycleError` is raised when a dependency-first ordering is requested for a cyclic chain. JSON output uses schema version `1.0`. The package is typed (`py.typed`).

## What “heaviest” means

Model definitions alone do not reveal actual engine CPU, cardinalities scanned, source-query latency, evaluated branches, or Fabric CU. Without usable runtime evidence, the default ranking is an **ordinal review priority**, with a complete score breakdown. It is not a runtime prediction or a complexity multiplier. With a runtime profile, automatic ranking prefers matched native query-context CU, then matched CPU, then static review priority. Explicit `rank_by` / `--rank-by` overrides this choice. Recorded zero values are known observations and sort before unknown values.

Each nonempty expression starts at one point. Explicit rules add points; a measure's score uses the longest weighted dependency path, a small logarithmic breadth term, and applicable relationship-context points. Unique dependencies are represented once. Branches are not assumed to all execute; textual occurrences are not engine evaluation counts. Cyclic/blocked chains have an unknown score. Incomplete chains remain visibly partial.

Patterns include:

- Iterators containing iterators, measure callbacks or explicit context transition.
- Whole-table `FILTER` under `CALCULATE`, distinguished from a predicate that itself requires a measure.
- A `SUMX` over one physical column, as a candidate for checking `SUM` equivalence.
- Repeated measure references, with context-sensitive variable-hoisting guidance.
- Virtual filter transfers, potentially expanding intermediate tables and text formatting.
- Import/DirectQuery filter paths, bidirectional and many-to-many relationship context.
- Time-dependent definitions, missing references, cycles and dynamic selected-measure behavior.

Every suggestion provides evidence, a source-expression line, and correctness checks. **No source model is rewritten.** Guarded alternatives are now generated for the narrow subset described below; they remain unvalidated until executed. Validate the grouping grain, filter context, BLANK behavior and totals before adopting a candidate change.

## Add runtime evidence

With offline semantic logs, the library can rank measures by expensive **query contexts**. Native capacity observations can be joined by dataset and operation ID. These columns are inclusive and non-additive: if a query selects two measures or contains an expensive descendant, the library does not pretend it measured each measure separately.

```python
from semantic_hotspots import RuntimeProfile, analyze, load_model

model = load_model("model.bim")
profile = RuntimeProfile.from_logs(
    model,
    "semantic-logs.json",
    "capacity-logs.json",          # optional
    dataset_id="your-dataset-id",  # required when the capture contains multiple models
    start="2025-01-01T14:00:00Z",   # optional, inclusive QueryBegin bound
    end="2025-01-01T15:00:00Z",     # optional, exclusive QueryBegin bound
)
result = analyze(model, profile=profile)  # automatic CU → CPU → static priority
# Use rank_by="query_cpu_context" to explicitly prioritize recorded CPU instead.
result.write("profiled-hotspots")
```

```powershell
py -m semantic_hotspots analyze model.bim --semantic-logs semantic.json --capacity-logs capacity.json --dataset-id DATASET-ID --rank-by query_cu_context -o profiled-hotspots
```

Formats accepted: a JSON row array, `{"rows": [...]}`, `{"value": [...]}`, or UTF-8 CSV with the following columns. Arbitrary Power BI Performance Analyzer exports are **not** silently treated as this format.

| File | Required columns | Meaning |
|---|---|---|
| Semantic | `ItemId`, `OperationId`, `OperationName`, `EventText` | `QueryBegin`, `QueryEnd`, `ExecutionMetrics`, `DirectQueryEnd`, `Error` rows from SemanticModelLogs |
| Semantic with time filtering | `Timestamp` | Timezone-aware ISO timestamp |
| Native capacity | `EventId`, `ItemId`, `OperationId`, `OperationName`, `CUSeconds` | Normalized native `Query` operation events; `CUSeconds` is already in seconds |

For `QueryBegin`, `EventText` contains the DAX query. For `ExecutionMetrics`, it contains a JSON object such as `{"totalCpuTimeMs": 100, "durationMs": 200}`. Discovery metrics are excluded. `CUSeconds` must be normalized from native `capacityUnitMs / 1000` by the exporter; the library never guesses units.

Capacity events are deduplicated by `EventId`, and different cost fragments for the same operation are summed. Conflicting repeated event IDs are rejected. Missing cost/CPU values stay **unknown**, including zero-CPU queries with no cost event. Partial query-end/metric coverage is reported. Wait for telemetry ingestion and provide a complete capture window before drawing conclusions.

Runtime analysis records direct query references and **potential chain contexts** separately. It does not prove a child measure executed in every branch, split a shared query's cost among measures, or equate total database accounting with one report query. Query-scoped `DEFINE MEASURE`/`FUNCTION` overrides are skipped with diagnostics to avoid false model attribution.

## Supported files and limits

| Input | Support |
|---|---|
| `model.bim`, model JSON, one-model TMSL create/alter wrapper | Measures, columns, expressions, partitions/storage modes, relationships, calculation items, functions and dynamic measure format strings |
| TMDL file or definition folder | Common object declarations, quoted names, indented and triple-backtick expressions, storage modes, relationships, calculated columns/tables, functions and calculation items |
| `.SemanticModel` directory or one-model PBIP project folder | Automatic `definition`/`model.bim` discovery; multiple models require an explicit selection |
| Fabric `getDefinition` JSON | InlineBase64 BIM or TMDL parts; no remote payload retrieval |
| Partial measure-catalog JSON | User-supplied definitions; missing columns, relationships and definitions remain unknown |
| Binary `.pbix` / `.pbit` | Not supported; export/save as PBIP/TMDL or BIM first |

General lineage uses lexical/call structure and metadata projection; rewrite proposals additionally use a small bound, typed logical subset. This is **not a full DAX compiler, TOM serializer, M parser, VertiPaq analyzer, query-plan profiler or engine binding authority**. It handles strings, comments, escaped identifiers, nested calls and case-insensitive model-name lookup. Complex variable/extension-column scopes and ambiguous names remain conservative diagnostics. Selected-measure calculation-group dependencies cannot be fully resolved statically. RLS expressions and calculation-group activation/precedence are not expanded into measure graphs. Table storage defaults/mixed partitions can remain unknown when not supplied.

TMDL's `ref` declarations and perspective entries are not table definitions; supply the full model folder. Model inputs are read-only. Connection strings and M/SQL partition source text are not retained in exported analysis; DAX expressions and source paths are retained. Reports may therefore contain your business logic and should be handled like the source model.

For authoritative dependency bindings, compare the output with an engine `INFO.CALCDEPENDENCY()` export. Its absence never causes this library to invent missing model objects. Suggestions are candidates to benchmark, not guaranteed fixes.

## Examples, tests and build

```powershell
py -m semantic_hotspots analyze examples\retail.bim -o example-report
py -m semantic_hotspots analyze examples\retail.bim --semantic-logs examples\semantic-logs.json --capacity-logs examples\capacity-logs.json -o example-profile
py -m unittest discover -s tests -v
py -m pip install build
py -m build
```

The test suite covers syntax edge cases, Unicode, TMDL/BIM equivalence, cycle handling, long chains, missing metadata, telemetry deduplication/non-additive attribution, automatic cost ranking, report drill-down targets, unique-query summary totals, partial coverage, output escaping and CLI behavior. GitHub Actions runs the test suite, wheel installation, and fictional example smoke checks on Windows and Linux with Python 3.10–3.14. Check the Actions tab for the status of a specific commit. DAX engine execution is a separate, user-configured integration step.

The example telemetry files are fictional demonstrations, not benchmark results.

## Generate mock source tables and benchmark without real data

The optional synthetic workflow produces **seeded data and a separate Import-mode BIM** using the model's schemas and relationships. It runs locally and does not connect to the real sources. Generation works with the same dependency-free Python package; measuring DAX requires a separately installed, compatible Analysis Services / Power BI engine.

```powershell
py -m semantic_hotspots simulate model.bim -o synthetic-lab --fact-rows 100000 --dimension-rows 1000 --seed 42
# Or customize row counts, distributions and queries:
py -m semantic_hotspots simulate examples\retail.bim --config examples\simulation-config.json -o retail-lab
```

The output directory must be new or empty. Outputs include:

- `simulation.html`: overview of generated tables, assumptions and workload queries. It records **DAX not run at generation**; subsequent engine results use a separate report.
- `tables/*.csv`: UTF-8 source tables with portable hashed filenames.
- `synthetic-model.bim`: original DAX and relationships, with physical source partitions replaced by CSV-based Import partitions. Original data sources, M expressions, refresh policies and roles are removed. The original model is unchanged.
- `simulation.json`: effective settings, per-table row counts/checksums, column null/distinct observations, relationship generation assumptions and workload definitions. Distinct tracking is capped at 10,000 values per column and flags incomplete counts.
- `queries/*.dax`: one measure per query, including its whole dependency chain, for a scalar total and each configured grouped/filtered scenario. By default the 20 highest static-priority measures are included; configure `top` to change this.
- `analysis/`: the original model's static HTML findings, JSON and chain graphs.
- `RUN-INSTRUCTIONS.txt`: how to load, process and benchmark the synthetic model.

A full BIM/TMSL input preserves more metadata, including calculation groups, UDFs and dynamic formats. Common TMDL can generate a projected BIM; it is not a lossless model conversion. Advanced objects require the full BIM template. Partial measure catalogs cannot establish source schemas and are rejected for generation. Physical data types supported are int64, double, fixed decimal, string, boolean and dateTime. Missing types are explicit assumptions, overridable in config.

Relationship endpoints share typed key domains, including inactive relationships. One-side keys are unique/non-null; many-side keys sample matching parent values. One-to-one relationships need equal configured row counts for complete matching keys. Conflicting types/domains or insufficient unique values fail before file generation. Calculated columns/tables are left for the engine; relationships involving calculated/missing key columns are currently rejected because the generator cannot guarantee those computed keys. It does not run Power Query or infer hidden business rules or virtual `TREATAS` joins.

### Configure realistic shapes

`examples/simulation-config.json` is an editable retail example. The configuration uses exact table/column names:

```json
{
  "seed": 42,
  "fact_rows": 100000,
  "dimension_rows": 1000,
  "tables": {
    "Customer": {
      "rows": 250,
      "columns": {"Region": {"values": ["North", "South", "East", "West"]}}
    },
    "Sales": {
      "columns": {
        "CustomerKey": {"skew": 0.8, "null_fraction": 0.01},
        "Amount": {"min": 0, "max": 10000, "distinct": 1000}
      }
    }
  },
  "workloads": [
    {"name": "by-region", "group_by": [["Customer", "Region"]]},
    {"name": "north-only", "filters": [{"table": "Customer", "column": "Region", "values": ["North"]}]}
  ]
}
```

Table `rows` overrides inferred counts. Tables used as one-side lookups default to `dimension_rows`; other source tables default to `fact_rows`. Measure-only tables receive a hidden dummy row column. These are shape assumptions, not inferred production sizes.

Column options: `type`, `values`, `distinct`, `min`, `max`, `start`, `end`, `null_fraction`, `skew`. Dates use ISO dates and whole-day granularity. Ordinary date columns span the configured start/end window; relationship date keys stay consecutive from the start date. `skew` is the probability of choosing from the first 5% of the domain; the remaining samples are uniform. `distinct` specifies a domain size, not guaranteed observed distinctness. `values` explicitly supplies the domain; blanks use `null_fraction`. Configure shared key-domain settings consistently across related columns. Defaults assume a fixed 2024–2026 date range and generic categories; widen date ranges for large unique date keys.

Choose fact row counts, distinct dimension keys, category distributions, date windows, and representative grouping/filter scenarios. Exact category labels matter: generic categories can make literal comparisons and measure branches return BLANK. Columns participating in virtual joins or correlated date ranges need compatible settings. The generator does not infer business rules or real data distributions. Test both detail cells and totals.

### Run real engine benchmarks on Windows

1. Copy the bundle to the engine machine and set the `SyntheticDataRoot` M expression in `synthetic-model.bim` to its absolute `tables` directory. You can instead specify `--data-root` during generation. The path must be readable by the engine process, which may run as a different account.
2. Open/deploy the BIM through Tabular Editor or a compatible SSAS Tabular development environment into a **separate simulation model**; process/refresh its tables. Power BI Desktop does not directly open a standalone BIM. The library does not deploy or overwrite models automatically.
3. Connect DAX Studio to that loaded model. Run the query files interactively, or install DAX Studio **3.6+**, which supplies `dscmd.exe benchmark`, and run:

```powershell
py -m semantic_hotspots benchmark synthetic-lab --dscmd "C:\Program Files\DAX Studio\dscmd.exe" --server localhost:PORT --database Synthetic_DATABASE_NAME --warm 3 --cold 0
```

Use the actual server/database identifiers shown by your engine tools. With `--cold 3`, DAX Studio clears that database's cache before each cold iteration. The default `--cold 0` performs no cache clearing. Warm iterations mean no cache clearing; verify cache behavior when interpreting the first iteration. Each workload has a configurable timeout (`--timeout 300` seconds). Failures/timeouts and incomplete iteration coverage remain visible and produce a nonzero CLI exit code.

Before timing, run representative queries interactively and verify expected non-BLANK values and matrix totals. Timing CSVs contain row counts but do not validate result values.

Read `synthetic-lab/benchmark-results/benchmark.html`: highest mean query durations first, cache mode, p95, successful/failed iteration counts, workload DAX, and links back to static findings/chains. JSON and raw dscmd CSVs sit beside it. If you run dscmd separately, use the manifest's workload IDs for filenames (`result-ID.csv`) and collect offline:

```powershell
py -m semantic_hotspots benchmark synthetic-lab --collect-only --results path\to\results
```

DAX Studio can fall back to client wall-clock timings when server tracing is unavailable; its FE/SE/CPU columns may then contain placeholder zeros. The library leaves those metrics **unknown** unless you independently verified tracing and pass `--trace-verified`. Durations retain their trace-or-wall-clock label. No duration or CPU is converted to CU. The benchmark parser skips dscmd's appended summary rows and excludes failed iterations from timing statistics.

```python
from semantic_hotspots import SimulationConfig, simulate, run_benchmarks

bundle = simulate("model.bim", "lab-small", config=SimulationConfig(
    seed=42, fact_rows=100_000, dimension_rows=1000,
    data_root=r"C:\Labs\lab-small\tables",
))
# After loading and refreshing bundle.model_path in a separate DAX engine:
results = run_benchmarks(
    bundle, dscmd=r"C:\Program Files\DAX Studio\dscmd.exe",
    server="localhost:PORT", database=bundle.manifest["synthetic_model_name"],
    cold=0, warm=3,
)
```

Compare variants using identical seeds, row counts, distributions, filters, hardware and cache settings; generate separate bundles at several scales. The runner records the supplied target and query iterations but cannot verify that the loaded engine model matches your BIM. Import-mode timings do not reproduce DirectQuery source/network latency, original partitioning, production correlations, RLS, concurrent users, or Fabric capacity behavior. For DirectQuery experiments, load the CSVs into a test SQL source and deliberately rebind a separate test model; this version does not provision that database. No actual DAX engine execution is claimed by the local generator tests.

The M import follows [Microsoft's Csv.Document documentation](https://learn.microsoft.com/en-us/powerquery-m/csv-document). Benchmark commands, CSV fields and trace fallback behavior follow the [DAX Studio benchmark command documentation](https://daxstudio.org/docs/features/command-line/commands/benchmark-command/).

## Guarded rewrite proposals and engine validation

Version 0.4 adds an optimizer-inspired proposal layer. The ordinary `analyze` report includes **Guarded rewrite proposals** under each measure: before/after DAX, typed partial logical trees, row/filter context annotations, direct dependencies, guard conditions, and replayable isolated or combined edit steps. `rewrite-plan.json` sits beside the report. Existing risk points remain review priorities, not an optimizer cost model.

The initial rule registry is deliberately small:

| Rule | Proposed change | Conditions |
|---|---|---|
| OPT001 | `SUMX(Sales, Sales[Amount])` → `SUM(Sales[Amount])` | Same physical base table, direct physical numeric column, known type, supported enclosing syntax |
| OPT002 | `FILTER(FILTER(Sales, p), q)` → `FILTER(Sales, (p) && (q))` | Same physical table; row-local comparisons of compatible columns/literals and Boolean combinations; no measures, calls, arithmetic or context changes inside predicates |

Recognized logical operations include scans, qualified columns, measure references, literals, parentheses, scalar binary operators, `SUM`, `SUMX`, `FILTER`, `COUNTROWS`, `CALCULATE`, and Boolean constants. This is **not a full DAX AST/compiler or executable physical planner**. VAR/virtual-table scopes, unsupported calls, unknown types, incomplete/cyclic chains, calculation groups, model functions, and dynamic root format expressions, and random/time-dependent chains withhold proposals. No variable hoisting, join reordering, general filter pushdown, automatic pre-aggregation or runtime plan fusion is implemented. The search emits at most 32 isolated proposals and a 16-step bounded fixed-point combination per measure. No timing benefit is inferred: the DAX engine may already normalize the alternatives.

```python
from semantic_hotspots import analyze, plan_rewrites

analysis = analyze("examples/rewrite-lab.bim")
analysis.write("reports/rewrite-review")
# API-only proposal generation; does not execute or modify the model:
plan = plan_rewrites("examples/rewrite-lab.bim")
for candidate in plan.candidates:
    print(candidate["before"], "=>", candidate["after"])
```

Prepare synthetic data and the experiment independently:

```powershell
python -m semantic_hotspots simulate examples/rewrite-lab.bim --config examples/rewrite-config.json -o synthetic-lab
python -m semantic_hotspots experiment synthetic-lab -o rewrite-experiment --top 20
```

Load and refresh `synthetic-lab/synthetic-model.bim` in a **separate test engine**, following the synthetic setup above. Then:

```powershell
python -m semantic_hotspots experiment-run rewrite-experiment --dscmd "C:\Program Files\DAX Studio\dscmd.exe" --server localhost:PORT --database SYNTHETIC_DATABASE --warm 5 --cold 0
```

The runner defines a unique **query-scoped alias measure**, leaving the loaded model unchanged. Each candidate changes only the root expression; its original dependencies remain intact. It checks a paired query using engine strict equality (`==`) plus explicit original/candidate `ISBLANK` flags for the scalar total and every configured grouped/filtered scenario. The equality flag also retains groups in which both values are BLANK. A mismatch, failed process, missing output or zero validation rows prevents timing that candidate. All-BLANK coverage is labeled uninformative and also prevents timing.

Passing candidates receive separate original/candidate DAX Studio benchmark queries on the same target, with alternating order between scenarios. The default clears no cache; `--cold` explicitly requests database-cache clearing. Keep the synthetic database, data, engine settings, hardware and concurrency stable. Warm results after paired validation may benefit from warmed engine caches. Repeat at several row counts, seeds, cardinalities and skews using separate simulation bundles.

Open `rewrite-experiment/experiment-results/experiment.html`. It ranks the highest **observed baseline mean query duration** first, with correctness and performance status separated. Click into each alternative for before/after DAX, logical plans/dependencies, scenario-specific original/candidate means, cache phases, iteration counts, percent reduction and exact query fingerprints. Mixed improvements/regressions are labeled `mixed_results`; incomplete timing stays unknown. No significance threshold or noise model is implied by `observed_improvement`. Among candidates with tested-case equality and no observed scenario regression, `selected_for_review` marks the lowest sum of equally weighted scenario/cache mean durations for that measure; this is a fixture-specific choice, not a production workload weighting.

`experiment.json`, query files, raw validation/timing CSVs and `rewrite-runner.json` retain reproducible evidence. Changed query files or mismatched capture fingerprints are rejected. Collect retained outputs without contacting an engine:

```powershell
python -m semantic_hotspots experiment-run rewrite-experiment --collect-only --results rewrite-experiment/experiment-results
```

```python
from semantic_hotspots import prepare_rewrite_experiment, run_rewrite_experiment

experiment = prepare_rewrite_experiment("synthetic-lab", "rewrite-experiment")
# After loading and refreshing the original synthetic BIM in a separate engine:
result = run_rewrite_experiment(
    experiment, dscmd=r"C:\Program Files\DAX Studio\dscmd.exe",
    server="localhost:PORT", database="SYNTHETIC_DATABASE", warm=5,
)
```

Equality covers **finite tested contexts**, not universal semantic equivalence. The runner does not compare runtime type metadata, verify loaded-model identity or attest offline CSV authenticity. Numeric comparison is exact; floating-point differences fail conservatively. Synthetic equality and timings do not establish production performance, DirectQuery latency, RLS behavior or CU. Add boundary values, duplicates, blanks, detail/subtotal scenarios, and empty selections to the workload configuration. The bundled example is fictional. Local and CI engine-runner tests use mocks; a real DAX engine is a separate integration requirement.

Query-scoped output collection follows the [DAX Studio CSV command](https://daxstudio.org/docs/features/command-line/commands/csv-command/). Rewrite guards use Microsoft's [SUMX](https://learn.microsoft.com/en-us/dax/sumx-function-dax) and [FILTER](https://learn.microsoft.com/en-us/dax/filter-function-dax) semantics. The design borrows the separation of analyzed logical structures and optimization rules illustrated by [Spark EXPLAIN](https://spark.apache.org/docs/latest/sql-ref-syntax-qry-explain.html).

## Rule references

The rule text was checked against these primary sources. Examples and advice are paraphrased; no page content is bundled.

- [TMDL syntax and expressions](https://learn.microsoft.com/en-us/analysis-services/tmdl/tmdl-overview)
- [FILTER arguments and Boolean-filter restrictions](https://learn.microsoft.com/en-us/dax/best-practices/dax-avoid-avoid-filter-as-filter-argument)
- [Variables and evaluation context](https://learn.microsoft.com/en-us/dax/best-practices/dax-variables)
- [CALCULATE and context transition](https://learn.microsoft.com/en-us/dax/calculate-function-dax)
- [Storage modes](https://learn.microsoft.com/en-us/power-bi/transform-model/desktop-storage-mode)
- [INFO.CALCDEPENDENCY](https://learn.microsoft.com/en-us/dax/info-calcdependency-function-dax)

MIT licensed. Version 0.4.0.

## Contributing and data handling

See [CONTRIBUTING.md](CONTRIBUTING.md) for development instructions and [SECURITY.md](SECURITY.md) for security reporting. All bundled models and telemetry are fictional. Keep model exports, generated reports, source data, credentials and local validation artifacts outside Git; these may contain business logic or sensitive values.
