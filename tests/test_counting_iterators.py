"""Independent counterexamples for guarded row counting and lexical syntax."""
import unittest
from semantic_hotspots import parse_model, plan_rewrites
from semantic_hotspots.dax import tokenize
from semantic_hotspots.optimizer import logical_plan


def model(expression):
    return parse_model({'model': {'tables': [{'name': 'Tasks', 'columns': [
        {'name': 'Started', 'dataType': 'dateTime'},
        {'name': 'Ended', 'dataType': 'dateTime'},
        {'name': 'Units', 'dataType': 'int64'}],
        'measures': [{'name': 'Result', 'expression': expression}]}]}})


class CountingIteratorTests(unittest.TestCase):
    def test_constant_one_retains_table_expression(self):
        for fn in ['COUNTX', 'SUMX']:
            for table in ['Tasks', 'VALUES(Tasks[Units])', 'FILTER(Tasks,Tasks[Units]>4)']:
                expr = fn+'('+table+',1)'
                candidates = plan_rewrites(model(expr), measure='Result').candidates
                self.assertEqual(candidates[0]['after'], 'COUNTROWS('+table+')')
                self.assertEqual(candidates[0]['steps'][0]['rule'], 'OPT008')

    def test_other_values_and_row_expressions_are_not_counted_as_rows(self):
        for body in ['0', '2', 'BLANK()', 'TRUE()', 'Tasks[Units]+1', '1/0']:
            for fn in ['COUNTX', 'SUMX']:
                candidates = plan_rewrites(model(fn+'(Tasks,'+body+')'), measure='Result').candidates
                self.assertFalse(any(s['rule']=='OPT008' for c in candidates for s in c['steps']), body)

    def test_nested_scope_and_single_underscore_variable(self):
        expr = 'VAR _Rows = FILTER(Tasks,Tasks[Units]>4) RETURN COUNTX(_Rows,1)'
        candidates = plan_rewrites(model(expr), measure='Result').candidates
        self.assertEqual(candidates[0]['after'], 'VAR _Rows = FILTER(Tasks,Tasks[Units]>4) RETURN COUNTROWS(_Rows)')
        for bad in ['VAR _ = 1 RETURN _', 'VAR Tasks=1 RETURN Tasks', 'VAR _x=1 VAR _x=2 RETURN _x']:
            m = model(bad)
            self.assertTrue(logical_plan(bad, m, m.measure('Result'))[1])

    def test_date_boundary_count_keeps_date_expression_and_enum(self):
        expr = 'SUMX(FILTER(Tasks,DATEDIFF(Tasks[Started],Tasks[Ended],DAY)<=2),1)'
        candidates = plan_rewrites(model(expr), measure='Result').candidates
        self.assertEqual(candidates[0]['after'], 'COUNTROWS(FILTER(Tasks,DATEDIFF(Tasks[Started],Tasks[Ended],DAY)<=2))')
        for bad in ['DAY', 'DATEDIFF(Tasks[Started],Tasks[Ended],"DAY")',
                    'DATEDIFF(Tasks[Started],Tasks[Ended],UNKNOWN)',
                    'DATEDIFF(Tasks[Started],Tasks[Ended],DAY,1)']:
            m = model(bad)
            self.assertTrue(logical_plan(bad, m, m.measure('Result'))[1], bad)

    def test_only_direct_row_invariant_counts_are_hoisted(self):
        for body in ['COUNT(Tasks[Units])','COUNTROWS(Tasks)']:
            expr = 'SUMX(Tasks,'+body+')'
            c = plan_rewrites(model(expr), measure='Result').candidates
            expected = "COUNT('Tasks'[Units])" if body.startswith('COUNT(') else "COUNTROWS('Tasks')"
            self.assertEqual(c[0]['after'], '(COUNTROWS(Tasks) * '+expected+')')
            self.assertEqual(c[0]['steps'][0]['rule'], 'OPT009')
        for body in ['CALCULATE(COUNT(Tasks[Units]))','SUM(Tasks[Units])']:
            c = plan_rewrites(model('SUMX(Tasks,'+body+')'), measure='Result').candidates
            self.assertFalse(any(s['rule']=='OPT009' for x in c for s in x['steps']))
        expr = 'SUMX(Tasks,SUMX(Tasks,COUNT(Tasks[Units])))'
        self.assertFalse(plan_rewrites(model(expr), measure='Result').candidates)

    def test_hoisted_count_retains_its_cross_table_column_binding(self):
        from semantic_hotspots.model import Entity
        expr = 'SUMX(Adjustments,COUNT([Units]))'
        m = model(expr)
        m.add_table('Adjustments')
        m.add(Entity('column','Adjustments','Units',metadata={'data_type':'int64'}))
        candidate = plan_rewrites(m, measure='Result').candidates[0]
        self.assertEqual(candidate['after'], "(COUNTROWS(Adjustments) * COUNT('Adjustments'[Units]))")
        before = candidate['logical_before']['children'][1]['children'][0]['target']
        after = candidate['logical_after']['children'][0]['children'][1]['children'][0]['target']
        self.assertEqual(before, after)

    def test_decimal_and_exponent_tokens_keep_exact_source_spans(self):
        expr = 'VAR _limit=.75 RETURN IF(1.>=_limit,1e-3,2E+4)'
        m = model(expr)
        self.assertFalse(logical_plan(expr, m, m.measure('Result'))[1])
        self.assertEqual([t.value for t in tokenize(expr) if t.kind=='NUMBER'], ['.75','1.','1e-3','2E+4'])
        for t in tokenize(expr):
            if t.kind=='NUMBER': self.assertEqual(expr[t.start:t.end], t.value)
        for bad in ['1..2','1e','2E+']:
            m = model(bad)
            self.assertTrue(logical_plan(bad,m,m.measure('Result'))[1],bad)
