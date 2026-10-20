from __future__ import annotations

import base64
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from semantic_hotspots import CycleError, DependencyGraph, Entity, Model, ModelError, RuntimeProfile, analyze, load_model, parse_model
from semantic_hotspots.cli import main
from semantic_hotspots.dax import Resolver, tokenize


def example():
    return parse_model({'name': 'Example', 'model': {'tables': [
        {'name': 'Sales', 'partitions': [{'mode': 'directQuery', 'source': {'type': 'm', 'expression': 'secret connection string'}}],
         'columns': [{'name': 'Amount'}, {'name': 'Customer Key'}],
         'measures': [{'name': 'Total', 'expression': 'SUM(Sales[Amount])'},
                      {'name': 'Twice', 'expression': '[Total] + [Total]'},
                      {'name': 'Slow', 'expression': 'SUMX(VALUES(Sales[Customer Key]), CALCULATE([Twice]))'}]},
        {'name': 'Customer', 'partitions': [{'mode': 'import'}], 'columns': [{'name': 'Key'}]},
    ], 'relationships': [{'name': 'CustomerSales', 'fromTable': 'Sales', 'fromColumn': 'Customer Key', 'toTable': 'Customer', 'toColumn': 'Key'}]}})


class LexicalTests(unittest.TestCase):
    def test_comments_and_literals_do_not_add_references(self):
        m = example()
        i = Resolver(m).scan('"[Bogus] SUMX(x,y)" & "a""[AlsoBogus]" // [Never]\n/* SUMX([Bad], [No]) */ [Total]')
        self.assertEqual(i.functions, {})
        self.assertEqual([r.target for r in i.references], [m.measure('Total').id])

    def test_unicode_and_escaped_identifiers(self):
        m = Model()
        m.add(Entity('column', "Café's", 'A]mount'))
        e = m.add(Entity('measure', "Café's", 'Méasure', "SUM('Café''s'[A]]mount])"))
        i = Resolver(m).scan(e.expression, e)
        self.assertEqual(len(i.references), 1)
        self.assertEqual(m.entities[i.references[0].target].name, 'A]mount')

    def test_return_does_not_become_a_table_qualifier(self):
        m = example()
        info = Resolver(m).scan('VAR x=1 RETURN [Total]')
        self.assertEqual([r.target for r in info.references], [m.measure('Total').id])
        self.assertFalse(info.unresolved)

    def test_case_insensitive_qualified_and_bare_measures(self):
        m = example()
        info = Resolver(m).scan('[total] + sales[TOTAL]')
        self.assertEqual([r.target for r in info.references], [m.measure('Total').id] * 2)

    def test_function_arguments_respect_strings_and_nested_calls(self):
        info = Resolver(example()).scan('IF(TRUE(), SUMX(Sales, Sales[Amount]), "a,b")')
        call = next(c for c in info.calls if c.name == 'IF')
        self.assertEqual(len(call.arguments), 3)
        self.assertEqual(info.functions, {'IF': 1, 'SUMX': 1, 'TRUE': 1})

    def test_virtual_column_does_not_become_missing_measure(self):
        info = Resolver(example()).scan('SUMX(ADDCOLUMNS(Sales,"@Val",[Total]), [@Val])')
        self.assertEqual(info.virtual_columns, ['@Val'])
        self.assertFalse(info.unresolved)

    def test_alias_collision_is_marked_ambiguous(self):
        m = example()
        info = Resolver(m).scan('SUMX(ADDCOLUMNS(Sales,"Total",1),[Total])')
        self.assertTrue(info.unresolved)
        self.assertIn('ambiguous', info.unresolved[0]['reason'])

    def test_unknown_qualified_column_is_not_invented(self):
        info = Resolver(example()).scan('SUM(Missing[Value])')
        self.assertEqual(info.references, [])
        self.assertEqual(info.unresolved[0]['table'], 'Missing')

    def test_same_name_measure_and_local_column_are_not_guessed(self):
        m = example()
        m.add(Entity('column', 'Sales', 'Total'))
        info = Resolver(m).scan('[Total]', m.measure('Twice'))
        self.assertEqual(info.references, [])
        self.assertEqual(info.unresolved[0]['reason'], 'ambiguous reference')

    def test_malformed_lexical_inputs(self):
        for text in ['"unclosed', '[unclosed', "'unclosed", '/* unclosed']:
            with self.subTest(text=text), self.assertRaises(ModelError):
                tokenize(text)
        for text in ['SUM(', '[Total])']:
            with self.subTest(text=text), self.assertRaises(ModelError):
                Resolver(example()).scan(text)

    def test_deep_comments_and_variables(self):
        info = Resolver(example()).scan('/* outer /* nested */ end */ VAR Sales = 1 RETURN Sales')
        self.assertEqual(info.references, [])
        self.assertEqual(info.variables, ['sales'])


class GraphAndRulesTests(unittest.TestCase):
    def test_dependency_first_order_and_direction(self):
        m = example(); g = DependencyGraph(m)
        order = g.dependency_order(m.measure('Slow').id)
        self.assertLess(order.index(m.measure('Total').id), order.index(m.measure('Twice').id))
        self.assertLess(order.index(m.measure('Twice').id), order.index(m.measure('Slow').id))
        self.assertIn(m.measure('Total').id, g.dependencies[m.measure('Twice').id])

    def test_ranking_and_rule_reasons(self):
        a = analyze(example())
        self.assertEqual(a.ranked[0]['name'], 'Slow')
        self.assertIn('DAX002', [f['rule'] for f in a.ranked[0]['findings']])
        self.assertIn('MODEL001', [f['rule'] for f in a.ranked[0]['findings']])
        self.assertIn('ordinal', a.to_dict()['static_score_definition'].lower())

    def test_repetition_is_not_duplicated_graph_work(self):
        m = example(); g = DependencyGraph(m)
        self.assertEqual(len([n for n in g.dependencies[m.measure('Twice').id] if m.entities[n].kind == 'measure']), 1)
        a = analyze(m); r = next(r for r in a.ranked if r['name'] == 'Twice')
        self.assertEqual(r['transitive_measure_count'], 1)
        self.assertIn('DAX006', [f['rule'] for f in r['findings']])
        edge = next(e for e in g.to_dict(m.measure('Twice').id)['edges'] if e['from'] == m.measure('Twice').id)
        self.assertEqual(edge['textual_occurrences'], 2)

    def test_branch_argument_context_is_exported_without_execution_counts(self):
        m = example(); m.add(Entity('measure', 'Sales', 'Choice', 'IF(TRUE(),[Total],[Twice])'))
        edges = DependencyGraph(m).to_dict(m.measure('Choice').id)['edges']
        edge = next(e for e in edges if e['from'] == m.measure('Choice').id and e['to'] == m.measure('Total').id)
        self.assertIn({'function': 'IF', 'argument': 2}, edge['contexts'][0]['call_arguments'])

    def test_cycle_witness_does_not_claim_dependents_are_cycle_members(self):
        m = Model()
        for n, e in [('A', '[B]'), ('B', '[A]'), ('C', '[A]')]:
            m.add(Entity('measure', 'T', n, e))
        g = DependencyGraph(m)
        self.assertFalse(g.to_dict()['is_acyclic'])
        self.assertNotIn(m.measure('C').id, {n for c in g.cycles for n in c})
        with self.assertRaises(CycleError):
            g.dependency_order()
        a = analyze(m)
        self.assertTrue(all(x['risk_score'] is None for x in a.ranked))

    def test_unrelated_cycle_does_not_block_valid_root(self):
        m = example(); m.add(Entity('measure', 'Sales', 'Cycle', '[Cycle]'))
        g = DependencyGraph(m)
        self.assertTrue(g.to_dict(m.measure('Total').id)['is_acyclic'])

    def test_missing_definition_is_partial(self):
        m = Model(); m.add(Entity('measure', 'T', 'Unknown')); m.add(Entity('measure', 'T', 'Parent', '[Unknown]'))
        a = analyze(m)
        self.assertTrue(all(not r['chain_complete'] for r in a.ranked))

    def test_invalid_dax_is_reported_not_a_complete_chain(self):
        m = Model(); m.add(Entity('measure', 'T', 'Bad', 'SUM('))
        a = analyze(m)
        self.assertFalse(a.ranked[0]['chain_complete'])
        self.assertTrue(a.warnings)

    def test_measure_filter_does_not_get_boolean_rewrite_advice(self):
        m = example(); m.add(Entity('measure', 'Sales', 'M', 'CALCULATE([Total], FILTER(Customer,[Total]>0))'))
        a = analyze(m); r = next(r for r in a.ranked if r['name'] == 'M')
        rule = next(f for f in r['findings'] if f['rule'] == 'DAX003')
        self.assertIn('cannot simply', rule['suggestion'])
        self.assertNotIn('DAX004', [f['rule'] for f in r['findings']])

    def test_plain_table_filter_and_sumx_candidate(self):
        m = example(); m.add(Entity('measure', 'Sales', 'M', 'CALCULATE(SUMX(Sales,Sales[Amount]),FILTER(Sales,Sales[Amount]>0))'))
        r = next(r for r in analyze(m).ranked if r['name'] == 'M')
        self.assertTrue({'DAX004', 'DAX005'}.issubset({f['rule']for f in r['findings']}))

    def test_filter_in_calculate_expression_is_not_a_filter_argument(self):
        m = example(); m.add(Entity('measure', 'Sales', 'M', 'CALCULATE(SUMX(FILTER(Sales,Sales[Amount]>0),Sales[Amount]))'))
        r = next(r for r in analyze(m).ranked if r['name'] == 'M')
        self.assertNotIn('DAX004', [f['rule'] for f in r['findings']])

    def test_relationship_table_names_resolve_case_insensitively(self):
        m = example(); m.relationships[0].to_table = 'CUSTOMER'; m.relationships[0].from_table = 'sales'
        r = next(r for r in analyze(m).ranked if r['name'] == 'Total')
        self.assertIn('Customer', r['potential_filter_context_tables'])

    def test_time_function_alone_not_declared_heavy(self):
        m = Model(); m.add(Entity('measure', 'T', 'Date', 'TODAY()'))
        a = analyze(m)
        self.assertEqual(a.ranked[0]['risk_score'], 1)
        self.assertEqual(a.ranked[0]['findings'][0]['points'], 0)

    def test_deep_chain_avoids_python_recursion_limit(self):
        m = Model()
        for i in range(1150):
            m.add(Entity('measure', 'T', f'M{i}', '1' if i == 0 else f'[M{i-1}]'))
        g = DependencyGraph(m)
        self.assertEqual(len(g.dependency_order(m.measure('M1149').id)), 1150)
        self.assertEqual(len(g.dependency_ids(m.measure('M1149').id)), 1149)

    def test_function_and_calculation_item_dependencies(self):
        m = parse_model({'model': {'tables': [{'name': 'T', 'measures': [{'name': 'M', 'expression': 'My.Func()'}],
                                               'calculationGroup': {'calculationItems': [{'name': 'Item', 'expression': 'SELECTEDMEASURE()'}]}}],
                                  'functions': [{'name': 'My.Func', 'expression': '() => 1'}]}})
        a = analyze(m); g = a.graph
        self.assertTrue(any(n.startswith('function:')for n in g.dependencies[m.measure('M').id]))
        self.assertTrue(any('Calculation groups' in w for w in a.warnings))

    def test_ids_are_collision_safe(self):
        m = Model(); a = m.add(Entity('measure', 'A/B', 'C')); b = m.add(Entity('measure', 'A', 'B/C'))
        self.assertNotEqual(a.id, b.id)


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text, encoding='utf-8'):
        p = self.root / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text, encoding=encoding); return p

    def test_tmsl_wrapper_and_expression_lines(self):
        m = parse_model({'createOrReplace': {'database': {'model': {'tables': [{'name': 'T', 'measures': [{'name': 'M', 'expression': ['VAR x=1', 'RETURN x']}]}]}}}})
        self.assertEqual(m.measure('M').expression, 'VAR x=1\nRETURN x')

    def test_tmdl_indentation_metadata_not_in_dax(self):
        p = self.write('model.tmdl', "table 'Café Table'\n\tmeasure 'Méasure' =\n\t\t\tVAR x=1\n\t\t\tRETURN x\n\t\tformatString: #,##0\n\tcolumn Amount\n\t\tdataType: double\n\tpartition P = m\n\t\tmode: directQuery\n\t\tsource =\n\t\t\tlet Source=\"password=SECRET\" in Source\n")
        m = load_model(p)
        self.assertEqual(m.measure('Méasure').expression, 'VAR x=1\nRETURN x')
        self.assertEqual(m.tables['Café Table'].storage_modes, {'directquery'})
        self.assertNotIn('SECRET', analyze(m).to_json())

    def test_fenced_dax_keeps_properties_out(self):
        p = self.write('a.tmdl', "table T\n\tmeasure M = ```\n\t\t\tVAR x=1\n\t\t\tRETURN x\n\t\t\t```\n\t\tformatString: 0\n")
        self.assertEqual(load_model(p).measure('M').expression, 'VAR x=1\nRETURN x')

    def test_fence_can_preserve_unusual_indentation(self):
        p = self.write('a.tmdl', 'table T\n\tmeasure M = ```\nVAR x=1\n RETURN x\n```\n')
        self.assertEqual(load_model(p).measure('M').expression, 'VAR x=1\n RETURN x')

    def test_unclosed_fence_errors(self):
        p = self.write('a.tmdl', 'table T\n\tmeasure M = ```\n1\n')
        with self.assertRaises(ModelError):
            load_model(p)

    def test_tmdl_relationships_active_and_cardinality(self):
        p = self.write('a.tmdl', "table Sales\n\tmeasure M = 1\nrelationship R\n\tfromColumn: Sales.'Customer Key'\n\ttoColumn: 'Customer.A'.Key\n\tisActive: false\n\tcrossFilteringBehavior: bothDirections\n")
        r = load_model(p).relationships[0]
        self.assertEqual((r.from_table, r.from_column, r.to_table), ('Sales', 'Customer Key', 'Customer.A'))
        self.assertFalse(r.active)

    def test_calculated_column_table_and_format_string(self):
        p = self.write('a.tmdl', "table T\n\tcolumn A = 1\n\tmeasure M = SUM(T[A])\n\t\tformatStringDefinition\n\t\t\texpression = \"0.0\"\n\tpartition P = calculated\n\t\tmode: import\n\t\tsource = {1,2}\n")
        m = load_model(p); g = DependencyGraph(m)
        self.assertEqual(m.entities[m.tables['T'].id].expression, '{1,2}')
        self.assertTrue(any(m.entities[n].kind == 'format_string'for n in g.dependencies[m.measure('M').id]))

    def test_utf16_and_utf8_bom(self):
        for encoding in ['utf-16', 'utf-8-sig']:
            p = self.write('a.bim', '{"model":{"tables":[]}}', encoding)
            self.assertEqual(load_model(p).source_format, 'bim/tmsl')

    def test_pbip_directory_and_ambiguous_models(self):
        self.write('Project.pbip', '{}')
        self.write('Sample.SemanticModel/definition/tables/T.tmdl', 'table T\n\tmeasure M = 1\n')
        self.assertEqual(len(load_model(self.root / 'Project.pbip').measures), 1)
        (self.root / 'Other.SemanticModel').mkdir()
        with self.assertRaises(ModelError):
            load_model(self.root)

    def test_inline_fabric_bim_and_tmdl(self):
        for path, text in [('model.bim', '{"model":{"tables":[]}}'), ('definition/tables/T.tmdl', 'table T\n\tmeasure M = 1')]:
            data = {'definition': {'parts': [{'path': path, 'payload': base64.b64encode(text.encode()).decode(), 'payloadType': 'InlineBase64'}]}}
            self.assertIsInstance(parse_model(data), Model)

    def test_fabric_mixed_or_invalid_payload_rejected(self):
        with self.assertRaises(ModelError):
            parse_model({'parts': [{'path': 'model.bim', 'payload': '@@not base64'}]})

    def test_partial_catalog_preserves_unknowns(self):
        m = parse_model({'measures': [{'name': 'A', 'expression': '[B]'}, {'name': 'B', 'definition_status': 'missing'}]})
        self.assertFalse(analyze(m).ranked[0]['chain_complete'])

    def test_partial_catalog_uses_a_generic_default_table(self):
        m = parse_model({'measures': [{'name': 'Total', 'expression': '1'}]})
        self.assertEqual(m.measure('Total').table, 'Measures')

    def test_bim_and_tmdl_resolve_the_same_edges(self):
        raw = {'model': {'tables': [{'name': 'T', 'columns': [{'name': 'Amount'}],
                                     'partitions': [{'mode': 'directQuery'}],
                                     'measures': [{'name': 'Base', 'expression': 'SUM(T[Amount])'},
                                                  {'name': 'Parent', 'expression': '[Base] + [Base]'}]}]}}
        p = self.write('a.tmdl', 'table T\n\tcolumn Amount\n\tmeasure Base = SUM(T[Amount])\n\tmeasure Parent = [Base] + [Base]\n\tpartition P = m\n\t\tmode: directQuery\n')
        a, b = analyze(raw), analyze(load_model(p))
        self.assertEqual(a.graph.dependencies, b.graph.dependencies)
        self.assertEqual([(r['id'], r['risk_score'])for r in a.ranked], [(r['id'], r['risk_score'])for r in b.ranked])

    def test_malformed_metadata_raises_public_error(self):
        with self.assertRaises(ModelError):
            parse_model({'model': {'tables': [{'measures': []}]}})

    def test_duplicate_case_insensitive_names_rejected(self):
        with self.assertRaises(ModelError):
            parse_model({'model': {'tables': [{'name': 'T', 'measures': [{'name': 'A'}, {'name': 'a'}]}]}})
        with self.assertRaises(ModelError):
            parse_model({'model': {'tables': [{'name': 'T', 'measures': [{'name': 'Méasure'}, {'name': 'MÉASURE'}]}]}})

    def test_multiple_tmsl_models_rejected(self):
        with self.assertRaises(ModelError):
            parse_model({'sequence': {'operations': [{'create': {'database': {'model': {'tables': []}}}}, {'create': {'database': {'model': {'tables': []}}}}]}})

    def test_binary_missing_and_oversize_errors(self):
        p = self.write('model.pbix', 'not a model')
        with self.assertRaisesRegex(ModelError, 'PBIX'):
            load_model(p)
        with self.assertRaisesRegex(ModelError, 'does not exist'):
            load_model(self.root / 'missing')
        p = self.write('a.bim', '{"model":{"tables":[]}}')
        with self.assertRaisesRegex(ModelError, 'max_file_bytes'):
            load_model(p, max_file_bytes=1)


def telemetry():
    def event(op, name, text=None, time='2025-01-01T00:00:01Z'):
        return {'ItemId': 'DATASET', 'OperationId': op, 'OperationName': name, 'EventText': text, 'Timestamp': time}
    rows = [event('Q1', 'QueryBegin', 'EVALUATE ROW("a",[Slow],"b",[Total])'), event('Q1', 'QueryEnd'),
            event('Q1', 'ExecutionMetrics', json.dumps({'totalCpuTimeMs': 100, 'durationMs': 200})),
            event('Q1', 'DirectQueryEnd', 'SELECT ...'),
            event('Q2', 'QueryBegin', 'EVALUATE ROW("x",[Total])', '2025-01-01T00:01:00Z'), event('Q2', 'QueryEnd'),
            event('Q2', 'ExecutionMetrics', json.dumps({'totalCpuTimeMs': 0, 'durationMs': 1}))]
    costs = [{'EventId': 'E1', 'ItemId': 'DATASET', 'OperationId': 'Q1', 'OperationName': 'Query', 'CUSeconds': 1.0},
             {'EventId': 'E2', 'ItemId': 'DATASET', 'OperationId': 'Q1', 'OperationName': 'Query', 'CUSeconds': .6}]
    return rows, costs


class RuntimeTests(unittest.TestCase):
    def test_fragments_dedup_and_unknown_cost(self):
        rows, costs = telemetry(); p = RuntimeProfile.from_logs(example(), rows, costs + costs)
        self.assertEqual(p.queries[0]['recorded_cu_seconds'], 1.6)
        self.assertIsNone(p.queries[1]['recorded_cu_seconds'])
        a = analyze(example(), profile=p)
        total = next(x for x in a.ranked if x['name'] == 'Total')['runtime']
        self.assertEqual(total['query_cu_seconds_context'], 1.6)
        self.assertEqual(total['cu_coverage'], 1)
        self.assertEqual(total['potential_chain_query_contexts'], 2)

    def test_shared_query_cost_not_split_or_duplicated_in_raw_queries(self):
        rows, costs = telemetry(); m = example(); p = RuntimeProfile.from_logs(m, rows, costs); metrics = p.metrics(DependencyGraph(m))
        self.assertEqual(sum(q['recorded_cu_seconds']or 0 for q in p.queries), 1.6)
        self.assertEqual(metrics[m.measure('Slow').id]['query_cu_seconds_context'], 1.6)
        self.assertEqual(metrics[m.measure('Twice').id]['direct_query_contexts'], 0)
        self.assertEqual(metrics[m.measure('Twice').id]['potential_chain_query_contexts'], 1)
        self.assertIn('Not an isolated', metrics[m.measure('Total').id]['interpretation'])

    def test_query_scope_override_skipped(self):
        rows, costs = telemetry(); rows[0]['EventText'] = 'DEFINE MEASURE Sales[Total]=999 EVALUATE ROW("v",[Total])'
        p = RuntimeProfile.from_logs(example(), rows, costs)
        self.assertEqual(len(p.queries), 1)
        self.assertTrue(p.diagnostics)

    def test_dataset_ambiguity_and_time_window(self):
        rows, costs = telemetry(); rows.append(dict(rows[0], ItemId='OTHER'))
        with self.assertRaises(ModelError):
            RuntimeProfile.from_logs(example(), rows, costs)
        p = RuntimeProfile.from_logs(example(), rows, costs, dataset_id='DATASET', end='2025-01-01T00:01:00Z')
        self.assertEqual(len(p.queries), 1)

    def test_conflicting_cost_event_and_missing_id_rejected(self):
        rows, costs = telemetry()
        with self.assertRaises(ModelError):
            RuntimeProfile.from_logs(example(), rows, costs + [dict(costs[0], CUSeconds=90)])
        del costs[0]['EventId']
        with self.assertRaises(ModelError):
            RuntimeProfile.from_logs(example(), rows, costs)

    def test_no_cpu_imputation_from_duration(self):
        rows, costs = telemetry()
        rows = [x for x in rows if x['OperationName'] != 'ExecutionMetrics']
        p = RuntimeProfile.from_logs(example(), rows, costs)
        self.assertTrue(all(q['cpu_ms'] is None for q in p.queries))

    def test_truncated_query_is_skipped_with_a_diagnostic(self):
        rows, costs = telemetry(); rows[0]['EventText'] = 'EVALUATE ROW("unterminated'
        p = RuntimeProfile.from_logs(example(), rows, costs)
        self.assertEqual(len(p.queries), 1)
        self.assertTrue(any('could not be analyzed' in x for x in p.diagnostics))

    def test_invalid_units_values_are_not_silently_accepted(self):
        rows, costs = telemetry()
        for value in ['NaN', -1, 'Infinity']:
            with self.subTest(value=value), self.assertRaises(ModelError):
                RuntimeProfile.from_logs(example(), rows, [dict(costs[0], CUSeconds=value)])

    def test_rank_runtime_without_profile_rejected(self):
        with self.assertRaises(ModelError):
            analyze(example(), rank_by='query_cu_context')

    def test_empty_capture_and_naive_time_bounds_rejected(self):
        rows, costs = telemetry()
        with self.assertRaises(ModelError):
            RuntimeProfile.from_logs(example(), [])
        with self.assertRaises(ModelError):
            RuntimeProfile.from_logs(example(), rows, start='2025-01-01T00:00:00')

    def test_missing_semantic_identity_rejected(self):
        rows, costs = telemetry(); del rows[0]['ItemId']
        with self.assertRaises(ModelError):
            RuntimeProfile.from_logs(example(), rows, costs)


class ArtifactTests(unittest.TestCase):
    def test_html_escaping_portable_names_and_no_connection_strings(self):
        m = example(); m.add(Entity('measure', 'Sales', '<script>alert(1)</script>', '"</pre><script>bad()</script>"'))
        with tempfile.TemporaryDirectory() as tmp:
            paths = analyze(m).write(tmp); text = paths['html'].read_text(encoding='utf-8')
            self.assertNotIn('<script>bad()', text)
            self.assertNotIn('secret connection string', text)
            index = json.loads(paths['chain_index'].read_text(encoding='utf-8'))
            self.assertTrue(all((Path(tmp) / p).is_file() for p in index.values()))
            self.assertTrue(all(not set('<>:"\\|?*').intersection(Path(p).name) for p in index.values()))

    def test_cli_round_trip_and_error_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); model = root / 'model.bim'
            model.write_text('{"model":{"tables":[{"name":"T","measures":[{"name":"M","expression":"1"}]}]}}', encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['analyze', str(model), '-o', str(root / 'out')]), 0)
                self.assertEqual(main(['chain', str(model), 'M', '--format', 'json', '-o', str(root / 'dag.json')]), 0)
                self.assertEqual(main(['chain', str(model), 'M', '--format', 'dot']), 0)
            self.assertEqual(json.loads((root / 'dag.json').read_text())['nodes'][0]['type'], 'measure')
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['analyze', str(root / 'absent')]), 2)


if __name__ == '__main__':
    unittest.main()
