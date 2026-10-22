"""Invented text-detail chains and lexical schemas; no external model fixtures."""
import copy
import unittest

from semantic_hotspots import create_measure, parse_model, helper_requirements
from semantic_hotspots.dax import Resolver
from semantic_hotspots.optimizer import logical_plan
from test_table_scopes import walk
from test_optimizer import source


def text_model():
    return {'name':'Fictional text scope lab','compatibilityLevel':1601,'model':{
        'culture':'en-US','tables':[
            {'name':'Sales','columns':[{'name':n,'dataType':t,'sourceColumn':n} for n,t in
                [('Amount','int64'),('Region','string'),('Year','int64')]],
             'partitions':[{'name':'Sales','mode':'import'}],
             'measures':[
                {'name':'Revenue','expression':'SUMX(Sales,Sales[Amount])'},
                {'name':'Delta','expression':'[Revenue]-10'},
                {'name':'Ready','expression':'VAR years=SELECTCOLUMNS(VALUES(Sales[Year]),"Year",Sales[Year]) RETURN COUNTROWS(FILTER(years,[Year]>=2024))'},
                {'name':'Details','expression':'''VAR scored=ADDCOLUMNS(VALUES(Sales[Region]),"@delta",[Delta])
VAR negativeRows=FILTER(scored,[@delta]<0 && NOT ISBLANK([@delta]))
RETURN IF(ISBLANK([Ready]) || COUNTROWS(negativeRows)=0,BLANK(),
    CONCATENATEX(negativeRows,"- " & Sales[Region] & ": " & FORMAT([@delta],"0.0","en-US"),UNICHAR(10),Sales[Region],ASC))'''}]}]}}


class TextIteratorTests(unittest.TestCase):
    def test_text_chain_rewrites_real_helper_path_and_preserves_output_structure(self):
        model=parse_model(text_model());before=copy.deepcopy(model)
        result=create_measure(model,'Details').data
        self.assertEqual(result['outcome'],'rewritten')
        self.assertIn("SUM('Sales'[Amount])",result['expression'])
        for token in ['CONCATENATEX','FORMAT','UNICHAR(10)',"'Sales'[Region],ASC",'COUNTROWS(negativeRows)=0']:
            self.assertIn(token,result['expression'])
        self.assertTrue(result['validation_available']);self.assertFalse(result['dependency_issues'])
        self.assertNotEqual(model.measure('Details').expression,result['expression'])
        self.assertEqual(model,before)
        for name in ['Ready','Details']:
            entity=model.measure(name);info=Resolver(model).scan(entity.expression,entity)
            self.assertFalse(info.unresolved);self.assertFalse(info.reference_blockers)
            self.assertFalse(any('overlaps model metadata' in d for d in info.diagnostics))
        for key in ['original','proposed']:
            self.assertTrue(result['dag_comparison'][key]['dependency_complete'])
            self.assertTrue(result['dag_comparison'][key]['logical_subset_validated'])

    def test_expression_and_sort_keys_use_input_schema_delimiter_does_not(self):
        text='VAR t=SELECTCOLUMNS(Sales,"Amount",Sales[Amount]) RETURN CONCATENATEX(t,[Amount],",",[Amount],DESC,[Amount],ASC)'
        model=parse_model(source(text));tree,issues=logical_plan(text,model,model.measure('Total'))
        self.assertFalse(issues)
        refs=[n for n in walk(tree) if n.operation=='extension_column']
        self.assertEqual(len(refs),3);self.assertEqual(len({n.target for n in refs}),1)
        self.assertTrue(all(n.row_context for n in refs))
        self.assertEqual(tree.result_type,'string')
        self.assertFalse(Resolver(model).scan(text,model.measure('Total')).unresolved)
        for expr in [text.replace('",",[Amount]','[Amount],[Amount]'),
                     'VAR t=SELECTCOLUMNS(Sales,"Year",1) RETURN CONCATENATEX(Sales,[Year],",")']:
            result=create_measure(source(expr),'Total').data
            self.assertFalse(result['validation_available'])
            self.assertFalse(helper_requirements(source(expr),'Total'))

    def test_supported_order_arguments_and_default_are_retained(self):
        for suffix in ['',',","',',",",Sales[Amount]', *[',",",Sales[Amount],'+o for o in ['ASC','DESC','0','1','TRUE','FALSE','TRUE()','FALSE()']]]:
            text='CONCATENATEX(Sales,FORMAT(Sales[Amount],"0")'+suffix+')'
            model=parse_model(source(text));tree,issues=logical_plan(text,model,model.measure('Total'))
            self.assertFalse(issues,text);self.assertEqual(tree.result_type,'string')
            self.assertEqual(create_measure(model,'Total').data['expression'],text)
        for expr in ['CONCATENATEX(Sales,Sales[Amount],",",Sales[Amount],2)',
                     'CONCATENATEX(Sales,Sales[Amount],",",Sales[Amount],"ASC")',
                     'CONCATENATEX(Sales,Sales[Amount],",",Sales[Amount],Unknown())',
                     'CONCATENATEX(Sales,Sales[Amount],Sales)',
                     'CONCATENATEX(Sales)', 'FORMAT(Sales,"0")','FORMAT(1,2)','UNICHAR(Sales)',
                     'SUMX(Sales,Sales[Amount])+ASC']:
            self.assertEqual(create_measure(source(expr),'Total').data['outcome'],'unchanged_copy',expr)

    def test_ampersand_precedence_and_associativity(self):
        text='"x" & 1+2*3 & 4 == "x74" && NOT 1=2'
        model=parse_model(source(text));tree,issues=logical_plan(text,model,model.measure('Total'))
        self.assertFalse(issues)
        self.assertEqual(tree.value,'&&')
        comparison=tree.children[0];self.assertEqual(comparison.value,'==')
        concat=comparison.children[0];self.assertEqual(concat.value,'&')
        self.assertEqual(concat.children[0].value,'&')
        add=concat.children[0].children[1];self.assertEqual(add.value,'+')
        self.assertEqual(add.children[1].value,'*')
        self.assertEqual(concat.result_type,'string')

    def test_true_missing_helpers_remain_recoverable_scope_errors_do_not(self):
        self.assertEqual(len(helper_requirements(source('[Absent]'),'Total')),1)
        for text in ['VAR t=ADDCOLUMNS(Sales,"Dependent",1) RETURN CONCATENATEX(t,[Dependent],",")',
                     'VAR t=SELECTCOLUMNS(Sales,"Year",1) RETURN CONCATENATEX(t,[Year] & Unknown(),",")']:
            result=create_measure(source(text),'Total').data
            self.assertEqual(result['outcome'],'unchanged_copy')
            self.assertFalse(result['dag_comparison']['original']['logical_subset_validated'])
            self.assertFalse(helper_requirements(source(text),'Total'))
