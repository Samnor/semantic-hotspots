"""Fictional fixed-clock comparisons and independent opaque-helper proposals."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from semantic_hotspots import create_measure, parse_model, ModelError, Entity
from semantic_hotspots.date_validation import freeze_today, validation_dates, date_controls
from semantic_hotspots.focus import write_measure_proposal
from test_optimizer import source


def stored(expression='Sales[Amount]'):
    return ('VAR clock=TODAY() VAR scored=ADDCOLUMNS(Sales,"@n",'+expression+') '
            'VAR keptRows=FILTER(scored,NOT ISBLANK([@n])) '
            'RETURN IF(YEAR(clock)>2000,IF(ISEMPTY(keptRows),BLANK(),SUMX(keptRows,[@n])),BLANK())')


class DateControlTests(unittest.TestCase):
    def test_today_preserved_while_both_paths_and_helpers_are_frozen_per_date(self):
        data=source('IF(YEAR(TODAY())>2000,SUMX(Sales,Sales[Amount]),[Clock])')
        data['model']['tables'][0]['measures'].append({'name':'Clock','expression':'YEAR(TODAY())'})
        before=copy.deepcopy(data);result=create_measure(data,'Total',validation_dates=['2024-02-29','2025-01-01'])
        self.assertEqual(data,before);self.assertEqual(result.data['outcome'],'rewritten')
        self.assertIn('TODAY()',result.data['expression']);self.assertTrue(result.data['validation_available'])
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'new';result.write(root)
            for day,literal in [('2024-02-29','DATE(2024, 2, 29)'),('2025-01-01','DATE(2025, 1, 1)')]:
                query=(root/'validation-dates'/day/'validate-total.dax').read_text()
                self.assertIn("MEASURE 'Sales'[Total]",query)
                self.assertIn("MEASURE 'Sales'[Clock]",query)
                self.assertIn("MEASURE 'Sales'[Total (candidate)]",query)
                self.assertEqual(query.count(literal),3)
            self.assertIn('TODAY()', (root/'new-measure.dax').read_text())
            self.assertIn('TODAY()', (root/'preview-query.dax').read_text())
            self.assertEqual(json.loads((root/'date-controls.json').read_text())['dates'],['2024-02-29','2025-01-01'])

    def test_helper_only_date_override_is_available_with_unchanged_root(self):
        data=source('[Clock]');data['model']['tables'][0]['measures'].append({'name':'Clock','expression':'TODAY()'})
        result=create_measure(data,'Total',validation_dates=['2026-12-31'])
        self.assertTrue(result.data['validation_available']);self.assertEqual(result.data['outcome'],'unchanged_copy')
        with tempfile.TemporaryDirectory() as tmp:
            query=(result.write(Path(tmp)/'new')['html'].parent/'validate-total.dax').read_text()
            self.assertIn("MEASURE 'Sales'[Clock]",query)
            self.assertIn("MEASURE 'Sales'[Total] =",query)

    def test_stored_callers_are_rebound_along_the_entire_clock_dependency_path(self):
        data=source('SUMX(Sales,Sales[Amount])+[Middle]')
        data['model']['tables'][0]['measures'].extend([
            {'name':'Middle','expression':'[Nested] + 1'},
            {'name':'Nested','expression':'[Clock] + 1'},
            {'name':'Clock','expression':'YEAR(TODAY())'},
            {'name':'Other','expression':'1'}])
        created=create_measure(data,'Total',validation_dates=['2024-02-29'])
        self.assertEqual({d['name'] for d in created.data['date_controls']['definitions']},{'Clock','Nested','Middle','Total'})
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'new';created.write(root);query=(root/'validate-total.dax').read_text()
            for name in ['Clock','Nested','Middle','Total','Total (candidate)']:
                self.assertIn("MEASURE 'Sales'["+name+"] =",query)
            self.assertNotIn("MEASURE 'Sales'[Other]",query)

    def test_missing_known_helper_kept_opaque_without_blocking_local_stored_filter_rule(self):
        text=stored('[Empty]');data=source(text);data['model']['tables'][0]['measures'].append({'name':'Empty'})
        result=create_measure(data,'Total')
        self.assertEqual(result.data['expression'],text.replace('SUMX(keptRows,[@n])','SUMX(scored, [@n])'))
        self.assertFalse(result.data['validation_available']);self.assertTrue(result.data['pending_helpers'])
        self.assertTrue(result.data['guard_evidence']['helper_callbacks_preserved'])
        self.assertFalse(result.data['guard_evidence']['missing_helper_definitions_assumed'])
        self.assertEqual(result.data['expression'].count('[Empty]'),1)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'new';result.write(root)
            self.assertFalse((root/'validate-total.dax').exists());self.assertFalse((root/'validation-dates').exists())

    def test_local_rewrite_works_with_unknown_surrounding_column_types(self):
        data=source(stored('IF(Sales[Amount]>0,[Empty],BLANK())'))
        data['model']['tables'][0]['columns'][0].pop('dataType')
        data['model']['tables'][0]['measures'].append({'name':'Empty'})
        result=create_measure(data,'Total').data
        self.assertEqual(result['outcome'],'rewritten');self.assertFalse(result['validation_available'])
        self.assertIn('Only independently guarded local rewrites',' '.join(result['diagnostics']))
        for expression in ['UNSUPPORTED(Sales[Amount])','IF(Sales[Amount])','IF("wrong",Sales[Amount],0)','DIVIDE(Sales[Amount],2,[Empty])']:
            with self.subTest(expression=expression):
                data['model']['tables'][0]['measures'][0]['expression']=stored(expression)
                self.assertEqual(create_measure(data,'Total').data['outcome'],'unchanged_copy')

    def test_shadowed_base_variable_cannot_be_substituted_at_consumer(self):
        text=('VAR scored=ADDCOLUMNS(Sales,"@n",Sales[Amount]) VAR keptRows=FILTER(scored,NOT ISBLANK([@n])) '
              'RETURN SUMX(Sales,VAR scored=ADDCOLUMNS(Sales,"@n",9) RETURN SUMX(keptRows,[@n]))')
        result=create_measure(source(text),'Total').data
        self.assertEqual(result['expression'],text)

    def test_now_and_random_or_unbound_and_cycles_still_block_automatic_rewrites(self):
        for part in ['NOW()','RAND()','RANDBETWEEN(1,9)','[NotInMetadata]','[Dependent]']:
            with self.subTest(part=part):
                result=create_measure(source('SUMX(Sales,Sales[Amount]) + '+part),'Total').data
                self.assertEqual(result['outcome'],'unchanged_copy')

    def test_freezing_ignores_strings_comments_and_dates_are_validated(self):
        text='IF("TODAY()"=="x", TODAY(), TODAY()) // TODAY()'
        self.assertEqual(freeze_today(text,'2024-02-29'),'IF("TODAY()"=="x", DATE(2024, 2, 29), DATE(2024, 2, 29)) // TODAY()')
        self.assertEqual(validation_dates(['2024-02-29','2024-02-29']),['2024-02-29'])
        for dates in [[],['2025-02-29'],['1899-12-31'],['bad'],[None],['2024-01-01']*13,'2024-01-01']:
            with self.subTest(dates=dates),self.assertRaises(ModelError):validation_dates(dates)

    def test_stored_clock_and_shadowing_function_are_not_frozen_as_measures(self):
        model=parse_model(source('SUMX(Sales,Sales[Amount])'))
        model.entities[next(e.id for e in model.entities.values() if e.kind=='column' and e.name=='Amount')].expression='YEAR(TODAY())'
        controls=date_controls(model,model.measure('Total'))
        self.assertTrue(controls['blocked_reasons'])
        model=parse_model(source('TODAY()'));model.add(Entity('function','','TODAY','1'))
        self.assertTrue(date_controls(model,model.measure('Total'))['blocked_reasons'])

    def test_custom_query_checks_both_chains_and_freezes_proposed_clock(self):
        model=parse_model(source('[Missing]'))
        with tempfile.TemporaryDirectory() as tmp:
            result=write_measure_proposal(model,'Total','1',Path(tmp)/'one')
            self.assertIsNone(result['validation']);self.assertIn('Missing',result['validation_reason'])
            model=parse_model(source('1'))
            result=write_measure_proposal(model,'Total','YEAR(TODAY())',Path(tmp)/'two',validation_dates=['2024-01-01'])
            query=result['validation'].read_text();self.assertIn('DATE(2024, 1, 1)',query)
            self.assertIn("MEASURE 'Sales'[Total]",query)

    def test_synthetic_rewrite_experiments_use_same_fixed_date_on_both_paths(self):
        from semantic_hotspots import simulate, prepare_rewrite_experiment
        from semantic_hotspots.experiments import _experiment
        data=source('IF(YEAR(TODAY())>2000,SUMX(Sales,Sales[Amount]),BLANK())')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);simulate(data,root/'lab',config={'fact_rows':12,'dimension_rows':3})
            prepare_rewrite_experiment(root/'lab',root/'experiment',validation_dates=['1999-12-31','2024-02-29'])
            _,manifest=_experiment(root/'experiment')
            self.assertEqual({c['as_of'] for c in manifest['cases']},{'1999-12-31','2024-02-29'})
            for case in manifest['cases']:
                literal='DATE('+', '.join(str(int(n)) for n in case['as_of'].split('-'))+')'
                for kind in ['baseline','candidate','validation']:
                    query=case['queries'][kind]['query']
                    self.assertEqual(query.count(literal),2)
                    self.assertIn("MEASURE 'Sales'[Total]",query)

    def test_fixed_date_visual_queries_preserve_separate_filters_and_record_date(self):
        data=source('IF(YEAR(TODAY())>2000,SUMX(Sales,Sales[Amount]),BLANK())')
        visuals=[{'id':label,'title':label,'values':[['Sales','Total']],
                  'rows':[['Sales','Category']],'context_complete':True,
                  'filters':[{'table':'Sales','column':'Category','values':[label]}]} for label in ['A','B']]
        created=create_measure(data,'Total',visuals=visuals,validation_dates=['2024-02-29','2025-01-01'])
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'new';created.write(root)
            for day,literal in [('2024-02-29','DATE(2024, 2, 29)'),('2025-01-01','DATE(2025, 1, 1)')]:
                for index,label in [(1,'A'),(2,'B')]:
                    folder=root/'validation-dates'/day/'visual-contexts'/str(index)/'landing-queries'
                    self.assertEqual(json.loads((folder/'contexts.json').read_text())['as_of'],day)
                    for query in folder.glob('*.dax'):
                        text=query.read_text()
                        self.assertIn(literal,text);self.assertIn('TREATAS({"'+label+'"}',text)
