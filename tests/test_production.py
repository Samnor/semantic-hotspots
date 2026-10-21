"""Fictional deep-chain workloads and strict guard regressions, no private evidence."""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout,redirect_stderr
from pathlib import Path

from semantic_hotspots import (production_model,production_data,create_production_lab,
                               create_measure,parse_model,plan_rewrites,ModelError)
from semantic_hotspots.cli import main
from semantic_hotspots.graph import DependencyGraph
from production_oracle import expected,contexts,total,difference,divide


def simple(expression,helper="SUM('Fact'[Units])",datatype='int64'):
    return {'model':{'tables':[{'name':'Fact','columns':[{'name':'Day','dataType':'dateTime'},
        {'name':'Units','dataType':datatype}], 'partitions':[{'name':'Rows','mode':'import'}],
        'measures':[{'name':'Base','expression':helper},{'name':'Root','expression':expression}]}]}}


class AdditiveGrainTests(unittest.TestCase):
    def candidates(self,expression,**kwargs):
        return plan_rewrites(simple(expression,**kwargs),measure='Root').candidates

    def test_only_transitioned_pure_exact_same_table_sums_collapse(self):
        for body in ['[Base]','CALCULATE([Base])',"CALCULATE(SUM('Fact'[Units]))"]:
            for typ in ['int64','decimal']:
                with self.subTest(body=body,typ=typ):
                    result=self.candidates("SUMX(VALUES('Fact'[Day]),"+body+')',datatype=typ)
                    self.assertEqual(result[0]['after'],"SUM('Fact'[Units])")
                    self.assertEqual(result[0]['steps'][0]['rule'],'OPT004')
        self.assertFalse(self.candidates("SUMX(VALUES('Fact'[Day]),SUM('Fact'[Units]))"))
        self.assertFalse(self.candidates("SUMX(VALUES('Fact'[Day]),[Base])",datatype='double'))

    def test_filtered_ratio_snapshot_and_outer_row_context_are_not_collapsed(self):
        expression="SUMX(VALUES('Fact'[Day]),[Base])"
        for helper in ["CALCULATE(SUM('Fact'[Units]),'Fact'[Units]>0)",
                       "DIVIDE(SUM('Fact'[Units]),COUNTROWS('Fact'))",
                       "AVERAGE('Fact'[Units])","MAX('Fact'[Units])",
                       "VAR lastDay=MAX('Fact'[Day]) RETURN CALCULATE(SUM('Fact'[Units]),'Fact'[Day]==lastDay)"]:
            self.assertFalse(self.candidates(expression,helper=helper),helper)
        nested="SUMX('Fact',SUMX(VALUES('Fact'[Day]),CALCULATE(SUM('Fact'[Units]))))"
        self.assertFalse(self.candidates(nested))
        data=simple(expression);data['model']['relationships']=[{'fromTable':'Fact','fromColumn':'Day',
            'toTable':'Fact','toColumn':'Day','crossFilteringBehavior':'bothDirections'}]
        self.assertFalse(plan_rewrites(data,measure='Root').candidates)

    def test_blank_filter_removal_rejects_floating_or_unestablished_types(self):
        text='VAR rows=ADDCOLUMNS(Fact,"@x",Fact[Units]) RETURN SUMX(FILTER(rows,NOT ISBLANK([@x])),[@x])'
        self.assertTrue(self.candidates(text))
        self.assertFalse(self.candidates(text,datatype='double'))
        text='VAR rows=ADDCOLUMNS(VALUES(Fact[Day]),"@x",[Base]) RETURN SUMX(FILTER(rows,NOT ISBLANK([@x])),[@x])'
        self.assertFalse(self.candidates(text))

    def test_other_table_calculated_column_and_missing_helper_are_not_assumed(self):
        data=simple("SUMX(VALUES('Fact'[Day]),[Base])")
        data['model']['tables'][0]['columns'][1]['expression']='1'
        self.assertFalse(plan_rewrites(data,measure='Root').candidates)
        self.assertFalse(self.candidates("SUMX(VALUES('Fact'[Day]),[Missing])"))
        data=simple("SUMX(VALUES('Fact'[Day]),[Base])",helper="SUM('Other'[Units])")
        other=copy.deepcopy(data['model']['tables'][0]);other['name']='Other';other['measures']=[]
        data['model']['tables'].append(other)
        self.assertFalse(plan_rewrites(data,measure='Root').candidates)


class ProductionChainTests(unittest.TestCase):
    def test_three_deep_roots_reduce_daily_loops_and_preserve_live_clock_and_helpers(self):
        document=production_model();frozen=copy.deepcopy(document);model=parse_model(document)
        self.assertEqual(len(model.measures),24);self.assertEqual(len(model.tables),8)
        graph=DependencyGraph(model)
        for name in ['Production rate by period','Stock coverage by period','Target variance by period']:
            result=create_measure(model,name,validation_dates=['2024-02-29']).data
            self.assertEqual(result['outcome'],'rewritten');self.assertTrue(result['validation_available'])
            self.assertTrue(result['chain_specialization']['source_helpers_unchanged'])
            self.assertGreaterEqual(len(graph.dependency_ids(model.measure(name).id)),15)
            def depth(identity):
                return 1+max([depth(d) for d in graph.dependencies[identity] if model.entities[d].kind=='measure'] or [0])
            self.assertEqual(depth(model.measure(name).id),8 if name.startswith('Stock') else 7)
            self.assertIn('TODAY()',result['expression']);self.assertIn('SUMX(keptRows',result['expression'])
            operators=result['dag_comparison']['difference']
            self.assertEqual(operators['query_operators_original']['SUMX'],5 if name.startswith('Target') else 6)
            self.assertEqual(operators['query_operators_proposed']['SUMX'],1)
            self.assertTrue(any(s['rule']=='OPT004' for s in result['steps']))
            self.assertTrue(any(s['rule']=='OPT005' for s in result['steps']))
            if name.startswith('Stock'):
                self.assertIn("'Metrics'[Closing stock units]",result['expression'])
                self.assertIn('DIVIDE(',result['expression'])
        self.assertEqual(document,frozen)

    def test_binding_is_qualified_before_transplant_and_unsafe_chain_remains_opaque(self):
        data=simple('VAR t=ADDCOLUMNS(VALUES(Fact[Day]),"Base",0,"@x",[Child]) RETURN SUMX(t,[@x])',
                    helper='SUMX(Fact,Fact[Units])')
        data['model']['tables'][0]['measures'].append({'name':'Child','expression':'[Base]+1'})
        result=create_measure(data,'Root').data
        self.assertTrue(result['chain_specialization'])
        self.assertIn('CALCULATE(',result['expression'])
        self.assertIn("SUM('Fact'[Units])",result['expression'])
        before=copy.deepcopy(data)
        data['model']['tables'][0]['measures'][1]['expression']='[Child]+RAND()'
        result=create_measure(data,'Root').data
        self.assertEqual(result['outcome'],'unchanged_copy')
        self.assertIsNone(result['chain_specialization'])
        # A missing body is never supplied by synthetic data.
        data=before;data['model']['tables'][0]['measures'][0]['expression']=None
        self.assertIsNone(create_measure(data,'Root').data['chain_specialization'])

    def test_unchanged_callback_cannot_be_captured_by_caller_extension_column(self):
        data=simple('VAR rows=ADDCOLUMNS(VALUES(Fact[Day]),"Steady",0,"@x",[Child]) RETURN SUMX(rows,[@x])',
                    helper='SUMX(Fact,Fact[Units])')
        data['model']['tables'][0]['measures'].extend([
            {'name':'Steady','expression':'MAX(Fact[Units])'},
            {'name':'Child','expression':'VAR own=[Base] RETURN own+[Steady]'}])
        result=create_measure(data,'Root').data
        self.assertTrue(result['chain_specialization'])
        self.assertIn("'Fact'[Steady]",result['expression'])
        self.assertNotIn('[Steady]',result['expression'].replace("'Fact'[Steady]",''))
        self.assertIn('VAR own=',result['expression'])

    def test_clock_inside_improved_helper_is_live_but_frozen_in_both_validation_paths(self):
        data=simple('[Clock]+1',helper='SUMX(Fact,Fact[Units])')
        data['model']['tables'][0]['measures'].append({'name':'Clock','expression':
            'IF(TODAY()<DATE(2024,3,1),[Base],BLANK())'})
        result=create_measure(data,'Root',validation_dates=['2024-02-29','2024-03-01'])
        self.assertTrue(result.data['chain_specialization']);self.assertIn('TODAY()',result.data['expression'])
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'new';result.write(root)
            query=(root/'validation-dates/2024-02-29/validate-total.dax').read_text()
            self.assertIn("MEASURE 'Fact'[Clock]",query)
            self.assertIn("MEASURE 'Fact'[Root]",query)
            self.assertNotIn('TODAY()', '\n'.join(line for line in query.splitlines() if not line.startswith('//')))
            self.assertIn('DATE(2024, 2, 29)',query)

    def test_specialization_is_bounded_and_does_not_expand_unchanged_helpers(self):
        data=simple('[Base]+1')
        self.assertFalse(plan_rewrites(data,measure='Root').candidates)
        data=simple('[Link 33]',helper='SUMX(Fact,Fact[Units])')
        measures=data['model']['tables'][0]['measures']
        for i in range(34):measures.append({'name':'Link '+str(i),'expression':'[Base]' if i==0 else '[Link '+str(i-1)+']'})
        self.assertFalse(plan_rewrites(data,measure='Root').candidates)

    def test_mock_grain_is_coherent_reproducible_and_contains_counterexamples(self):
        data=production_data();self.assertEqual(data,production_data())
        keys=[tuple(r[:3]) for r in data['Production Daily']];self.assertEqual(len(keys),len(set(keys)))
        self.assertTrue(any(r[3] is not None and r[3]<0 for r in data['Production Daily']))
        self.assertTrue(any(r[3]==0 for r in data['Production Daily']))
        self.assertTrue(any(r[3] is None for r in data['Production Daily']))
        self.assertNotEqual(max(r[0] for r in data['Production Daily'] if r[0] and r[1]==1),
                            max(r[0] for r in data['Production Daily'] if r[0] and r[1]==3))
        for context in [{'family':'Empty'},{'region':'absent'},{'period':999},{'dates':[]}]:
            self.assertEqual(expected(data,'2025-01-01',context),[None,None,None])
        self.assertEqual(expected(data,'2025-01-01',{'dates':['2024-01-06','2024-01-07']})[:2],[None,None])
        self.assertTrue(all(v is not None for v in expected(data,'2024-02-15',{})))
        self.assertNotEqual(expected(data,'2025-01-01',{})[1],sum(expected(data,'2025-01-01',{'site':s})[1] for s in [1,2,3]))
        for kwargs in [{'days':1},{'days':True},{'sites':1},{'products':13}]:
            with self.assertRaises(ModelError):production_data(**kwargs)

    def test_independent_oracle_has_hand_computed_blank_zero_and_period_cases(self):
        self.assertIsNone(total([None]));self.assertEqual(total([0,None]),0)
        self.assertEqual(difference(None,4),-4);self.assertIsNone(difference(None,None))
        self.assertIsNone(divide(5,0));self.assertEqual(divide(0,5),0)
        data={'Site':[[1,'North']],'Product':[[1,'Widget']],'Budget':[[1,'Original']],
            'Periods':[[1,'2024-01-02']],'Calendar':[['2024-01-01',1,1],['2024-01-02',1,1]],
            'Production Daily':[['2024-01-01',1,1,10,1,20,1],['2024-01-02',1,1,20,3,40,1]],
            'Plan Daily':[['2024-01-01',1,1,1,20,2,1],['2024-01-02',1,1,1,20,2,1]]}
        self.assertEqual(expected(data,'2024-01-03',{}),[13,40/13,-10])
        self.assertEqual(expected(data,'2024-01-02',{}),[18,40/18,-36])
        self.assertGreater(len(contexts(production_data())),15)

    def test_export_and_cli_are_source_free_and_contain_one_comparison_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'lab'
            with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                self.assertEqual(main(['production-lab','-o',str(root),'--days','28']),0)
            manifest=json.loads((root/'production-lab.json').read_text())
            self.assertEqual(len(manifest['cases']),3)
            self.assertEqual(len(list((root/'Showcase.Report').rglob('visual.json'))),1)
            model=json.loads((root/'Showcase.SemanticModel/model.bim').read_text())
            self.assertEqual(len(model['model']['tables'][-1]['measures']),27)
            for table in model['model']['tables']:
                text=table['partitions'][0]['source']['expression']
                self.assertIn('Binary.FromText',text);self.assertNotIn('File.Contents',text)
            for i in range(1,4):
                files=root/'measures'/str(i)
                self.assertTrue((files/'preview-query.dax').is_file())
                self.assertTrue((files/'validation-dates/2024-02-29/validate-total.dax').is_file())
            with self.assertRaises(ModelError):create_production_lab(root)
