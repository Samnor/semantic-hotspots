"""Compare recorded interactive compute, without converting latency or CPU to CUs.

Captures and telemetry are caller-supplied evidence, not attested engine execution.
Exact query tokens + operation identities join samples to native consumption records.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .dax import tokenize
from .model import ModelError
from .profile import _number as _profile_number, read_rows


def _number(value):
    if isinstance(value, bool):
        raise ModelError('Compute metrics must be numbers, not booleans')
    try:
        return _profile_number(value)
    except (TypeError, ValueError) as exc:
        raise ModelError('Invalid compute metric') from exc


def query_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()



def tag_compute_query(template, tag):
    """Use an unused DEFINE variable: Fabric monitoring removes DAX comments.

    The same variable name and tagging shape are used on both alternatives.
    It is capture metadata, never a model edit or a result expression.
    """
    if not isinstance(tag, str) or not re.fullmatch(r'[A-Za-z0-9:_-]{1,160}', tag):
        raise ModelError('Capture tags must be 1–160 ASCII identifier characters')
    tokens = tokenize(template)
    if any(t.kind == 'NAME' and t.value.casefold() == '__sh_compute_tag' for t in tokens):
        raise ModelError('Capture variable collides with a query identifier')
    first = tokens[0] if tokens else None
    declaration = 'DEFINE VAR __sh_compute_tag = "'+tag+'"\n'
    return declaration+template[first.end:] if first and first.kind == 'NAME' and first.value.upper() == 'DEFINE' else declaration+template


def _query_key(text):
    # Workspace monitoring removes comments and replaces newlines by spaces.
    # Preserve every token value, including strings and the in-query tag.
    return tuple((t.kind, t.value) for t in tokenize(text))


def _object(source):
    if isinstance(source, (str, Path)):
        with Path(source).open(encoding='utf-8-sig') as stream:
            source = json.load(stream)
    if not isinstance(source, dict):
        raise ModelError('Compute capture must be a JSON object')
    return source


def normalize_capacity_events(source):
    """Read native CloudEvents (or KQL Data envelopes); ignore summary events.

    CU seconds = the documented capacityUnitMs / 1000, never CPU or duration.
    No tenant identifiers, credentials or endpoints are bundled in this module.
    """
    output, seen = [], {}
    for row in read_rows(source):
        event = row.get('Data', row)
        if isinstance(event, str):
            event = json.loads(event)
        if not isinstance(event, dict):
            raise ModelError('Capacity events must be JSON objects')
        if event.get('type') != 'Microsoft.Fabric.CapacityOperationEvents.Operation':
            continue
        data = event.get('data')
        if not isinstance(data, dict):
            raise ModelError('Capacity operation data must be a JSON object')
        if not event.get('id') or not data.get('capacityId') or not data.get('itemId'):
            raise ModelError('Operation events require event, capacity and item IDs')
        amount = _number(data.get('capacityUnitMs'))
        normalized = {'EventId': event['id'], 'CapacityId': data['capacityId'],
                      'ItemId': data['itemId'], 'OperationId': data.get('operationId'),
                      'OperationName': data.get('operationName'),
                      'CUSeconds': amount / 1000 if amount is not None else None,
                      'Utilization': data.get('utilizationType'), 'Status': data.get('status'),
                      'ThrottlingMs': _number(data.get('throttlingDelayMs')),
                      'Started': data.get('operationStartTime')}
        if event['id'] in seen:
            if seen[event['id']] != normalized:
                raise ModelError('Conflicting duplicate capacity event')
            continue
        seen[event['id']] = normalized
        output.append(normalized)
    return output


def _capacity_rows(source):
    rows = read_rows(source)
    if any('Data' in r or 'type' in r for r in rows):
        return normalize_capacity_events(rows)
    return rows


def _identity(row):
    return str(row.get('ItemId', '')).casefold(), str(row.get('OperationId', '')).casefold()


def _validation(scenario):
    evidence = scenario.get('validation', {})
    rows = evidence.get('rows', [])
    if not isinstance(rows, list) or not rows:
        return {'status': 'not_run', 'rows': 0, 'nonblank_rows': 0}
    count = nonblank = 0
    for row in rows:
        # Explicit strict engine comparison and separate BLANK flags are required.
        flags = [row.get('['+k+']', row.get(k)) for k in ['Equal', 'OriginalBlank', 'CandidateBlank']]
        if (type(flags[0]) is not int or flags[0] != 1 or type(flags[1]) is not bool
                or type(flags[2]) is not bool or flags[1] != flags[2]):
            return {'status': 'failed', 'rows': len(rows), 'nonblank_rows': nonblank}
        count += 1
        nonblank += not flags[1]
    return {'status': 'passed_tested_rows', 'rows': count, 'nonblank_rows': nonblank}


def _comparison(pairs, field, *, valid, min_pairs):
    observed = [(p['baseline'].get(field), p['candidate'].get(field)) for p in pairs]
    complete = bool(observed) and all(a is not None and b is not None for a, b in observed)
    left = [a for a, b in observed if a is not None]
    right = [b for a, b in observed if b is not None]
    a, b = (statistics.median(v) if v else None for v in [left, right])
    status = 'unknown_missing_evidence'
    if not valid:
        status = 'withheld_validation_or_capture_failed'
    elif complete:
        status = 'inconclusive'
        if len(pairs) >= min_pairs:
            if a and b < a * .9 and max(right) < min(left):
                status = 'observed_reduction'
            elif a is not None and b > a * 1.1 and min(right) > max(left):
                status = 'observed_increase'
    return {'status': status, 'complete': complete, 'paired_repetitions': len(pairs),
            'baseline_recorded_samples': len(left), 'candidate_recorded_samples': len(right),
            'baseline_median': a, 'candidate_median': b,
            'baseline_total': sum(left) if complete else None,
            'candidate_total': sum(right) if complete else None,
            'reduction_percent': (a-b)/a*100 if complete and a else None,
            'baseline_range': [min(left), max(left)] if left else None,
            'candidate_range': [min(right), max(right)] if right else None}


@dataclass
class ComputeComparison:
    data: dict

    def to_dict(self):
        return self.data

    def write(self, directory):
        from .report import _CSS, _number
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        (root/'compute-results.json').write_text(json.dumps(self.data, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
        esc = lambda value: html.escape(str(value), quote=True)
        labels = {'observed_reduction': 'Observed reduction', 'observed_increase': 'Observed increase',
                  'inconclusive': 'Inconclusive', 'unknown_missing_evidence': 'Missing telemetry',
                  'withheld_validation_or_capture_failed': 'Comparison withheld',
                  'uninformative_all_blank': 'All values BLANK',
                  'passed_tested_rows': 'Passed for tested rows', 'failed': 'Failed', 'not_run': 'Not checked'}
        label = lambda value: esc(labels.get(value, value))
        metric_labels = {'cu_seconds': 'Native interactive CU (seconds)', 'cpu_ms': 'Total query CPU (ms)',
                         'engine_duration_ms': 'Engine duration (ms)', 'client_elapsed_ms': 'Client elapsed (ms)'}
        percent = lambda value: 'Unknown' if value is None else f'{value:.1f}%'
        ranking, details = [], []
        for i, case in enumerate(self.data['scenarios'], 1):
            metrics = case['metrics']
            cu = metrics['cu_seconds']
            ranking.append(f'<tr><td><a href="#case-{i}">{esc(case["label"])}</a></td><td>{_number(cu["baseline_median"], "CU-s")}</td><td>{_number(cu["candidate_median"], "CU-s")}</td><td>{percent(cu["reduction_percent"])}</td><td>{label(cu["status"])}</td></tr>')
            rows = ''.join(f'<tr><td>{esc(metric_labels[field])}</td><td>{_number(m["baseline_median"])}</td><td>{_number(m["candidate_median"])}</td><td>{label(m["status"])}</td><td>{m["baseline_recorded_samples"]}/{case["expected_pairs"]} → {m["candidate_recorded_samples"]}/{case["expected_pairs"]}</td></tr>' for field, m in metrics.items())
            reasons = ''.join('<li>'+esc(v)+'</li>' for v in case['blockers'])
            details.append(f'<details id="case-{i}"><summary>{esc(case["label"])}</summary><p>Correctness: {label(case["validation"]["status"])} · {case["validation"]["rows"]} checked rows · {case["validation"]["nonblank_rows"]} non-BLANK.</p><ul>{reasons}</ul><table><thead><tr><th>Metric</th><th>Original median</th><th>Candidate median</th><th>Observation</th><th>Coverage</th></tr></thead><tbody>{rows}</tbody></table><details><summary>Paired operations, missing evidence and query fingerprints</summary><pre>{esc(json.dumps(case["pairs"], indent=2))}</pre></details></details>')
        journey = self.data['journey']
        jcu = journey['metrics']['cu_seconds']
        journey_text = ('<section><h2>Complete replay</h2><p>'+str(journey['interactions_per_replay'])+' predeclared interactions per replay · '+str(journey['expected_pairs'])+' measured pairs.</p><p>Original median: <b>'+_number(jcu['baseline_median'], 'CU-s')+'</b> · Candidate: <b>'+_number(jcu['candidate_median'], 'CU-s')+'</b> · Reduction: '+percent(jcu['reduction_percent'])+' · '+label(jcu['status'])+'</p><p>Includes only the queries listed in this capture; this is not an estimate of a production session.</p></section>')
        warnings = ''.join('<li>'+esc(v)+'</li>' for v in self.data['limitations'])
        page = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Interactive compute comparison</title><style>{_CSS}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}</style></head><body><main><header><div class="eyebrow">SEMANTIC HOTSPOTS / COMPUTE EVIDENCE</div><h1>Interactive capacity first.</h1><p>Native CU-seconds are the primary outcome. CPU and elapsed time are separate observations.</p></header>{journey_text}<section><h2>Highest recorded original CU consumption first</h2><p>Median CU-seconds per completed interaction. Positive reduction means less recorded candidate consumption. Unknown evidence is never zero.</p><div class="scroll"><table><thead><tr><th>Measure / interaction</th><th>Original</th><th>Candidate</th><th>Reduction</th><th>Capacity evidence</th></tr></thead><tbody>{''.join(ranking)}</tbody></table></div></section><section><h2>Inspect an interaction</h2>{''.join(details)}</section><section><details><summary>Method and limits</summary><ul>{warnings}</ul><pre>{esc(json.dumps(self.data['environment'], indent=2))}</pre></details><p><a href="compute-results.json">Download evidence JSON</a></p></section></main><script>function reveal(){{const d=document.getElementById(location.hash.slice(1));if(d)d.open=true}}addEventListener('hashchange',reveal);reveal();</script></body></html>'''
        (root/'compute.html').write_text(page, encoding='utf-8')
        return {'json': root/'compute-results.json', 'html': root/'compute.html'}


def _compare_compute(capture, semantic_logs, capacity_logs=None, *, min_pairs=5):
    """Join tagged captures to exact semantic query tokens and native operation CU.

    Each sample is one completed interaction, containing one or more queries.
    Warm-up samples are retained but excluded. All expected pairs must have
    matching values, scope and telemetry before a reduction can be reported.
    """
    capture = _object(capture)
    if capture.get('kind') != 'compute-comparison-capture' or capture.get('schema_version') != '1.0':
        raise ModelError('Expected compute-comparison-capture schema 1.0')
    if type(min_pairs) is not int or min_pairs < 5:
        raise ModelError('At least five matched pairs are required')
    env = capture.get('environment', {})
    if not all(env.get(k) for k in ['capacity_id', 'dataset_id', 'model_sha256', 'cache_policy']):
        raise ModelError('Capture requires capacity, dataset, model fingerprint and cache policy')
    repetitions = capture.get('repetitions')
    if type(repetitions) is not int or repetitions < 1:
        raise ModelError('Capture must declare the expected measured repetitions')
    scenario_list, samples = capture.get('scenarios', []), capture.get('samples', [])
    if not scenario_list or not samples:
        raise ModelError('Capture requires scenarios and samples')
    scenarios = {s['id']: s for s in scenario_list}
    if len(scenarios) != len(scenario_list):
        raise ModelError('Duplicate scenario identity')
    dataset = str(env['dataset_id']).casefold()
    capacity = str(env['capacity_id']).casefold()
    semantic, begins = defaultdict(list), defaultdict(set)
    for row in read_rows(semantic_logs):
        if _identity(row)[0] != dataset or not _identity(row)[1]:
            continue
        identity = _identity(row)
        semantic[identity].append(row)
        if row.get('OperationName') == 'QueryBegin':
            try:
                begins[_query_key(str(row.get('EventText', '')))].add(identity)
            except ModelError:
                continue
    native, event_ids = {}, {}
    for row in _capacity_rows(capacity_logs) if capacity_logs is not None else []:
        identity = _identity(row)
        if identity[0] != dataset or row.get('OperationName') != 'Query':
            continue
        if not row.get('EventId') or not identity[1] or not row.get('CapacityId'):
            raise ModelError('Compute comparisons require native event, capacity, item and operation IDs')
        if str(row['CapacityId']).casefold() != capacity:
            continue
        values = (_number(row.get('CUSeconds')), str(row.get('Utilization', '')).casefold(),
                  str(row.get('Status', '')).casefold(), _number(row.get('ThrottlingMs')))
        eid = str(row['EventId']).casefold()
        fingerprint = (identity, values)
        if eid in event_ids and event_ids[eid] != fingerprint:
            raise ModelError('Conflicting duplicate capacity EventId')
        event_ids[eid] = fingerprint
        if identity in native and native[identity] != values:
            raise ModelError('Conflicting capacity totals for the same operation; reconcile before comparing')
        native[identity] = values
    used_samples, used_queries, used_operations, cells = set(), set(), set(), {}
    captured = []
    for sample in samples:
        sid = sample['id']
        key = sample['scenario_id'], sample['repeat'], sample['variant']
        if (sid in used_samples or key in cells or key[0] not in scenarios or key[2] not in {'baseline', 'candidate'}
                or type(key[1]) is not int or not 0 <= key[1] <= repetitions
                or type(sample.get('warmup')) is not bool or sample['warmup'] != (key[1] == 0)):
            raise ModelError('Invalid or duplicate compute sample')
        used_samples.add(sid)
        totals = {'cu_seconds': [], 'cpu_ms': [], 'engine_duration_ms': []}
        issues, operations = [], []
        if sample.get('status') != 'success':
            issues.append('Captured interaction did not complete successfully')
        queries = sample.get('queries', [])
        if not queries:
            issues.append('No captured queries')
        for query in queries:
            text, digest, tag = query['text'], query['sha256'], query['tag']
            if query_hash(text) != digest or tag in used_queries:
                raise ModelError('Changed query fingerprint, duplicate tag or missing leading capture tag')
            used_queries.add(tag)
            template = scenarios[key[0]]['queries'][key[2]]
            if text != tag_compute_query(template, tag):
                raise ModelError('Captured query differs from its scenario and variant template')
            identities = begins.get(_query_key(text), set())
            if len(identities) != 1:
                issues.append('Exact token-matched tagged QueryBegin missing or ambiguous: '+tag)
                continue
            identity = next(iter(identities))
            if identity in used_operations:
                raise ModelError('An operation cannot be attributed to multiple captured queries')
            used_operations.add(identity)
            trace = semantic[identity]
            operations.append({'operation_id': identity[1], 'query_sha256': digest})
            if (not any(r.get('OperationName') == 'QueryEnd' for r in trace)
                    or any(r.get('OperationName') == 'Error' for r in trace)
                    or any(r.get('OperationName') == 'QueryEnd' and str(r.get('Status', '')).casefold() not in {'', 'success', 'succeeded'} for r in trace)):
                issues.append('QueryEnd absent or engine error present: '+identity[1])
            metrics = []
            for row in trace:
                if row.get('OperationName') != 'ExecutionMetrics':
                    continue
                obj = row.get('EventText') or {}
                try:
                    obj = json.loads(obj) if isinstance(obj, str) else obj
                    if isinstance(obj, dict) and 'discoverType' not in obj:
                        metrics.append((_number(obj.get('totalCpuTimeMs')), _number(obj.get('durationMs'))))
                except (ValueError, TypeError):
                    issues.append('Malformed ExecutionMetrics: '+identity[1])
            if len(set(metrics)) == 1:
                cpu, duration = metrics[0]
                if cpu is not None: totals['cpu_ms'].append(cpu)
                if duration is not None: totals['engine_duration_ms'].append(duration)
            elif len(set(metrics)) > 1:
                issues.append('Conflicting query metrics: '+identity[1])
            cost, utilization, status, throttle = native.get(identity, (None, '', '', None))
            if cost is not None and utilization == 'interactive':
                totals['cu_seconds'].append(cost)
            if identity in native and (utilization != 'interactive' or status not in {'success', 'succeeded'} or throttle is None or throttle > 0):
                issues.append('Noninteractive, failed, missing throttle evidence or throttled operation: '+identity[1])
        record = {'id': sid, 'repeat': key[1], 'variant': key[2], 'warmup': sample['warmup'],
                  'operation_ids': operations, 'issues': issues, 'capture_success': sample.get('status') == 'success'}
        for metric, values in totals.items():
            record[metric] = sum(values) if queries and len(values) == len(queries) else None
        record['client_elapsed_ms'] = _number(sample.get('client_elapsed_ms'))
        cells[key] = record
        captured.append(record)
    cases = []
    for scenario in scenario_list:
        sid = scenario['id']
        repeats = range(1, repetitions + 1)
        pairs = []
        validation = _validation(scenario)
        blockers = []
        if capture.get('completed') is not True: blockers.append('Capture has not completed')
        if validation['status'] != 'passed_tested_rows': blockers.append('Strict value/BLANK validation is missing or failed')
        if scenario.get('matched_query_scope') is not True: blockers.append('Matched query scope has not been confirmed')
        valid = not blockers
        for rep in repeats:
            left, right = cells.get((sid, rep, 'baseline')), cells.get((sid, rep, 'candidate'))
            if left is None or right is None or left['warmup'] or right['warmup']:
                valid = False
                blockers.append('Measured pair missing or mislabeled as warm-up: '+str(rep))
                left, right = left or {}, right or {}
            if left.get('issues') or right.get('issues') or not left.get('capture_success') or not right.get('capture_success'):
                valid = False
                blockers.append('Query capture or telemetry issues in pair: '+str(rep))
            pairs.append({'repeat': rep, 'baseline': left, 'candidate': right})
        metrics = {field: _comparison(pairs, field, valid=valid, min_pairs=min_pairs)
                   for field in ['cu_seconds', 'cpu_ms', 'engine_duration_ms', 'client_elapsed_ms']}
        if valid and validation['nonblank_rows'] == 0:
            for metric in metrics.values():
                metric['status'] = 'uninformative_all_blank'
        cases.append({'id': sid, 'label': scenario.get('label', sid), 'validation': validation,
                      'expected_pairs': len(repeats), 'pairs': pairs, 'evidence_valid': valid, 'blockers': blockers,
                      'metrics': metrics})
    cases.sort(key=lambda c: (c['metrics']['cu_seconds']['baseline_median'] is None,
                              -(c['metrics']['cu_seconds']['baseline_median'] or 0), c['id']))
    # A replay includes every predeclared interaction once per repetition.
    # Sum unique query costs, not inclusive per-measure profile totals.
    journey_pairs = []
    for rep in range(1, repetitions + 1):
        pair = {'repeat': rep}
        for variant in ['baseline', 'candidate']:
            rows = [next(p[variant] for p in c['pairs'] if p['repeat'] == rep) for c in cases]
            pair[variant] = {}
            for field in ['cu_seconds', 'cpu_ms', 'engine_duration_ms', 'client_elapsed_ms']:
                values = [r.get(field) for r in rows]
                pair[variant][field] = sum(values) if all(v is not None for v in values) else None
        journey_pairs.append(pair)
    journey_valid = all(c['evidence_valid'] for c in cases)
    journey_metrics = {field: _comparison(journey_pairs, field, valid=journey_valid, min_pairs=min_pairs)
                       for field in ['cu_seconds', 'cpu_ms', 'engine_duration_ms', 'client_elapsed_ms']}
    if journey_valid and not any(c['validation']['nonblank_rows'] for c in cases):
        for metric in journey_metrics.values(): metric['status'] = 'uninformative_all_blank'
    return ComputeComparison({'schema_version': '1.0', 'kind': 'compute-comparison-results',
        'objective': 'interactive_cu_seconds', 'environment': env, 'scenarios': cases,
        'journey': {'interactions_per_replay': len(cases), 'expected_pairs': repetitions,
                    'metrics': journey_metrics, 'pairs': journey_pairs},
        'excluded_warmup_samples': [r for r in captured if r['warmup']],
        'limitations': [
            'Native operation CU-seconds are primary. CPU, engine duration and client elapsed are separate metrics; no conversion between them is performed.',
            'Costs belong to entire captured queries/interactions, including helper chains; never add these inclusive costs as if each measure consumed them independently.',
            'Missing events/metrics remain unknown. Partial sums are withheld. Explicit recorded zeros are retained. Failed/throttled operations retain their cost but block reduction claims.',
            f'Descriptive reduction requires every matched sample, tested-row equality, matched query scope, at least {min_pairs} pairs, median reduction greater than 10%, and nonoverlapping ranges. This is not a statistical significance test.',
            'Only tagged query consumption is included. Model loading, refresh, materialization, other visuals, source workloads and monitoring overhead are not a net capacity saving and require separate accounting.',
            'Unique tag literals may change request/plan cache behavior relative to browser queries; both alternatives use the same tagging shape.',
            'Warm-up excluded; cache policy and query-scope equivalence are supplied capture metadata. Offline imports are not an attestation of model/data identity or complete browser interactions.',
            'A whole scenario omitted by a capture cannot be detected. Define representative opening, filtering, expanding and subtotal interactions before running the experiment.',
        ]})


def compare_compute(capture, semantic_logs, capacity_logs=None, *, min_pairs=5):
    """Compare native interactive CU and independent CPU/latency evidence.

    See README's compute-comparison capture schema. This reads local telemetry;
    it neither authenticates nor executes queries nor estimates capacity costs.
    """
    try:
        return _compare_compute(capture, semantic_logs, capacity_logs, min_pairs=min_pairs)
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelError('Malformed compute capture or telemetry: '+str(exc)) from exc
