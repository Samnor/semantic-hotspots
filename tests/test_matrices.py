"""Fictional matrix contracts; no private report fixtures belong in this file."""
from __future__ import annotations
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout,redirect_stderr
from pathlib import Path

from semantic_hotspots import (MatrixSpec,ModelError,analyze_matrices,load_matrices,parse_model,
                               simulate,collect_benchmarks)
from semantic_hotspots.cli import main


def model():
    return {'name':'Fictional matrix lab','model':{'tables':[
        {'name':'Sales','columns':[{'name':c,'dataType':t} for c,t in [('CustomerKey','int64'),('DateKey','int64'),('OrderKey','int64'),('Amount','double'),('Cost','double')]],
         'partitions':[{'name':'Sales','mode':'directQuery'}],
         'measures':[{'name':'Revenue','expression':'SUM(Sales[Amount])'},
                     {'name':'Cost total','expression':'SUM(Sales[Cost])'},
                     {'name':'Margin','expression':'DIVIDE([Revenue]-[Cost total], [Revenue])'},
                     {'name':'Average amount','expression':'AVERAGE(Sales[Amount])'},
                     {'name':'Customers','expression':'DISTINCTCOUNT(Sales[CustomerKey])'},
                     {'name':'Order calculation','expression':'SUMX(VALUES(Sales[OrderKey]), CALCULATE([Revenue]))'}]},
        {'name':'Customer','columns':[{'name':'Key','dataType':'int64'},{'name':'Region','dataType':'string'}]},
        {'name':'Calendar','columns':[{'name':'Key','dataType':'int64'},{'name':'Month','dataType':'string'},{'name':'Day','dataType':'dateTime'}]}],
        'relationships':[{'name':'CustomerSales','fromTable':'Sales','fromColumn':'CustomerKey','toTable':'Customer','toColumn':'Key'},
                         {'name':'DateSales','fromTable':'Sales','fromColumn':'DateKey','toTable':'Calendar','toColumn':'Key'}]}}


def matrix(**changes):
    value={'id':'monthly','title':'Monthly revenue and margin','rows':[['Customer','Region'],['Customer','Key']],
           'columns':[['Calendar','Month']],'values':[['Sales','Revenue'],['Sales','Margin']],
           'context_complete':True}
    value.update(changes);return value


class MatrixGrainTests(unittest.TestCase):
    def test_relationship_preserving_and_exploratory_grains_are_distinct(self):
        result=analyze_matrices(model(),[matrix(retain=[['Calendar','Day']])])
        row=result.matrices[0];candidate=row['grain_advice']['candidates'][0]
        self.assertEqual(candidate['status'],'guarded_candidate_pending_engine_validation')
        self.assertEqual(candidate['relationship_preserving_grain'],[['Sales','CustomerKey'],['Sales','DateKey']])
        self.assertIn(['Calendar','Day'],candidate['coarser_matrix_grain'])
        self.assertIn('requires',candidate['coarser_grain_note'])
        self.assertEqual({s['expression'] for s in candidate['component_states']},{"SUM('Sales'[Amount])","SUM('Sales'[Cost])"})
        self.assertIsNone(candidate['predicted_speedup'])
        self.assertIsNone(candidate['grouped_rows'])
        self.assertIn('SUMMARIZE',candidate['group_count_probe'])
        self.assertIn('after', ' '.join(candidate['rollup_instructions']))
        self.assertIn(parse_model(model()).measure('Revenue').id,row['shared_measure_ids'])

    def test_average_uses_sum_and_nonblank_count_and_distinct_keys_survive(self):
        result=analyze_matrices(model(),[matrix(values=[['Sales','Average amount'],['Sales','Customers']])])
        candidate=result.matrices[0]['grain_advice']['candidates'][0]
        self.assertEqual({s['function'] for s in candidate['component_states']},{'SUM','COUNT','DISTINCTCOUNT'})
        self.assertIn(['Sales','CustomerKey'],candidate['relationship_preserving_grain'])
        text=' '.join(candidate['rollup_instructions'])
        self.assertIn('nonblank',text)
        self.assertIn('never sum subgroup distinct counts',text)
        self.assertIn('never average subgroup averages',text)

    def test_iterator_keys_are_retained_before_opaque_row_calculations(self):
        result=analyze_matrices(model(),[matrix(values=[['Sales','Order calculation']])])
        advice=result.matrices[0]['grain_advice'];candidate=advice['candidates'][0]
        self.assertEqual(candidate['status'],'needs_review')
        self.assertIsNone(candidate['group_count_probe'])
        self.assertIn(['Sales','OrderKey'],candidate['relationship_preserving_grain'])
        self.assertTrue(any(r['function']=='SUMX' and 'OrderKey' in r['input'] for r in advice['iterator_review']))

    def test_unknown_context_security_and_measure_filters_block_safe_claims(self):
        for spec in [matrix(context_complete=False),matrix(filter_measures=[['Sales','Revenue']]),matrix(unsupported=['TopN not modeled'])]:
            candidate=analyze_matrices(model(),[spec]).matrices[0]['grain_advice']['candidates'][0]
            self.assertEqual(candidate['status'],'needs_review')
            self.assertIsNone(candidate['group_count_probe'])
        data=model();data['model']['roles']=[{'name':'Example role'}]
        self.assertIsNone(analyze_matrices(data,[matrix()]).matrices[0]['grain_advice']['candidates'][0]['group_count_probe'])
        result=analyze_matrices(model(),[matrix(filter_measures=[['Sales','Revenue']])])
        self.assertFalse(result.workloads)
        self.assertTrue(result.matrices[0]['diagnostics'])

    def test_disconnected_ambiguous_inactive_and_complex_paths_are_review_only(self):
        for mode in ['missing','inactive','both','ambiguous']:
            data=model()
            if mode=='missing':data['model']['relationships'].pop(0)
            if mode=='inactive':data['model']['relationships'][0]['isActive']=False
            if mode=='both':data['model']['relationships'][0]['crossFilteringBehavior']='bothDirections'
            if mode=='ambiguous':
                extra=copy.deepcopy(data['model']['relationships'][0]);extra['name']='SecondPath'
                data['model']['relationships'].append(extra)
            with self.subTest(mode=mode):
                candidate=analyze_matrices(data,[matrix()]).matrices[0]['grain_advice']['candidates'][0]
                self.assertEqual(candidate['status'],'needs_review')
                self.assertIsNone(candidate['group_count_probe'])

    def test_unknown_fields_and_missing_definitions_are_reported_not_invented(self):
        result=analyze_matrices(model(),[matrix(rows=[['Customer','Missing']],values=[['Sales','Missing']])])
        self.assertFalse(result.workloads)
        self.assertFalse(result.matrices[0]['chain_complete'])
        self.assertGreaterEqual(len(result.matrices[0]['diagnostics']),2)
        data=model();data['model']['tables'][0]['measures'][0].pop('expression')
        result=analyze_matrices(data,[matrix()])
        self.assertFalse(result.workloads)
        self.assertFalse(result.matrices[0]['chain_complete'])

    def test_multiple_facts_do_not_produce_an_unvalidated_join_recipe(self):
        data=model();data['model']['tables'].append({'name':'Budget','columns':[{'name':'Amount','dataType':'double'}],
            'measures':[{'name':'Budget amount','expression':'SUM(Budget[Amount])'}]})
        advice=analyze_matrices(data,[matrix(values=[['Sales','Revenue'],['Budget','Budget amount']])]).matrices[0]['grain_advice']
        self.assertEqual(len(advice['candidates']),2)
        self.assertTrue(any('Multiple fact' in text for text in advice['blockers']))
        self.assertTrue(all(c['group_count_probe'] is None for c in advice['candidates']))

    def test_static_priority_uses_maximum_chain_priority_not_repeated_values(self):
        result=analyze_matrices(model(),[matrix()])
        row=result.matrices[0]
        self.assertEqual(row['priority'],max(v['risk_score'] for v in row['measure_findings']))
        self.assertEqual(result.rank_by,'maximum_known_measure_review_priority')


class MatrixWorkloadTests(unittest.TestCase):
    def test_all_values_share_each_query_and_totals_use_recomputed_measures(self):
        result=analyze_matrices(model(),[matrix()])
        self.assertEqual(len(result.workloads),6)
        self.assertEqual({(w['row_depth'],w['column_depth']) for w in result.workloads},{(r,c) for r in range(3) for c in range(2)})
        for workload in result.workloads:
            self.assertIn("'Sales'[Revenue]",workload['query'])
            self.assertIn("'Sales'[Margin]",workload['query'])
            self.assertNotIn('SUM([Margin])',workload['query'])
        total=next(w for w in result.workloads if w['row_depth']==w['column_depth']==0)
        self.assertIn('ROW(',total['query']);self.assertFalse(total['group_by'])

    def test_depth_limits_total_toggles_and_dates_filters_are_explicit(self):
        spec=matrix(grand_total=False,row_subtotals=False,column_subtotals=False,
                    drill_states=[{'name':'collapsed','row_depth':1,'column_depth':1}],
                    filters=[{'table':'Calendar','column':'Day','values':['2025-01-02']}])
        result=analyze_matrices(model(),[spec])
        self.assertEqual(len(result.workloads),1)
        self.assertEqual(result.workloads[0]['row_depth'],1)
        self.assertIn('DATE(2025, 1, 2)',result.workloads[0]['query'])
        self.assertNotIn("'Customer'[Key]",result.workloads[0]['query'])

    def test_workload_id_changes_when_definition_filter_or_drill_changes(self):
        base=analyze_matrices(model(),[matrix()]).workloads[0]['id']
        data=model();data['model']['tables'][0]['measures'][0]['expression']='SUM(Sales[Amount])+1'
        self.assertNotEqual(base,analyze_matrices(data,[matrix()]).workloads[0]['id'])
        self.assertNotEqual(base,analyze_matrices(model(),[matrix(context_complete=False)]).workloads[0]['id'])
        parsed=parse_model(model())
        column=next(e for e in parsed.entities.values() if e.kind=='column' and e.name=='Amount')
        column.metadata={'DATATYPE':'decimal'}
        self.assertNotEqual(base,analyze_matrices(parsed,[matrix()]).workloads[0]['id'])

    def test_invalid_contracts_fail_cleanly(self):
        for spec in [{},matrix(unknown=True),matrix(rows=[['Customer','Region'],['customer','region']]),
                     matrix(drill_states=[{'name':'x','row_depth':True}]),
                     matrix(drill_states=[{'name':'x','filters':None}]),
                     matrix(drill_states=[{'name':'x','row_depth':10}]),matrix(values=['Revenue']),
                     matrix(context_complete='yes'),matrix(filters=[{'table':'Customer','column':'Region','values':[]}]),
                     matrix(filters=[{'table':'Customer','column':'Region','values':['A']}],
                            drill_states=[{'name':'same','filters':[{'table':'Customer','column':'Region','values':['B']}]}])]:
            with self.subTest(spec=spec),self.assertRaises(ModelError):load_matrices([spec])
        with self.assertRaises(ModelError):load_matrices([matrix(),matrix()])

    def test_oversized_context_expansion_is_rejected_without_partial_writes(self):
        states=[{'name':str(i)} for i in range(44)]
        with self.assertRaises(ModelError):analyze_matrices(model(),[matrix(drill_states=states)])

    def test_simulation_adds_matrices_and_benchmark_links_match_report_anchors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            bundle=simulate(model(),root/'lab',config={'fact_rows':16,'dimension_rows':4,'top':1,'matrices':[matrix()]})
            matrix_cases=[w for w in bundle.manifest['workloads'] if w.get('matrix_id')]
            self.assertEqual(len(matrix_cases),6)
            self.assertTrue((bundle.directory/'matrix-analysis/report.html').is_file())
            output=collect_benchmarks(bundle,root/'not-run').write(root/'results')
            page=output['html'].read_text(encoding='utf-8')
            self.assertIn('matrix-analysis/report.html#matrix-',page)
            self.assertNotIn('measure-'+matrix_cases[0]['measure_id'],page)

    def test_attached_benchmarks_rank_known_zero_before_unknown_without_summing_contexts(self):
        result=analyze_matrices(model(),[matrix(id='a'),matrix(id='b')])
        case=next(w for w in result.workloads if w['matrix_id']=='b')
        capture={'kind':'synthetic-engine-benchmarks','cases':[dict(case,phases={'warm':{'mean_ms':0,'successes':1,'attempts':3}})]}
        result.attach_benchmarks(capture)
        self.assertEqual(result.matrices[0]['id'],'b')
        self.assertEqual(result.matrices[0]['highest_observed_mean_ms'],0)
        self.assertIsNone(result.matrices[1]['highest_observed_mean_ms'])
        self.assertEqual(result.matrices[0]['benchmark_evidence'][0]['attempts'],3)
        self.assertIn('not synthetic CSV',result.benchmark_context['dataset_verification'])
        capture['cases'][0]['query']='wrong'
        with self.assertRaises(ModelError):result.attach_benchmarks(capture)

    def test_html_and_json_escape_untrusted_names_and_include_chain_definitions(self):
        result=analyze_matrices(model(),[matrix(title='<script>alert(1)</script>',values=[['Sales','Margin']])])
        with tempfile.TemporaryDirectory() as tmp:
            files=result.write(tmp);page=files['html'].read_text(encoding='utf-8')
            self.assertIn('&lt;script&gt;',page)
            self.assertNotIn('<script>alert(1)',page)
            self.assertIn('DIVIDE',page)
            self.assertIn('SUM(Sales[Cost])',page)
            self.assertIn('Unique dependency edges',page)
            self.assertIn('relationship_preserving_grain',files['json'].read_text(encoding='utf-8'))
            self.assertTrue(all((Path(tmp)/w['query_file']).is_file() for w in result.workloads))

    def test_cli_analysis_and_simulation(self):
        with tempfile.TemporaryDirectory() as tmp,redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            root=Path(tmp);(root/'model.bim').write_text(json.dumps(model()));(root/'matrices.json').write_text(json.dumps({'matrices':[matrix()]}))
            self.assertEqual(main(['matrices',str(root/'model.bim'),'--matrices',str(root/'matrices.json'),'-o',str(root/'report')]),0)
            self.assertEqual(main(['simulate',str(root/'model.bim'),'--matrices',str(root/'matrices.json'),'--fact-rows','16','--dimension-rows','4','-o',str(root/'lab')]),0)


class PbirMatrixTests(unittest.TestCase):
    def visual(self):
        def field(kind,table,name):return {kind:{'Expression':{'SourceRef':{'Entity':table}},'Property':name}}
        return {'name':'example-matrix','visual':{'visualType':'pivotTable','query':{'queryState':{
            'Rows':{'projections':[{'field':field('Column','Customer','Region')}]},
            'Columns':{'projections':[{'field':field('Column','Calendar','Month')}]},
            'Values':{'projections':[{'field':field('Measure','Sales','Revenue')}]}}}}}

    def test_pbir_binds_explicit_projections_but_does_not_assume_complete_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'definition/pages/pageA/visuals/one/visual.json';path.parent.mkdir(parents=True)
            path.write_text(json.dumps(self.visual()))
            specs=load_matrices(Path(tmp));spec=specs[0]
            self.assertEqual(spec.rows,[['Customer','Region']]);self.assertEqual(spec.values,[['Sales','Revenue']])
            self.assertFalse(spec.context_complete)
            candidate=analyze_matrices(model(),specs).matrices[0]['grain_advice']['candidates'][0]
            self.assertEqual(candidate['status'],'needs_review')

    def test_pbir_preserves_literal_titles_page_labels_and_stable_visual_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            report=Path(tmp)/'Example.Report'
            path=report/'definition/pages/pageA/visuals/one/visual.json';path.parent.mkdir(parents=True)
            (path.parents[2]/'page.json').write_text(json.dumps({'displayName':'Sales overview'}))
            raw=self.visual()
            raw['visual']['visualContainerObjects']={'title':[{'properties':{'text':{'expr':{'Literal':{'Value':"'Customer''s revenue <view>'"}}}}}]}
            path.write_text(json.dumps(raw));spec=load_matrices(report)[0]
            self.assertEqual(spec.id,'pageA/example-matrix');self.assertEqual(spec.visual_id,'example-matrix')
            self.assertEqual(spec.title,"Customer's revenue <view>")
            self.assertEqual(spec.page_name,'Sales overview');self.assertEqual(spec.report_name,'Example')
            # A dynamic/hidden/malformed title is never evaluated as DAX or used for binding.
            raw['visual']['visualContainerObjects']['title'][0]['properties']['show']={'expr':{'Literal':{'Value':'false'}}}
            path.write_text(json.dumps(raw))
            self.assertEqual(load_matrices(report)[0].title,'example-matrix')
            raw['visual']['visualContainerObjects']['title'][0]['properties']['text']={'expr':{'Measure':{'Property':'Title measure'}}}
            del raw['visual']['visualContainerObjects']['title'][0]['properties']['show']
            path.write_text(json.dumps(raw))
            self.assertEqual(load_matrices(report)[0].title,'example-matrix')
            raw['visual']['visualContainerObjects']={'title':[None,{'properties':[]}]}
            path.write_text(json.dumps(raw));self.assertEqual(load_matrices(report)[0].title,'example-matrix')
            (path.parents[2]/'page.json').write_text(json.dumps({'displayName':None}))
            self.assertEqual(load_matrices(report)[0].page_name,'pageA')

    def test_optional_labels_round_trip_and_validate_without_changing_context(self):
        from semantic_hotspots import MatrixSpec
        raw=matrix(report_name='Example',page_name='Overview',visual_id='one')
        spec=MatrixSpec.from_dict(raw)
        self.assertEqual(MatrixSpec.from_dict(spec.to_dict()).page_name,'Overview')
        for key in ['report_name','page_name','visual_id']:
            with self.subTest(key=key),self.assertRaises(ModelError):MatrixSpec.from_dict(dict(raw,**{key:None}))

    def test_filters_implicit_measures_and_extra_roles_are_withheld(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'visual.json';raw=self.visual();raw['filterConfig']={'filters':[{'name':'unknown-filter'}]}
            raw['visual']['query']['queryState']['Values']['projections'][0]['field']={'Aggregation':{}}
            path.write_text(json.dumps(raw))
            specs=load_matrices(path)
            self.assertTrue(specs[0].unsupported)
            result=analyze_matrices(model(),specs)
            self.assertFalse(result.workloads)

    def test_nonmatrix_report_is_not_silently_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'visual.json';path.write_text(json.dumps({'visual':{'visualType':'tableEx'}}))
            with self.assertRaises(ModelError):load_matrices(path)

    def test_malformed_pbir_nested_shapes_raise_model_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'visual.json'
            for value in [None,[],{'queryState':[]},{'queryState':{'Rows':None}},
                          {'queryState':{'Rows':{'projections':{}}}},
                          {'queryState':{'Rows':{'projections':[None]}}}]:
                raw=self.visual();raw['visual']['query']=value
                path.write_text(json.dumps(raw))
                with self.subTest(query=value),self.assertRaises(ModelError):load_matrices(path)
            raw=self.visual();raw['filterConfig']=[];path.write_text(json.dumps(raw))
            with self.assertRaises(ModelError):load_matrices(path)
