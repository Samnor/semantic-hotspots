"""Fictional failure cases distinguish unsupported binding from actual cycles."""
import unittest

from semantic_hotspots import create_measure, computation_dag, compare_measure_dags
from test_optimizer import source


class SpecificBlockerTests(unittest.TestCase):
    def test_column_binding_gap_names_reference_and_location_without_claiming_a_cycle(self):
        result=create_measure(source('VAR clock = TODAY()\nRETURN [Amount]'),'Total').data
        messages=' '.join(result['diagnostics'])
        self.assertIn('Cannot bind [Amount] at line 2, offset ',messages)
        self.assertIn("TODAY() in 'Sales'[Total] at line 1",messages)
        self.assertIn("Reference scope is not established for 'Sales'[Total]",messages)
        self.assertNotIn('cyclic',messages)
        self.assertNotIn('cycle',messages)
        self.assertTrue(result['dag_comparison']['original']['is_acyclic'])
        self.assertFalse(result['validation_available'])
        self.assertEqual({x['code'] for x in result['dependency_issues']},{'reference_scope'})

    def test_volatile_helper_is_identified_without_replacing_its_expression(self):
        data=source('SUMX(Sales, Sales[Amount]) + [Clock]')
        data['model']['tables'][0]['measures'].append({'name':'Clock','expression':'\nTODAY()'})
        result=create_measure(data,'Total').data
        self.assertEqual(result['outcome'],'rewritten')
        self.assertIn("TODAY() in 'Sales'[Clock] at line 2",' '.join(result['diagnostics']))
        self.assertFalse(result['dependency_issues'])
        self.assertTrue(result['validation_available'])

    def test_missing_reference_and_missing_definition_are_distinct(self):
        data=source('[Absent] + [Empty]')
        data['model']['tables'][0]['measures'].append({'name':'Empty'})
        result=create_measure(data,'Total').data
        codes={x['code'] for x in result['dependency_issues']}
        self.assertEqual(codes,{'unresolved_reference','missing_definition'})
        messages=' '.join(result['diagnostics'])
        self.assertIn('[Absent] at line 1, offset 0',messages)
        self.assertIn("Missing measure definition: 'Sales'[Empty]",messages)
        self.assertNotIn('cycle',messages)

    def test_actual_cycle_is_reported_only_when_present(self):
        dag=computation_dag(source('[Dependent]'),'Total')
        self.assertFalse(dag['is_acyclic'])
        self.assertEqual([x['code'] for x in dag['dependency_issues']],['cycle'])

    def test_syntax_failure_is_not_a_scope_or_cycle_diagnostic(self):
        result=create_measure(source('SUM('),'Total').data
        self.assertEqual({x['code'] for x in result['dependency_issues']},{'syntax_scan'})
        self.assertIn("DAX syntax scan failed for 'Sales'[Total]",' '.join(result['diagnostics']))

    def test_custom_expression_uses_its_own_line_offsets(self):
        result=compare_measure_dags(source(),'Total',proposed_expression='1 +\n [Missing]')
        issue=next(x for x in result['proposed']['dependency_issues'] if x['code']=='unresolved_reference')
        self.assertEqual(issue['line'],2)
        self.assertIn('line 2',issue['message'])
