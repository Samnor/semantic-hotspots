"""Independent synthetic table schemas, lexical lifetimes and guarded candidates."""
import copy
import tempfile
import unittest
from pathlib import Path

from semantic_hotspots import create_measure, computation_dag, parse_model, plan_rewrites
from semantic_hotspots.dax import Resolver
from semantic_hotspots.optimizer import logical_plan
from test_optimizer import source


def walk(node):
    yield node
    for child in node.children:
        yield from walk(child)


class TableScopeTests(unittest.TestCase):
    def test_extension_survives_table_variables_filter_and_row_local_let(self):
        text=('VAR groups = VALUES(Sales[Category])\n'
              'VAR scored = ADDCOLUMNS(groups,"@score",VAR n = CALCULATE(SUMX(Sales,Sales[Amount])) RETURN IF(ISBLANK(n),BLANK(),n))\n'
              'VAR keptRows = FILTER(scored,NOT ISBLANK([@score]))\n'
              'RETURN IF(ISEMPTY(keptRows),BLANK(),SUMX(keptRows,[@score]))')
        model=parse_model(source(text));before=copy.deepcopy(model)
        info=Resolver(model).scan(text,model.measure('Total'))
        self.assertEqual(info.virtual_columns,['@score','@score'])
        self.assertFalse(info.unresolved);self.assertFalse(info.reference_blockers)
        result=create_measure(model,'Total').data
        self.assertEqual(result['outcome'],'rewritten')
        self.assertEqual(result['expression'],text.replace('SUMX(Sales,Sales[Amount])','SUM(Sales[Amount])').replace('SUMX(keptRows,[@score])','SUMX(scored, [@score])'))
        self.assertTrue(result['validation_available'])
        self.assertEqual(model,before)
        for key in ['original','proposed']:
            dag=result['dag_comparison'][key]
            self.assertTrue(dag['dependency_complete']);self.assertTrue(dag['logical_subset_validated'])
            self.assertTrue(dag['is_acyclic']);self.assertFalse(dag['unresolved'])
            definitions=[n for n in dag['nodes'] if n.get('operation')=='extension_definition']
            references=[n for n in dag['nodes'] if n.get('operation')=='extension_column']
            self.assertEqual(len(definitions),1);self.assertEqual(len(references),2)
            self.assertEqual(sum(e['kind']=='extension_reference' and e['to']==definitions[0]['id'] for e in dag['edges']),2)
            self.assertFalse(any(n.get('type')=='column' and n.get('name')=='@score' for n in dag['nodes']))
        with tempfile.TemporaryDirectory() as tmp:
            files=create_measure(model,'Total').write(Path(tmp)/'output')
            self.assertIn('SUM(Sales[Amount])',files['query'].read_text())
            self.assertTrue((Path(tmp)/'output/validate-total.dax').exists())

    def test_values_projects_only_the_named_column(self):
        text='VAR keys = VALUES(Sales[Category]) RETURN SUMX(keys,[Amount])'
        model=parse_model(source(text))
        tree,issues=logical_plan(text,model,model.measure('Total'))
        self.assertEqual(tree.operation,'opaque')
        self.assertIn('Cannot bind [Amount]',issues[0])
        self.assertFalse(create_measure(model,'Total').data['validation_available'])
        model=parse_model(source('VAR keys=VALUES(Sales[Category]) RETURN SUMX(keys,IF([Category]=="A",1,0))'))
        tree,issues=logical_plan(model.measure('Total').expression,model,model.measure('Total'))
        self.assertFalse(issues)
        column=next(n for n in walk(tree) if n.operation=='column' and n.value=='Category')
        self.assertEqual(column.table,'Sales')
        self.assertIn('Sales',column.row_context)

    def test_no_global_alias_visibility_or_sibling_alias_visibility(self):
        cases=[
            'VAR t=ADDCOLUMNS(Sales,"@n",1) RETURN [@n]',
            'VAR t=ADDCOLUMNS(Sales,"@n",1) RETURN SUMX(Sales,[@n])',
            'VAR t=ADDCOLUMNS(Sales,"@n",1) RETURN SUMX(VALUES(Sales[Category]),[@n])',
            'SUMX(ADDCOLUMNS(Sales,"@n",[@n]),[@n])',
            'SUMX(ADDCOLUMNS(Sales,"@a",1,"@b",[@a]),[@b])',
            'VAR t=SELECTCOLUMNS(Sales,"@n",1) RETURN SUMX(t,[Amount])',
            'VAR t=ADDCOLUMNS(Sales,"@n",1) RETURN SUMX(t,t[@n])',
            'SUMX(Sales,[@undefined])',
            'SUMX(UnknownTable(),[Dependent])',
        ]
        for text in cases:
            with self.subTest(text=text):
                result=create_measure(source(text),'Total').data
                self.assertEqual(result['outcome'],'unchanged_copy')
                self.assertFalse(result['validation_available'])
                self.assertTrue(result['dependency_issues'])

    def test_duplicate_aliases_and_input_column_collisions_block_binding(self):
        for text in ['SUMX(ADDCOLUMNS(Sales,"@n",1,"@N",2),[@n])',
                     'SUMX(ADDCOLUMNS(Sales,"Amount",1),[Amount])',
                     'SUMX(ADDCOLUMNS(Sales,1,2),[Amount])',
                     'SUMX(SELECTCOLUMNS(Sales,"@n",1,"@N",2),[@n])']:
            with self.subTest(text=text):
                self.assertFalse(create_measure(source(text),'Total').data['validation_available'])

    def test_same_alias_in_independent_scopes_has_different_definition_identity(self):
        text=('VAR a=ADDCOLUMNS(Sales,"@x",1) '
              'VAR b=ADDCOLUMNS(Sales,"@x",2) '
              'RETURN SUMX(a,[@x])+SUMX(b,[@x])')
        model=parse_model(source(text));tree,issues=logical_plan(text,model,model.measure('Total'))
        self.assertFalse(issues)
        refs=[n.target for n in walk(tree) if n.operation=='extension_column']
        self.assertEqual(len(set(refs)),2)
        dag=computation_dag(model,'Total')
        self.assertTrue(dag['is_acyclic']);self.assertTrue(dag['dependency_complete'])
        self.assertEqual(sum(n.get('operation')=='extension_definition' for n in dag['nodes']),2)

    def test_nested_table_shadowing_and_outer_row_scope(self):
        text=('VAR t=ADDCOLUMNS(Sales,"@x",1) RETURN '
              'SUMX(t,[@x]+(VAR t=ADDCOLUMNS(Sales,"@x",2) RETURN SUMX(t,[@x])))')
        model=parse_model(source(text));tree,issues=logical_plan(text,model,model.measure('Total'))
        self.assertFalse(issues)
        self.assertEqual(len({n.target for n in walk(tree) if n.operation=='extension_column'}),2)
        self.assertTrue(computation_dag(model,'Total')['is_acyclic'])
        text='VAR t=ADDCOLUMNS(Sales,"@x",1) RETURN SUMX(t,SUMX(VALUES(Sales[Category]),[@x]))'
        model=parse_model(source(text));tree,issues=logical_plan(text,model,model.measure('Total'))
        self.assertFalse(issues)
        self.assertEqual(len(next(n for n in walk(tree) if n.operation=='extension_column').row_context),2)

    def test_selectcolumns_exposes_projection_only_and_retains_input_dependencies(self):
        text='VAR t=SELECTCOLUMNS(Sales,"@n",CALCULATE(SUMX(Sales,Sales[Amount]))) RETURN SUMX(t,[@n])'
        result=create_measure(source(text),'Total').data
        self.assertEqual(result['outcome'],'rewritten')
        dag=result['dag_comparison']['original']
        var=next(n for n in dag['nodes'] if n.get('label')=='VAR t')
        self.assertEqual(set(var['table_columns']),{'@n'})
        self.assertEqual(var['table_columns']['@n']['kind'],'extension_column')
        self.assertTrue(any(n.get('name')=='Amount' and n['type']=='column' for n in dag['nodes']))

    def test_model_measure_alias_collision_is_conservative_but_qualification_works(self):
        text='VAR t=ADDCOLUMNS(Sales,"Dependent",1) RETURN SUMX(t,[Dependent])'
        result=create_measure(source(text),'Total').data
        self.assertFalse(result['validation_available'])
        self.assertIn('ambiguous',' '.join(result['diagnostics']))
        # Qualification explicitly selects the model measure, not the extension.
        data=source('VAR t=ADDCOLUMNS(Sales,"Helper",1) RETURN SUMX(t,Sales[Helper])')
        data['model']['tables'][0]['measures'].append({'name':'Helper','expression':'1'})
        result=create_measure(data,'Total').data
        self.assertTrue(result['validation_available'])
        self.assertFalse(result['dependency_issues'])

    def test_unrelated_model_column_does_not_make_a_bound_alias_ambiguous(self):
        text='VAR t=SELECTCOLUMNS(Sales,"Amount",1) RETURN SUMX(t,[Amount])'
        model=parse_model(source(text));info=Resolver(model).scan(text,model.measure('Total'))
        self.assertFalse(info.unresolved);self.assertFalse(info.reference_blockers)
        self.assertEqual(info.virtual_columns,['Amount'])
        self.assertNotIn('column:Sales/Amount',{r.target for r in info.references})
        result=create_measure(model,'Total').data
        self.assertTrue(result['validation_available'])

    def test_escaped_unicode_aliases_retain_exact_identity_in_exported_dags(self):
        for alias,reference in [('a]b','[a]]b]'),('@résumé','[@résumé]')]:
            with self.subTest(alias=alias):
                text='VAR t=ADDCOLUMNS(Sales,"'+alias+'",1) RETURN SUMX(t,'+reference+')'
                result=create_measure(source(text),'Total').data
                dag=result['dag_comparison']['original']
                self.assertTrue(dag['dependency_complete'])
                use=next(n for n in dag['nodes'] if n.get('operation')=='extension_column')
                self.assertEqual(use['expression'],reference)
                definition=next(n for n in dag['nodes'] if n.get('operation')=='extension_definition')
                self.assertEqual(definition['label'],'['+alias+'] :=')
                self.assertTrue(any(e['from']==use['id'] and e['to']==definition['id'] for e in dag['edges']))

    def test_today_and_missing_helpers_remain_separate_guards(self):
        text='VAR clock=TODAY() VAR t=ADDCOLUMNS(Sales,"@n",IF(YEAR(clock)>2000,[Empty],BLANK())) RETURN SUMX(t,[@n])'
        data=source(text);data['model']['tables'][0]['measures'].append({'name':'Empty'})
        result=create_measure(data,'Total').data
        self.assertEqual(result['outcome'],'unchanged_copy')
        self.assertEqual({x['code'] for x in result['dependency_issues']},{'missing_definition'})
        self.assertIn('the generated measure keeps live TODAY()',' '.join(result['diagnostics']))
        self.assertTrue(result['dag_comparison']['original']['operator_complete'])
        self.assertFalse(result['dag_comparison']['original']['reference_blockers'])
        self.assertEqual(result['expression'],text)

    def test_blank_only_filter_rule_requires_same_stored_column(self):
        prefix='VAR t=ADDCOLUMNS(Sales,"@n",Sales[Amount]) RETURN '
        text=prefix+'SUMX(FILTER(t,NOT ISBLANK([@n])),[@n])'
        result=create_measure(source(text),'Total').data
        self.assertEqual(result['expression'],prefix+'SUMX(t, [@n])')
        self.assertEqual(result['steps'][0]['rule'],'OPT003')
        self.assertTrue(result['validation_available'])
        for suffix in ['SUMX(FILTER(t,NOT ISBLANK([@n])),Sales[Amount])',
                       'SUMX(FILTER(t,NOT ISBLANK([@n])),[@n]+1)',
                       'SUMX(FILTER(t,ISBLANK([@n])),[@n])',
                       'SUMX(FILTER(t,NOT ISBLANK([@n]) && Sales[Amount]>0),[@n])',
                       'SUMX(FILTER(t,NOT ISBLANK([Dependent])),[Dependent])']:
            with self.subTest(suffix=suffix):
                self.assertFalse(plan_rewrites(source(prefix+suffix)).candidates)

    def test_table_var_and_extensions_do_not_become_physical_sum_shortcuts(self):
        for text in ['VAR t=Sales RETURN SUMX(t,Sales[Amount])',
                     'VAR t=ADDCOLUMNS(Sales,"@n",Sales[Amount]) RETURN SUMX(t,[@n])',
                     'SUMX(SELECTCOLUMNS(Sales,"@n",Sales[Amount]),[@n])']:
            with self.subTest(text=text):
                result=create_measure(source(text),'Total').data
                self.assertEqual(result['outcome'],'unchanged_copy')
                self.assertTrue(result['validation_available'])
                self.assertTrue(result['dag_comparison']['original']['logical_subset_validated'])

    def test_period_branch_operators_keep_blank_and_switch_structure(self):
        text=('VAR year=YEAR(DATE(2030,1,1)) '
              'VAR t=ADDCOLUMNS(VALUES(Sales[Category]),"@n",'
              'SWITCH(TRUE(),ISBLANK([Category]),BLANK(),year>2020,CALCULATE(MAX(Sales[Amount])),0)) '
              'RETURN IF(ISEMPTY(t),BLANK(),SUMX(t,[@n]))')
        model=parse_model(source(text));tree,issues=logical_plan(text,model,model.measure('Total'))
        self.assertFalse(issues)
        self.assertEqual({n.operation for n in walk(tree)} & {'YEAR','DATE','SWITCH','ISEMPTY','MAX'},
                         {'YEAR','DATE','SWITCH','ISEMPTY','MAX'})
        self.assertTrue(computation_dag(model,'Total')['dependency_complete'])
