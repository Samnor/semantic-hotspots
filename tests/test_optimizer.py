"""Guard invariants, bounded composition, and honest evidence status."""
from __future__ import annotations

import copy
import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch
import subprocess

from semantic_hotspots import (ModelError, analyze, parse_model, plan_rewrites, simulate,
                               prepare_rewrite_experiment, collect_rewrite_results, run_rewrite_experiment)
from semantic_hotspots.cli import main
from semantic_hotspots.experiments import _experiment
from semantic_hotspots.optimizer import logical_plan


def source(expression='SUMX(Sales, Sales[Amount])', datatype='double'):
    return {'name': 'Fictional rewrite lab', 'model': {'tables': [
        {'name': 'Sales', 'columns': [{'name': 'Amount', 'dataType': datatype},
                                    {'name': 'Category', 'dataType': 'string'}],
         'measures': [{'name': 'Total', 'expression': expression},
                      {'name': 'Dependent', 'expression': '[Total] + 1'}],
         'partitions': [{'name': 'Sales', 'mode': 'import'}]}]}}


class LogicalRewriteTests(unittest.TestCase):
    def test_numeric_aggregate_bound_type_scope_and_source_are_retained(self):
        model = parse_model(source())
        measure = model.measure('Total')
        plan = plan_rewrites(model)
        row = plan.measures[0]
        self.assertEqual(row['logical_before']['operation'], 'SUMX')
        self.assertEqual(row['logical_before']['result_type'], 'double')
        self.assertEqual(row['logical_before']['children'][1]['row_context'], ('Sales',))
        self.assertIn(model.measure('Dependent').id, row['affected_downstream_measures'])
        candidate = row['candidates'][0]
        self.assertEqual(candidate['after'], 'SUM(Sales[Amount])')
        self.assertEqual(candidate['logical_after']['children'][0]['row_context'], ())
        self.assertIn('source_span', candidate['logical_after']['children'][0])
        self.assertEqual(candidate['validation_status'], 'not_run')
        self.assertIsNone(candidate['predicted_speedup'])
        self.assertEqual(measure.expression, 'SUMX(Sales, Sales[Amount])')

    def test_only_physical_numeric_direct_columns_are_eligible(self):
        for typ in ['int64', 'double', 'decimal']:
            self.assertTrue(plan_rewrites(source(datatype=typ)).candidates)
        for typ in ['string', 'boolean', 'dateTime', None, 'variant']:
            self.assertFalse(plan_rewrites(source(datatype=typ)).candidates)
        for expression in ['SUMX(Sales, [Total])', 'SUMX(Sales, Sales[Amount] * 2)',
                           'SUMX(FILTER(Sales, Sales[Amount]>0), Sales[Amount])',
                           'SUMX(VALUES(Sales[Amount]), Sales[Amount])',
                           'IF(UnknownCondition(), SUMX(Sales, Sales[Amount]), 0)',
                           'VAR t = Sales RETURN SUMX(t, Sales[Amount])',
                           'SUMX(Sales, [Missing])', 'SUMX(Sales, Sales[Amount]) + Missing(1)',
                           'SUMX(Sales, Sales[Amount]) /*', 'SUMX(Sales, Sales[Amount]']:
            with self.subTest(expression=expression):
                self.assertFalse(plan_rewrites(source(expression)).candidates)
        data = source()
        data['model']['tables'][0]['columns'][0]['expression'] = '1'
        self.assertFalse(plan_rewrites(data).candidates)
        data = source()
        data['model']['tables'][0]['partitions'][0]['source'] = {'type': 'calculated', 'expression': 'ROW("Amount",1)'}
        self.assertFalse(plan_rewrites(data).candidates)

    def test_filter_fusion_restricts_predicates_and_preserves_parentheses(self):
        text = 'COUNTROWS(FILTER(FILTER(Sales, Sales[Amount] > -1 || Sales[Amount] == 0), Sales[Category] <> "X"))'
        plan = plan_rewrites(source(text))
        candidate = plan.candidates[0]
        self.assertEqual(candidate['steps'][0]['rule'], 'OPT002')
        self.assertEqual(candidate['after'], 'COUNTROWS(FILTER(Sales, (Sales[Amount] > -1 || Sales[Amount] == 0) && (Sales[Category] <> "X")))')
        for predicate in ['[Dependent] > 1', 'Sales[Amount] / 0 > 1',
                          'CALCULATE(SUM(Sales[Amount])) > 1',
                          'Sales[Amount] == "1"', 'Sales[Amount] > BLANK()']:
            text = f'COUNTROWS(FILTER(FILTER(Sales, Sales[Amount] > 0), {predicate}))'
            self.assertFalse(plan_rewrites(source(text)).candidates, predicate)

    def test_filter_cannot_be_returned_by_a_measure(self):
        self.assertFalse(plan_rewrites(source('FILTER(FILTER(Sales, Sales[Amount]>0), Sales[Amount]<10)')).candidates)

    def test_fixed_point_combines_multiple_edits_without_overlap(self):
        expr = ('SUMX(Sales, Sales[Amount]) + SUMX(Sales, Sales[Amount]) + '
                'COUNTROWS(FILTER(FILTER(FILTER(Sales, Sales[Amount] > 0), Sales[Amount] < 10), Sales[Category] == "A"))')
        plan = plan_rewrites(source(expr))
        combined = next(c for c in plan.candidates if c['combined'])
        self.assertEqual(len(combined['steps']), 4)
        self.assertNotIn('SUMX', combined['after'])
        self.assertEqual(combined['after'].count('FILTER('), 1)
        self.assertEqual(len({c['id'] for c in plan.candidates}), len(plan.candidates))
        self.assertEqual(plan.to_dict(), plan_rewrites(source(expr)).to_dict())

    def test_calculate_marks_context_and_does_not_hoist_variables(self):
        plan = plan_rewrites(source('CALCULATE(SUMX(Sales, Sales[Amount]), FILTER(Sales, Sales[Amount] > 0))'))
        candidate = plan.candidates[0]
        self.assertIn('CALCULATE: transition/modified filters', candidate['logical_before']['children'][0]['filter_context'])
        self.assertIn('CALCULATE(SUM(', candidate['after'])
        self.assertNotIn('VAR', candidate['after'])

    def test_escaped_names_comments_and_same_table_binding(self):
        data = source("SUMX('Sa''les', 'Sa''les'[A]]mount]) /* keep */")
        table = data['model']['tables'][0]
        table['name'] = "Sa'les"
        table['columns'][0]['name'] = 'A]mount'
        self.assertEqual(plan_rewrites(data).candidates[0]['after'], "SUM('Sa''les'[A]]mount]) /* keep */")
        data = source('SUMX(Sales, Other[Amount])')
        data['model']['tables'].append({'name': 'Other', 'columns': [{'name': 'Amount', 'dataType': 'double'}]})
        self.assertFalse(plan_rewrites(data).candidates)

    def test_dynamic_models_and_incomplete_chains_are_withheld(self):
        data = source()
        data['model']['tables'].append({'name': 'Calc', 'calculationGroup': {'calculationItems': [{'name': 'Item', 'expression': 'SELECTEDMEASURE()'}]}})
        self.assertFalse(plan_rewrites(data).candidates)
        data = source('SUMX(Sales, Sales[Amount]) + [Dependent]')
        self.assertFalse(plan_rewrites(data).candidates)
        data = source('SUMX(Sales, Sales[Amount]) + [Broken]')
        data['model']['tables'][0]['measures'].append({'name': 'Broken', 'expression': 'SUM('})
        self.assertFalse(plan_rewrites(data).candidates)
        data = source('SUMX(Sales, Sales[Amount]) + [Volatile]')
        data['model']['tables'][0]['measures'].append({'name':'Volatile','expression':'RAND()'})
        self.assertFalse(plan_rewrites(data).candidates)
        data = source()
        data['model']['tables'][0]['measures'][0]['formatStringDefinition'] = {'expression': 'IF([Dependent]>0,"0.0","0")'}
        self.assertFalse(plan_rewrites(data).candidates)

    def test_report_and_json_include_candidates_escaped_and_unvalidated(self):
        data = source('COUNTROWS(FILTER(FILTER(Sales, Sales[Category] == "<script>"), Sales[Amount] > 0))')
        with tempfile.TemporaryDirectory() as tmp:
            files = analyze(data).write(tmp)
            report = files['html'].read_text(encoding='utf-8')
            self.assertIn('Guarded rewrite proposals', report)
            self.assertIn('&lt;script&gt;', report)
            self.assertNotIn('== "<script>"', report)
            self.assertTrue((Path(tmp)/'rewrite-plan.json').is_file())
            self.assertIn('rewrite_plan', json.loads(files['json'].read_text(encoding='utf-8')))

    def test_deep_and_unsupported_syntax_is_opaque(self):
        model = parse_model(source())
        tree, issues = logical_plan('('*2000+'1'+')'*2000, model, model.measure('Total'))
        self.assertFalse(tree.supported)
        self.assertTrue(issues)


class RewriteExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = simulate(source(), self.root/'lab', config={'fact_rows': 12, 'workloads': [
            {'name': 'groups', 'group_by': [['Sales', 'Category']]},
            {'name': 'empty', 'filters': [{'table': 'Sales', 'column': 'Amount', 'values': [-999]}]}]})
        self.directory = prepare_rewrite_experiment(self.bundle, self.root/'experiment')
        self.manifest = _experiment(self.directory)[1]
        self.executable = self.root/'dscmd.exe'
        self.executable.write_text('Mock runner, never executed', encoding='utf-8')

    def fake_runner(self, *, mismatch=False, blank=False, fail_validation=False,
                    fail_benchmark=False, missing_iterations=False, timeout=False, times=(20, 10)):
        cases = {c['id']: c for c in self.manifest['cases']}
        def runner(argv, **kwargs):
            self.assertFalse(kwargs.get('shell', False))
            if timeout:
                raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
            output = Path(argv[2])
            identity, kind = output.stem.split('-', 1)
            case = cases[identity]
            if kind == 'validation':
                if fail_validation:
                    return subprocess.CompletedProcess(argv, 1, '', 'Fictional engine error')
                cols = case['columns']
                with output.open('w', encoding='utf-8', newline='') as stream:
                    writer = csv.writer(stream)
                    writer.writerow(['['+v+']' for v in cols.values()])
                    writer.writerow(['' if blank else '42', '' if blank else '42', '0' if mismatch else '1', int(blank), int(blank)])
            else:
                if fail_benchmark:
                    return subprocess.CompletedProcess(argv, 1, '', 'Fictional benchmark error')
                with output.open('w', encoding='utf-8', newline='') as stream:
                    writer = csv.writer(stream)
                    writer.writerow(['Sequence', 'Cache', 'TotalDuration_ms', 'Error', 'RowCount'])
                    sequence = 0
                    for flag, cache in [('--cold', 'Cold'), ('--warm', 'Warm')]:
                        count = int(argv[argv.index(flag)+1])
                        if missing_iterations:
                            count = max(0, count-1)
                        for _ in range(count):
                            sequence += 1
                            writer.writerow([sequence, cache, times(case, kind) if callable(times) else times[kind == 'candidate'], '', 1])
            return subprocess.CompletedProcess(argv, 0, '', '')
        return runner

    def execute(self, runner, **kwargs):
        with patch('semantic_hotspots.experiments.subprocess.run', side_effect=runner) as mock:
            result = run_rewrite_experiment(self.directory, dscmd=self.executable,
                                            server='localhost:TEST', database='Fictional', **kwargs)
        return result, mock

    def test_preparation_covers_total_group_filter_and_query_scoped_aliases(self):
        self.assertEqual(len(self.manifest['cases']), 3)
        case = self.manifest['cases'][0]
        self.assertIn('DEFINE MEASURE', case['queries']['candidate']['query'])
        self.assertNotIn('DEFINE MEASURE', case['queries']['baseline']['query'])
        self.assertIn('ISBLANK', case['queries']['validation']['query'])
        self.assertIn(' == ', case['queries']['validation']['query'])
        self.assertIn('not_run', (self.directory/'rewrite-results.json').read_text())
        before = self.bundle.model_path.read_bytes()
        self.execute(self.fake_runner())
        self.assertEqual(before, self.bundle.model_path.read_bytes())

    def test_direct_client_preserves_validation_gate_and_fingerprinted_evidence(self):
        for mismatch in [False, True]:
            calls = []
            fake = self.fake_runner(mismatch=mismatch)
            def direct(argv, **kwargs):
                self.assertEqual(argv[2], 'semantic_hotspots._adomd_worker')
                operation = argv[3]
                output = Path(argv[argv.index('--output')+1])
                translated = ['client', operation, str(output)] + argv[argv.index('--server'):]
                calls.append(operation)
                result = fake(translated, **kwargs)
                if operation == 'benchmark':
                    with output.open(newline='', encoding='utf-8') as stream:
                        records = list(csv.reader(stream))
                    with output.open('w', newline='', encoding='utf-8') as stream:
                        csv.writer(stream).writerows([records[0]+['TimingSource']] + [r+['client_elapsed'] for r in records[1:]])
                return result
            with patch('semantic_hotspots.experiments.subprocess.run', side_effect=direct):
                result = run_rewrite_experiment(self.directory, server='localhost:123', database='Synthetic',
                                                results_directory=self.root/('direct-fail' if mismatch else 'direct-pass'), warm=1)
            status = result.to_dict()['ranked_candidates'][0]
            self.assertEqual(status['validation_status'], 'failed' if mismatch else 'passed_tested_cases')
            self.assertEqual('benchmark' in calls, not mismatch)
            if not mismatch:
                self.assertTrue(all(r['timing_source'].startswith('client elapsed') for c in result.cases for rows in c['benchmark_rows'].values() for r in rows))

    def test_validated_winner_retains_evidence_and_all_cache_phases(self):
        result, mock = self.execute(self.fake_runner(), cold=2, warm=3)
        data = result.to_dict()['ranked_candidates'][0]
        self.assertEqual(data['validation_status'], 'passed_tested_cases')
        self.assertEqual(data['performance_status'], 'observed_improvement')
        self.assertTrue(data['selected_for_review'])
        self.assertTrue(result.to_dict()['fastest_observed_candidate_ids'])
        self.assertEqual(len(data['cases']), 3)
        self.assertEqual(len(data['cases'][0]['comparisons']), 2)
        self.assertEqual(data['cases'][0]['comparisons'][0]['change_percent'], 50)
        self.assertEqual(mock.call_count, 9)
        self.assertTrue(all('csv' == c.args[0][1] for c in mock.call_args_list[:3]))
        report = (self.directory/'experiment-results/experiment.html').read_text(encoding='utf-8')
        self.assertIn('Before', report)
        self.assertIn('Candidate', report)
        self.assertIn('query_sha256', (self.directory/'experiment-results/rewrite-runner.json').read_text())

    def test_failures_blank_outputs_and_timeouts_never_become_improvements(self):
        for options, validation, performance in [
            ({'mismatch': True}, 'failed', 'withheld_validation_failed'),
            ({'blank': True}, 'passed_tested_cases', 'uninformative_all_blank'),
            ({'fail_validation': True}, 'failed', 'withheld_validation_failed'),
            ({'timeout': True}, 'failed', 'withheld_validation_failed')]:
            with self.subTest(options=options):
                result, mock = self.execute(self.fake_runner(**options), results_directory=self.root/str(len(list(self.root.iterdir()))))
                row = result.to_dict()['ranked_candidates'][0]
                self.assertEqual(row['validation_status'], validation)
                self.assertEqual(row['performance_status'], performance)
                self.assertEqual(mock.call_count, 3)

    def test_timing_errors_missing_iterations_and_slower_candidates_are_honest(self):
        for options, expected in [({'fail_benchmark': True}, 'unknown'),
                                  ({'missing_iterations': True}, 'unknown'),
                                  ({'times': (10, 20)}, 'no_observed_improvement'),
                                  ({'times': (10, 10)}, 'no_observed_improvement')]:
            with self.subTest(options=options):
                result, _ = self.execute(self.fake_runner(**options), results_directory=self.root/str(len(list(self.root.iterdir()))))
                self.assertEqual(result.to_dict()['ranked_candidates'][0]['performance_status'], expected)

    def test_scenario_regression_is_mixed_and_never_selected_for_review(self):
        def times(case, kind):
            return 20 if kind == 'baseline' else 10 if case['scenario'] == 'total' else 30
        result, _ = self.execute(self.fake_runner(times=times))
        row = result.to_dict()['ranked_candidates'][0]
        self.assertEqual(row['performance_status'], 'mixed_results')
        self.assertFalse(row['selected_for_review'])
        self.assertFalse(result.to_dict()['fastest_observed_candidate_ids'])

    def test_missing_or_foreign_runner_cannot_establish_complete_timings(self):
        result, _ = self.execute(self.fake_runner())
        out = self.directory/'experiment-results'
        runner = out/'rewrite-runner.json'
        capture = runner.read_text()
        runner.unlink()
        result = collect_rewrite_results(self.directory, out)
        self.assertEqual(result.to_dict()['ranked_candidates'][0]['performance_status'], 'unknown')
        data = json.loads(capture)
        data['experiment_sha256'] = 'wrong'
        runner.write_text(json.dumps(data))
        with self.assertRaises(ModelError):
            collect_rewrite_results(self.directory, out)

    def test_changed_result_csv_and_duplicate_run_records_are_rejected(self):
        self.execute(self.fake_runner())
        out = self.directory/'experiment-results'
        capture = out/'rewrite-runner.json'
        runner = json.loads(capture.read_text())
        runner['runs'].append(copy.deepcopy(runner['runs'][0]))
        capture.write_text(json.dumps(runner))
        with self.assertRaises(ModelError):
            collect_rewrite_results(self.directory, out)
        runner['runs'].pop()
        capture.write_text(json.dumps(runner))
        csv_path = out/(self.manifest['cases'][0]['id']+'-candidate.csv')
        csv_path.write_text(csv_path.read_text().replace(',10,', ',1,'))
        with self.assertRaises(ModelError):
            collect_rewrite_results(self.directory, out)

    def test_changed_queries_invalid_bounds_and_nonempty_outputs_are_rejected(self):
        with self.assertRaises(ModelError):
            prepare_rewrite_experiment(self.bundle, self.directory)
        for kwargs in [{'warm': 0}, {'warm': True}, {'cold': -1}, {'timeout': 0}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ModelError):
                self.execute(self.fake_runner(), **kwargs)
        query = self.directory/self.manifest['cases'][0]['queries']['candidate']['file']
        query.write_text('EVALUATE ROW("changed",1)')
        with self.assertRaises(ModelError):
            collect_rewrite_results(self.directory, self.root/'missing-results')

    def test_zero_rows_bad_headers_and_blank_mismatch_fail_validation(self):
        from semantic_hotspots.experiments import _validation
        path = self.root/'validation.csv'
        columns = self.manifest['cases'][0]['columns']
        headers = ','.join(columns.values())+'\n'
        for contents in [headers, headers+'1,1,1,0,1\n', headers+'1,1,no,0,0\n', 'wrong\n1\n']:
            path.write_text(contents)
            self.assertEqual(_validation(path, columns)['status'], 'failed')

    def test_cli_prepares_collects_and_signals_incomplete_evidence(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(['experiment', str(self.bundle.directory), '-o', str(self.root/'cli')]), 0)
            self.assertEqual(main(['experiment-run', str(self.directory), '--collect-only']), 1)
            self.assertEqual(main(['experiment-run', str(self.directory)]), 2)
