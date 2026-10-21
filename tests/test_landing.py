"""A matrix provides optional context; it never broadens the selected root."""
import copy
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from contextlib import redirect_stdout,redirect_stderr
from semantic_hotspots.cli import main

from semantic_hotspots import analyze, export_project, parse_model, review_measure, ModelError
from semantic_hotspots.focus import write_measure_proposal
from test_matrices import model, matrix


def source():
    value=model()
    value['model']['tables'][0]['measures'].append({'name':'Iterator','expression':'SUMX(Sales, Sales[Amount])'})
    return value


class LandingVisualTests(unittest.TestCase):
    def test_optional_visual_scopes_only_values_and_preserves_the_contract(self):
        data=source();spec=matrix(filters=[{'table':'Customer','column':'Region','values':['North']}],
                                  row_subtotals=False,grand_total=False)
        frozen=copy.deepcopy((data,spec))
        plain=review_measure(data,'Iterator');self.assertIsNone(plain.data['landing_visual'])
        review=review_measure(data,'Iterator',landing_matrix=spec).data
        context=review['landing_visual'];self.assertEqual(context['spec']['values'],[['Sales','Iterator']])
        self.assertEqual(context['original_values'],spec['values'])
        for key in ['rows','columns','filters','row_subtotals','grand_total']:
            self.assertEqual(context['spec'][key],spec[key])
        self.assertTrue(context['workloads']);self.assertTrue(review['candidates'])
        for w in context['workloads']:
            self.assertEqual(w['measure_ids'],[review['measure_id']]);self.assertIn('TREATAS',w['query'])
            self.assertIn('North',w['query']);self.assertNotIn('[Margin]',w['query'])
            self.assertTrue(w['group_by'])
        self.assertFalse(context['summary_export']['eligible']) # Filtered PBIR export remains guarded.
        self.assertEqual((data,spec),frozen)

    def test_downloaded_context_queries_preserve_grouping_filters_and_one_pair(self):
        review=review_measure(source(),'Iterator',landing_matrix=matrix(filters=[{'table':'Customer','column':'Region','values':['North']}]))
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'review';review.write(root)
            c=review.data['candidates'][0];folder=root/'candidates'/c['id']
            with zipfile.ZipFile(folder/'landing-queries.zip') as archive:
                index=json.loads(archive.read('contexts.json'))
                self.assertEqual(len(index['cases']),len(review.data['landing_visual']['workloads']))
                for case in index['cases']:
                    query=archive.read(case['queries']['validation']).decode('utf-8')
                    self.assertIn('ISBLANK',query);self.assertIn('SUMMARIZECOLUMNS',query)
                    self.assertIn('TREATAS',query);self.assertIn('North',query)
                    self.assertNotIn('[Margin]',query)
            self.assertTrue((root/'landing-visual.json').is_file())

    def test_incomplete_or_unsupported_context_does_not_block_measure_proposals(self):
        for changes in [{'context_complete':False},{'unsupported':['Unknown visual behavior']},
                        {'filter_measures':[['Sales','Revenue']]}]:
            with self.subTest(changes=changes):
                review=review_measure(source(),'Iterator',landing_matrix=matrix(**changes)).data
                self.assertTrue(review['candidates'])
                context=review['landing_visual'];self.assertFalse(context['workloads'])
                self.assertFalse(context['summary_export']['eligible'])
                for key,value in changes.items():self.assertEqual(context['spec'][key],value)

    def test_generated_landing_has_one_measure_pair_even_when_original_had_many(self):
        data=source();spec=matrix();before=copy.deepcopy((data,spec))
        with tempfile.TemporaryDirectory() as tmp:
            result=export_project(data,[spec],Path(tmp)/'project',measure='Iterator',table='Sales',config={'fact_rows':12,'dimension_rows':3})
            m=result.manifest;self.assertEqual(m['selected_measure_id'],'measure:Sales/Iterator')
            self.assertEqual(m['spec']['values'],[['Sales','Iterator']]);self.assertEqual(len(m['changes']),1)
            self.assertEqual(m['landing_visual']['original_values'],spec['values'])
            visual=json.loads(next(result.directory.glob('Showcase.Report/definition/pages/*/visuals/*/visual.json')).read_text(encoding='utf-8'))
            self.assertEqual(len(visual['visual']['query']['queryState']['Values']['projections']),2)
            bim=json.loads(result.model_path.read_text(encoding='utf-8'))
            names={m['name'] for t in bim['model']['tables'] for m in t.get('measures',[])}
            self.assertEqual(names,{'Iterator','Candidate Iterator'})
        self.assertEqual((data,spec),before)
        with self.assertRaises(ModelError):export_project(data,[spec,matrix(id='second')],'unused',measure='Iterator')

    def test_manual_cycle_keeps_graph_but_withholds_misleading_alias_probe(self):
        m=parse_model(source());context=review_measure(m,'Iterator',landing_matrix=matrix()).data['landing_visual']
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            files=write_measure_proposal(m,'Iterator','[Iterator] + 1',root/'bad',landing_visual=context)
            self.assertIsNone(files['validation']);self.assertIn('cyclic',files['validation_reason'])
            self.assertFalse((root/'bad/validate-total.dax').exists())
            self.assertFalse(json.loads(files['json'].read_text(encoding='utf-8'))['proposed']['is_acyclic'])
            valid=write_measure_proposal(m,'Iterator','SUM(Sales[Amount])',root/'good',landing_visual=context)
            self.assertTrue(valid['validation'].is_file());self.assertTrue((root/'good/landing-queries.zip').is_file())

    def test_ranking_without_eager_rewrites_preserves_scores_and_dependencies(self):
        m=parse_model(source());full=analyze(m);deferred=analyze(m,include_rewrites=False)
        self.assertEqual(full.ranked,deferred.ranked)
        self.assertEqual(full.graph.to_dict(),deferred.graph.to_dict())
        self.assertIsNone(deferred.rewrite_plan)
        self.assertTrue(review_measure(m,'Iterator',analysis=deferred).data['candidates'])

    def test_cli_requires_an_explicit_optional_visual_and_keeps_one_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'model.bim').write_text(json.dumps(source()),encoding='utf-8')
            (root/'matrices.json').write_text(json.dumps([matrix()]),encoding='utf-8')
            with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                code=main(['optimize-measure',str(root/'model.bim'),'Iterator','--matrices',str(root/'matrices.json'),'--landing-matrix','monthly','-o',str(root/'out')])
                self.assertEqual(code,0)
                self.assertNotEqual(main(['optimize-measure',str(root/'model.bim'),'Iterator','--matrices',str(root/'matrices.json')]),0)
            saved=json.loads((root/'out/measure-review.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['landing_visual']['spec']['values'],[['Sales','Iterator']])
