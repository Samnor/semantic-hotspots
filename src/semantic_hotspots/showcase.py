"""Bounded, read-only validation of a generated showcase on a local DAX engine.

This runner checks every generated context before collecting any timings. Its
binding checks identify expected measure definitions and physical row counts;
they are not a full attestation of loaded data, relationships or security.
"""
from __future__ import annotations

import csv
import hashlib
import html
import json
import os
import statistics
from dataclasses import dataclass
from pathlib import Path

from .benchmark import read_benchmark_csv
from .dax import tokenize
from .engine import query_to_csv, local_server, _timeout
from .model import Entity, ModelError
from .matrices import MatrixSpec
from .project import _comparison_queries


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _inside(root, name):
    if not isinstance(name, str) or not name or '\\' in name:
        raise ModelError('Invalid showcase file path')
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ModelError('Showcase file is missing or outside the project')
    return path


def _load(directory):
    root = Path(directory).resolve()
    manifest = json.loads((root / 'showcase.json').read_text(encoding='utf-8-sig'))
    if manifest.get('kind') != 'power-bi-showcase-project' or manifest.get('schema_version') != '1.0':
        raise ModelError('Expected a generated showcase.json project')
    bim = _inside(root, 'Showcase.SemanticModel/model.bim')
    if _digest(bim) != manifest.get('model_sha256'):
        raise ModelError('Showcase model fingerprint changed; regenerate the project')
    if not manifest.get('changes') or not manifest.get('cases'):
        raise ModelError('Showcase has no changes or validation contexts')
    spec = MatrixSpec.from_dict(manifest['spec'])
    document = json.loads(bim.read_text(encoding='utf-8-sig'))
    definitions = {(t['name'], m['name']): m['expression'] for t in document['model']['tables'] for m in t.get('measures', [])}
    for change in manifest['changes']:
        if change['original_name'] == change['candidate']['name'] or any(
                definitions.get((change['table'], name)) != expression for name, expression in [
                    (change['original_name'], change['before']), (change['candidate']['name'], change['candidate']['expression'])]):
            raise ModelError('Showcase change definitions differ from the fingerprinted model')
    selected = [(c['table'].casefold(), c['original_name'].casefold()) for c in manifest['changes']]
    if selected != [(p[0].casefold(), p[1].casefold()) for p in spec.values]:
        raise ModelError('Showcase measure pairs differ from the declared matrix')
    if not spec.context_complete or spec.filters or spec.filter_measures or spec.drill_states or not all([spec.row_subtotals, spec.column_subtotals, spec.grand_total]):
        raise ModelError('Showcase runner requires the exporter’s complete, unfiltered default context')
    expected_contexts = [(f'expanded: rows={r}, columns={c}', spec.rows[:r]+spec.columns[:c])
                         for r in range(len(spec.rows)+1) for c in range(len(spec.columns)+1)]
    if [(c['scenario'], c['group_by']) for c in manifest['cases']] != expected_contexts:
        raise ModelError('Showcase prefix contexts are incomplete or changed')
    checked = {'showcase.json': _digest(root / 'showcase.json'),
               'Showcase.SemanticModel/model.bim': _digest(bim)}
    paths = set()
    for case in manifest['cases']:
        canonical = _comparison_queries(case['group_by'], manifest['changes'])
        for kind in ('validation', 'baseline', 'candidate'):
            entry = case['queries'][kind]
            path = _inside(root, entry['file'])
            if path in paths or _digest(path) != entry['sha256']:
                raise ModelError('Duplicate or changed showcase query fingerprint')
            if path.read_text(encoding='utf-8-sig') != canonical[kind]:
                raise ModelError('Showcase query does not match the canonical paired comparison')
            paths.add(path)
            checked[entry['file']] = entry['sha256']
    # Row counts are rebuilt from the fingerprinted BIM, not an untracked file.
    return root, manifest, document, checked


def _rows(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.reader(stream, strict=True)
        try:
            header = next(reader)
        except StopIteration:
            raise ModelError('Engine CSV has no header') from None
        header = [h[1:-1].replace(']]', ']') if h.startswith('[') and h.endswith(']') else h for h in header]
        if not header or len(set(header)) != len(header):
            raise ModelError('Engine CSV has duplicate columns')
        result = []
        for row in reader:
            if len(row) != len(header):
                raise ModelError('Engine CSV has a malformed row')
            result.append(dict(zip(header, row)))
    return result


def _tokens(text):
    return [(t.kind, t.value if t.kind == 'STRING' else t.value.casefold()) for t in tokenize(text)]


def _validate(rows, count, grouped):
    if not rows or (not grouped and len(rows) != 1):
        raise ModelError('Validation returned no rows or an invalid total row count')
    nonblank = [0] * count
    mismatches = 0
    nonblank_rows = 0
    for row in rows:
        present = False
        for i in range(1, count + 1):
            keys = [f'Equal_{i}', f'OriginalBlank_{i}', f'CandidateBlank_{i}']
            flags = [row.get(k) for k in keys]
            if any(f not in {'0', '1'} for f in flags):
                raise ModelError('Missing or malformed equality/BLANK flags')
            for prefix, flag in [('Original', flags[1]), ('Candidate', flags[2])]:
                value = row.get(f'{prefix}_{i}')
                if value is None or (flag == '1') != (value == ''):
                    raise ModelError('Value and BLANK flag disagree')
            mismatches += flags[0] != '1' or flags[1] != flags[2]
            if flags[1] == '0':
                nonblank[i-1] += 1
                present = True
        nonblank_rows += present
    return {'status': 'failed' if mismatches else 'passed', 'rows': len(rows),
            'mismatches': mismatches, 'nonblank_values_by_measure': nonblank,
            'timing_rows': nonblank_rows if grouped else 1}


def _timing(path, iterations, expected_rows):
    records = read_benchmark_csv(path)
    if len(records) != iterations + 1 or {r['sequence'] for r in records} != set(range(1, iterations + 2)):
        raise ModelError('Incomplete timing iterations')
    records.sort(key=lambda r: r['sequence'])
    if any(r['error'] or r['cache'] != 'warm' or r['rows'] != expected_rows or
           not r['timing_source'].startswith('client elapsed') for r in records):
        raise ModelError('Timing failed, returned different rows, or has an unexpected source')
    # The first iteration is an explicit warm-up; it remains in raw evidence.
    samples = [r['duration_ms'] for r in records[1:]]
    return {'iterations': records, 'excluded_warmup_sequence': 1,
            'median_ms': statistics.median(samples), 'min_ms': min(samples),
            'max_ms': max(samples), 'samples': len(samples)}


def _verdict(before, after):
    """Descriptive, conservative separation; this is not a significance test."""
    b, a = before['median_ms'], after['median_ms']
    threshold = max(1.0, b * .10)
    if b - a > threshold and after['max_ms'] < before['min_ms']:
        return 'observed_faster'
    if a - b > threshold and before['max_ms'] < after['min_ms']:
        return 'observed_slower'
    return 'inconclusive'


@dataclass
class ShowcaseResults:
    directory: Path
    data: dict

    @property
    def report_path(self):
        return self.directory / 'report.html'

    def to_dict(self):
        return self.data


def _save(result):
    path = result.directory / 'results.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(result.data, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    os.replace(temporary, path)
    result.report_path.write_text(render_showcase_results(result.data), encoding='utf-8')


def run_showcase(directory, *, server, database, results_directory=None, adomd_path=None,
                 dotnet_runtime='netfx', warm=7, timeout=120, progress=None):
    """Check generated bindings/rows, validate all pairs, then time both paths.

    A fresh results directory is required. The selected loaded model is queried
    read-only; Desktop opening/refresh remain explicit user steps. A failed run
    retains its report, error, progress and complete query/output fingerprints.
    """
    server = local_server(server)
    _timeout(timeout)
    if not isinstance(database, str) or not database or '\x00' in database:
        raise ModelError('Select an explicit loaded database')
    if isinstance(warm, bool) or not isinstance(warm, int) or not 5 <= warm <= 50:
        raise ModelError('warm must be between 5 and 50 measured iterations')
    try:
        root, manifest, bim, fingerprints = _load(directory)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ModelError('Malformed showcase manifest or model; regenerate the project') from exc
    out = Path(results_directory).resolve() if results_directory else root / 'showcase-results'
    if out.exists():
        raise ModelError('Results directory already exists; choose a new output')
    out.mkdir(parents=True)
    result = ShowcaseResults(out, {'schema_version': '1.0', 'kind': 'showcase-engine-results',
        'status': 'running', 'performance_status': 'not_run', 'validation_status': 'not_run',
        'server': server, 'database': database, 'matrix_id': manifest['matrix_id'], 'cases': [],
        'source_fingerprints': fingerprints, 'runs': [], 'progress': 'Checking loaded measure bindings',
        'limitations': [
            'Binding checks compare all generated measure expressions and physical row counts, not data contents, relationships, partitions, formats or security.',
            'Exact equality is limited to generated synthetic prefix contexts, not full visual semantics, production data, RLS or future slicers.',
            'Client elapsed includes execution and result reading; CPU/FE/SE are unknown. No cache clearing or deployment is performed.',
            'First timing iteration is excluded as warm-up. Order alternates between contexts; paths are not interleaved within a context.',
            'Observed faster/slower requires nonoverlapping sample ranges and median change greater than both 10% and 1 ms. This descriptive rule is not a statistical significance test.',
            'Row reduction does not establish speedup. Summary refresh time and model memory are not measured.']})

    def update(message):
        result.data['progress'] = message
        _save(result)
        if progress:
            progress(message)

    def execute(label, text, *, timing=False):
        query = out / (label + '.dax')
        query.write_text(text, encoding='utf-8', newline='\n')
        output = out / (label + '.csv')
        record = {'query': query.name, 'query_sha256': _digest(query), 'output': output.name, 'status': 'running'}
        result.data['runs'].append(record)
        try:
            query_to_csv(query, output, server=server, database=database, adomd_path=adomd_path,
                         dotnet_runtime=dotnet_runtime, timeout=timeout, benchmark=timing, warm=warm+1)
            record.update(status='completed', output_sha256=_digest(output))
            return output
        except Exception:
            record['status'] = 'failed'
            raise

    try:
        update('Checking loaded measure bindings')
        tables = _rows(execute('loaded-tables', 'EVALUATE INFO.TABLES()\n'))
        measures = _rows(execute('loaded-measures', 'EVALUATE INFO.MEASURES()\n'))
        table_ids = {r['ID']: r['Name'] for r in tables}
        known = {}
        for m in measures:
            key = (table_ids.get(m['TableID']), m['Name'])
            if key in known:
                raise ModelError('Duplicate loaded measure binding')
            known[key] = m['Expression']
        expected = bim['model']['tables']
        expected_measures = {(t['name'], m['name']): m['expression'] for t in expected for m in t.get('measures', [])}
        if not expected_measures or any(t['name'] not in table_ids.values() for t in expected):
            raise ModelError('Selected model is missing showcase tables; open and refresh the generated project')
        if any(k not in known or _tokens(known[k]) != _tokens(v) for k, v in expected_measures.items()):
            raise ModelError('Selected model has different measure definitions; select the generated showcase')
        result.data['binding_check'] = {'status': 'passed', 'checked_measures': len(expected_measures),
                                        'scope': result.data['limitations'][0]}
        update('Checking refreshed physical row counts')
        # Obtain expected counts from CSVs embedded in the fingerprinted model.
        import base64
        import gzip
        import io
        import re
        physical = []
        for t in expected:
            parts = t.get('partitions', [])
            if len(parts) != 1:
                raise ModelError('Expected one showcase partition per table')
            source = parts[0]['source']
            if source['type'] == 'calculated':
                continue
            encoded = re.findall(r'Binary.FromText\("([A-Za-z0-9+/=]+)"', source['expression'])
            if len(encoded) != 1:
                raise ModelError('Expected one embedded synthetic CSV per physical table')
            raw = gzip.decompress(base64.b64decode(encoded[0]))
            count = sum(1 for _ in csv.reader(io.StringIO(raw.decode('utf-8-sig')))) - 1
            physical.append((t['name'], count))
        fields = []
        for i, (table, _) in enumerate(physical):
            fields += [f'"Physical_{i}"', 'COUNTROWS(' + Entity('table', table, '').qualified_name + ')']
        fields += ['"SummaryRows"', 'COUNTROWS(' + Entity('table', manifest['summary_table'], '').qualified_name + ')']
        counts_query = 'EVALUATE ROW(' + ', '.join(fields) + ')\n'
        counts = _rows(execute('row-counts', counts_query))
        if len(counts) != 1:
            raise ModelError('Unexpected engine row-count result')
        result.data['row_counts'] = {'physical': [], 'summary_rows': int(counts[0]['SummaryRows'])}
        for i, (table, count) in enumerate(physical):
            actual = int(counts[0][f'Physical_{i}'])
            result.data['row_counts']['physical'].append({'table': table, 'expected': count, 'actual': actual})
            if actual != count:
                raise ModelError('Physical row counts differ from embedded mock data; refresh the generated model')
        if result.data['row_counts']['summary_rows'] <= 0:
            raise ModelError('Summary is empty; refresh the generated model')
        pairs = len(manifest['changes'])
        meaningful = [0] * pairs
        for n, case in enumerate(manifest['cases']):
            update(f'Validating context {n+1} of {len(manifest["cases"])}')
            query = _inside(root, case['queries']['validation']['file']).read_text(encoding='utf-8-sig')
            validation = _validate(_rows(execute(f'context-{n}-validation', query)), pairs, bool(case['group_by']))
            result.data['cases'].append({'scenario': case['scenario'], 'group_by': case['group_by'],
                                         'validation': validation, 'timings': {}, 'performance_status': 'not_run'})
            meaningful = [a+b for a, b in zip(meaningful, validation['nonblank_values_by_measure'])]
        result.data['nonblank_values_by_measure'] = meaningful
        passed = all(c['validation']['status'] == 'passed' for c in result.data['cases'])
        result.data['validation_status'] = 'passed_tested_cases' if passed else 'failed'
        if not passed or not all(meaningful):
            result.data['status'] = 'validation_failed' if not passed else 'insufficient_nonblank_data'
            result.data['performance_status'] = 'withheld'
            for case in result.data['cases']:
                case['performance_status'] = 'withheld'
        else:
            for n, case in enumerate(manifest['cases']):
                current = result.data['cases'][n]
                for kind in (('baseline', 'candidate') if n % 2 == 0 else ('candidate', 'baseline')):
                    update(f'Timing {kind}, context {n+1} of {len(manifest["cases"])}')
                    query = _inside(root, case['queries'][kind]['file']).read_text(encoding='utf-8-sig')
                    output = execute(f'context-{n}-{kind}', query, timing=True)
                    current['timings'][kind] = _timing(output, warm, current['validation']['timing_rows'])
                current['performance_status'] = _verdict(current['timings']['baseline'], current['timings']['candidate'])
            verdicts = {c['performance_status'] for c in result.data['cases']}
            result.data['status'] = 'completed'
            result.data['performance_status'] = next(iter(verdicts)) if len(verdicts) == 1 else 'inconclusive'
            update('Rechecking loaded definitions and row counts')
            final_tables = _rows(execute('loaded-tables-final', 'EVALUATE INFO.TABLES()\n'))
            final_ids = {r['ID']: r['Name'] for r in final_tables}
            final_measures = _rows(execute('loaded-measures-final', 'EVALUATE INFO.MEASURES()\n'))
            final_bindings = {(final_ids.get(r['TableID']), r['Name']): r['Expression'] for r in final_measures}
            if any(k not in final_bindings or _tokens(final_bindings[k]) != _tokens(v) for k,v in expected_measures.items()):
                raise ModelError('Loaded measure definitions changed during the run')
            if _rows(execute('row-counts-final', counts_query)) != counts:
                raise ModelError('Loaded row counts changed during the run')
        # Detect concurrent edits rather than blessing a different project.
        if any(_digest(_inside(root, name)) != value for name, value in fingerprints.items()):
            raise ModelError('Showcase files changed during the run; results are invalid')
    except Exception as exc:
        result.data.update(status='failed', performance_status='withheld', error=str(exc))
        for case in result.data['cases']:
            case['performance_status'] = 'withheld'
    update('Finished' if result.data['status'] == 'completed' else 'Stopped — inspect the result and next steps')
    return result


def render_showcase_results(data):
    from .report import _CSS
    esc = lambda value: html.escape(str(value), quote=True)
    labels = {'observed_faster': 'Observed faster', 'observed_slower': 'Observed slower',
              'inconclusive': 'Inconclusive', 'withheld': 'Timings withheld', 'not_run': 'Not run'}
    table, details = [], []
    for case in sorted(data['cases'], key=lambda c: -c['timings'].get('baseline', {}).get('median_ms', 0)):
        def ms(kind):
            value = case['timings'].get(kind, {}).get('median_ms')
            return '—' if value is None else f'{value:.3f}'
        table.append(f'<tr><td>{esc(case["scenario"])}</td><td>{esc(case["validation"]["status"])}</td><td>{ms("baseline")}</td><td>{ms("candidate")}</td><td>{esc(labels[case["performance_status"]])}</td></tr>')
        details.append('<details><summary>' + esc(case['scenario']) + '</summary><pre>' + esc(json.dumps(case, indent=2)) + '</pre></details>')
    error = '<p class="warning">' + esc(data['error']) + '</p>' if data.get('error') else ''
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Showcase validation</title><style>{_CSS}</style><main>
<h1>{esc(labels[data['performance_status']])}</h1><p>Run: {esc(data['status'])} · Correctness: {esc(data['validation_status'])}</p>{error}
<p>One synthetic matrix, separate baseline/candidate queries. Largest measured baseline medians first. Client elapsed milliseconds; CPU/FE/SE unknown.</p>
<div class="scroll"><table><thead><tr><th>Context</th><th>Validation</th><th>Baseline median ms</th><th>Candidate median ms</th><th>Observation</th></tr></thead><tbody>{''.join(table)}</tbody></table></div>
<details><summary>Refreshed row counts and binding checks</summary><pre>{esc(json.dumps({k:data.get(k) for k in ['row_counts','binding_check','nonblank_values_by_measure']},indent=2))}</pre></details>
<h2>Context details</h2>{''.join(details)}<h2>Scope and interpretation</h2><ul>{''.join('<li>'+esc(w)+'</li>' for w in data['limitations'])}</ul>
<p><a href="results.json">Full evidence and query/output fingerprints</a></p></main></html>'''
