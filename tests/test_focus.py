"""Independently invented root-only measure selection and proposal fixtures."""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from semantic_hotspots import analyze, parse_model, plan_rewrites, review_measure, ModelError
from semantic_hotspots.cli import main
from semantic_hotspots.focus import write_measure_proposal


def source():
    return {'model':{'tables':[{'name':'Sales','partitions':[{'name':'Rows','mode':'import'}],
        'columns':[{'name':'Amount','dataType':'double'}],
        'measures':[{'name':'Chosen','expression':'SUMX(Sales, Sales[Amount])'},
                    {'name':'Other','expression':'SUMX(Sales, Sales[Amount]) * 2'},
                    {'name':'Helper consumer','expression':'[Chosen] + 1'},
                    {'name':'Opaque','expression':'VAR x = [Chosen] RETURN x'}]}]}}


class FocusTests(unittest.TestCase):
    def test_only_the_selected_root_is_optimized_and_source_is_not_mutated(self):
        data=source();before=copy.deepcopy(data);events=[]
        plan=plan_rewrites(data,measure='Chosen',table='Sales',progress=events.append)
        self.assertEqual([r['measure'] for r in plan.measures],["'Sales'[Chosen]"])
        self.assertEqual(plan.candidates[0]['after'],'SUM(Sales[Amount])')
        self.assertTrue(all(e['total']==1 for e in events if e['stage']=='rewrites'))
        review=review_measure(data,'Chosen',table='Sales')
        self.assertEqual(review.data['scope'],'root-expression-only')
        self.assertTrue(all(c['measure_id']==review.data['measure_id'] for c in review.data['candidates']))
        self.assertIn('measure:Sales/Helper%20consumer',review.data['affected_downstream_measures'])
        self.assertFalse(any(n.get('name')=='Other' for n in review.data['original_dag']['nodes']))
        self.assertEqual(review.data['validation_status'],'not_run')
        self.assertEqual(review.data['performance_status'],'unknown');self.assertEqual(data,before)

    def test_improved_helpers_can_be_specialized_in_one_new_root(self):
        review=review_measure(source(),'Helper consumer')
        self.assertTrue(any(c['chain_specialization'] for c in review.data['candidates']))
        self.assertIn('Chosen',[n.get('name') for n in review.data['original_dag']['nodes']])
        self.assertTrue(review_measure(source(),'Opaque').data['candidates'])
        with tempfile.TemporaryDirectory() as tmp:
            html=review.write(Path(tmp)/'review')['html'].read_text(encoding='utf-8')
            self.assertIn('Full chain and definitions',html)

    def test_cached_analysis_selects_one_measure_without_replanning_and_qualifies_duplicates(self):
        model=parse_model(source());analysis=analyze(model)
        with patch('semantic_hotspots.focus.plan_rewrites',side_effect=AssertionError('Replanned')):
            review=review_measure(model,'Chosen',analysis=analysis)
        self.assertEqual(len(review.data['candidates']),1)
        result=review.to_dict();result['candidates'].clear();self.assertTrue(review.data['candidates'])
        with self.assertRaises(ModelError):review_measure(parse_model(source()),'Chosen',analysis=analysis)
        data=source();data['model']['tables'].append({'name':'Display','measures':[{'name':'Chosen','expression':'1'}]})
        with self.assertRaises(ModelError):review_measure(data,'Chosen')
        self.assertEqual(review_measure(data,'Chosen',table='Display').data['table'],'Display')
        with self.assertRaises(ModelError):plan_rewrites(data,table='Sales')

    def test_exports_only_chosen_proposals_and_query_alias_avoids_collisions(self):
        data=source();first=review_measure(data,'Chosen').data['validation_alias']
        data['model']['tables'][0]['measures'].append({'name':first,'expression':'0'})
        review=review_measure(data,'Chosen');self.assertNotEqual(review.data['validation_alias'],first)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'review';files=review.write(root)
            self.assertEqual(json.loads(files['json'].read_text(encoding='utf-8')),review.to_dict())
            folders=list((root/'candidates').iterdir());self.assertEqual(len(folders),1)
            query=(folders[0]/'validate-total.dax').read_text(encoding='utf-8')
            self.assertTrue(query.startswith('DEFINE MEASURE'))
            self.assertIn(review.data['validation_alias'],query)
            self.assertIn("'Sales'[Chosen]",query);self.assertIn('ISBLANK',query)
            self.assertNotIn("'Sales'[Other]",query)
            page=files['html'].read_text(encoding='utf-8')
            self.assertIn('only this scalar total',page);self.assertIn('all-BLANK',page)
            self.assertTrue((folders[0]/'original.dag.json').is_file())
            self.assertTrue((folders[0]/'proposed.dag.json').is_file())
            with self.assertRaises(ModelError):review.write(root)

    def test_user_authored_proposal_keeps_root_and_cli_is_measure_scoped(self):
        model=parse_model(source());before=[m.expression for m in model.measures]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            files=write_measure_proposal(model,'Chosen','[Other] + 3',root/'custom')
            comparison=json.loads(files['json'].read_text(encoding='utf-8'))
            self.assertEqual(comparison['original']['root'],model.measure('Chosen').id)
            self.assertEqual(comparison['proposed']['root'],model.measure('Chosen').id)
            self.assertEqual((root/'custom/proposed.dax').read_text(encoding='utf-8'),'[Other] + 3')
            (root/'model.bim').write_text(json.dumps(source()),encoding='utf-8')
            with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                self.assertEqual(main(['optimize-measure',str(root/'model.bim'),'Chosen','--table','Sales','-o',str(root/'cli')]),0)
            saved=json.loads((root/'cli/measure-review.json').read_text(encoding='utf-8'))
            self.assertTrue(all(c['measure_id']==model.measure('Chosen').id for c in saved['candidates']))
        self.assertEqual([m.expression for m in model.measures],before)
