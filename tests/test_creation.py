"""Named, root-scoped generation with independent optional visual contexts."""
import copy
import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from semantic_hotspots import ModelError, create_measure, parse_model, review_measure
from semantic_hotspots.cli import main
from semantic_hotspots.model import Entity
from test_focus import source


def visual(identity='north', value='North'):
    return {'id':identity,'title':identity,'values':[['Sales','Chosen']],
            'rows':[['Sales','Amount']],'filters':[{'table':'Sales','column':'Amount','values':[value]}],
            'context_complete':True}


class CreateMeasureTests(unittest.TestCase):
    def test_desktop_definition_and_query_preview_have_distinct_formats_and_instructions(self):
        created=create_measure(source(),'Chosen')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'new';files=created.write(root)
            definition=files['dax'].read_text(encoding='utf-8')
            query=files['query'].read_text(encoding='utf-8')
            self.assertTrue(definition.startswith('Chosen (candidate) =\n'))
            self.assertIn("DEFINE\n    MEASURE 'Sales'[Chosen (candidate)] =\nSUM(Sales[Amount])\n\nEVALUATE",query)
            self.assertIn('ROW("Value", \'Sales\'[Chosen (candidate)])',query)
            self.assertIn('not a runnable DAX query',(root/'HOW-TO-USE.txt').read_text(encoding='utf-8'))
            self.assertIn('DAX Studio query (preview)',files['html'].read_text(encoding='utf-8'))
            with zipfile.ZipFile(files['zip']) as z:
                self.assertIn('preview-query.dax',z.namelist());self.assertIn('HOW-TO-USE.txt',z.namelist())

    def test_preview_keeps_escaping_and_expression_ending_with_a_line_comment(self):
        data={'model':{'tables':[{'name':"Team's Totals",'measures':[{'name':'Résumé value','expression':'1 // preserve trailing comment'}]}]}}
        created=create_measure(data,'Résumé value')
        with tempfile.TemporaryDirectory() as tmp:
            query=created.write(Path(tmp)/'new')['query'].read_text(encoding='utf-8')
            qualified=Entity('measure',"Team's Totals",created.data['name']).qualified_name
            self.assertIn('MEASURE '+qualified+' =\n1 // preserve trailing comment\n\nEVALUATE',query)
            self.assertIn('ROW("Value", '+qualified+')',query)
            self.assertEqual(query.count('EVALUATE'),1)

    def test_preview_does_not_enable_withheld_validation_or_rewrite_the_source(self):
        data=source();data['model']['tables'][0]['measures'][0]['expression']='VAR x = TODAY() RETURN [Missing] + x'
        before=copy.deepcopy(data);created=create_measure(data,'Chosen')
        self.assertFalse(created.data['validation_available']);self.assertEqual(created.data['outcome'],'unchanged_copy')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'new';files=created.write(root)
            query=files['query'].read_text(encoding='utf-8')
            self.assertIn(created.data['expression'],query)
            self.assertIn('scalar preview only',query);self.assertIn('Not a correctness comparison',query)
            self.assertFalse((root/'validate-total.dax').exists())
            with zipfile.ZipFile(files['zip']) as z:self.assertNotIn('validate-total.dax',z.namelist())
        self.assertEqual(data,before)

    def test_creates_a_new_named_root_with_rewritten_dax_without_changing_any_existing_object(self):
        model=parse_model(source());before=copy.deepcopy(model)
        result=create_measure(model,'Chosen').data
        self.assertEqual(result['outcome'],'rewritten');self.assertEqual(result['expression'],'SUM(Sales[Amount])')
        self.assertNotEqual(result['measure_id'],model.measure('Chosen').id)
        self.assertEqual(result['dag_comparison']['proposed']['root'],result['measure_id'])
        self.assertIn('SUM(Sales[Amount])',result['definition']);self.assertFalse(result['visual_contexts'])
        self.assertTrue(result['validation_available']);self.assertEqual(result['performance_status'],'unknown')
        self.assertNotIn('Other',[n.get('name') for n in result['dag_comparison']['proposed']['nodes']])
        self.assertEqual(model,before)

    def test_applies_the_bounded_combination_without_a_speedup_claim(self):
        data=source();data['model']['tables'][0]['measures'][0]['expression']='SUMX(Sales, Sales[Amount]) + SUMX(Sales, Sales[Amount])'
        result=create_measure(data,'Chosen').data
        self.assertEqual(result['expression'],'SUM(Sales[Amount]) + SUM(Sales[Amount])')
        self.assertEqual(len(result['steps']),2);self.assertIn('no timing',result['selection_basis'])

    def test_multiple_visuals_keep_filters_separate_in_queries_and_dag_context(self):
        contexts=[visual('one',1),visual('two',2)];frozen=copy.deepcopy(contexts)
        result=create_measure(source(),'Chosen',visuals=contexts)
        self.assertEqual([c['matrix_id'] for c in result.data['visual_contexts']],['one','two'])
        self.assertEqual(result.data['dag_comparison']['context']['visuals'][1]['filters'][0]['values'],[2])
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'new';files=result.write(root)
            self.assertTrue(files['dax'].read_text().startswith('Chosen (candidate) =\nSUM('))
            with zipfile.ZipFile(root/'visual-context-queries.zip') as z:
                index=json.loads(z.read('index.json'));self.assertEqual(len(index['visuals']),2)
                for i,expected in [(1,1),(2,2)]:
                    context=json.loads(z.read(f'{i}/context.json'))
                    self.assertEqual(context['spec']['filters'][0]['values'],[expected])
                    query=z.read(next(n for n in z.namelist() if n.startswith(f'{i}/') and n.endswith('-validation.dax'))).decode()
                    self.assertIn('TREATAS({'+str(expected)+'}',query)
                    self.assertIn(result.data['name'],query);self.assertIn('ISBLANK',query)
            with zipfile.ZipFile(files['zip']) as z:
                self.assertIn('new-measure.dax',z.namelist());self.assertIn('dags/proposed.dag.json',z.namelist())
            with self.assertRaises(ModelError):result.write(root)
        self.assertEqual(contexts,frozen)

    def test_no_guarded_match_is_an_explicit_unchanged_copy_with_helpers(self):
        result=create_measure(source(),'Helper consumer').data
        self.assertEqual(result['outcome'],'unchanged_copy');self.assertEqual(result['expression'],'[Chosen] + 1')
        self.assertIn('No supported guarded rewrite',result['explanation'])
        self.assertIn('Chosen',[n.get('name') for n in result['dag_comparison']['proposed']['nodes']])
        self.assertFalse(result['steps'])

    def test_unique_names_missing_definitions_and_incomplete_chains_are_handled_honestly(self):
        data=source();data['model']['tables'][0]['measures'].append({'name':'Chosen (CANDIDATE)','expression':'0'})
        self.assertEqual(create_measure(data,'Chosen').data['name'],'Chosen (candidate) 2')
        data['model']['tables'][0]['measures'][0]['expression']=None
        with self.assertRaisesRegex(ModelError,'no expression'):create_measure(data,'Chosen')
        data['model']['tables'][0]['measures'][0]['expression']='[Missing helper] + 1'
        result=create_measure(data,'Chosen');self.assertFalse(result.data['validation_available'])
        with tempfile.TemporaryDirectory() as tmp:
            result.write(Path(tmp)/'new');self.assertFalse((Path(tmp)/'new/validate-total.dax').exists())

    def test_visual_binding_and_duplicate_guards_and_partial_contexts(self):
        bad=visual();bad['values']=[['Sales','Other']]
        with self.assertRaisesRegex(ModelError,'directly display'):create_measure(source(),'Chosen',visuals=[bad])
        with self.assertRaisesRegex(ModelError,'once'):create_measure(source(),'Chosen',visuals=[visual(),visual()])
        partial=visual();partial['context_complete']=False
        result=create_measure(source(),'Chosen',visuals=[partial]).data
        self.assertEqual(result['outcome'],'rewritten');self.assertFalse(result['visual_contexts'][0]['workloads'])
        self.assertTrue(result['visual_contexts'][0]['diagnostics'])
        with self.assertRaises(ModelError):review_measure(source(),'Chosen',landing_matrix=visual(),landing_matrices=[])

    def test_cli_creates_one_named_measure_with_optional_repeated_visual_flags(self):
        with tempfile.TemporaryDirectory() as tmp,redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            root=Path(tmp);(root/'model.bim').write_text(json.dumps(source()))
            (root/'visuals.json').write_text(json.dumps({'matrices':[visual('one',1),visual('two',2)]}))
            args=['create-measure',str(root/'model.bim'),'Chosen','--table','Sales']
            self.assertEqual(main(args+['-o',str(root/'plain')]),0)
            self.assertEqual(main(args+['--matrices',str(root/'visuals.json'),'--visual','one','--visual','two','-o',str(root/'many')]),0)
            saved=json.loads((root/'many/created-measure.json').read_text())
            self.assertEqual(len(saved['visual_contexts']),2)
            self.assertEqual(main(args+['--visual','missing','-o',str(root/'invalid')]),2)
