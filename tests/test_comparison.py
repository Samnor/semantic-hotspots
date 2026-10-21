"""Independently invented computation-DAG examples; no engine is emulated."""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from semantic_hotspots import (DependencyGraph, ModelError, analyze, compare_measure_dags,
    computation_dag, dag_to_text, export_project, load_model, parse_model, plan_rewrites, write_dag_comparison)
from semantic_hotspots.cli import main
from test_matrices import model as matrix_model, matrix


def sample():
    return {'model': {'tables': [{'name': 'Orders', 'columns': [
        {'name':'Amount','dataType':'double'}, {'name':'Flag','dataType':'int64'}],
        'measures': [{'name':'Leaf','expression':'SUMX(Orders, Orders[Amount])'},
                     {'name':'Left','expression':'[Leaf] + 1'},
                     {'name':'Right','expression':'[Leaf] * 2'},
                     {'name':'Root','expression':'[Left] + [Right]'}]}]}}


def assert_graph(test, graph):
    ids = [n['id'] for n in graph['nodes']]
    test.assertEqual(len(ids), len(set(ids)))
    test.assertTrue(all(e['from'] in ids and e['to'] in ids for e in graph['edges']))
    if graph['is_acyclic']:
        order = {n:i for i,n in enumerate(graph['evaluation_order'])}
        test.assertEqual(set(order), set(ids))
        test.assertTrue(all(order[e['to']] < order[e['from']] for e in graph['edges']))


class DagComparisonTests(unittest.TestCase):
    def test_operator_change_visible_when_object_dependencies_are_identical(self):
        data=sample();frozen=copy.deepcopy(data)
        comparison=compare_measure_dags(data,'Leaf',proposed_expression='SUM(Orders[Amount])')
        before,after=comparison['original'],comparison['proposed']
        self.assertEqual(comparison['difference']['query_operators_original'],{'SUMX':1,'scan':1})
        self.assertEqual(comparison['difference']['query_operators_proposed'],{'SUM':1})
        self.assertTrue(comparison['difference']['changed_node_ids'])
        self.assertEqual(comparison['validation_status'],'not_run')
        self.assertIsNone(comparison['predicted_speedup'])
        for g in [before,after]:assert_graph(self,g)
        self.assertEqual(data,frozen)
        self.assertEqual(comparison,compare_measure_dags(data,'Leaf',proposed_expression='SUM(Orders[Amount])'))

    def test_diamond_keeps_shared_helper_once_and_full_transitive_definitions(self):
        m=parse_model(sample());g=computation_dag(m,'Root')
        leaf=m.measure('Leaf').id
        self.assertEqual(sum(n['id']==leaf for n in g['nodes']),1)
        self.assertEqual(sum(e['to']==leaf for e in g['edges']),2)
        self.assertEqual(next(n for n in g['nodes'] if n['id']==leaf)['expression'],'SUMX(Orders, Orders[Amount])')
        text=dag_to_text(g);self.assertIn('argument 1',text);self.assertIn('not an engine execution schedule',text)
        assert_graph(self,g)

    def test_override_adds_a_new_transitive_branch_without_mutating_source(self):
        m=parse_model(sample());g=DependencyGraph(m)
        result=compare_measure_dags(m,'Leaf',proposed_expression='[Left]',graph=g)
        self.assertFalse(result['proposed']['is_acyclic'])
        self.assertFalse(result['proposed']['dependency_complete'])
        self.assertIsNone(result['proposed']['evaluation_order'])
        self.assertTrue(g.to_dict(m.measure('Leaf').id)['is_acyclic'])
        self.assertEqual(m.measure('Leaf').expression,'SUMX(Orders, Orders[Amount])')
        result=compare_measure_dags(m,'Left',proposed_expression='[Right] + 3',graph=g)
        self.assertIn(m.measure('Right').id,{n['id'] for n in result['proposed']['nodes']})
        assert_graph(self,result['proposed'])

    def test_opaque_and_missing_dependencies_remain_unknown(self):
        data=sample();data['model']['tables'][0]['measures'] += [
            {'name':'Unknown','expression':'VAR x = [Missing] RETURN x + [Leaf]'},
            {'name':'Absent'}]
        dag=computation_dag(data,'Unknown')
        self.assertFalse(dag['operator_complete']);self.assertFalse(dag['dependency_complete'])
        self.assertTrue(dag['unresolved'])
        self.assertTrue(any(n.get('operation')=='opaque' for n in dag['nodes']))
        self.assertTrue(any(n['name']=='Leaf' for n in dag['nodes'] if n['type']=='measure'))
        missing=computation_dag(data,'Absent');self.assertTrue(missing['missing_definitions'])
        self.assertFalse(missing['dependency_complete'])

    def test_syntax_only_calls_are_not_invented_or_claimed_typed(self):
        data=sample();data['model']['tables'][0]['measures'][0]['expression']='DIVIDE(SUM(Orders[Amount]), 2)'
        g=computation_dag(data,'Leaf');assert_graph(self,g)
        self.assertTrue(g['operator_complete']);self.assertFalse(g['logical_subset_validated'])
        divide=next(n for n in g['nodes'] if n.get('operation')=='DIVIDE')
        self.assertFalse(divide['supported']);self.assertTrue(g['diagnostics'])

    def test_summary_retains_refresh_work_and_uses_real_generated_definitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'project'
            result=export_project(matrix_model(),[matrix()],root,config={'fact_rows':24,'dimension_rows':3})
            entry=next(c for c in result.manifest['dag_comparisons'] if c['measure_id'].endswith('/Margin'))
            comparison=json.loads((root/entry['directory']/'comparison.json').read_text(encoding="utf-8"))
            old,new=comparison['original'],comparison['proposed'];query=set(new['query_time_node_ids'])
            self.assertTrue(any(n['name']=='Revenue' for n in old['nodes'] if n['type']=='measure'))
            self.assertFalse(any(n['name']=='Revenue' for n in new['nodes'] if n['type']=='measure'))
            summary=next(n for n in new['nodes'] if n['type']=='table' and n['name']=='' and n['table']==result.manifest['summary_table'])
            self.assertTrue(summary['materialized']);self.assertIn(summary['id'],query)
            build=next(n for n in new['nodes'] if n.get('operation')=='SUMMARIZE')
            self.assertNotIn(build['id'],query);self.assertEqual(build['definition_phase'],'refresh')
            self.assertIn(build['id'],new['refresh_time_node_ids'])
            self.assertEqual(comparison['difference']['refresh_operators_proposed']['SUMMARIZE'],1)
            self.assertNotIn('SUMMARIZE',comparison['difference']['query_operators_proposed'])
            self.assertIn('CustomerKey',summary['expression']);self.assertIn('DateKey',summary['expression'])
            self.assertTrue(any(n['table']=='Sales' and n['name']=='Amount' for n in new['nodes'] if n['type']=='column'))
            loaded=load_model(result.model_path)
            for key in ['original','proposed']:assert_graph(self,comparison[key])
            root_node=next(n for n in new['nodes'] if n['id']==new['root'])
            self.assertEqual(root_node['expression'],loaded.measure('Candidate Margin','Sales').expression)
            self.assertEqual(comparison['context']['summary_grain'],result.manifest['grain'])
            self.assertIn('refresh-time',(root/'showcase.html').read_text(encoding="utf-8").lower())
            self.assertTrue((root/entry['directory']/'original.txt').is_file())

    def test_calculated_column_is_a_materialization_boundary(self):
        data=sample();data['model']['tables'][0]['columns'].append({'name':'Double','dataType':'double','expression':'Orders[Amount] * 2'})
        data['model']['tables'][0]['measures'][0]['expression']='SUM(Orders[Double])'
        g=computation_dag(data,'Leaf');q=set(g['query_time_node_ids'])
        column=next(n for n in g['nodes'] if n['type']=='column' and n['name']=='Double')
        self.assertIn(column['id'],q);self.assertTrue(column['materialized'])
        self.assertTrue(any(n.get('operation')=='binary' and n['id'] not in q for n in g['nodes']))

    def test_safe_html_unicode_dot_and_complete_json_files(self):
        data=sample();name='Vä<unsafe>"'
        data['model']['tables'][0]['measures'][0]['name']=name
        c=compare_measure_dags(data,name,proposed_expression='SUM(Orders[Amount])')
        with tempfile.TemporaryDirectory() as tmp:
            files=write_dag_comparison(c,tmp);root=Path(tmp)
            page=files['html'].read_text(encoding="utf-8");self.assertIn('Vä&lt;unsafe&gt;&quot;',page)
            self.assertNotIn('<unsafe>',page);self.assertNotIn('<script',page)
            self.assertIn('Vä',(root/'original.dot').read_text(encoding="utf-8"))
            self.assertEqual(json.loads(files['json'].read_text(encoding="utf-8")),c)
            self.assertEqual(json.loads((root/'original.dag.json').read_text(encoding="utf-8")),c['original'])
            self.assertEqual(json.loads((root/'proposed.dag.json').read_text(encoding="utf-8")),c['proposed'])
            self.assertIn('Original measure DAG',page);self.assertIn('Proposed measure DAG',page)
            self.assertIn('Opaque',json.dumps(compare_measure_dags(data,name,proposed_expression='VAR x = 1 RETURN x')))

    def test_analysis_and_standalone_rewrite_reports_export_proposal_dags(self):
        for plan in [analyze(sample()),plan_rewrites(sample())]:
            with tempfile.TemporaryDirectory() as tmp:
                files=plan.write(tmp);report=files['html'].read_text(encoding="utf-8")
                self.assertIn('Original and proposed measure DAGs',report)
                candidates=plan.rewrite_plan.candidates if hasattr(plan,'rewrite_plan') else plan.candidates
                for c in candidates:
                    folder=Path(tmp)/'dag-comparisons'/c['id']
                    self.assertTrue((folder/'comparison.json').is_file())
                    self.assertIn('dag_comparison',c)

    def test_comparison_inputs_and_cli(self):
        for kwargs in [{},{'proposed_expression':'1','proposed_source':sample()},
                       {'proposed_expression':''},{'proposed_expression':'1','proposed_measure':'New'}]:
            with self.assertRaises(ModelError):compare_measure_dags(sample(),'Leaf',**kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'model.bim';source.write_text(json.dumps(sample()));expr=root/'new.dax';expr.write_text('SUM(Orders[Amount])')
            with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                self.assertEqual(main(['compare-dags',str(source),'Leaf','--expression-file',str(expr),'-o',str(root/'out')]),0)
            self.assertTrue((root/'out/report.html').is_file())
            other=sample();other['model']['tables'][0]['measures'][0]['name']='Alternative';other_path=root/'other.bim';other_path.write_text(json.dumps(other))
            with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                self.assertEqual(main(['compare-dags',str(source),'Leaf','--candidate-model',str(other_path),'--candidate-measure','Alternative','--candidate-table','Orders','-o',str(root/'other')]),0)
            self.assertIn('Alternative',(root/'other/proposed.txt').read_text(encoding="utf-8"))

    def test_large_graph_still_exports_every_node_without_svg(self):
        data=sample();measures=data['model']['tables'][0]['measures']
        for i in range(90):measures.append({'name':'M'+str(i),'expression':'[Leaf] + '+str(i) if i==0 else '[M'+str(i-1)+'] + 1'})
        c=compare_measure_dags(data,'M89',proposed_expression='[M88] + 2')
        with tempfile.TemporaryDirectory() as tmp:
            page=write_dag_comparison(c,tmp)['html'].read_text(encoding="utf-8")
            self.assertIn('Diagram omitted above 80 nodes',page)
            self.assertIn('M0',page);self.assertIn('M89',page)
            self.assertEqual(len(json.loads((Path(tmp)/'original.dag.json').read_text(encoding="utf-8"))['nodes']),len(c['original']['nodes']))
