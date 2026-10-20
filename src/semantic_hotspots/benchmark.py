"""Explicit DAX Studio engine benchmarks; never approximate DAX timing in Python."""
from __future__ import annotations

import csv
import html
import json
import math
import os
import re
import statistics
import subprocess
from urllib.parse import quote
from dataclasses import dataclass
from pathlib import Path

from .model import ModelError


def _manifest(bundle):
    root = Path(bundle.directory if hasattr(bundle, 'directory') else bundle).resolve()
    try:
        manifest = json.loads((root/'simulation.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ModelError('Expected a generated simulation bundle with simulation.json') from exc
    if manifest.get('kind') != 'synthetic-benchmark-bundle':
        raise ModelError('Not a synthetic benchmark bundle')
    if not all(k in manifest for k in ['source_model', 'seed', 'warnings']) or not isinstance(manifest['warnings'], list):
        raise ModelError('Bundle summary metadata is missing or malformed')
    workloads = manifest.get('workloads')
    if not isinstance(workloads, list):
        raise ModelError('Bundle workloads must be a list')
    seen = set()
    for w in workloads:
        if not isinstance(w, dict) or not re.fullmatch(r'[a-f0-9]{20}', str(w.get('id', ''))) or w['id'] in seen:
            raise ModelError('Workload IDs must be unique 20-character hex strings')
        if not all(isinstance(w.get(k), str) for k in ['measure_id', 'measure', 'scenario', 'query_file', 'query']) or not all(isinstance(w.get(k), list) for k in ['group_by', 'filters']):
            raise ModelError('Workload query and scenario metadata is missing or malformed')
        seen.add(w['id'])
        path = (root/w.get('query_file', '')).resolve()
        if not path.is_relative_to(root/'queries') or path.suffix != '.dax' or not path.is_file():
            raise ModelError('Query files must exist under the bundle queries directory')
        if path.read_text(encoding='utf-8') != w.get('query'):
            raise ModelError('Query file differs from its recorded workload; regenerate the bundle to keep evidence reproducible')
    return root, manifest


def _number(value, name):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ModelError(f'Invalid benchmark {name}: {value!r}') from exc
    if not math.isfinite(result) or result < 0:
        raise ModelError(f'Benchmark {name} must be finite and nonnegative')
    return result


def read_benchmark_csv(path, *, trace_verified=False):
    """Read dscmd 3.6+ detail rows, excluding its appended summary section.

    Without independently verified server trace availability, FE/SE/CPU fields
    stay unknown: dscmd can write zero when it falls back to client wall timing.
    """
    # CSV records, rather than physical lines, preserve quoted multiline errors.
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        try:
            reader = csv.DictReader(stream, strict=True)
            fields = reader.fieldnames
            records = []
            for row in reader:
                if row.get('Sequence') == '# Summary' and all(v is None or v == '' for k, v in row.items() if k != 'Sequence'):
                    break
                records.append(row)
        except csv.Error as exc:
            raise ModelError('Malformed benchmark CSV') from exc
    required = {'Sequence', 'Cache', 'TotalDuration_ms', 'Error'}
    if not fields or not required.issubset(fields):
        raise ModelError('Expected DAX Studio 3.6+ BENCHMARK CSV detail columns')
    rows, seen = [], set()
    for row in records:
        if None in row or not isinstance(row.get('Sequence'), str) or not row['Sequence'].isdigit() or int(row['Sequence']) < 1 or any(row.get(k) is None for k in required):
            raise ModelError('Malformed benchmark detail row or missing # Summary boundary')
        sequence = int(row['Sequence'])
        if sequence in seen:
            raise ModelError('Duplicate benchmark Sequence')
        seen.add(sequence)
        cache = row['Cache'].strip().casefold()
        if cache not in {'cold', 'warm'}:
            raise ModelError('Benchmark Cache must be Cold or Warm')
        error = row.get('Error') or None
        result = {'sequence': sequence, 'cache': cache, 'error': error,
                  'duration_ms': None if error else _number(row['TotalDuration_ms'], 'duration'),
                  'timing_source': 'server trace (user verified)' if trace_verified else 'server trace or client wall clock; unverified',
                  'cpu_ms': None, 'formula_engine_ms': None, 'storage_engine_ms': None,
                  'rows': None if error or row.get('RowCount') in {None, ''} else _number(row['RowCount'], 'RowCount')}
        if trace_verified and not error:
            for target, source in [('cpu_ms', 'TotalCpu_ms'), ('formula_engine_ms', 'FormulaEngineDuration_ms'), ('storage_engine_ms', 'StorageEngineDuration_ms')]:
                if row.get(source) not in {None, ''}:
                    result[target] = _number(row[source], source)
        rows.append(result)
    return rows


@dataclass
class BenchmarkResults:
    manifest: dict
    cases: list[dict]
    diagnostics: list[str]
    trace_verified: bool = False
    bundle_directory: Path | None = None

    def to_dict(self):
        return {'schema_version': '1.0', 'kind': 'synthetic-engine-benchmarks',
                'source_model': self.manifest['source_model'], 'seed': self.manifest['seed'],
                'storage_mode': 'import', 'trace_verified': self.trace_verified,
                'interpretation': 'Observed synthetic-workload query durations, not isolated inner-measure CPU, production predictions, or CU estimates.',
                'diagnostics': self.diagnostics, 'cases': self.cases}

    def write(self, directory):
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        (root/'benchmark.json').write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
        source_link = None
        if self.bundle_directory is not None:
            try:
                source_link = quote(Path(os.path.relpath(self.bundle_directory/'analysis/report.html', root.resolve())).as_posix(), safe='/')
            except ValueError:  # Different Windows volumes cannot have relative links.
                pass
        (root/'benchmark.html').write_text(render_benchmark_html(self, source_link), encoding='utf-8')
        return {'json': root/'benchmark.json', 'html': root/'benchmark.html'}


def collect_benchmarks(bundle, results_directory, *, trace_verified=False) -> BenchmarkResults:
    """Aggregate existing dscmd CSVs without contacting an engine."""
    bundle_root, manifest = _manifest(bundle)
    results = Path(results_directory)
    diagnostics = []
    cases = []
    for w in manifest['workloads']:
        path = results/('result-' + w['id'] + '.csv')
        rows = read_benchmark_csv(path, trace_verified=trace_verified) if path.is_file() else []
        if not path.is_file():
            diagnostics.append(f'{w["measure"]} / {w["scenario"]}: benchmark CSV missing; duration unknown')
        if any(r['rows'] == 0 and not r['error'] for r in rows):
            diagnostics.append(f'{w["measure"]} / {w["scenario"]}: at least one successful query returned zero rows. Check synthetic categories, filters and BLANK branches before interpreting speed.')
        phases = {}
        for cache in ['cold', 'warm']:
            attempts = [r for r in rows if r['cache'] == cache]
            timings = sorted(r['duration_ms'] for r in attempts if not r['error'])
            phases[cache] = {'attempts': len(attempts), 'successes': len(timings),
                             'errors': [r['error'] for r in attempts if r['error']],
                             'mean_ms': statistics.mean(timings) if timings else None,
                             'median_ms': statistics.median(timings) if timings else None,
                             'p95_ms': timings[max(0, math.ceil(.95*len(timings))-1)] if timings else None,
                             'min_ms': min(timings) if timings else None, 'max_ms': max(timings) if timings else None}
        cases.append(dict(w, rows=rows, phases=phases))
    runner_file = results/'runner.json'
    if runner_file.is_file():
        try:
            capture = json.loads(runner_file.read_text(encoding='utf-8'))
            for run in capture['runs']:
                if run['returncode'] != 0:
                    diagnostics.append(f'Workload {run["id"]}: dscmd failed or timed out; see runner.json. Any successful iterations are only a partial result.')
            for case in cases:
                for cache in ['cold', 'warm']:
                    expected = capture[cache]
                    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
                        raise ValueError('Invalid expected iteration count')
                    actual = case['phases'][cache]['attempts']
                    if actual != expected:
                        diagnostics.append(f'{case["measure"]} / {case["scenario"]} / {cache}: expected {expected} iterations, found {actual}')
        except (ValueError, KeyError, TypeError) as exc:
            raise ModelError('Malformed runner.json iteration coverage metadata') from exc
    if not trace_verified:
        diagnostics.append('Server trace availability was not verified. CPU/FE/SE are unknown; duration may use client wall-clock fallback. Zero trace fields are not assumed to be measured zeros.')
    diagnostics += manifest['warnings']
    return BenchmarkResults(manifest, cases, diagnostics, trace_verified, bundle_root)


def run_benchmarks(bundle, *, dscmd, server, database, results_directory=None,
                   cold=0, warm=3, timeout=300, trace_verified=False) -> BenchmarkResults:
    """Run a user-selected dscmd 3.6+ against an already loaded synthetic model.

    No model deployment or refresh occurs. Explicit cold>0 asks dscmd to clear the
    database cache. Use a separate simulation database. Commands use argv, no shell.
    """
    root, manifest = _manifest(bundle)
    for value in [cold, warm]:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ModelError('cold and warm must be nonnegative integers')
    if cold + warm == 0:
        raise ModelError('At least one cold or warm iteration is required')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ModelError('timeout must be finite and positive')
    if not server or not database:
        raise ModelError('Provide an explicit server and database for the already loaded synthetic model')
    executable = Path(dscmd).resolve()
    if not executable.is_file():
        raise ModelError('dscmd must be a path to an installed DAX Studio dscmd.exe 3.6+')
    out = Path(results_directory).resolve() if results_directory is not None else root/'benchmark-results'
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ModelError('Benchmark results directory must be absent or empty')
    out.mkdir(parents=True, exist_ok=True)
    runs = []
    for w in manifest['workloads']:
        result_file = out/('result-' + w['id'] + '.csv')
        command = [str(executable), 'benchmark', str(result_file), '--server', str(server),
                   '--database', str(database), '--file', str(root/w['query_file']),
                   '--cold', str(cold), '--warm', str(warm)]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout, check=False)
            run = {'id': w['id'], 'returncode': completed.returncode,
                   'stdout': completed.stdout, 'stderr': completed.stderr}
        except subprocess.TimeoutExpired:
            run = {'id': w['id'], 'returncode': None, 'stdout': '', 'stderr': f'Timeout after {timeout} seconds'}
        except OSError as exc:
            run = {'id': w['id'], 'returncode': None, 'stdout': '', 'stderr': str(exc)}
        runs.append(run)
    (out/'runner.json').write_text(json.dumps({'server': str(server), 'database': str(database),
                                             'cold': cold, 'warm': warm, 'timeout': timeout, 'runs': runs}, indent=2)+'\n', encoding='utf-8')
    result = collect_benchmarks(root, out, trace_verified=trace_verified)
    result.write(out)
    return result


def render_simulation_html(manifest):
    from .report import _CSS
    esc = lambda value: html.escape(str(value), quote=True)
    table_rows = ''.join(f'<tr><td>{esc(t["name"])}</td><td>{esc(t["kind"])}</td><td>{t["rows"] if t["rows"] is not None else "Engine calculated"}</td><td>{len(t["columns"])}</td></tr>' for t in manifest['tables'])
    cases = ''.join(f'<details><summary>{esc(w["measure"])} · {esc(w["scenario"])}</summary><p>Groups: {esc(w["group_by"])} · Filters: {esc(w["filters"])}</p><pre>{esc(w["query"])}</pre><a href="{esc(w["query_file"])}">Query file</a></details>' for w in manifest['workloads'])
    warnings = ''.join(f'<li>{esc(w)}</li>' for w in manifest['warnings'])
    matrix_link = '<a href="matrix-analysis/report.html">Matrix grain advice and contexts</a> · ' if manifest.get('config',{}).get('matrices') else ''
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Synthetic model · {esc(manifest['source_model'])}</title><style>{_CSS}</style></head><body><main>
<header><div class="eyebrow">SEMANTIC HOTSPOTS / SYNTHETIC LAB</div><h1>Test the model.<br>Keep the real sources offline.</h1><p>{esc(manifest['source_model'])} · seed {manifest['seed']}</p></header>
<section><h2>Data ready. Engine benchmarking is a separate step.</h2><p>The bundle contains reproducible CSV tables, a source-free Import model and single-measure and optional matrix benchmark queries. This generation report contains no DAX timings. Subsequent engine runs write a separate benchmark.html results report.</p>
<div class="stats"><div class="stat"><span>Synthetic source tables</span><strong>{sum(t['kind']=='synthetic-physical' for t in manifest['tables'])}</strong></div><div class="stat"><span>Rows generated</span><strong>{sum(t['rows'] or 0 for t in manifest['tables']):,}</strong></div><div class="stat"><span>Workload queries</span><strong>{len(manifest['workloads'])}</strong></div><div class="stat"><span>DAX timings at generation</span><strong>Not run</strong></div></div>
<div class="interpretation">Import-mode experiments can compare DAX alternatives on the same synthetic data. They do not simulate DirectQuery source latency, actual production distributions, RLS, or Fabric CU consumption.</div>
<p>{matrix_link}<a href="RUN-INSTRUCTIONS.txt">How to load and benchmark</a> · <a href="synthetic-model.bim">Synthetic model BIM</a> · <a href="analysis/report.html">Static measure findings and chains</a> · <a href="simulation.json">Full manifest</a></p></section>
<section><h2>Table shape</h2><div class="scroll"><table><thead><tr><th>Table</th><th>Source</th><th>Rows</th><th>Generated columns</th></tr></thead><tbody>{table_rows}</tbody></table></div><p>Relationship key domains are shared; one-side keys are unique and non-null. Configured foreign-key blanks are deliberate. Actual engine validation and refresh are still required.</p></section>
<section><details><summary>Assumptions and unsupported behavior</summary><ul>{warnings}</ul><p>Metadata fidelity: {esc(manifest['metadata_fidelity'])}. Calculated objects are evaluated only after loading into an engine.</p></details><details><summary>Benchmark workloads · {len(manifest['workloads'])}</summary>{cases}</details></section>
<footer>Synthetic fixture · No source connections or engine execution performed</footer></main></body></html>'''


def render_benchmark_html(result, source_link=None):
    from .report import _CSS, _number, _anchor
    esc = lambda value: html.escape(str(value), quote=True)
    ranking = [(case, cache, stats) for case in result.cases for cache, stats in case['phases'].items() if stats['attempts']]
    for case in result.cases:
        if not case['rows']:
            ranking.append((case, 'not collected', case['phases']['warm']))
    ranking.sort(key=lambda item: (item[2]['mean_ms'] is None, -(item[2]['mean_ms'] or 0), item[0]['id'], item[1]))
    rows = ''.join(f'<tr><td>{i}</td><td><a href="#case-{case["id"]}">{esc(case["measure"])}</a><small>{esc(case["scenario"])}</small></td><td>{cache}</td><td>{_number(stats["mean_ms"], "ms")}</td><td>{_number(stats["p95_ms"], "ms")}</td><td>{stats["successes"]}/{stats["attempts"]}</td></tr>' for i, (case, cache, stats) in enumerate(ranking, 1))
    details = []
    for case in result.cases:
        attempts = ''.join(f'<tr><td>{r["sequence"]}</td><td>{r["cache"]}</td><td>{_number(r["duration_ms"], "ms")}</td><td>{_number(r["cpu_ms"], "ms")}</td><td>{_number(r["rows"])}</td><td>{esc(r["error"] or "Success")}</td></tr>' for r in case['rows'])
        chain_link = f'<p><a href="{esc(source_link)}#{_anchor("measure-", case["measure_id"])}">Inspect static findings, DAX and dependency chain</a></p>' if source_link else ''
        if source_link and case.get('matrix_id'):
            matrix_link = source_link.replace('analysis/report.html', 'matrix-analysis/report.html')
            chain_link = f'<p><a href="{esc(matrix_link)}#{_anchor("matrix-", case["matrix_id"])}">Inspect matrix context, measure chains and grain advice</a></p>'

        details.append(f'<details id="case-{case["id"]}"><summary>{esc(case["measure"])} · {esc(case["scenario"])}</summary><p>Groups: {esc(case["group_by"])} · Filters: {esc(case["filters"])}</p>{chain_link}<details><summary>DAX workload</summary><pre>{esc(case["query"])}</pre></details><details><summary>Individual iterations</summary><table><thead><tr><th>Run</th><th>Cache</th><th>Duration</th><th>CPU (verified trace only)</th><th>Rows returned</th><th>Outcome</th></tr></thead><tbody>{attempts}</tbody></table></details></details>')
    warnings = ''.join(f'<li>{esc(w)}</li>' for w in result.diagnostics)
    count = sum(len(c['rows']) for c in result.cases)
    failures = sum(bool(r['error']) for c in result.cases for r in c['rows'])
    missing = sum(not c['rows'] for c in result.cases)
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Synthetic benchmark results</title><style>{_CSS}</style></head><body><main>
<header><div class="eyebrow">SEMANTIC HOTSPOTS / ENGINE BENCHMARKS</div><h1>Synthetic workloads.<br>{'Measured query durations.' if count else 'No engine timings collected.'}</h1><p>{esc(result.manifest['source_model'])} · Import mode · seed {result.manifest['seed']}</p></header>
<section><h2>Slowest measured workloads first</h2><p>{count} captured iterations · {failures} failed iterations · {len(result.cases)} workload cases · {missing} cases without results.</p><div class="interpretation">Compare like-for-like workloads and cache modes. These timings include the selected measures' full chains under synthetic data. They are not isolated child-measure costs, production predictions or CU estimates. Duration may include client wall-clock fallback when server traces are unavailable. Failed runs are excluded from timing averages and remain visible. Timing CSVs do not validate result values: inspect the queries to check meaningful values and BLANK branches.</div>
<div class="scroll"><table><thead><tr><th>#</th><th>Measure and scenario</th><th>Cache</th><th>Mean duration ↓</th><th>p95 duration</th><th>Successful runs</th></tr></thead><tbody>{rows or '<tr><td colspan="6">No iterations collected. Performance is unknown.</td></tr>'}</tbody></table></div></section>
<section><h2>Inspect a workload</h2>{''.join(details)}</section><section><details><summary>Coverage and assumptions · {len(result.diagnostics)}</summary><ul>{warnings}</ul></details><p><a href="benchmark.json">Full benchmark JSON</a></p></section>
<footer>Observed synthetic-workload timings · CPU fields require independently verified server traces</footer></main><script>document.addEventListener('click',function(e){{const a=e.target.closest('a[href^="#case-"]');if(a){{const d=document.getElementById(a.hash.slice(1));if(d)d.open=true;}}}});</script></body></html>'''
