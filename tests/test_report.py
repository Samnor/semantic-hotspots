"""Report behavior and attribution regressions, using only the standard library."""
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest

from semantic_hotspots import RuntimeProfile, analyze, parse_model


def fixture():
    model = parse_model({'name': 'Report fixture', 'model': {'tables': [{'name': 'T', 'measures': [
        {'name': 'CPU heavy', 'expression': '1'},
        {'name': 'CU heavy', 'expression': '2'},
        {'name': 'Shared child', 'expression': '3'},
        {'name': 'Unknown', 'expression': '4'},
    ]}]}})
    # Add a shared descendant so context totals must never be summed in the overview.
    model.measure('CPU heavy').expression = '[Shared child]'
    def query(op, measure, cpu, cu):
        return {'operation_id': op, 'dataset_id': 'fixture', 'started': '2025-01-01T00:00:00Z',
                'measure_ids': [model.measure(measure).id], 'query_end_seen': True,
                'execution_metrics_seen': cpu is not None, 'cpu_ms': cpu, 'duration_ms': 20,
                'recorded_cu_seconds': cu, 'direct_query_requests': 0, 'errors': []}
    return model, RuntimeProfile([
        query('cpu-query', 'CPU heavy', 100, 1),
        query('cu-query', 'CU heavy', 0, 5),
    ], [], 'fixture')


class ReportDocument(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.ids = set()
        self.duplicates = []
        self.hrefs = []
        self.details = {}
        self.stack = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            if attrs['id'] in self.ids:
                self.duplicates.append(attrs['id'])
            self.ids.add(attrs['id'])
        if tag == 'a':
            self.hrefs.append(attrs.get('href', ''))
        if tag == 'details':
            identity = attrs.get('id', '')
            if identity:
                self.details[identity] = (list(self.stack), 'open' in attrs)
            self.stack.append(identity)

    def handle_endtag(self, tag):
        if tag == 'details':
            self.stack.pop()


class ReportTests(unittest.TestCase):
    def render(self, analysis):
        with tempfile.TemporaryDirectory() as temp:
            return analysis.write(temp)['html'].read_text(encoding='utf-8')

    def test_auto_cost_ranking_prefers_native_cu_over_cpu(self):
        model, profile = fixture()
        result = analyze(model, profile=profile)
        self.assertEqual(result.rank_by, 'query_cu_context')
        self.assertEqual(result.ranked[0]['name'], 'CU heavy')
        self.assertEqual(result.ranked[-1]['name'], 'Unknown')
        overridden = analyze(model, profile=profile, rank_by='query_cpu_context')
        self.assertEqual(overridden.ranked[0]['runtime']['query_cpu_ms_context'], 100)
        self.assertEqual(overridden.ranked[-2]['name'], 'CU heavy')  # recorded zero before unknown

    def test_default_falls_back_to_cpu_then_risk(self):
        model, profile = fixture()
        for query in profile.queries:
            query['recorded_cu_seconds'] = None
        self.assertEqual(analyze(model, profile=profile).rank_by, 'query_cpu_context')
        for query in profile.queries:
            query['cpu_ms'] = None
        self.assertEqual(analyze(model, profile=profile).rank_by, 'risk')
        self.assertEqual(analyze(model).rank_by, 'risk')

    def test_unmatched_telemetry_does_not_choose_cost_ranking(self):
        model, profile = fixture()
        for query in profile.queries:
            query['measure_ids'] = []
        self.assertEqual(analyze(model, profile=profile).rank_by, 'risk')

    def test_summary_uses_unique_query_cost_not_shared_measure_totals(self):
        model, profile = fixture()
        report = self.render(analyze(model, profile=profile))
        self.assertIn('<strong>6 CU-s</strong>', report)
        self.assertIn('<strong>100 ms</strong>', report)
        self.assertIn('2 of 2 captured operations recorded · counted once', report)
        self.assertIn('These costs overlap across measures', report)

    def test_report_ranking_and_priority_cards_follow_cost_order(self):
        model, profile = fixture()
        report = self.render(analyze(model, profile=profile))
        priorities = report.split('<div class="priorities">', 1)[1].split('</section>', 1)[0]
        table = report.split('<tbody id="ranking-body">', 1)[1].split('</tbody>', 1)[0]
        for section in [priorities, table]:
            self.assertLess(section.index('CU heavy</a>'), section.index('CPU heavy</a>'))
        self.assertLess(table.index('CPU heavy</a>'), table.index('Unknown</a>'))
        self.assertIn('5 CU-s', priorities)

    def test_drill_down_targets_exist_and_are_initially_closed(self):
        model, profile = fixture()
        report = self.render(analyze(model, profile=profile))
        parsed = ReportDocument(report)
        self.assertFalse(parsed.duplicates)
        self.assertFalse(parsed.stack)
        for href in parsed.hrefs:
            if href.startswith('#'):
                self.assertIn(href[1:], parsed.ids)
        roots = [n for n in parsed.details if n.startswith('measure-') and len(n) == 28]
        self.assertEqual(len(roots), 4)
        for root in roots:
            self.assertFalse(parsed.details[root][1])
            for suffix in ['-dax', '-fixes', '-chain', '-score', '-runtime']:
                ancestors, opened = parsed.details[root + suffix]
                self.assertIn(root, ancestors)
                self.assertFalse(opened)
        query_targets = [n for n in parsed.details if n.startswith('query-') and n != 'query-evidence']
        self.assertEqual(len(query_targets), 2)
        self.assertTrue(all('query-evidence' in parsed.details[n][0] for n in query_targets))

    def test_partial_coverage_and_unknown_stay_visible(self):
        model, profile = fixture()
        profile.queries[1]['measure_ids'] = [model.measure('CPU heavy').id]
        profile.queries[1]['recorded_cu_seconds'] = None
        report = self.render(analyze(model, profile=profile))
        self.assertIn('1 of 2 contexts recorded · partial coverage', report)
        self.assertIn('1 of 2 captured operations recorded', report)
        self.assertIn('Unknown', report)
        self.assertIn('recorded subtotal, not an estimate of missing cost', report)

    def test_static_report_cannot_claim_measured_cost(self):
        model, _ = fixture()
        report = self.render(analyze(model))
        self.assertIn('Estimated review priority', report)
        self.assertIn('not predicted execution time', report)
        self.assertNotIn('Recorded cost, highest first', report)
        self.assertNotIn('id="query-evidence"', report)
        self.assertLess(report.index('id="overview"'), report.index('id="ranking"'))
        self.assertLess(report.index('id="ranking"'), report.index('id="details"'))

    def test_cycles_and_empty_models_render_without_recursion(self):
        model, _ = fixture()
        model.measure('Shared child').expression = '[CPU heavy]'
        result = analyze(model)
        self.assertIn('Dependency cycle witnesses', self.render(result))
        empty = parse_model({'name': 'Empty', 'model': {'tables': []}})
        self.assertIn('This model contains no measures', self.render(analyze(empty)))

    def test_names_paths_and_telemetry_are_escaped(self):
        model, profile = fixture()
        model.name = '<img src=x onerror=bad()>'
        profile.queries[0]['operation_id'] = '<script>op()</script>'
        profile.queries[0]['errors'] = ['</p><script>bad()</script>']
        report = self.render(analyze(model, profile=profile))
        self.assertNotIn('<script>bad()', report)
        self.assertNotIn('<script>op()', report)
        self.assertNotIn('<img src=x', report)
        parsed = ReportDocument(report)
        self.assertTrue(all(href[1:] in parsed.ids for href in parsed.hrefs if href.startswith('#')))


if __name__ == '__main__':
    unittest.main()
