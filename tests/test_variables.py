"""Fictional scoped-variable cases: preserve bindings and explain coverage gaps."""
import copy
import tempfile
import unittest
from pathlib import Path

from semantic_hotspots import create_measure, computation_dag, parse_model, plan_rewrites
from semantic_hotspots.optimizer import logical_plan
from test_optimizer import source


class VariableTests(unittest.TestCase):
    def test_scalar_variables_and_branches_allow_local_aggregate_rewrite(self):
        expression=('VAR gross = CALCULATE(SUMX(Sales, Sales[Amount]), REMOVEFILTERS(Sales[Category]))\n'
                    'VAR half = DIVIDE(gross, 2)\nRETURN IF(NOT ISBLANK(gross), half, BLANK())')
        model=parse_model(source(expression));before=copy.deepcopy(model)
        result=create_measure(model,'Total').data
        self.assertEqual(result['outcome'],'rewritten')
        self.assertEqual(result['expression'],expression.replace('SUMX(Sales, Sales[Amount])','SUM(Sales[Amount])'))
        self.assertEqual(model,before)
        self.assertTrue(result['validation_available'])
        dag=result['dag_comparison']['proposed']
        self.assertTrue(dag['is_acyclic']);self.assertTrue(dag['operator_complete'])
        self.assertTrue(dag['reference_diagnostics']);self.assertFalse(dag['reference_blockers'])
        variable=next(n for n in dag['nodes'] if n.get('label')=='VAR gross')
        self.assertGreaterEqual(sum(e['to']==variable['id'] for e in dag['edges']),3)
        self.assertEqual(result['validation_status'],'not_run')

    def test_binding_is_not_hoisted_into_the_context_of_a_use(self):
        expression='VAR amount = SUMX(Sales, Sales[Amount]) RETURN CALCULATE(amount, Sales[Category] == "A")'
        result=create_measure(source(expression),'Total').data
        self.assertEqual(result['expression'],expression.replace('SUMX(Sales, Sales[Amount])','SUM(Sales[Amount])'))
        for key,aggregate in [('original','SUMX'),('proposed','SUM')]:
            dag=result['dag_comparison'][key]
            node=next(n for n in dag['nodes'] if n.get('operation')==aggregate)
            self.assertFalse(node['filter_context'])
            calculate=next(n for n in dag['nodes'] if n.get('operation')=='CALCULATE')
            variable=next(n for n in dag['nodes'] if n.get('label')=='VAR amount')
            self.assertTrue(any(e['from']==calculate['id'] and e['to']==variable['id'] for e in dag['edges']))

    def test_nested_shadowing_and_previous_bindings_are_lexical(self):
        text='VAR x = 1 VAR y = x + 1 RETURN x + (VAR x = 7 RETURN x + y)'
        model=parse_model(source(text));tree,issues=logical_plan(text,model,model.measure('Total'))
        self.assertFalse(issues)
        outer_x,outer_y,body=tree.children
        inner=body.children[1].children[0]
        inner_x,inner_body=inner.children
        self.assertNotEqual(outer_x.target,inner_x.target)
        self.assertEqual(outer_y.children[0].children[0].target,outer_x.target)
        self.assertEqual(body.children[0].target,outer_x.target)
        self.assertEqual(inner_body.children[0].target,inner_x.target)
        self.assertEqual(inner_body.children[1].target,outer_y.target)
        self.assertTrue(computation_dag(model,'Total')['is_acyclic'])

    def test_invalid_or_ambiguous_scopes_never_enable_a_rewrite(self):
        for text in ['VAR x = later VAR later = SUMX(Sales, Sales[Amount]) RETURN x',
                     'VAR x = x RETURN SUMX(Sales, Sales[Amount])',
                     'VAR x = 1 VAR x = SUMX(Sales, Sales[Amount]) RETURN x',
                     'VAR Sales = 1 RETURN SUMX(Sales, Sales[Amount])',
                     'VAR x = SUMX(Sales, Sales[Amount])',
                     'VAR x = SUMX(Sales, Sales[Amount]) RETURN x + Missing(1)',
                     'VAR x = [Missing] RETURN SUMX(Sales, Sales[Amount])',
                     'VAR x = RAND() RETURN SUMX(Sales, Sales[Amount])']:
            with self.subTest(text=text):self.assertFalse(plan_rewrites(source(text)).candidates)

    def test_table_variables_are_visible_but_still_outside_rewrite_coverage(self):
        text='VAR rows = Sales RETURN SUMX(rows, Sales[Amount])'
        result=create_measure(source(text),'Total').data
        dag=result['dag_comparison']['original']
        self.assertEqual(result['outcome'],'unchanged_copy')
        self.assertTrue(dag['dependency_complete']);self.assertTrue(dag['operator_complete'])
        self.assertFalse(dag['logical_subset_validated'])
        self.assertIn('table VAR',' '.join(result['diagnostics']))
        self.assertEqual(result['reason_kind'],'unsupported_syntax_or_semantics')

    def test_unsupported_calls_explain_coverage_above_unchanged_dags(self):
        text='VAR amount = MEDIAN(Sales[Amount]) RETURN amount'
        result=create_measure(source(text),'Total')
        self.assertEqual(result.data['outcome'],'unchanged_copy')
        self.assertTrue(result.data['validation_available'])
        self.assertIn('MEDIAN at offset',' '.join(result.data['diagnostics']))
        with tempfile.TemporaryDirectory() as tmp:
            result.write(Path(tmp)/'new')
            page=(Path(tmp)/'new/dags/report.html').read_text(encoding='utf-8')
            self.assertLess(page.index('Unchanged copy — no automatic rewrite applied'),page.index('Original measure DAG'))
            self.assertIn('MEDIAN at offset',page)
            self.assertIn('informational',page)

    def test_branch_types_and_nonconstant_divide_alternatives_are_not_guessed(self):
        for text in ['IF(Sales, SUMX(Sales, Sales[Amount]), 0)',
                     'DIVIDE(SUMX(Sales, Sales[Amount]),2,[Dependent])',
                     'VAR n = 0 RETURN DIVIDE(SUMX(Sales, Sales[Amount]),2,n)',
                     'NOT(SUMX(Sales, Sales[Amount]))']:
            with self.subTest(text=text):self.assertFalse(plan_rewrites(source(text)).candidates)
