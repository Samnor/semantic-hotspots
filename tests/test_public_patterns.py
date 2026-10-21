"""Independent small fixtures for patterns encountered during public-model audits."""
import unittest

from semantic_hotspots import create_measure, parse_model, plan_rewrites
from semantic_hotspots.optimizer import logical_plan


def fixture(expression):
    return parse_model({'model': {'tables': [{'name': 'Transactions', 'columns': [
        {'name': 'Quantity', 'dataType': 'int64'}, {'name': 'Revenue', 'dataType': 'decimal'},
        {'name': 'Label', 'dataType': 'string'}], 'measures': [
        {'name': 'Quantity total', 'expression': 'SUM([Quantity])'},
        {'name': 'Revenue total', 'expression': 'SUM([Revenue])'},
        {'name': 'Result', 'expression': expression}]}]}})


class PublicPatternTests(unittest.TestCase):
    def test_column_only_arguments_bind_local_columns(self):
        for expr in ['SUM([Quantity])', 'AVERAGE([Quantity])', 'COUNTA([Label])',
                     'COUNT([Quantity])', 'DISTINCTCOUNT([Quantity])',
                     'COUNTROWS(ALL([Quantity]))', 'COUNTROWS(VALUES([Quantity]))']:
            model = fixture(expr)
            _, issues = logical_plan(expr, model, model.measure('Result'))
            self.assertFalse(issues, (expr, issues))
        for expr in ['[Quantity] + 1', 'SUM([Unknown])', 'SUM([Revenue total])']:
            model = fixture(expr)
            _, issues = logical_plan(expr, model, model.measure('Result'))
            self.assertTrue(issues, expr)

    def test_guarded_division_preserves_blank_alternate(self):
        for expr in ['IF([Quantity total]<>0,[Revenue total]/[Quantity total],BLANK())',
                     'IF(0<>[Quantity total],[Revenue total]/[Quantity total],BLANK())']:
            plan = plan_rewrites(fixture(expr))
            rows = [r for r in plan.measures if r['measure_id'].endswith('/Result')]
            self.assertEqual(rows[0]['candidates'][0]['after'], 'DIVIDE([Revenue total], [Quantity total])')
            self.assertEqual(rows[0]['candidates'][0]['steps'][0]['rule'], 'OPT006')

    def test_guarded_division_rejects_different_denominator_or_alternate(self):
        for expr in ['IF([Quantity total]<>0,[Revenue total]/[Revenue total],BLANK())',
                     'IF([Quantity total]<>0,[Revenue total]/[Quantity total],0)',
                     'IF([Quantity total]==0,[Revenue total]/[Quantity total],BLANK())',
                     'IF([Label]<>0,[Revenue total]/[Label],BLANK())']:
            self.assertFalse(plan_rewrites(fixture(expr)).candidates, expr)

    def test_guarded_division_rejects_text_helper_and_volatile_chain(self):
        expr = 'IF([Quantity total]<>0,[Revenue total]/[Quantity total],BLANK())'
        for bad in ['"7"', 'RAND()', 'UnknownCall()']:
            model = fixture(expr)
            model.measure('Quantity total').expression = bad
            self.assertFalse(plan_rewrites(model).candidates, bad)

    def test_constant_format_expression_is_preserved_as_static_format(self):
        source = {'model': {'tables': [{'name': 'Ledger', 'columns': [
            {'name': 'Amount', 'dataType': 'int64'}], 'measures': [{'name': 'Total',
            'expression': 'SUMX(Ledger,Ledger[Amount])',
            'formatStringDefinition': {'expression': '/* literal */ "#,0;(#,0)"'}}]}]}}
        model = parse_model(source)
        self.assertFalse(any(e.kind == 'format_string' for e in model.entities.values()))
        created = create_measure(model, 'Total').data
        self.assertEqual(created['outcome'], 'rewritten')
        self.assertEqual(model.measure('Total').metadata['format_string'], '#,0;(#,0)')
        self.assertEqual(created['measure_object']['formatString'], '#,0;(#,0)')

    def test_contextual_or_malformed_formats_remain_guarded(self):
        for expression in ['IF([Result]>0,"0.0","0")', '"0" & "0"', '"unterminated']:
            model = fixture('SUMX(Transactions,Transactions[Quantity])')
            # Exercise the same normalization used for BIM, TMDL and Desktop recovery.
            from semantic_hotspots.parser import _format_expression
            _format_expression(model, model.measure('Result'), expression)
            self.assertTrue(any(e.kind == 'format_string' for e in model.entities.values()))
            self.assertFalse(plan_rewrites(model, measure='Result').candidates)

    def test_empty_literal_format_is_explicitly_preserved(self):
        model = fixture('SUMX(Transactions,Transactions[Quantity])')
        from semantic_hotspots.parser import _format_expression
        _format_expression(model, model.measure('Result'), '""')
        self.assertEqual(create_measure(model, 'Result').data['measure_object']['formatString'], '')

    def test_exact_denominator_variable_uses_fresh_local_name(self):
        model = fixture('VAR __SH_Denominator = 1 RETURN IF([Quantity total]<>0,[Revenue total]/[Quantity total],BLANK())')
        candidates = plan_rewrites(model, measure='Result').candidates
        candidate = next(c for c in candidates if c['steps'][0]['rule'] == 'OPT007')
        self.assertNotIn('DIVIDE(', candidate['after'])
        self.assertIn('VAR __SH_Denominator_ = [Quantity total]', candidate['after'])
        self.assertIn('[Revenue total] / __SH_Denominator_', candidate['after'])

    def test_floating_point_division_and_variable_rewrites_are_retained(self):
        model = fixture('IF([Quantity total]<>0,[Revenue total]/[Quantity total],BLANK())')
        for e in model.entities.values():
            if e.kind == 'column' and e.name in {'Quantity', 'Revenue'}:e.metadata['data_type'] = 'double'
        row = plan_rewrites(model, measure='Result').measures[0]
        self.assertFalse(row['candidates'])
        self.assertTrue(any('aggregation order' in d for d in row['diagnostics']))

    def test_constant_denominator_is_left_alone(self):
        self.assertFalse(plan_rewrites(fixture('IF(2<>0,[Revenue total]/2,BLANK())')).candidates)
