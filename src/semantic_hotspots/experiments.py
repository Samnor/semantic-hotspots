"""Query-scoped candidate validation and A/B timing on an existing synthetic model.

The DAX engine evaluates equality including BLANK flags. Python never evaluates
DAX or infers engine work from row counts. Source models are never deployed/edited.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import html
import json
import math
import re
import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .benchmark import _manifest, read_benchmark_csv
from .model import Entity, ModelError
from .parser import load_model
from .simulation import SimulationConfig, _workload_queries


def _hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')


def _fresh(path):
    path = Path(path).resolve()
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ModelError('Output directory must be absent or empty')
    path.mkdir(parents=True, exist_ok=True)
    return path


def prepare_rewrite_experiment(bundle, directory, *, top=20, validation_dates=None):
    """Prepare immutable query alternatives; the already loaded model stays unchanged.

    Scenarios are inherited from the synthetic bundle. A scalar total is always
    included. Each candidate changes only its own root measure expression.
    """
    if isinstance(top, bool) or not isinstance(top, int) or top < 1:
        raise ModelError('top must be a positive candidate count')
    source_root, source = _manifest(bundle)
    model_path = source_root/'synthetic-model.bim'
    model = load_model(model_path)
    from .analyzer import analyze
    analysis = analyze(model)
    plan = analysis.rewrite_plan
    from .date_validation import date_controls, query_definitions
    candidates = copy.deepcopy([c for c in plan.candidates if c.get('validation_available',True)][:top])
    for candidate in candidates:
        candidate['date_controls']=date_controls(model,model.entities[candidate['measure_id']],dates=validation_dates,graph=analysis.graph)
    candidates=[c for c in candidates if not (c.get('date_controls') or {}).get('blocked_reasons')]
    if not candidates:
        raise ModelError('No guarded rewrite candidates in the supported logical subset')
    config = SimulationConfig.from_dict(source['config'])
    # Include every candidate root, even if the simulation timed a smaller top-N.
    selected_ids = list(dict.fromkeys(c['measure_id'] for c in candidates))
    config.top = len(selected_ids)
    workloads = _workload_queries(model, config, [{'id': n} for n in selected_ids])
    files, cases = {}, []
    names = {e.name.casefold() for e in model.entities.values()}
    for candidate in candidates:
        alias_name = '__sh_candidate_' + candidate['id']
        if alias_name.casefold() in names:
            raise ModelError('Generated candidate name collides with model metadata')
        owner = model.entities[candidate['measure_id']]
        alias = Entity('measure', owner.table, alias_name).qualified_name
        controls=candidate.get('date_controls')
        expanded=[]
        for workload in workloads:
            if workload['measure_id']!=candidate['measure_id']:continue
            for as_of in controls['dates'] if controls else [None]:
                w=copy.deepcopy(workload)
                w['as_of']=as_of
                if as_of:w['scenario']+='-as-of-'+as_of
                expanded.append(w)
        for w in expanded:
            definition=query_definitions(owner.table,owner.name,candidate['after'],alias_name,controls,w['as_of'])
            identity = _hash(candidate['id']+'\x00'+w['scenario'])[:20]
            prefix = '__sh_' + identity + '_'
            columns = {key: prefix+key for key in ['original', 'candidate', 'equal', 'original_blank', 'candidate_blank']}
            if any(name.casefold() in names for name in columns.values()):
                raise ModelError('Validation column name collides with model metadata')
            original = owner.qualified_name
            fields = [f'"{columns["original"]}", {original}', f'"{columns["candidate"]}", {alias}',
                      f'"{columns["equal"]}", IF(ISBLANK({original}) == ISBLANK({alias}) && {original} == {alias}, 1, 0)',
                      f'"{columns["original_blank"]}", IF(ISBLANK({original}), 1, 0)',
                      f'"{columns["candidate_blank"]}", IF(ISBLANK({alias}), 1, 0)']
            # Replace the one generated result projection, preserving group/filter DAX.
            marker = '"Result", ' + original
            if w['query'].count(marker) != 1:
                raise ModelError('Unexpected generated workload projection')
            validation = definition+w['query'].replace(marker, ',\n    '.join(fields))
            queries = {'validation': validation, 'baseline': (definition if controls else '')+w['query'],
                       'candidate': definition+w['query'].replace(marker, '"Result", '+alias)}
            paths = {}
            for kind, query in queries.items():
                filename = f'queries/{identity}-{kind}.dax'
                files[filename] = query
                paths[kind] = {'file': filename, 'sha256': _hash(query), 'query': query}
            cases.append({'id': identity, 'candidate_id': candidate['id'], 'measure_id': candidate['measure_id'],
                          'measure': candidate['measure'], 'scenario': w['scenario'], 'as_of':w['as_of'], 'columns': columns,
                          'group_by': w['group_by'], 'filters': w['filters'], 'queries': paths})
    manifest = {'schema_version': '1.0', 'kind': 'rewrite-experiment', 'source_model': source['source_model'],
                'synthetic_model_name': source['synthetic_model_name'], 'seed': source['seed'],
                'synthetic_model_sha256': _hash(model_path.read_text(encoding='utf-8')),
                'simulation_manifest_sha256': _hash((source_root/'simulation.json').read_text(encoding='utf-8')),
                'config': source['config'], 'candidates': candidates, 'cases': cases,
                'warnings': source['warnings'] + [
                    'Finite synthetic cases are evidence, not universal equivalence or production speed predictions.',
                    'Exact engine equality and explicit BLANK flags are checked; floating-point differences fail conservatively.',
                    'No runtime type metadata, model identity, source latency, RLS, or production capacity validation is inferred.',
                    'Timing noise is not a statistically established improvement. Candidate aliases may affect engine caching.']}
    root = _fresh(directory)
    (root/'queries').mkdir()
    for filename, query in files.items():
        (root/filename).write_text(query, encoding='utf-8')
    _write_json(root/'experiment.json', manifest)
    plan.write(root/'proposals')
    (root/'RUN-INSTRUCTIONS.txt').write_text(
        'Load and refresh the original synthetic-model.bim in a separate DAX engine.\n'
        'Use that same model, data, server and database for all candidates.\n'
        'No model deployment or source edits are performed by this experiment.\n'
        'Run: python -m semantic_hotspots experiment-run EXPERIMENT --dscmd PATH --server SERVER --database DATABASE\n'
        'Cold iterations clear the selected database cache; the default is zero.\n'
        'Inspect experiment-results/experiment.html for validation and paired query timings.\n'
        'Add grouped, filtered, empty-selection, boundary and BLANK scenarios to SimulationConfig before preparing.\n', encoding='utf-8')
    RewriteResults(manifest, [], ['Engine validation and timing have not run.']).write(root)
    return root


def _experiment(directory):
    root = Path(directory).resolve()
    try:
        data = json.loads((root/'experiment.json').read_text(encoding='utf-8'))
        if data['kind'] != 'rewrite-experiment' or not data['cases'] or not data['candidates']:
            raise ValueError('Missing experiment cases')
        ids = [c['id'] for c in data['candidates']]
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate candidates')
        for candidate in data['candidates']:
            if candidate['before_sha256'] != _hash(candidate['before']) or candidate['after_sha256'] != _hash(candidate['after']):
                raise ValueError('Changed candidate expression')
            if candidate['id'] != _hash(candidate['measure_id']+'\x00'+candidate['before']+'\x00'+candidate['after'])[:20]:
                raise ValueError('Changed candidate identity')
        seen = set()
        for case in data['cases']:
            if not re.fullmatch('[a-f0-9]{20}', case['id']) or case['id'] in seen or case['candidate_id'] not in ids:
                raise ValueError('Invalid/duplicate case identity')
            if case['id'] != _hash(case['candidate_id']+'\x00'+case['scenario'])[:20]:
                raise ValueError('Changed scenario/candidate identity')
            if set(case['columns']) != {'original', 'candidate', 'equal', 'original_blank', 'candidate_blank'} or len(set(case['columns'].values())) != 5:
                raise ValueError('Invalid validation columns')
            seen.add(case['id'])
            for kind in ['validation', 'baseline', 'candidate']:
                query = case['queries'][kind]
                path = (root/query['file']).resolve()
                if not path.is_relative_to(root/'queries') or not path.is_file() or path.suffix != '.dax':
                    raise ValueError('Invalid query path')
                if not re.fullmatch('[a-f0-9]{64}', str(query['sha256'])):
                    raise ValueError('Invalid query fingerprint')
                if query['sha256'] != _hash(query['query']) or path.read_text(encoding='utf-8') != query['query']:
                    raise ValueError('Query differs from its recorded experiment')
        if set(ids) != {c['candidate_id'] for c in data['cases']}:
            raise ValueError('Candidate lacks validation cases')
        return root, data
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise ModelError('Invalid or modified rewrite experiment: '+str(exc)) from exc


def _validation(path, columns):
    if not path.is_file():
        return {'status': 'not_run', 'rows': 0, 'nonblank_original_rows': 0, 'mismatches': None}
    try:
        with path.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream, strict=True)
            headers = reader.fieldnames or []
            # dscmd commonly brackets calculated projection names.
            normalized = [h[1:-1] if h.startswith('[') and h.endswith(']') else h for h in headers]
            if len(normalized) != len(set(normalized)) or not set(columns.values()).issubset(normalized):
                raise ValueError('Missing or duplicate validation columns')
            index = dict(zip(normalized, headers))
            count = nonblank = mismatches = 0
            for row in reader:
                if None in row or any(v is None for v in row.values()):
                    raise ValueError('Malformed validation CSV row')
                flags = [row[index[columns[k]]].strip() for k in ['equal', 'original_blank', 'candidate_blank']]
                if any(f not in {'0', '1'} for f in flags):
                    raise ValueError('Validation flags must be engine-produced 0/1 values')
                count += 1
                nonblank += flags[1] == '0'
                mismatches += flags[0] != '1' or flags[1] != flags[2]
            return {'status': 'passed' if count and not mismatches else 'failed', 'rows': count,
                    'nonblank_original_rows': nonblank, 'mismatches': mismatches,
                    'error': None if count else 'Zero validation rows; no correctness evidence'}
    except (ValueError, csv.Error, UnicodeError) as exc:
        return {'status': 'failed', 'rows': 0, 'nonblank_original_rows': 0, 'mismatches': None, 'error': str(exc)}


def _phases(rows, expected=None):
    phases = {}
    for cache in ['cold', 'warm']:
        attempts = [r for r in rows if r['cache'] == cache]
        values = [r['duration_ms'] for r in attempts if not r['error']]
        phases[cache] = {'attempts': len(attempts), 'successes': len(values),
                         'mean_ms': statistics.mean(values) if values else None,
                         'complete': bool(attempts) and len(values) == len(attempts)
                                     and expected is not None and len(attempts) == expected[cache]}
    return phases


@dataclass
class RewriteResults:
    manifest: dict
    cases: list[dict]
    diagnostics: list[str]

    def to_dict(self):
        ranked = []
        for candidate in self.manifest['candidates']:
            cases = [c for c in self.cases if c['candidate_id'] == candidate['id']]
            expected_cases = [c for c in self.manifest['cases'] if c['candidate_id'] == candidate['id']]
            full = len(cases) == len(expected_cases)
            validation = 'not_run'
            if any(c['validation']['status'] == 'failed' for c in cases):
                validation = 'failed'
            elif full and cases and all(c['validation']['status'] == 'passed' for c in cases):
                validation = 'passed_tested_cases'
            nonblank = sum(c['validation']['nonblank_original_rows'] for c in cases)
            comparisons = [p for c in cases for p in c['comparisons']]
            performance = 'unknown'
            if validation == 'failed':
                performance = 'withheld_validation_failed'
            elif validation == 'passed_tested_cases' and not nonblank:
                performance = 'uninformative_all_blank'
            elif validation == 'passed_tested_cases' and comparisons and all(p['complete'] for p in comparisons):
                changes = [p['baseline_mean_ms'] - p['candidate_mean_ms'] for p in comparisons]
                performance = ('observed_improvement' if all(v >= 0 for v in changes) and any(v > 0 for v in changes)
                               else 'mixed_results' if any(v > 0 for v in changes) and any(v < 0 for v in changes)
                               else 'no_observed_improvement')
            baseline = [p['baseline_mean_ms'] for p in comparisons if p['baseline_mean_ms'] is not None]
            ranked.append(dict(candidate, validation_status=validation, performance_status=performance,
                               nonblank_original_rows=nonblank, tested_cases=len(cases), expected_cases=len(expected_cases),
                               highest_baseline_mean_ms=max(baseline, default=None), cases=cases))
        # Equal-weight tested scenarios/cache phases; this is a fixture-specific
        # choice for review, not a production cost estimate or automatic edit.
        fastest = {}
        for candidate in ranked:
            candidate['selected_for_review'] = False
            if candidate['performance_status'] == 'observed_improvement':
                duration = sum(p['candidate_mean_ms'] for c in candidate['cases'] for p in c['comparisons'])
                measure = candidate['measure_id']
                previous = fastest.get(measure)
                if previous is None or duration < previous[0]:
                    fastest[measure] = (duration, candidate['id'])
        for candidate in ranked:
            candidate['selected_for_review'] = candidate['id'] in {v[1] for v in fastest.values()}
        ranked.sort(key=lambda r: (r['highest_baseline_mean_ms'] is None, -(r['highest_baseline_mean_ms'] or 0)))
        return {'schema_version': '1.0', 'kind': 'rewrite-experiment-results',
                'interpretation': 'Tested-case equality and observed query durations only; no universal proof, isolated measure cost, significance claim, or production prediction.',
                'diagnostics': self.diagnostics,
                'selection_basis': 'Lowest sum of equally weighted scenario/cache mean durations among candidates with tested-case equality and no observed scenario regression. Not significance-tested or a production workload weighting.',
                'fastest_observed_candidate_ids': {m: v[1] for m, v in fastest.items()},
                'ranked_candidates': ranked}

    def write(self, directory):
        from .report import _CSS, _number
        from .comparison import DAG_CSS, write_candidate_dags, render_dag_comparison
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        data = self.to_dict()
        _write_json(root/'rewrite-results.json', data)
        write_candidate_dags(data['ranked_candidates'], root/'dag-comparisons')
        esc = lambda value: html.escape(str(value), quote=True)
        rows, details = [], []
        for i, candidate in enumerate(data['ranked_candidates'], 1):
            anchor = 'candidate-'+candidate['id']
            rows.append(f'<tr><td>{i}</td><td><a href="#{anchor}">{esc(candidate["measure"])}</a></td><td>{_number(candidate["highest_baseline_mean_ms"], "ms")}</td><td>{esc(candidate["validation_status"])}</td><td>{esc(candidate["performance_status"])}{" · selected for review" if candidate["selected_for_review"] else ""}</td></tr>')
            evidence = []
            for case in candidate['cases']:
                timings = ''.join(f'<tr><td>{esc(p["cache"])}</td><td>{_number(p["baseline_mean_ms"], "ms")}</td><td>{_number(p["candidate_mean_ms"], "ms")}</td><td>{_number(p["change_percent"], "%")}</td><td>{esc(p["complete"])}</td></tr>' for p in case['comparisons'])
                evidence.append(f'<details><summary>{esc(case["scenario"])} · validation {esc(case["validation"]["status"])}</summary><p>{esc(case["validation"])}</p><table><thead><tr><th>Cache</th><th>Before</th><th>Candidate</th><th>Reduction</th><th>Complete</th></tr></thead><tbody>{timings}</tbody></table><details><summary>Executed query definitions and fingerprints</summary><pre>{esc(json.dumps(case["queries"], indent=2))}</pre></details></details>')
            dags = ('<details><summary>Original and proposed measure DAGs · proposal snapshot</summary>'+render_dag_comparison(candidate['dag_comparison'])+'<p><a href="dag-comparisons/'+candidate['id']+'/report.html">Open comparison and download DAGs</a></p></details>') if candidate.get('dag_comparison') else ''
            details.append(f'<details id="{anchor}"><summary>{esc(candidate["measure"])} · {esc(candidate["performance_status"])}</summary><p>Validation: {esc(candidate["validation_status"])} · {candidate["tested_cases"]}/{candidate["expected_cases"]} cases · {candidate["nonblank_original_rows"]} non-BLANK original rows.</p><h3>Before</h3><pre>{esc(candidate["before"])}</pre><h3>Candidate</h3><pre>{esc(candidate["after"])}</pre><details><summary>Rewrite steps and logical plan</summary><pre>{esc(json.dumps({"steps": candidate["steps"], "logical_before": candidate["logical_before"], "logical_after": candidate["logical_after"], "direct_dependencies_before": candidate["direct_dependencies_before"], "direct_dependencies_after": candidate["direct_dependencies_after"]}, indent=2))}</pre></details>{dags}{"".join(evidence)}</details>')
        warnings = ''.join(f'<li>{esc(x)}</li>' for x in self.diagnostics+self.manifest['warnings'])
        page = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Rewrite experiment results</title><style>{_CSS}{DAG_CSS}</style></head><body><main><h1>Rewrite evidence</h1><p><a href="rewrite-results.json">Download full evidence JSON</a></p><p>Highest observed baseline mean first. Missing timing stays unknown. Equality covers only the tested contexts; all-BLANK cases cannot establish a useful performance win. Positive reduction means a lower observed candidate duration.</p><div class="scroll"><table><thead><tr><th>Priority</th><th>Measure</th><th>Highest baseline mean</th><th>Equality</th><th>Performance</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div><p>{esc(data['selection_basis'])}</p><h2>Inspect alternatives</h2>{''.join(details)}<details><summary>Coverage and limitations</summary><ul>{warnings}</ul></details></main><script>function reveal(){{const e=document.getElementById(location.hash.slice(1));if(e){{e.open=true;e.scrollIntoView();}}}}addEventListener('hashchange',reveal);reveal();</script></body></html>'''
        (root/'experiment.html').write_text(page, encoding='utf-8')
        return {'json': root/'rewrite-results.json', 'html': root/'experiment.html'}


def collect_rewrite_results(directory, results_directory, *, trace_verified=False):
    """Collect paired outputs; offline files are reported evidence, not attested runs."""
    _, manifest = _experiment(directory)
    out = Path(results_directory)
    runner_path = out/'rewrite-runner.json'
    runner = json.loads(runner_path.read_text(encoding='utf-8')) if runner_path.is_file() else None
    diagnostics = ['Durations may use server traces or client wall-clock fallback; see timing_source on retained rows. No CU conversion is performed.', 'CSV evidence is not an attestation of engine/model identity. Verify the loaded synthetic model and retained runner outputs.']
    expected = None
    runs = {}
    if runner is not None:
        if not isinstance(runner, dict):
            raise ModelError('Malformed runner metadata')
        if runner.get('engine') == 'adomd':
            diagnostics.append('Direct ADOMD client elapsed timings include execution, transfer and complete result reading/conversion, excluding connection setup. CPU/FE/SE are unmeasured. Warm means no cache clearing; the first iteration can encounter cold caches. Worker startup and connection setup occur again for each validation or timing workload.')
        if runner.get('experiment_sha256') != _hash(json.dumps(manifest, sort_keys=True, ensure_ascii=False)):
            raise ModelError('Runner capture belongs to a different experiment')
        expected = {k: runner.get(k) for k in ['cold', 'warm']}
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in expected.values()) or not sum(expected.values()):
            raise ModelError('Invalid runner iteration counts')
        try:
            runs = {(r['case_id'], r['kind']): r for r in runner['runs']}
            if len(runs) != len(runner['runs']):
                raise ValueError('Duplicate run records')
            cases_by_id = {c['id']: c for c in manifest['cases']}
            for (case_id, kind), run in runs.items():
                if run['query_sha256'] != cases_by_id[case_id]['queries'][kind]['sha256']:
                    raise ValueError('Run/query fingerprint mismatch')
                output_path = out/f'{case_id}-{kind}.csv'
                actual = _file_hash(output_path) if output_path.is_file() else None
                if actual != run.get('output_sha256'):
                    raise ValueError('Run/output fingerprint mismatch')
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelError('Malformed or mismatched runner records') from exc
    else:
        diagnostics.append('Runner metadata missing: iteration coverage and timing completion are unknown.')
    cases = []
    for case in manifest['cases']:
        identity = case['id']
        validation = _validation(out/f'{identity}-validation.csv', case['columns'])
        run = runs.get((identity, 'validation'))
        if runner is not None and (run is None or run.get('returncode') != 0):
            validation['status'] = 'failed' if run else 'not_run'
            validation['error'] = 'Validation process failed or has no runner record'
        phases, benchmark_rows = {}, {}
        for kind in ['baseline', 'candidate']:
            path = out/f'{identity}-{kind}.csv'
            rows = read_benchmark_csv(path, trace_verified=trace_verified) if path.is_file() else []
            benchmark_rows[kind] = rows
            phases[kind] = _phases(rows, expected)
            expected_rows = 1 if not case['group_by'] and not case['filters'] else validation['nonblank_original_rows']
            if any(r['rows'] is not None and r['rows'] != expected_rows for r in rows if not r['error']):
                diagnostics.append(f'{identity}/{kind}: benchmark row count differs from paired validation; timing coverage withheld.')
                for p in phases[kind].values():
                    p['complete'] = False
            run = runs.get((identity, kind))
            if runner is None or run is None or run.get('returncode') != 0:
                for p in phases[kind].values():
                    p['complete'] = False
        comparisons = []
        for cache in ['cold', 'warm']:
            if expected is not None and not expected[cache]:
                continue
            b, a = phases['baseline'][cache], phases['candidate'][cache]
            before, after = b['mean_ms'], a['mean_ms']
            comparisons.append({'cache': cache, 'baseline_mean_ms': before, 'candidate_mean_ms': after,
                                'change_percent': (before-after)/before*100 if before and after is not None else None,
                                'baseline_iterations': b, 'candidate_iterations': a,
                                'complete': b['complete'] and a['complete']})
        cases.append(dict(case, validation=validation, comparisons=comparisons, benchmark_rows=benchmark_rows))
    return RewriteResults(manifest, cases, diagnostics)


def run_rewrite_experiment(directory, *, server, database, dscmd=None, engine='auto',
                           adomd_path=None, dotnet_runtime='netfx', results_directory=None,
                           cold=0, warm=3, timeout=300, trace_verified=False):
    """Validate first, then benchmark paired alternatives if every case matches.

    Only query-scoped alias measures are defined. No source/model edits or
    deployment. Cold>0 explicitly requests clearing the selected database cache.
    """
    root, manifest = _experiment(directory)
    from .engine import EngineRunner
    runner = EngineRunner(server=server, database=database, engine=engine, dscmd=dscmd,
                          adomd_path=adomd_path, dotnet_runtime=dotnet_runtime,
                          cold=cold, trace_verified=trace_verified)
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in [cold, warm]) or not cold+warm:
        raise ModelError('cold/warm must be nonnegative integers with at least one iteration')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ModelError('timeout must be finite and positive')
    out = _fresh(results_directory or root/'experiment-results')
    capture = {'experiment_sha256': _hash(json.dumps(manifest, sort_keys=True, ensure_ascii=False)),
               'engine': runner.engine, 'server': runner.server, 'database': str(database), 'cold': cold, 'warm': warm,
               'timeout': timeout, 'runs': []}
    def run(case, kind):
        output = out/f'{case["id"]}-{kind}.csv'
        command = runner.command('csv' if kind == 'validation' else 'benchmark', output,
                                 root/case['queries'][kind]['file'], cold=cold, warm=warm, timeout=timeout)
        try:
            process = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout, check=False)
            result = {'returncode': process.returncode, 'stdout': process.stdout, 'stderr': process.stderr}
        except (subprocess.TimeoutExpired, OSError) as exc:
            result = {'returncode': None, 'stdout': '', 'stderr': str(exc)}
        capture['runs'].append(dict(result, case_id=case['id'], kind=kind, query_sha256=case['queries'][kind]['sha256'],
                                    output_sha256=_file_hash(output) if output.is_file() else None))
        _write_json(out/'rewrite-runner.json', capture)
        return result['returncode'] == 0
    validation = {}
    for case in manifest['cases']:
        success = run(case, 'validation')
        evidence = _validation(out/f'{case["id"]}-validation.csv', case['columns'])
        validation[case['id']] = evidence if success else dict(evidence, status='failed')
    for candidate in manifest['candidates']:
        cases = [c for c in manifest['cases'] if c['candidate_id'] == candidate['id']]
        if (not all(validation[c['id']]['status'] == 'passed' for c in cases)
                or not any(validation[c['id']]['nonblank_original_rows'] for c in cases)):
            continue
        for i, case in enumerate(cases):
            # Alternate order to reduce a systematic always-second advantage.
            for kind in (['baseline', 'candidate'] if i % 2 == 0 else ['candidate', 'baseline']):
                run(case, kind)
    result = collect_rewrite_results(root, out, trace_verified=trace_verified)
    result.write(out)
    return result
