# semantic-hotspots

An offline Python library and command-line tool for exploring Power BI semantic-model measure chains and prioritizing performance investigations.

**Pure Python, Python 3.10+, no runtime dependencies.** The universal wheel installs on Windows without Rust, Visual Studio, .NET, Power BI Desktop, or a compiler. Parsing, static analysis, reports and mock-data generation run locally without a cloud account or API key. An optional Windows `[engine]` extra connects directly to a local Power BI/Analysis Services engine through Microsoft ADOMD and Python.NET. DAX Studio is optional; model deployment is not automatic.

## Install in a virtual environment

Python 3.10 or later is required. Create a virtual environment, then install the universal wheel from the GitHub release. On Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install "https://github.com/Samnor/semantic-hotspots/releases/download/v0.17.0/semantic_hotspots-0.17.0-py3-none-any.whl"
.\.venv\Scripts\python.exe -m semantic_hotspots --version
```

On macOS or Linux, use `.venv/bin/python` instead. No activation, compiler, Git installation, cloud account, or API key is needed to install the wheel. The package has no runtime dependencies and has not been published to PyPI.

For a source installation, with Git installed:

```shell
python -m pip install "git+https://github.com/Samnor/semantic-hotspots.git@v0.17.0"
```

To develop from a checkout, use `python -m pip install -e .` from the repository root. DAX execution requires a separately installed engine and client libraries; parsing, analysis, reports, and mock-data generation run locally without them.

## Guided local workflow

After installing, launch Studio from your virtual environment:

```powershell
python -m semantic_hotspots studio --workspace .
```

The command opens a local browser interface. Select a PBIP, BIM or TMDL model within your workspace, or click **Try synthetic demo**; the demo is included in the wheel. You can browse an adjacent PBIR report, load existing matrix specifications, or build a matrix by selecting its rows and columns for the chosen measure. Imported PBIR contexts require explicit review; unsupported filters, roles and visual behavior remain blockers.

During analysis, a persistent activity panel shows the actual stage, current measure/object, completed-item counts where known, elapsed time and completed stages. Model reading, dependency tracing, rule/ranking review, rewrite checks, matrix grain review and report writing have separate checkpoints. A responsive Studio with no new work checkpoint is distinguished from a lost browser connection. Remaining time is unknown; no overall percentage or ETA is fabricated. Completion and failures remain visible, with a link to findings or an analysis retry. Matrix eligibility checks reuse the same request’s analysis instead of repeatedly reanalyzing every measure.

Studio starts with measure selection and **Create new measure**. If no automatic rewrite applies, the result says **Unchanged copy**, explains the coverage gap or blocker, and repeats that explanation above the paired DAGs. Matching DAGs in this case are expected and do not mean the original is optimal. Reference-binding failures identify the owning measure, reference and source location where available. Scope-analysis limitations are separate from detected cycles and missing definitions. `TODAY()` remains live in the generated production measure. Validation packages freeze both paths and known date-dependent helpers through query-scoped definitions at explicit as-of dates. `NOW`, `RAND`, `RANDBETWEEN`, stored calculated clocks and ambiguous references retain their guards. Visual contexts are optional and default to none; select any matching matrices with checkboxes. Initial analysis ranks measures and defers rewrite checks until creation. Copy or download the new named DAX for the same model. The separate advanced summary-table workflow generates a PBIP ZIP for one matrix. Open and refresh that project explicitly in Desktop, find the loaded model, choose it using the matching-definition hint, then click **Validate, then benchmark**. A matching hint does not auto-select or fully attest a loaded model.

Optionally choose a native **Performance Analyzer version 1.0.0 JSON export** when opening a model. Known recorded DAX contexts sort first; unknown contexts retain static priority. Measure associations use bound query references, and matrix associations require an exact unambiguous visual ID. Titles are not fuzzy-matched. These are inclusive/shared DSE wall-clock query durations through first row, not isolated measure cost, full result-reading time, CPU or FE/SE. Missing/truncated events and query-scoped bindings stay explicit. See Microsoft's [export schema](https://github.com/microsoft/powerbi-desktop-samples/blob/main/Performance%20Analyzer/performanceAnalyzerExport.schema.json) and [event-format documentation](https://github.com/microsoft/powerbi-desktop-samples/blob/main/Performance%20Analyzer/Power%20BI%20Performance%20Analyzer%20Export%20File%20Format.docx). Recordings are read offline and never executed.

For Windows engine access, install this release with `[engine]` as shown below, then use **Download & install official client** in engine setup, or run:

```powershell
python -m semantic_hotspots engine-setup
```

This explicit action downloads the pinned official Microsoft ADOMD 19.118.3 package over HTTPS, checks its SHA256, and extracts the x64 .NET Framework client plus dependencies into a per-user cache. The downloaded SDK is detected automatically. No engine, Python.NET or model is installed by this command. It requires x64 Python on x64 Windows. Existing unknown/modified SDK directories are not overwritten. An independently installed SDK can still be selected explicitly.

Studio binds only to `127.0.0.1`, uses a session token and same-origin POST checks, and serves only registered generated artifacts. Model/source data is not uploaded; the SDK setup button is the explicit external download. Generated files remain under `.semantic-hotspots-ui` by default. Use `-o OUTPUT` to choose another directory, `--no-browser` to print the local URL, and Ctrl+C to stop the server. The GUI is local; it is not a multi-user hosted service.

Guided workflows have automated orchestration, HTTP and desktop/mobile browser checks. Live engine integration evidence is bounded to the fictional scenarios described below; CI does not launch Power BI Desktop or execute DAX.

### Date-dependent measures and missing helpers

`TODAY()` is not a blanket rewrite blocker. Created measures keep their live clock. The package includes `date-controls.json` and separate `validation-dates/YYYY-MM-DD/` queries that override the original, candidate, and date-dependent helper measures and their callers for that query only. Stored callers are re-declared because a helper override alone does not replace already-compiled references. Defaults cover today, this month’s boundaries, and this year’s boundaries. Add the relevant planning-period and leap-day dates yourself. These finite comparisons do not establish universal equivalence or speedup. Stored calculated tables/columns using a clock cannot be frozen by measure overrides.

```powershell
python -m semantic_hotspots create-measure model.bim "Selected measure" --table Measures --as-of 2024-02-29 --as-of 2025-01-01 --definitions matching-export.bim -o measure-review
```

A missing helper definition does not prevent a local simplification that leaves its callback and context intact. For example, a stored `FILTER(t, NOT ISBLANK([@value]))` table variable passed to `SUMX(..., [@value])` can be replaced at that SUMX use with its base table variable. Other consumers of the filtered variable remain unchanged. This is a proposal, not an equivalence proof, and the engine may already optimize it.

In Studio, expand **Recover missing helper definitions**, choose a matching BIM/TMDL export and import it, or find open Desktop models and explicitly choose the matching one. Live recovery requires the `[engine]` extra and Microsoft’s official ADOMD client. It reads table/column/measure metadata, requires an exact token match for the selected root, and never edits the loaded engine. Exports that contain the root must match too. Existing definitions are not overwritten, ambiguous names are rejected, and only reachable missing definitions and metadata are merged into a copy for analysis. No helper formula is invented. If a definition remains unavailable, its exact requirement stays visible and validation remains withheld.

```python
from semantic_hotspots import recover_helpers, create_measure

recovery = recover_helpers("model.bim", "matching-export.bim", "Selected measure", table="Measures")
created = create_measure(recovery.model, "Selected measure", table="Measures", validation_dates=["2024-02-29"])
created.write("measure-review")
```


## Try a complex daily-production chain

```powershell
python -m semantic_hotspots production-lab --days 364 --sites 6 --products 8 -o daily-production-lab
```

Open `daily-production-lab/report.html` for the three new measures and their paired DAGs. Open `Showcase.pbip` in Power BI Desktop and **refresh** to load the embedded fictional rows. A single matrix compares each original with its candidate. There are no external source connections. To use Studio, analyze the generated `daily-production.bim`, select **Production rate by period**, **Stock coverage by period**, or **Target variance by period**, then choose **Create new measure**. Visual selection is optional.

The eight-table, 24-measure model starts at **day × site × product**, with an additional budget version on plan rows. Plans sum the visible budget versions; select one version for a single-plan context. Its shared chains include rejects, eligibility filters, workdays, rates, last-observation stock and actual/plan selection by planning period. It contains missing days, negative corrections, zeros, all-BLANK products, budget revisions and an unknown date member. Closing stock deliberately has different last observation dates across sites. Each root is a separate business rule: sum period rates, sum period stock coverage, or sum completed-period actual-minus-plan and future-period remaining target variance. These totals are intentional example rules, not universal KPI definitions.

The rate and stock candidates reduce six static `SUMX` operators to one period iterator; target variance reduces five to one. This is a structural count, **not a measured execution count or speedup**. The generated root specializes only helper paths with actual guarded changes. Every transplanted measure call retains `CALCULATE` context transition; model references are qualified in their original scope. Source helpers stay unchanged. Snapshots and ratios keep their existing aggregation rules. Floating-point and unestablished scalar values retain outer BLANK filters: removing those can change floating-point summation order even when the mathematical sum is unchanged. Expansion is limited to 32 reachable helpers and 50,000 characters, plus parser limits; unsupported paths stay as callbacks.

The new additive rule only collapses `SUMX(VALUES(Fact[Grain]), [Pure sum helper])` or its explicit-context-transition equivalent when the grain and exact numeric sum belong to the same physical table. It rejects floating-point doubles, filtered/ratio/snapshot helpers, enclosing row contexts, and bidirectional/many-to-many relationships. Engine validation remains necessary, including overflow-sensitive values; no universal equivalence proof is claimed. The selected output may get longer while eliminating repeated daily work. Whole-model triage retains bounded local checks; selecting a measure enables helper-path specialization.

Each measure package contains fixed-date total and representative matrix validation queries, including explicit BLANK flags. Run those against the refreshed model before interpreting timings. The public tests also use an independent arithmetic oracle built from source rows and stated business rules; it is not a general DAX emulator. From a source checkout, `python tools/check_production_engine.py daily-production-lab --server localhost:PORT --database CATALOG --adomd-path PATH` runs the finite engine checks against an explicitly selected local catalog. Add `--benchmark` for seven warm client samples only after strict original/candidate equality, BLANK checks and oracle comparisons pass. The original/candidate comparison is exact; a small tolerance is used only between the independent Python floating-point oracle and the engine. The generator itself does not execute these checks or benchmark an engine.

```python
from semantic_hotspots import create_production_lab, production_model, create_measure

create_production_lab("daily-production-lab", days=364, sites=6, products=8)
created = create_measure(production_model(), "Production rate by period")
assert created.data["chain_specialization"]["source_helpers_unchanged"]
created.write("production-rate-review")
```

## Create one new measure

After **Analyze model**:

1. Search for and select one measure.
2. Optionally expand **Add visual context** and tick zero, one or more matching matrices.
3. Click **Create new measure**, then **Copy Power BI measure** or download its definition for Power BI Desktop’s **New measure** editor.
4. For DAX Studio or Power BI’s **DAX query view**, choose **Download DAX Studio query (preview)**. It contains a query-scoped `DEFINE MEASURE` and `EVALUATE ROW`; it returns one total without saving a model measure. This is an unexecuted preview, separate from the guarded validation queries.

`new-measure.dax` is a measure definition, not a standalone query. The full ZIP includes `preview-query.dax` and `HOW-TO-USE.txt`. The wrapper follows Microsoft’s [DAX query syntax](https://learn.microsoft.com/en-us/dax/dax-queries).

The result is a separately named measure, with a collision-free name and a complete `Name = expression` definition for Power BI Desktop’s New measure editor. It belongs in the indicated table of the same model. Its unexpanded helper measures and source tables remain dependencies; the generated definition is not a standalone replacement semantic model. The original measure and every helper are unchanged. The downloadable ZIP contains the DAX, a measure JSON object (including the original static format string when present), an offline report, original/new DAGs and available unexecuted validation queries. Dynamic format expressions need separate review.

The generator first prefers a bounded proposal that specializes improved helper paths into the selected root, then a combined local rewrite, then an isolated local rewrite. This is a deterministic rule choice, not evidence of correctness or lower cost. If no guarded rewrite matches, it creates an **explicitly labeled unchanged copy**; it never invents an optimization or reports a speedup. A missing root definition blocks creation. Missing helper definitions can still allow an independent local proposal; unresolved/ambiguous references and cycles retain their guards. Incomplete chains retain their graphs but withhold validation probes.

Optional visuals are matched by explicit value slots bound to the selected measure’s table/name. Helper-only and visual-filter-only references do not count as displayed values. Report/page/title labels identify choices; duplicate labels include stable IDs and dynamic/hidden titles fall back to IDs. **Clear visual contexts** removes all selections. Their axes, filters, drill/total settings and unsupported flags remain separate: one visual’s filters are never intersected with another’s or baked into the new DAX. Supported reviewed contexts export representative baseline/candidate/BLANK-validation queries; partial or unsupported contexts retain diagnostics and withhold their queries. No query is executed by creation.

Changes to the selected measure or visuals hide the previous result until it matches or is recreated. Source DAX, alternative proposals, manual comparison and validation details sit under **Inspect source, alternatives and validation**. The optional summary-table PBIP comparison is a separate advanced feature for exactly one visual; it is not needed to create a new measure.

```powershell
python -m semantic_hotspots create-measure model.bim Revenue --table Sales -o new-measure
python -m semantic_hotspots create-measure model.bim Revenue --table Sales --matrices matrices.json --visual monthly --visual quarterly -o new-measure-contexts
```

```python
from semantic_hotspots import create_measure, load_matrices

result = create_measure("model.bim", "Revenue", table="Sales")
result.write("new-measure")
# Supply zero or more explicitly chosen MatrixSpec objects or dictionaries:
visuals = [s for s in load_matrices("matrices.json") if s.id in {"monthly", "quarterly"}]
result = create_measure("model.bim", "Revenue", table="Sales", visuals=visuals)
```

Matrix specification JSON may include string fields `report_name`, `page_name` and `visual_id` for labels. PBIR imports preserve the report folder name, page display name and literal visual title when available. These labels never determine bindings.

The lower-level single-root proposal review API and CLI remain available:

```powershell
python -m semantic_hotspots optimize-measure model.bim "Revenue" --table Sales -o revenue-review
# Optional explicit landing context:
python -m semantic_hotspots optimize-measure model.bim "Revenue" --table Sales --matrices matrices.json --landing-matrix monthly -o revenue-with-context
```

```python
from semantic_hotspots import review_measure

review = review_measure("model.bim", "Revenue", table="Sales")
review.write("revenue-review")  # Choose an absent or empty output directory.
# Optional: review_measure(model, "Revenue", table="Sales", landing_matrix=matrix_spec)
```

Open `revenue-review/report.html`. It includes exact original DAX, the full original chain, diagnostics and only this measure’s proposals. Each proposal has paired DAG exports, proposed DAX and a total validation query. Source models stay unchanged. Cyclic/incomplete user-authored proposals retain their comparison graphs but withhold validation probes. `analyze(..., include_rewrites=False)` ranks/scans without generating all rewrite proposals; Studio uses this mode initially. `plan_rewrites(..., measure="Revenue", table="Sales")` also restricts rule evaluation to one root.

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

General lineage uses lexical/call structure and metadata projection; rewrite proposals additionally use a small bound, typed logical subset. This is **not a full DAX compiler, TOM serializer, M parser, VertiPaq analyzer, query-plan profiler or engine binding authority**. It handles strings, comments, escaped identifiers, nested calls and case-insensitive model-name lookup. Scoped `VAR ... RETURN` declarations retain separate binding nodes and shared references, including nested scalar and table scopes. Their definitions are never moved or inlined. Existing guarded rules can run inside recognized scalar variables and `IF`/`NOT`/`ISBLANK`/`BLANK`/`DIVIDE` expressions, including `CALCULATE` with `REMOVEFILTERS`. Known table schemas flow through physical scans, VALUES, ADDCOLUMNS, SELECTCOLUMNS, FILTER and lexical table variables. Unqualified row columns bind only in the active iterator scope; extension columns retain their defining expressions without being treated as physical lineage. Duplicate aliases, unknown input schemas and ambiguous model-measure/row-column names remain blockers. Informational notes about bound variables do not mark a dependency chain incomplete; actual reference blockers remain explicit. Selected-measure calculation-group dependencies cannot be fully resolved statically. RLS expressions and calculation-group activation/precedence are not expanded into measure graphs. Table storage defaults/mixed partitions can remain unknown when not supplied.

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

### Direct engine access on Windows, without DAX Studio

Install the optional client extra into the same virtual environment. The package is not on PyPI; use the released wheel with its extra:

```powershell
python -m pip install "semantic-hotspots[engine] @ https://github.com/Samnor/semantic-hotspots/releases/download/v0.17.0/semantic_hotspots-0.17.0-py3-none-any.whl"
python -m semantic_hotspots engine-sessions
python -m semantic_hotspots benchmark synthetic-lab --server localhost:PORT --database DATABASE --warm 3
python -m semantic_hotspots experiment-run rewrite-experiment --server localhost:PORT --database DATABASE --warm 5
```

Open and refresh the intended synthetic model first. `engine-sessions` lists local Desktop endpoints and their catalog names; select both explicitly. Discovery uses best-effort workspace paths that can change or contain stale entries. `engine-sessions --server localhost:PORT` probes an explicit local endpoint when discovery is unavailable. The library does not identify a model by its open window title or attest that the selected database has the intended data.

The direct backend is a thin client using [Microsoft ADOMD.NET](https://learn.microsoft.com/analysis-services/client-libraries) through [Python.NET](https://pythonnet.github.io/pythonnet/python.html). It uses the actual loaded DAX engine. It is **not an emulator**, and does not need a DAX Studio installation. Python.NET installs via pip; Microsoft's DLL is not bundled or downloaded during ordinary query execution. The explicit `engine-setup` command or Studio setup button can install the pinned official client. The normal Desktop installation's `bin` directory is searched, but current installations may contain only an internal `Microsoft.PowerBI.AdomdClient.dll`; that is not the public SDK used here. If the public client is absent, install Microsoft's [ADOMD client package](https://www.nuget.org/packages/Microsoft.AnalysisServices.AdomdClient/) separately and pass `--adomd-path` to its trusted `Microsoft.AnalysisServices.AdomdClient.dll` or directory. Preserve the package's required managed and native dependencies. Microsoft Store installations may also require an explicit accessible client DLL. The default runtime is .NET Framework (`netfx`); a .NET Core client DLL needs an installed compatible .NET runtime and `--dotnet-runtime coreclr`. Match client/Python architecture and runtime; copying a DLL without its dependencies may not work.

ADOMD currently supports **local loopback endpoints and warm iterations only**. Each workload runs in an isolated Python subprocess with a wall-clock timeout, so a blocked .NET call cannot hang the parent indefinitely. One connection is reused for that workload's iterations. Timings start after connection setup and include execution, result transfer, and complete client reading/conversion. Warm means no cache clearing, including the first iteration; it does not guarantee a prewarmed cache. CPU, FE and SE remain unknown, even if a collected CSV is read with `--trace-verified`. These are observed client elapsed times, not server-only timings, production predictions, or CU estimates. The backend does not refresh/deploy/edit the model or clear its cache.

For a generated showcase's validation and row-count queries, export a single result set directly:

```powershell
python -m semantic_hotspots query powerbi-showcase/queries/QUERY-validation.dax --server localhost:PORT --database DATABASE -o validation.csv
python -m semantic_hotspots query powerbi-showcase/queries/row-counts.dax --server localhost:PORT --database DATABASE -o row-counts.csv
python -m semantic_hotspots query powerbi-showcase/queries/QUERY-baseline.dax --server localhost:PORT --database DATABASE --benchmark --warm 5 -o baseline-timing.csv
python -m semantic_hotspots query powerbi-showcase/queries/QUERY-candidate.dax --server localhost:PORT --database DATABASE --benchmark --warm 5 -o candidate-timing.csv
```

Replace `QUERY-*` with actual generated filenames. Existing output files are never overwritten. Inspect all equality/BLANK flags and meaningful nonblank values before timing; a successful query alone is not a correctness verdict. Benchmark CSVs can be read with `read_benchmark_csv`. Use `showcase-run` or Studio to automate showcase validation/A/B comparison. These individual query commands remain available for inspection; `experiment-run` accepts rewrite experiments, not `showcase.json`.

The core remains dependency-free. Client orchestration and error/evidence handling have automated tests; Windows CI additionally loads the official ADOMD library and checks real .NET value serialization. A manual Windows Server 2022 check with Power BI Desktop 2.159.883.0, Python 3.14 and the explicit official ADOMD 19.118.3 .NET Framework client opened/refreshed one fictional showcase and executed its validation and timing queries. All six prefix contexts passed strict value/BLANK comparisons (40 nonblank original values); five iterations per baseline/candidate workload produced mixed median client timings of roughly 2–5 ms, so no speedup is established. This is bounded integration evidence for one fixture; CI does not launch Desktop or execute DAX. Static analysis and mock generation do not reproduce VertiPaq/Formula Engine execution or DirectQuery source behavior.

### Optional DAX Studio tracing and cold-cache benchmarks

1. Copy the bundle to the engine machine and set the `SyntheticDataRoot` M expression in `synthetic-model.bim` to its absolute `tables` directory. You can instead specify `--data-root` during generation. The path must be readable by the engine process, which may run as a different account.
2. Open/deploy the BIM through Tabular Editor or a compatible SSAS Tabular development environment into a **separate simulation model**; process/refresh its tables. Power BI Desktop does not directly open a standalone BIM. The library does not deploy or overwrite models automatically.
3. For server trace diagnostics or cold-cache tests, optionally install DAX Studio **3.6+**, which supplies `dscmd.exe benchmark`, and run:

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

The rule registry is deliberately small:

| Rule | Proposed change | Conditions |
|---|---|---|
| OPT001 | `SUMX(Sales, Sales[Amount])` → `SUM(Sales[Amount])` | Same physical base table, direct physical numeric column, known type, supported enclosing syntax |
| OPT002 | `FILTER(FILTER(Sales, p), q)` → `FILTER(Sales, (p) && (q))` | Same physical table; row-local comparisons of compatible columns/literals and Boolean combinations; no measures, calls, arithmetic or context changes inside predicates |
| OPT003 | `SUMX(FILTER(t, NOT ISBLANK([@score])), [@score])` → `SUMX(t, [@score])` | `t` is a bound table variable; both column references bind to the same exact numeric stored column; floating-point and unestablished scalar types retain the filter |
| OPT004 | `SUMX(VALUES(Fact[Day]), [Pure sum])` → `SUM(Fact[Units])` | Context transition, pure exact numeric sum and grain from the same physical table; no outer row context, bidirectional or many-to-many relationships |
| OPT005 | Specialize improved helper paths into one new root | Complete supported changed paths; explicit CALCULATE per transplanted measure call; qualified model bindings; original helpers unchanged; bounded expansion |

Recognized logical operations include scans, scoped physical/extension columns, measure references, lexical scalar/table variables, literals, parentheses, scalar binary operators, `SUM`, `SUMX`, `FILTER`, `VALUES`, `ADDCOLUMNS`, paired-name `SELECTCOLUMNS`, `COUNTROWS`, `ISEMPTY`, `MAX`/`MIN`, `YEAR`/`DATE`, `SWITCH`, `IF`, Boolean/blank tests, `DIVIDE`, and `CALCULATE`/`REMOVEFILTERS`. Temporary-column DAG references link to explicit definition nodes. Existing rewrites can run inside supported extension expressions without moving their row/filter context. This is **not a full DAX AST/compiler or executable physical planner**. Unknown table schemas, unsupported calls, ambiguous references, cycles, calculation groups, model functions and dynamic root formats retain their guards. Independently guarded root-local rewrites can preserve known-but-undefined helpers and unknown surrounding column types without inferring their behavior. Those proposals remain unvalidated; actual helper definitions are required before comparison queries are exported. TODAY stays live in production and is frozen only in validation queries; NOW and random evaluation remain blocked. No variable hoisting, join reordering, general filter pushdown, automatic pre-aggregation or runtime plan fusion is implemented. The search emits at most 32 isolated proposals and a 16-step bounded fixed-point combination per measure. No timing benefit is inferred: the DAX engine may already normalize the alternatives.

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

The runner defines a unique **query-scoped alias measure**, leaving the loaded model unchanged. Each candidate changes only the output root expression; original helper definitions remain intact. Selected-root proposals can specialize improved helper paths, so the resulting root can depend on fewer helpers. Whole-model scans keep local proposals to bound analysis work. It checks a paired query using engine strict equality (`==`) plus explicit original/candidate `ISBLANK` flags for the scalar total and every configured grouped/filtered scenario. The equality flag also retains groups in which both values are BLANK. A mismatch, failed process, missing output or zero validation rows prevents timing that candidate. All-BLANK coverage is labeled uninformative and also prevents timing.

Passing candidates receive separate original/candidate queries on the same target, with alternating order between scenarios. Omit `--dscmd` to use the direct local ADOMD client. The default clears no cache; `--cold` explicitly requests database-cache clearing. Keep the synthetic database, data, engine settings, hardware and concurrency stable. Warm results after paired validation may benefit from warmed engine caches. Repeat at several row counts, seeds, cardinalities and skews using separate simulation bundles.

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

## Original versus proposed computation DAGs

Guarded rewrite proposals now include **Original measure DAG** and **Proposed measure DAG** views. In Studio, generate an eligible matrix comparison and click **Compare original & proposed DAGs**. Expand a measure to inspect both paths. The standalone comparison report also exports the full original/proposed graphs as JSON, text and Graphviz DOT.

The diagrams retain helper-measure definitions and shared branches. Recognized syntax appears as operator nodes, so `SUMX → SUM` and filter fusion remain visible even when their object dependencies barely change. Borders identify nodes appearing in only one path and changed definitions. These are structural differences, not deletions from the source model or measured operation counts.

For a summary candidate, the proposed query path ends at the stored summary. Expand **Refresh-time lineage and materialization** to see its `SUMMARIZE`, retained keys, aggregation states and physical inputs. Work moved to refresh is still part of the full DAG. Matrix context and added relationships are retained separately; implicit filter propagation is not invented as an execution edge.

Refresh boundaries require confirmed Import storage and standard/default expression context. DirectQuery, Dual, user-context and unknown calculated definitions retain their full possible query paths and are flagged as storage-dependent; they are excluded from confirmed query/refresh operator counts. This follows the storage/context distinction in Microsoft’s [calculated-column documentation](https://learn.microsoft.com/en-us/power-bi/transform-model/desktop-calculated-columns).

You can also compare your own proposed expression without changing the model:

```powershell
python -m semantic_hotspots compare-dags model.bim Revenue --table Sales --expression-file proposed.dax -o dag-comparison
```

Or compare against a separate proposed model, including newly added summary tables:

```powershell
python -m semantic_hotspots compare-dags original.bim Revenue --table Sales --candidate-model proposed.bim --candidate-measure "Candidate Revenue" --candidate-table Sales -o dag-comparison
```

Open `dag-comparison/report.html`. The directory includes `original.dag.json`, `proposed.dag.json`, `comparison.json`, `original.txt`, `proposed.txt`, `original.dot` and `proposed.dot`. Large diagrams use complete node/edge detail instead of truncating JSON. Cycles, unresolved references and missing formulas stay explicit. Syntax outside the parsed subset stays opaque; parsed calls outside the guarded logical subset are labelled as syntax without established type/context semantics. No result equivalence or performance improvement is inferred.

```python
from semantic_hotspots import compare_measure_dags, write_dag_comparison

comparison = compare_measure_dags(
    "model.bim", "Revenue", table="Sales",
    proposed_expression="SUM('Sales'[Amount])",
)
write_dag_comparison(comparison, "dag-comparison")
# comparison["original"] and comparison["proposed"] contain complete graphs.
# query_time_node_ids and refresh_time_node_ids separate structural phases.
```

The same comparison snapshots are embedded in `rewrite-plan.json`, and generated showcases link them from `showcase.json`. They remain unvalidated proposals; test correctness and measure timings separately.

## Matrix-specific grain analysis

Analyze a matrix's row hierarchy, column hierarchy, explicit value measures, enumerated column filters, potential slicer/drill columns and totals together:

```powershell
python -m semantic_hotspots matrices examples/matrix-model.bim --matrices examples/matrices.json -o matrix-report
# Or extract common explicit bindings from an exported PBIR report:
python -m semantic_hotspots matrices model.bim --matrices Retail.Report -o matrix-report
```

Open `matrix-report/report.html`. The highest known measure-chain review priorities come first. Each matrix expands into value/filter measure chains, shared dependencies, exact DAX, unsupported context, aggregation components, proposed grains, grouping-count probes where eligible, and representative multi-measure queries. JSON and editable normalized specifications sit beside the report.

An explicit specification looks like:

```json
{
  "schema_version": "1.0",
  "matrices": [{
    "id": "monthly-revenue",
    "title": "Monthly revenue",
    "rows": [["Customer", "Region"]],
    "columns": [["Calendar", "Year"], ["Calendar", "Month"]],
    "values": [["Sales", "Revenue"], ["Sales", "Margin"]],
    "filters": [{"table": "Customer", "column": "Region", "values": ["North"]}],
    "retain": [["Calendar", "Day"]],
    "row_subtotals": true,
    "column_subtotals": true,
    "grand_total": true,
    "context_complete": true
  }]
}
```

`rows` and `columns` are ordered hierarchy levels. `values` and `filter_measures` contain `[table, measure]` pairs; names bind case-insensitively. `retain` lists additional columns that future slicers/drill states must preserve. Optional `drill_states` entries have a unique `name`, `row_depth`, `column_depth`, and additional enumerated `filters`. Depths select hierarchy prefixes; base and state filters must not repeat the same column. Limits are 16 axis fields and 256 generated contexts per matrix.

`context_complete` defaults to **false**. Set it true only when the specification deliberately covers the required filters/drill behavior. This declaration is not engine proof. A `filter_measures` entry records a visual-level filtering dependency; because its predicate/grain is unspecified, executable workloads and safe aggregation claims are withheld. `unsupported` records features requiring manual review, such as TopN, visual calculations or unmodeled interaction filters. Unknown fields/measures and missing definitions remain visible rather than invented.

### What grain advice means

For a matrix grouped by region and month, the report distinguishes:

- **Relationship-preserving grain:** retain fact-side customer/date keys needed for unique active regular many-to-one filter paths. This can reduce repeated fact rows while retaining the existing dimensions; it may be finer than the displayed matrix.
- **Exploratory matrix/filter grain:** group by the displayed and explicitly retained/filter columns. Dropping customer/date keys requires dimension remapping and complete filter/security validation. A day-level retained filter prevents an unconditional monthly-only recommendation.

The initial component analysis recognizes direct physical numeric `SUM`, `MIN`, `MAX`, `AVERAGE`, `COUNT`, physical-table `COUNTROWS`, physical-column `DISTINCTCOUNT`, simple numeric-column `SUMX`, scalar arithmetic and `DIVIDE` over supported components/referenced measures. It proposes SUM plus nonblank COUNT for averages; distinct keys are retained for recomputing DISTINCTCOUNT. Ratios/arithmetic are re-evaluated after state rollup, not summed or averaged from matrix cells. Missing/opaque formulas, VAR contexts, row-dependent calculations, multiple facts, unsupported filter paths, calculation groups/functions/dynamic formats and security gaps produce review blockers. Recognized leaf components and possible iteration/predicate keys can still guide manual review; they do not authorize a rewrite.

Where no recognized static blocker remains, a **group-count probe** compares fact row count with the distinct retained-key combinations under base matrix filters. Counts and speedup remain unknown until executed; state-specific filters require separate counts. The library never substitutes a Cartesian product of dimension cardinalities for an observed grouping count. Matrix analysis by itself creates no aggregation tables, SQL materializations, storage mappings, or rewritten measure chains. The coarser grain is a hypothesis for source/model/DAX-intermediate experiments, not a proven optimal grain. The separate `project` exporter below can create a new experimental model with a guarded summary candidate.

### Benchmark detail, subtotals and totals together

Add the matrices to a synthetic bundle:

```powershell
python -m semantic_hotspots simulate examples/matrix-model.bim --matrices examples/matrices.json --fact-rows 100000 -o synthetic-lab
# After loading and refreshing the separate synthetic BIM in a test engine:
python -m semantic_hotspots benchmark synthetic-lab --dscmd "C:\Program Files\DAX Studio\dscmd.exe" --server localhost:PORT --database SYNTHETIC_DATABASE --warm 5 --cold 0
```

`synthetic-lab/matrix-analysis/report.html` contains grain advice. The bundle's ordinary `benchmark` command now also times **multi-measure matrix contexts**. It emits full-detail and configured prefix subtotal/grand-total contexts, recomputing original measures in each context, including ratios and scope-dependent measures. These are separate representative queries; they do not reproduce the exact Power BI ROLLUPADDISSUBTOTAL flags, visual-level measure filtering, data reduction, show-items-with-no-data, field parameters, visual calculations, sorting/expansion behavior or visual rendering. They are baseline workloads, **not an A/B test of a materialized aggregation**. The existing rewrite experiment remains a separate per-measure workflow and does not automatically test these proposed grains.

To attach matching observed workload means to a matrix report:

```powershell
python -m semantic_hotspots matrices examples/matrix-model.bim --matrices examples/matrices.json --benchmark-results synthetic-lab/benchmark-results/benchmark.json -o matrix-report-measured
```

Only matching workload identities and query text are attached; identities include the matrix contract and model definitions/types/relationships. They do not verify synthetic CSV contents or generation settings; review the original simulation manifest. The report retains the capture's source-model/seed provenance. Matrices then rank by their highest known synthetic context mean, without summing shared measures or multiplying by cell counts. Missing contexts stay unknown; partial iteration counts are shown. Import-mode durations do not predict the original DirectQuery model, source latency, visual rendering or aggregation speedup.

```python
from semantic_hotspots import analyze_matrices, load_matrices, SimulationConfig, simulate

specs = load_matrices("examples/matrices.json")
result = analyze_matrices("examples/matrix-model.bim", specs)
result.write("matrix-report")
simulate("examples/matrix-model.bim", "synthetic-lab", config=SimulationConfig(
    matrices=[spec.to_dict() for spec in specs], fact_rows=100_000,
))
```

PBIR extraction reads common `pivotTable` Column/Measure projections from a `.Report/definition/pages/.../visual.json` tree, a definition folder, or a single visual file. It retains report slicer column bindings conservatively but does not know their current selections. Implicit aggregations, hierarchy/parameter expressions, extra roles and observed untranslated report/page/visual filters are flagged. Review the exported `matrix-specs.json` and supply an explicit contract before treating it as a complete visual. Binary PBIX and legacy report.json conversion are unsupported.

The format follows Microsoft's [PBIR report definition](https://learn.microsoft.com/en-us/power-bi/developer/projects/projects-report) and [matrix projection example](https://github.com/microsoft/skills-for-fabric/blob/main/skills/powerbi-report-cli/references/authoring/table.md). Exact visual-query validation remains a separate step; [Performance Analyzer](https://learn.microsoft.com/en-us/power-bi/create-reports/performance-analyzer) can capture the actual query and interactions.

## Export a minimal Power BI showcase

Create a **new project with one page and one matrix**, showing original and candidate measure values side by side:

```powershell
python -m semantic_hotspots project examples/matrix-model.bim --matrices examples/matrices.json --matrix monthly-margin --config examples/showcase-config.json -o powerbi-showcase
```

Copy the entire output folder to Windows, open `Showcase.pbip` in a current Power BI Desktop with PBIP/PBIR support, and refresh. Its new `Showcase.SemanticModel/model.bim` embeds compressed mock CSV data in Import partitions, so the showcase needs no real sources, credentials or machine-specific CSV path. A 32 MiB uncompressed embedded-data limit keeps this a small example; reduce simulation row counts if needed. The `simulation` subdirectory is an inspectable mock-data bundle; its separate file-based BIM is not the embedded-data Showcase model.

For a measure-focused landing export, pass `--measure "Revenue" --table Sales --matrix MATRIX_ID` to `project`; the matrix’s values become only that measure’s baseline/candidate pair and the remaining context/guards stay intact. Without measure scoping, the library retains its existing matrix-wide export behavior.

The exporter selects the highest static-priority **eligible** matrix, or the exact ID supplied by `--matrix`. It keeps only the selected baseline measure chains, physical columns and necessary dimension paths in a metadata projection. It adds a DAX calculated summary table at the relationship-preserving key grain and candidate measures that recombine its states. The original measures remain intact in this isolated model. Ratios are recomputed from components; averages use sum/count states and distinct counts retain their keys. This is a refresh-time materialization with explicit candidate measures, not automatic aggregation mappings, source SQL preaggregation, or a guaranteed performance improvement. The extra summary consumes memory and refresh work.

This initial exporter requires a complete, unfiltered matrix contract, default hierarchy/total settings, one supported fact recipe and unique regular dimension paths. It rejects fact-column axes/retained filters, measure filters, explicit column filters, non-default drill/total settings, unknown metadata, opaque/context-dependent calculations, multiple facts and security gaps. It does not silently drop these features or change their grain. These restrictions are narrower than matrix *analysis*: a matrix can be analyzed even when it cannot safely produce a showcase. Future slicers outside the declared dimension contract require new grain/equivalence review. Source files are never overwritten; the destination must be absent or empty.

The showcase contains exactly **one visual**. A title/subtitle and paired value columns explain the comparison without adding cards, charts or duplicate pages. It uses a new PBIR layout, rather than reproducing all formatting/interactions of the original visual. `showcase.html`, `showcase.json`, and `OPEN-IN-DESKTOP.txt` describe the proposed change, summary grain, skipped matrices and limitations.

### Validate before calling it improved

For manual inspection after opening and refreshing the new model, use the direct client or optional DAX Studio:

1. Run `queries/*-validation.dax`. Check every `Equal_*` flag is 1, BLANK flags match, and meaningful nonblank baseline values exist. Generated cases include hierarchy prefixes, subtotals and the grand total. Add empty selections, boundary cases, duplicates and representative slicers before broader claims.
2. Run `queries/row-counts.dax` for engine counts. CSV generation and table expressions do not establish actual engine scans or row reduction.
3. Only after correctness checks, time the separate `*-baseline.dax` and `*-candidate.dax` queries under matching data/hardware/cache conditions. The combined comparison visual executes both paths, so its duration cannot establish the candidate's speedup.

Run the generated showcase through Studio, or use the dedicated runner:

```powershell
python -m semantic_hotspots showcase-run powerbi-showcase --server localhost:PORT --database DATABASE
```

It checks all generated measure definitions and physical row counts against the embedded mock data, validates every canonical prefix context with exact equality and BLANK flags, and requires nonblank evidence for every measure pair before any timings. It rechecks definitions/counts and local fingerprints after timing. These checks do not attest loaded data contents, relationships, partitions or security.

Read `powerbi-showcase/showcase-results/report.html` and its retained query/CSV fingerprints. The runner excludes one explicit warm-up and collects seven measured iterations by default (`--warm 5` through `50`). Workload order alternates between contexts, without interleaving within a context or clearing caches. Faster/slower observations require nonoverlapping sample ranges and a median change exceeding both 10% and 1 ms; otherwise results are **inconclusive**. This is a conservative descriptive rule, not a significance test. Existing result directories are not overwritten; use `-o NEW_DIRECTORY` to repeat.

The earlier `experiment-run` command accepts query-scoped rewrite experiments, not `showcase.json`. Floating-point regrouping may change results; exact comparisons fail conservatively. Finite synthetic cases do not prove production behavior, RLS equivalence, or universal correctness.

```python
from semantic_hotspots import export_project

result = export_project(
    "examples/matrix-model.bim", "examples/matrices.json", "powerbi-showcase",
    matrix_id="monthly-margin", config={"fact_rows": 5000, "dimension_rows": 12},
)
print(result.project_path)
```

The exporter writes the documented [PBIP report structure](https://learn.microsoft.com/en-us/power-bi/developer/projects/projects-report) and [BIM semantic model structure](https://learn.microsoft.com/en-us/power-bi/developer/projects/projects-dataset), using a [calculated table](https://learn.microsoft.com/en-us/analysis-services/tabular-models/create-a-calculated-table-ssas-tabular) and [embedded compressed data](https://learn.microsoft.com/en-us/powerquery-m/binary-decompress). CI checks eight project/report/connection documents against Microsoft's versioned schemas (`tools/check_project_schemas.py`, development-only `jsonschema` dependency). Schema validation does not validate BIM semantics, Desktop loading, refresh, visual rendering or DAX results. The manual Windows integration check described above also rendered the one-page comparison matrix and refreshed its 5,000 detail rows into 288 summary rows. This is a row-count reduction, not a measured performance multiplier. Automated local and CI tests do not launch Desktop or execute DAX. PBIX saving requires Power BI Desktop; this library exports PBIP and does not publish or deploy it.

## Rule references

The rule text was checked against these primary sources. Examples and advice are paraphrased; no page content is bundled.

- [TMDL syntax and expressions](https://learn.microsoft.com/en-us/analysis-services/tmdl/tmdl-overview)
- [FILTER arguments and Boolean-filter restrictions](https://learn.microsoft.com/en-us/dax/best-practices/dax-avoid-avoid-filter-as-filter-argument)
- [Variables and evaluation context](https://learn.microsoft.com/en-us/dax/best-practices/dax-variables)
- [CALCULATE and context transition](https://learn.microsoft.com/en-us/dax/calculate-function-dax)
- [Storage modes](https://learn.microsoft.com/en-us/power-bi/transform-model/desktop-storage-mode)
- [INFO.CALCDEPENDENCY](https://learn.microsoft.com/en-us/dax/info-calcdependency-function-dax)

MIT licensed. Version 0.17.0.

## Contributing and data handling

See [CONTRIBUTING.md](CONTRIBUTING.md) for development instructions and [SECURITY.md](SECURITY.md) for security reporting. All bundled models and telemetry are fictional. Keep model exports, generated reports, source data, credentials and local validation artifacts outside Git; these may contain business logic or sensitive values.

Scoped-variable syntax follows Microsoft’s [VAR reference](https://learn.microsoft.com/en-us/dax/var-dax). The recognized scalar forms follow [IF](https://learn.microsoft.com/en-us/dax/if-function-dax) and [DIVIDE](https://learn.microsoft.com/en-us/dax/divide-function-dax); this bounded representation does not implement a DAX engine.
