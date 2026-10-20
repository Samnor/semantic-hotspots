from __future__ import annotations

import contextlib
import copy
import csv
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from semantic_hotspots import (ModelError, SimulationConfig, collect_benchmarks, load_model,
                               read_benchmark_csv, run_benchmarks, simulate)
from semantic_hotspots.cli import main


def retail():
    return {'name': 'Synthetic test', 'compatibilityLevel': 1601, 'model': {
        'dataSources': [{'name': 'Original', 'connectionString': 'password=DO_NOT_COPY'}],
        'expressions': [{'name': 'OldSource', 'kind': 'm', 'expression': 'Sql.Database("REAL_SERVER","REAL_DATABASE")'}],
        'roles': [{'name': 'Restricted', 'tablePermissions': []}],
        'tables': [
            {'name': 'Facts', 'columns': [{'name': 'Key', 'dataType': 'int64'},
                                         {'name': 'Amount', 'dataType': 'double'},
                                         {'name': 'Date', 'dataType': 'dateTime'},
                                         {'name': 'Flag', 'dataType': 'boolean'}],
             'partitions': [{'name': 'Original', 'mode': 'directQuery', 'source': {'type': 'm', 'expression': 'Sql.Database("REAL_SERVER","REAL_DATABASE")'}}],
             'refreshPolicy': {'policyType': 'basic'},
             'measures': [{'name': 'Total', 'expression': 'SUM(Facts[Amount])'},
                          {'name': 'Twice', 'expression': '[Total] + [Total]'}]},
            {'name': 'Dim', 'columns': [{'name': 'Key', 'dataType': 'int64'}, {'name': 'Label', 'dataType': 'string'}]},
        ], 'relationships': [{'name': 'Join', 'fromTable': 'Facts', 'fromColumn': 'Key', 'toTable': 'Dim', 'toColumn': 'Key',
                             'isActive': False, 'crossFilteringBehavior': 'bothDirections'}]}}


HEADER = 'Sequence,Cache,TotalDuration_ms,FormulaEngineDuration_ms,StorageEngineDuration_ms,StorageEngineQueryCount,StorageEngineCpu_ms,TotalCpu_ms,VertipaqCacheMatches,RowCount,Error\n'
DETAIL = HEADER + '1,Cold,80,33,47,1,0,16,0,15,\n2,Warm,0,0,0,1,0,0,1,15,\n# Summary\nCache,Statistic,TotalDuration_ms\nCold,Average,80\n'


class SimulationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def generate(self, config=None, model=None, name='out'):
        settings = {'fact_rows': 500, 'dimension_rows': 20}
        settings.update(config or {})
        return simulate(model if model is not None else retail(), self.root/name, config=settings)

    def records(self, bundle, table):
        path = next(t['csv'] for t in bundle.manifest['tables'] if t['name'] == table)
        with (bundle.directory/path).open(newline='', encoding='utf-8') as f:
            return list(csv.DictReader(f))

    def test_seeded_tables_preserve_unique_keys_and_inactive_relationships(self):
        first = self.generate()
        second = self.generate(name='second')
        self.assertEqual([t.get('sha256') for t in first.manifest['tables']], [t.get('sha256') for t in second.manifest['tables']])
        facts, dims = self.records(first, 'Facts'), self.records(first, 'Dim')
        self.assertEqual(len(facts), 500); self.assertEqual(len(dims), 20)
        keys = [row['Key'] for row in dims]
        self.assertEqual(len(set(keys)), 20)
        self.assertTrue(all(row['Key'] in keys for row in facts))
        self.assertFalse(first.manifest['relationships'][0]['active'])
        self.assertEqual(first.manifest['engine_execution'], 'not_run')
        third = self.generate({'seed': 99}, name='third')
        self.assertNotEqual(self.records(first, 'Facts'), self.records(third, 'Facts'))

    def test_skew_blanks_and_exact_categories_are_configurable(self):
        bundle = self.generate({'fact_rows': 4000, 'tables': {
            'Facts': {'columns': {'Key': {'skew': .95, 'null_fraction': .15}, 'Amount': {'min': 2, 'max': 8, 'distinct': 2}}},
            'Dim': {'columns': {'Label': {'values': ['North', 'South']}}}}})
        facts = self.records(bundle, 'Facts')
        blanks = sum(row['Key'] == '' for row in facts)
        hottest = sum(row['Key'] == '1' for row in facts)
        self.assertTrue(400 < blanks < 800)
        self.assertGreater(hottest, 2800)
        self.assertEqual({row['Amount'] for row in facts}, {'2.0', '8.0'})
        self.assertEqual({row['Label'] for row in self.records(bundle, 'Dim')}, {'North', 'South'})

    def test_original_sources_removed_and_dax_relationship_metadata_preserved(self):
        original = retail(); before = copy.deepcopy(original)
        bundle = self.generate(model=original)
        self.assertEqual(original, before)
        text = bundle.model_path.read_text()
        self.assertNotIn('DO_NOT_COPY', text); self.assertNotIn('REAL_SERVER', text)
        bim = json.loads(text)
        self.assertNotIn('roles', bim['model']); self.assertNotIn('dataSources', bim['model'])
        self.assertEqual(bim['model']['tables'][0]['measures'], original['model']['tables'][0]['measures'])
        self.assertEqual(bim['model']['relationships'], original['model']['relationships'])
        self.assertNotIn('refreshPolicy', bim['model']['tables'][0])
        self.assertTrue(all(p['mode'] == 'import' for t in bim['model']['tables'] for p in t.get('partitions', [])))
        self.assertEqual(len(load_model(bundle.model_path).measures), 2)
        self.assertTrue(bundle.report_path.is_file())
        self.assertIn('DAX timings', bundle.report_path.read_text())
        self.assertIn('Not run', bundle.report_path.read_text())

    def test_calculated_objects_preserved_and_no_fake_source_rows(self):
        model = retail()
        model['model']['tables'][0]['columns'].append({'name': 'Derived', 'type': 'calculated', 'dataType': 'double', 'expression': 'Facts[Amount] * 2'})
        model['model']['tables'].append({'name': 'Calc', 'columns': [{'name': 'Value', 'dataType': 'int64', 'type': 'calculatedTableColumn', 'sourceColumn': '[Value]'}],
                                         'partitions': [{'name': 'Calc', 'source': {'type': 'calculated', 'expression': 'GENERATESERIES(1,10)'}}]})
        bundle = self.generate(model=model)
        self.assertNotIn('Derived', self.records(bundle, 'Facts')[0])
        calc = next(t for t in bundle.manifest['tables'] if t['name'] == 'Calc')
        self.assertEqual(calc['kind'], 'engine-calculated'); self.assertIsNone(calc['rows'])
        self.assertNotIn('csv', calc)
        synthetic = json.loads(bundle.model_path.read_text())
        self.assertEqual(synthetic['model']['tables'][0]['columns'][-1]['expression'], 'Facts[Amount] * 2')

    def test_date_foreign_keys_and_multiple_parent_constraints(self):
        model = retail()
        model['model']['tables'][0]['columns'][0]['dataType'] = 'dateTime'
        model['model']['tables'][1]['columns'][0]['dataType'] = 'dateTime'
        model['model']['tables'].append({'name': 'OtherDim', 'columns': [{'name': 'Key', 'dataType': 'dateTime'}]})
        model['model']['relationships'].append({'name': 'Other', 'fromTable': 'Facts', 'fromColumn': 'Key', 'toTable': 'OtherDim', 'toColumn': 'Key'})
        bundle = self.generate({'tables': {'OtherDim': {'rows': 10}}}, model=model)
        for table in ['Dim', 'OtherDim']:
            keys = {r['Key'] for r in self.records(bundle, table)}
            self.assertTrue(all(r['Key'] in keys for r in self.records(bundle, 'Facts')))
        self.assertTrue(self.records(bundle, 'Facts')[0]['Key'].endswith('T00:00:00'))

    def test_total_group_and_filter_queries_quote_dax_identifiers(self):
        bundle = self.generate({'workloads': [
            {'name': 'by label', 'group_by': [['Dim', 'Label']]},
            {'name': 'filtered', 'filters': [{'table': 'Dim', 'column': 'Label', 'values': ['a"b', None]}]}
        ]})
        self.assertEqual(len(bundle.manifest['workloads']), 6)
        texts = [w['query'] for w in bundle.manifest['workloads']]
        self.assertTrue(any('ROW("Result", \'Facts\'[Total])' in t for t in texts))
        self.assertTrue(any("SUMMARIZECOLUMNS(\n    'Dim'[Label]" in t for t in texts))
        self.assertTrue(any('TREATAS({"a""b", BLANK()}, \'Dim\'[Label])' in t for t in texts))
        self.assertTrue(all((bundle.directory/w['query_file']).is_file() for w in bundle.manifest['workloads']))

    def test_bad_key_domains_fail_before_output_exists(self):
        settings = [
            {'tables': {'Dim': {'columns': {'Key': {'values': [1, 2]}}}}},
            {'tables': {'Dim': {'columns': {'Key': {'null_fraction': .01}}}}},
            {'tables': {'Facts': {'columns': {'Key': {'distinct': 5}}}}},
            {'tables': {'Facts': {'columns': {'Key': {'type': 'string'}}}}},
            {'tables': {'Dim': {'columns': {'Key': {'min': 1, 'max': 2}}}}},
        ]
        for i, setting in enumerate(settings):
            with self.subTest(setting=setting), self.assertRaises(ModelError):
                self.generate(setting, name=f'bad{i}')
            self.assertFalse((self.root/f'bad{i}').exists())

    def test_one_to_one_reversed_and_many_to_many_relations(self):
        model = retail()
        r = model['model']['relationships'][0]
        r.update(fromCardinality='one', toCardinality='one')
        bundle = self.generate({'tables': {'Facts': {'rows': 20}}}, model=model)
        self.assertEqual([r['Key'] for r in self.records(bundle, 'Facts')], [r['Key'] for r in self.records(bundle, 'Dim')])
        with self.assertRaisesRegex(ModelError, 'equal configured'):
            self.generate({'tables': {'Facts': {'rows': 30}}}, model=model, name='bad')
        model = retail(); r = model['model']['relationships'][0]
        r.update(fromTable='Dim', toTable='Facts', fromCardinality='one', toCardinality='many')
        self.generate(model=model, name='reversed')
        r.update(fromCardinality='many', toCardinality='many')
        self.generate(model=model, name='many')

    def test_invalid_config_fails_with_public_error(self):
        configs = [{'seed': True}, {'fact_rows': 0}, {'fact_rows': 1.5}, {'dimension_rows': -1},
                   {'unknown_setting': 3}, {'tables': {'Missing': {}}}, {'tables': {'Facts': {'rowz': 1}}},
                   {'tables': {'Facts': {'columns': {'Missing': {}}}}},
                   {'tables': {'Facts': {'columns': {'Amount': {'null_fraction': float('nan')}}}}},
                   {'tables': {'Facts': {'columns': {'Amount': {'skew': 2}}}}},
                   {'tables': {'Facts': {'columns': {'Amount': {'type': 'binary'}}}}},
                   {'tables': {'Facts': {'columns': {'Amount': {'values': [float('inf')]}}}}},
                   {'tables': {'Facts': {'columns': {'Flag': {'values': [1, 0]}}}}},
                   {'tables': {'Facts': {'columns': {'Date': {'start': 'tomorrow'}}}}},
                   {'workloads': [{'name': 'total'}]}, {'workloads': [{'name': 'x', 'group_by': [['No', 'Col']]}]}]
        for i, config in enumerate(configs):
            with self.subTest(config=config), self.assertRaises(ModelError):
                self.generate(config, name=f'bad-{i}')

    def test_output_not_overwritten(self):
        self.generate()
        with self.assertRaisesRegex(ModelError, 'not overwritten'):
            self.generate()

    def test_reusing_config_does_not_change_existing_bundle_provenance(self):
        config = SimulationConfig(fact_rows=20, dimension_rows=5,
                                  workloads=[{'name': 'group', 'group_by': [['Dim', 'Label']]}])
        bundle = simulate(retail(), self.root/'frozen', config=config)
        config.fact_rows = 999
        config.workloads[0]['group_by'].clear()
        self.assertEqual(bundle.manifest['config']['fact_rows'], 20)
        self.assertEqual(bundle.manifest['config']['workloads'][0]['group_by'], [['Dim', 'Label']])
        self.assertEqual(bundle.manifest['workloads'][2]['group_by'], [['Dim', 'Label']])

    def test_date_filters_use_typed_date_literals(self):
        bundle = self.generate({'workloads': [{'name': 'date', 'filters': [{'table': 'Facts', 'column': 'Date', 'values': ['2024-02-03']}]}]})
        self.assertTrue(any('TREATAS({DATE(2024, 2, 3)}' in w['query'] for w in bundle.manifest['workloads']))

    def test_non_key_dates_span_the_requested_window(self):
        bundle = self.generate({'tables': {'Facts': {'columns': {'Date': {'start': '2024-01-01', 'end': '2024-12-31', 'distinct': 2}}}}})
        self.assertEqual({r['Date'] for r in self.records(bundle, 'Facts')}, {'2024-01-01T00:00:00', '2024-12-31T00:00:00'})

    def test_distinct_tracking_is_bounded_and_labeled(self):
        bundle = self.generate({'dimension_rows': 10001, 'fact_rows': 5})
        key = next(c for t in bundle.manifest['tables'] if t['name'] == 'Dim' for c in t['columns'] if c['name'] == 'Key')
        self.assertEqual(key['distinct_observed'], 10000)
        self.assertFalse(key['distinct_complete'])

    def test_typed_key_values_cannot_collapse_in_engine_conversion(self):
        model = retail()
        for table in model['model']['tables']:
            table['columns'][0]['dataType'] = 'decimal'
        with self.assertRaisesRegex(ModelError, 'type conversion'):
            self.generate({'dimension_rows': 2, 'tables': {'Dim': {'columns': {'Key': {'values': [1.00001, 1.00002]}}}}}, model=model)
        for table in model['model']['tables']:
            table['columns'][0]['dataType'] = 'string'
        with self.assertRaisesRegex(ModelError, 'case-insensitive'):
            self.generate({'dimension_rows': 2, 'tables': {'Dim': {'columns': {'Key': {'values': ['A', 'a']}}}}}, model=model)

    def test_full_bim_keeps_dynamic_formats_and_advanced_objects(self):
        model = retail(); model['compatibilityLevel'] = 1702
        measure = model['model']['tables'][0]['measures'][0]
        measure['formatStringDefinition'] = {'expression': '"0.00"'}
        model['model']['functions'] = [{'name': 'PlusOne', 'expression': '(x: SCALAR) => x + 1'}]
        model['model']['tables'].append({'name': 'Group', 'columns': [{'name': 'Name', 'dataType': 'string'}],
                                         'calculationGroup': {'precedence': 10, 'calculationItems': [{'name': 'Identity', 'expression': 'SELECTEDMEASURE()'}]}})
        bundle = self.generate(model=model)
        generated = json.loads(bundle.model_path.read_text())
        self.assertEqual(generated['compatibilityLevel'], 1702)
        self.assertEqual(generated['model']['functions'], model['model']['functions'])
        self.assertEqual(generated['model']['tables'][0]['measures'][0], measure)
        self.assertEqual(generated['model']['tables'][-1]['calculationGroup']['precedence'], 10)

    def test_calculated_relationship_keys_fail_explicitly(self):
        model = retail()
        model['model']['tables'][1]['columns'][0]['expression'] = '1'
        with self.assertRaisesRegex(ModelError, 'require physical'):
            self.generate(model=model)

    def test_tmdl_type_case_and_projection(self):
        path = self.root/'model.tmdl'
        path.write_text('table T\n\tcolumn Amount\n\t\tDATATYPE: double\n\tmeasure Total = SUM(T[Amount])\n', encoding='utf-8')
        bundle = self.generate(model=path)
        self.assertEqual(bundle.manifest['tables'][0]['columns'][0]['type'], 'double')
        self.assertIn('projection', bundle.manifest['metadata_fidelity'])

    def test_path_and_html_names_are_escaped(self):
        model = retail(); model['name'] = '<script>bad()</script>'
        bundle = self.generate({'data_root': 'C:\\mock#(lf)\\"test"'}, model=model)
        self.assertNotIn('<script>bad()', bundle.report_path.read_text())
        m = json.loads(bundle.model_path.read_text())['model']['expressions'][0]['expression']
        self.assertIn('#(#)(lf)', m); self.assertIn('""test""', m)
        self.assertTrue(all(not set('<>:"\\|?*').intersection(Path(t['csv']).name) for t in bundle.manifest['tables']))

    def test_cli_generates_and_collects_without_an_engine(self):
        path = self.root/'model.bim'; path.write_text(json.dumps(retail()))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['simulate', str(path), '-o', str(self.root/'cli'), '--fact-rows', '30', '--dimension-rows', '5']), 0)
            self.assertEqual(main(['benchmark', str(self.root/'cli'), '--collect-only']), 1)
        self.assertTrue((self.root/'cli/benchmark-results/benchmark.html').is_file())


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = simulate(retail(), self.root/'bundle', config={'fact_rows': 20, 'dimension_rows': 5})

    def csv(self, content=DETAIL):
        path = self.root/'test.csv'; path.write_text(content, encoding='utf-8')
        return path

    def test_summary_rows_excluded_and_zero_cpu_not_imputed(self):
        rows = read_benchmark_csv(self.csv())
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]['duration_ms'], 0)
        self.assertTrue(all(r['cpu_ms'] is None for r in rows))
        self.assertEqual(read_benchmark_csv(self.csv(), trace_verified=True)[0]['cpu_ms'], 16)

    def test_multiline_errors_and_bad_rows(self):
        text = HEADER + '1,Cold,0,0,0,0,0,0,0,0,"Engine error\n# Summary\ncontinued"\n'
        rows = read_benchmark_csv(self.csv(text))
        self.assertIn('# Summary', rows[0]['error'])
        self.assertIsNone(rows[0]['duration_ms'])
        for text in [HEADER+'1,Cold,NaN,0,0,0,0,0,0,0,\n', HEADER+'1,Cold,-1,0,0,0,0,0,0,0,\n',
                     HEADER+'1,Other,1,0,0,0,0,0,0,0,\n', HEADER+'1,Cold,1,0,0,0,0,0,0,0,\n1,Warm,1,0,0,0,0,0,0,0,\n',
                     'Not,a,dscmd,benchmark\n1,2,3,4\n']:
            with self.subTest(text=text), self.assertRaises(ModelError):
                read_benchmark_csv(self.csv(text))

    def test_aggregated_report_ranks_measured_durations_and_retains_failures(self):
        results = self.root/'results'; results.mkdir()
        first, second = self.bundle.manifest['workloads']
        (results/f'result-{first["id"]}.csv').write_text(HEADER+'1,Warm,10,0,0,0,0,0,0,1,\n2,Warm,0,0,0,0,0,0,0,0,Failure\n')
        (results/f'result-{second["id"]}.csv').write_text(HEADER+'1,Warm,90,0,0,0,0,0,0,1,\n2,Warm,110,0,0,0,0,0,0,1,\n')
        report = collect_benchmarks(self.bundle, results)
        self.assertEqual(report.cases[0]['phases']['warm']['successes'], 1)
        self.assertEqual(report.cases[0]['phases']['warm']['mean_ms'], 10)
        self.assertEqual(report.cases[1]['phases']['warm']['mean_ms'], 100)
        self.assertEqual(report.cases[1]['phases']['warm']['p95_ms'], 110)
        html = report.write(results)['html'].read_text()
        ranking = html.split('<tbody>',1)[1].split('</tbody>',1)[0]
        self.assertLess(ranking.index(second['measure'].replace("'", '&#x27;')), ranking.index(first['measure'].replace("'", '&#x27;')))
        self.assertIn('1 failed iterations', html)
        self.assertIn('Failure', html)

    def test_missing_csv_stays_unknown(self):
        report = collect_benchmarks(self.bundle, self.root/'absent')
        self.assertIsNone(report.cases[0]['phases']['warm']['mean_ms'])
        self.assertTrue(any('CSV missing' in d for d in report.diagnostics))

    def test_runner_uses_argument_array_and_produces_results(self):
        exe = self.root/'dscmd.exe'; exe.touch()
        def fake(command, **kwargs):
            self.assertIsInstance(command, list)
            self.assertNotIn('shell', kwargs)
            self.assertEqual(command[1], 'benchmark')
            Path(command[2]).write_text(DETAIL)
            return subprocess.CompletedProcess(command, 0, 'test fixture', '')
        with patch('semantic_hotspots.benchmark.subprocess.run', side_effect=fake) as run:
            report = run_benchmarks(self.bundle, dscmd=exe, server='localhost:123', database='Synthetic', cold=1, warm=1)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(report.cases[0]['phases']['cold']['mean_ms'], 80)
        self.assertTrue((self.bundle.directory/'benchmark-results/benchmark.html').is_file())
        self.assertFalse(any('expected ' in d for d in report.diagnostics))

    def test_runner_timeouts_are_failures_not_fake_performance(self):
        exe = self.root/'dscmd.exe'; exe.touch()
        with patch('semantic_hotspots.benchmark.subprocess.run', side_effect=subprocess.TimeoutExpired('fixture', 1)):
            report = run_benchmarks(self.bundle, dscmd=exe, server='localhost', database='Synthetic', timeout=1)
        self.assertTrue(any('dscmd failed' in d for d in report.diagnostics))
        self.assertIsNone(report.cases[0]['phases']['warm']['mean_ms'])
        reread = collect_benchmarks(self.bundle, self.bundle.directory/'benchmark-results')
        self.assertTrue(any('dscmd failed' in d for d in reread.diagnostics))

    def test_partial_iterations_remain_partial_when_collected_again(self):
        out = self.root/'partial'; out.mkdir()
        case = self.bundle.manifest['workloads'][0]
        (out/f'result-{case["id"]}.csv').write_text(DETAIL)
        (out/'runner.json').write_text(json.dumps({'cold': 3, 'warm': 3, 'runs': [{'id': case['id'], 'returncode': 0}]}))
        result = collect_benchmarks(self.bundle, out)
        self.assertTrue(any('expected 3 iterations, found 1' in d for d in result.diagnostics))

    def test_incomplete_or_quoted_benchmark_records_fail_cleanly(self):
        for text in [HEADER + '1\n', HEADER + '1,Warm,1,0,0,0,0,0,0,1,"unterminated\n']:
            with self.subTest(text=text), self.assertRaises(ModelError):
                read_benchmark_csv(self.csv(text))

    def test_tampered_queries_and_paths_are_rejected(self):
        path = self.bundle.directory/self.bundle.manifest['workloads'][0]['query_file']
        path.write_text('EVALUATE ROW("changed", 1)')
        with self.assertRaisesRegex(ModelError, 'differs'):
            collect_benchmarks(self.bundle, self.root/'results')
        manifest = copy.deepcopy(self.bundle.manifest)
        manifest['workloads'][0]['query_file'] = '../../outside.dax'
        (self.bundle.directory/'simulation.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ModelError, 'under the bundle'):
            collect_benchmarks(self.bundle, self.root/'results')

    def test_invalid_runner_settings_fail_before_execution(self):
        exe = self.root/'dscmd.exe'; exe.touch()
        configs = [{'cold': -1}, {'warm': True}, {'cold': 0, 'warm': 0}, {'timeout': float('inf')}]
        for config in configs:
            with self.subTest(config=config), self.assertRaises(ModelError):
                run_benchmarks(self.bundle, dscmd=exe, server='localhost', database='Synthetic', **config)


if __name__ == '__main__':
    unittest.main()
