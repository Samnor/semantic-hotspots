"""Fictional telemetry tests; fabricated test values are never benchmark evidence."""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from semantic_hotspots import compare_compute, normalize_capacity_events, ModelError
from semantic_hotspots.compute import query_hash, tag_compute_query
from semantic_hotspots.cli import main


def fixture():
    scenario = {'id': 'matrix', 'label': '<Matrix> expansion', 'matched_query_scope': True,
                'queries': {'baseline': 'DEFINE MEASURE Metrics[Trial] = 1\nEVALUATE ROW("Value", [Trial])',
                            'candidate': 'DEFINE MEASURE Metrics[Trial] = 1+0\nEVALUATE ROW("Value", [Trial])'},
                'validation': {'rows': [{'Equal': 1, 'OriginalBlank': False, 'CandidateBlank': False}]}}
    capture = {'kind': 'compute-comparison-capture', 'schema_version': '1.0', 'completed': True,
               'repetitions': 5,
               'environment': {'capacity_id': 'capacity', 'dataset_id': 'model',
                               'model_sha256': 'fictional-model-fingerprint', 'cache_policy': 'excluded prime then warm'},
               'scenarios': [scenario], 'samples': []}
    semantic, capacity = [], []
    for rep in range(6):
        for variant in ['baseline', 'candidate']:
            sid = f'{variant}-{rep}'
            tag = 'semantic-hotspots-compute:'+sid
            text = tag_compute_query(scenario['queries'][variant], tag)
            capture['samples'].append({'id': sid, 'scenario_id': 'matrix', 'repeat': rep,
                'variant': variant, 'warmup': rep == 0, 'status': 'success', 'client_elapsed_ms': 500,
                'queries': [{'text': text, 'sha256': query_hash(text), 'tag': tag}]})
            for op, content in [('QueryBegin', text), ('QueryEnd', ''),
                                ('ExecutionMetrics', json.dumps({'totalCpuTimeMs': 100 if variant == 'baseline' else 50,
                                                                'durationMs': 200}))]:
                semantic.append({'ItemId': 'model', 'OperationId': sid, 'OperationName': op, 'EventText': content})
            capacity.append({'EventId': sid, 'CapacityId': 'capacity', 'ItemId': 'model', 'OperationId': sid,
                             'OperationName': 'Query', 'CUSeconds': 2 if variant == 'baseline' else 1,
                             'Utilization': 'Interactive', 'Status': 'Success', 'ThrottlingMs': 0})
    return capture, semantic, capacity


class ComputeTests(unittest.TestCase):
    def result(self, a, b, c=None):
        return compare_compute(a, b, c).data['scenarios'][0]['metrics']

    def test_less_cu_and_cpu_with_unchanged_latency(self):
        a, b, c = fixture(); m = self.result(a, b, c)
        self.assertEqual(m['cu_seconds']['status'], 'observed_reduction')
        self.assertEqual(m['cu_seconds']['reduction_percent'], 50)
        self.assertEqual(m['cpu_ms']['status'], 'observed_reduction')
        self.assertEqual(m['client_elapsed_ms']['status'], 'inconclusive')
        self.assertEqual(m['cu_seconds']['baseline_total'], 10)
        self.assertEqual(len(compare_compute(a, b, c).data['excluded_warmup_samples']), 2)

    def test_cpu_does_not_estimate_or_stand_in_for_capacity(self):
        a, b, _ = fixture(); m = self.result(a, b)
        self.assertEqual(m['cpu_ms']['status'], 'observed_reduction')
        self.assertEqual(m['cu_seconds']['status'], 'unknown_missing_evidence')
        self.assertIsNone(m['cu_seconds']['candidate_total'])

    def test_partial_costs_and_wholly_absent_pairs_do_not_make_a_saving(self):
        a, b, c = fixture(); m = self.result(a, b, c[:-1])
        self.assertFalse(m['cu_seconds']['complete'])
        self.assertIsNone(m['cu_seconds']['reduction_percent'])
        self.assertIsNone(m['cu_seconds']['baseline_total'])
        a['samples'] = [s for s in a['samples'] if s['repeat'] != 5]
        self.assertEqual(self.result(a, b, c)['cu_seconds']['status'], 'withheld_validation_or_capture_failed')

    def test_deduplicate_operations_not_just_events_and_reject_conflicts(self):
        a, b, c = fixture(); c.append(dict(c[-1], EventId='reexport'))
        self.assertEqual(self.result(a, b, c)['cu_seconds']['candidate_total'], 5)
        c[-1]['CUSeconds'] = 7
        with self.assertRaises(ModelError): self.result(a, b, c)

    def test_failure_throttle_missing_end_and_ambiguous_query_block_claims(self):
        for mode in ['failure', 'throttle', 'missing-end', 'ambiguous', 'wrong-scope', 'wrong-value']:
            a, b, c = fixture()
            if mode == 'failure': c[-1]['Status'] = 'Failed'
            if mode == 'throttle': c[-1]['ThrottlingMs'] = 5
            if mode == 'missing-end': b = [r for r in b if not (r['OperationId'] == 'candidate-5' and r['OperationName'] == 'QueryEnd')]
            if mode == 'ambiguous':
                row = next(r for r in b if r['OperationId'] == 'candidate-5' and r['OperationName'] == 'QueryBegin')
                b.append(dict(row, OperationId='other-operation'))
            if mode == 'wrong-scope': a['scenarios'][0]['matched_query_scope'] = False
            if mode == 'wrong-value': a['scenarios'][0]['validation']['rows'][0]['Equal'] = 0
            with self.subTest(mode=mode):
                self.assertEqual(self.result(a, b, c)['cu_seconds']['status'], 'withheld_validation_or_capture_failed')

    def test_wrong_capacity_and_wrong_dataset_are_not_joined(self):
        a, b, c = fixture()
        for r in c:r['CapacityId'] = 'another-capacity'
        self.assertEqual(self.result(a, b, c)['cu_seconds']['status'], 'unknown_missing_evidence')
        for r in b:r['ItemId'] = 'another-model'
        self.assertEqual(self.result(a, b, c)['cpu_ms']['status'], 'withheld_validation_or_capture_failed')

    def test_known_zero_is_preserved_but_missing_is_not_zero(self):
        a, b, c = fixture()
        for r in c:
            if r['OperationId'].startswith('candidate'):r['CUSeconds'] = 0
        m = self.result(a, b, c)['cu_seconds']
        self.assertEqual(m['candidate_median'], 0)
        self.assertEqual(m['reduction_percent'], 100)
        c[-1]['CUSeconds'] = None
        self.assertIsNone(self.result(a, b, c)['cu_seconds']['reduction_percent'])

    def test_query_hash_template_and_capture_tags_are_checked(self):
        for mode in ['hash', 'template', 'duplicate', 'repeat']:
            a, b, c = fixture()
            if mode == 'hash': a['samples'][-1]['queries'][0]['text'] += ' '
            if mode == 'template': a['scenarios'][0]['queries']['candidate'] += ' '
            if mode == 'duplicate': a['samples'].append(copy.deepcopy(a['samples'][-1]))
            if mode == 'repeat': a['samples'][-1]['repeat'] = 20
            with self.subTest(mode=mode), self.assertRaises(ModelError): self.result(a, b, c)

    def test_multiple_queries_per_interaction_are_summed_once(self):
        a, b, c = fixture()
        # Reuse no operation: an interaction may issue several distinct queries.
        for sample in a['samples']:
            q = sample['queries'][0]; tag = q['tag']+'-extra'
            text = tag_compute_query(a['scenarios'][0]['queries'][sample['variant']], tag)
            sample['queries'].append({'text': text, 'tag': tag, 'sha256': query_hash(text)})
            sid = sample['id']; extra = sid+'-extra'
            for row in list(b):
                if row['OperationId'] == sid:b.append(dict(row, OperationId=extra, EventText=text if row['OperationName']=='QueryBegin' else row['EventText']))
            row = next(r for r in c if r['OperationId'] == sid);c.append(dict(row, EventId=extra, OperationId=extra))
        self.assertEqual(self.result(a, b, c)['cu_seconds']['baseline_total'], 20)

    def test_native_cloud_event_milliseconds_and_deduplication(self):
        e = {'id': 'event', 'type': 'Microsoft.Fabric.CapacityOperationEvents.Operation',
             'data': {'capacityId': 'capacity', 'itemId': 'model', 'operationId': 'query',
                      'capacityUnitMs': 1250, 'operationName': 'Query', 'utilizationType': 'Interactive',
                      'throttlingDelayMs': 0, 'status': 'Success'}}
        rows = normalize_capacity_events([{'Data': json.dumps(e)}, {'Data': e}, {'type': 'Summary'}])
        self.assertEqual(len(rows), 1); self.assertEqual(rows[0]['CUSeconds'], 1.25)
        e['data']['capacityUnitMs'] = -1
        with self.assertRaises(ModelError):normalize_capacity_events([e])

    def test_monitoring_whitespace_and_comments_preserve_tag_and_strings(self):
        a, b, c = fixture()
        for row in b:
            if row['OperationName'] == 'QueryBegin':
                row['EventText'] = row['EventText'].replace('\n', ' ') + ' /* monitoring normalization */'
        self.assertEqual(self.result(a, b, c)['cu_seconds']['status'], 'observed_reduction')
        row = next(r for r in b if r['OperationName'] == 'QueryBegin' and r['OperationId'] == 'candidate-5')
        row['EventText'] = row['EventText'].replace('candidate-5', 'another-tag')
        self.assertEqual(self.result(a, b, c)['cu_seconds']['status'], 'withheld_validation_or_capture_failed')

    def test_malformed_metrics_conflicts_and_all_blank_are_not_wins(self):
        for value in [True, -1, float('nan'), 'bad']:
            a, b, c = fixture(); c[-1]['CUSeconds'] = value
            with self.subTest(value=value), self.assertRaises(ModelError): self.result(a, b, c)
        a, b, c = fixture()
        b.append({'ItemId': 'model', 'OperationId': 'candidate-5', 'OperationName': 'ExecutionMetrics',
                  'EventText': '{"totalCpuTimeMs": 8, "durationMs": 500}'})
        self.assertEqual(self.result(a, b, c)['cu_seconds']['status'], 'withheld_validation_or_capture_failed')
        a, b, c = fixture()
        a['scenarios'][0]['validation']['rows'] = [{'Equal': 1, 'OriginalBlank': True, 'CandidateBlank': True}]
        self.assertEqual(self.result(a, b, c)['cu_seconds']['status'], 'uninformative_all_blank')
        self.assertEqual(self.result(a, b, c)['cu_seconds']['candidate_total'], 5)

    def test_journey_sums_queries_across_scenarios_and_withholds_partial_totals(self):
        a, b, c = fixture()
        scenario = copy.deepcopy(a['scenarios'][0]); scenario['id'] = 'second'
        a['scenarios'].append(scenario)
        for sample in list(a['samples']):
            other = copy.deepcopy(sample); other['id'] += '-second'; other['scenario_id'] = 'second'
            q = other['queries'][0]; q['tag'] += '-second'
            q['text'] = tag_compute_query(scenario['queries'][other['variant']], q['tag']); q['sha256'] = query_hash(q['text'])
            a['samples'].append(other)
            for row in list(b):
                if row['OperationId'] == sample['id']:
                    b.append(dict(row, OperationId=other['id'], EventText=q['text'] if row['OperationName']=='QueryBegin' else row['EventText']))
            row = next(r for r in c if r['OperationId'] == sample['id'])
            c.append(dict(row, EventId=other['id'], OperationId=other['id']))
        result = compare_compute(a, b, c).data['journey']
        self.assertEqual(result['interactions_per_replay'], 2)
        self.assertEqual(result['metrics']['cu_seconds']['baseline_median'], 4)
        self.assertEqual(result['metrics']['cu_seconds']['candidate_median'], 2)
        c.pop()
        self.assertIsNone(compare_compute(a, b, c).data['journey']['metrics']['cu_seconds']['baseline_total'])

    def test_cli_and_escaped_html_report(self):
        a, b, c = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, data in [('capture', a), ('semantic', b), ('capacity', c)]:
                (root/(name+'.json')).write_text(json.dumps(data))
            with redirect_stdout(io.StringIO()):
                status = main(['compute-compare', str(root/'capture.json'), '--semantic-logs', str(root/'semantic.json'),
                               '--capacity-logs', str(root/'capacity.json'), '-o', str(root/'report')])
            self.assertEqual(status, 0)
            page = (root/'report/compute.html').read_text()
            self.assertIn('&lt;Matrix&gt;', page); self.assertNotIn('<Matrix>', page)
            self.assertIn('Interactive capacity first.', page)
            self.assertEqual(json.loads((root/'report/compute-results.json').read_text())['objective'], 'interactive_cu_seconds')


if __name__ == '__main__': unittest.main()
