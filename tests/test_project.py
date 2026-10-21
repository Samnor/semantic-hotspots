"""Generic, independently invented PBIP showcase regressions."""
import base64
import copy
import gzip
import hashlib
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout,redirect_stderr
from pathlib import Path
from unittest.mock import patch

from semantic_hotspots import export_project,load_matrices,load_model,ModelError,DependencyGraph
from semantic_hotspots.cli import main
from test_matrices import model,matrix


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'showcase'
        self.addCleanup(self.tmp.cleanup)
        self.config={'fact_rows':32,'dimension_rows':4}

    def export(self,source=None,spec=None,**kwargs):
        return export_project(source or model(),[spec or matrix()],self.root,config=self.config,**kwargs)

    def read(self,path):return json.loads((self.root/path).read_text(encoding='utf-8'))

    def test_one_page_one_visual_and_only_selected_measure_chains(self):
        result=self.export()
        self.assertTrue(result.project_path.is_file());self.assertTrue(result.model_path.is_file())
        visuals=list(self.root.glob('Showcase.Report/definition/pages/*/visuals/*/visual.json'))
        self.assertEqual(len(visuals),1)
        visual=json.loads(visuals[0].read_text(encoding="utf-8"))['visual']
        self.assertEqual(visual['visualType'],'pivotTable')
        self.assertEqual(len(visual['query']['queryState']['Values']['projections']),4)
        self.assertEqual(len(list(self.root.glob('Showcase.Report/definition/pages/*/page.json'))),1)
        self.assertEqual(result.manifest['visual_count'],1)
        self.assertEqual(self.read('Showcase.Report/definition/version.json')['version'],'2.0.0')
        self.assertEqual(self.read('Showcase.SemanticModel/model.bim')['model']['defaultPowerBIDataSourceVersion'],'powerBI_V3')
        names={m['name'] for t in self.read('Showcase.SemanticModel/model.bim')['model']['tables'] for m in t.get('measures',[])}
        self.assertEqual(names,{'Revenue','Cost total','Margin','Candidate Revenue','Candidate Margin'})
        self.assertIn('not run',(self.root/'showcase.html').read_text(encoding="utf-8"))

    def test_summary_preserves_keys_and_recomposes_ratio_from_components(self):
        result=self.export();bim=self.read('Showcase.SemanticModel/model.bim')
        table=next(t for t in bim['model']['tables'] if t['name']==result.manifest['summary_table'])
        expr=table['partitions'][0]['source']['expression']
        self.assertIn("'Sales'[CustomerKey]",expr);self.assertIn("'Sales'[DateKey]",expr)
        self.assertIn("SUM('Sales'[Amount])",expr);self.assertIn("COUNTROWS('Sales')",expr)
        bindings={c['name']:c['sourceColumn'] for c in table['columns']}
        self.assertEqual(bindings['CustomerKey'],'Sales[CustomerKey]')
        self.assertEqual(bindings['DateKey'],'Sales[DateKey]')
        self.assertEqual(bindings['State 1'],'[State 1]')
        candidate=result.manifest['changes'][1]['candidate']['expression']
        self.assertIn('DIVIDE',candidate);self.assertIn("'Showcase Summary'[State 1]",candidate)
        self.assertNotIn("'Sales'[",candidate)
        self.assertEqual({r['toTable'] for r in result.manifest['added_relationships']},{'Customer','Calendar'})
        self.assertTrue(all(r['crossFilteringBehavior']=='oneDirection' for r in result.manifest['added_relationships']))
        generated=load_model(result.model_path);graph=DependencyGraph(generated)
        self.assertTrue(graph.to_dict()['is_acyclic'])

    def test_average_count_and_distinct_keys_have_recomposable_states(self):
        data=model();data['model']['tables'][0]['measures'] += [
            {'name':'Rows','expression':'COUNTROWS(Sales)'},{'name':'Maximum','expression':'MAX(Sales[Amount])'}]
        result=self.export(data,matrix(values=[['Sales','Average amount'],['Sales','Customers'],['Sales','Rows'],['Sales','Maximum']]))
        changes={c['original_name']:c['candidate']['expression'] for c in result.manifest['changes']}
        self.assertIn('DIVIDE(SUM(',changes['Average amount'])
        self.assertIn('DISTINCTCOUNT(',changes['Customers'])
        self.assertIn('[CustomerKey]',changes['Customers'])
        self.assertIn('MAX(',changes['Maximum'])
        self.assertIn('SUM(',changes['Rows'])
        bindings=result.manifest['component_bindings']
        self.assertIn('COUNT',{b['function'] for b in bindings})
        self.assertIn('COUNTROWS',{b['function'] for b in bindings})

    def test_embedded_csv_roundtrips_and_project_needs_no_absolute_paths(self):
        self.export();bim=self.read('Showcase.SemanticModel/model.bim')
        manifest=self.read('simulation/simulation.json')
        for entry in manifest['tables']:
            if entry['kind']!='synthetic-physical':continue
            table=next(t for t in bim['model']['tables'] if t['name']==entry['name'])
            expr=table['partitions'][0]['source']['expression']
            token=re.search(r'Binary.FromText\("([A-Za-z0-9+/=]+)"',expr).group(1)
            data=gzip.decompress(base64.b64decode(token))
            self.assertEqual(data,(self.root/'simulation'/entry['csv']).read_bytes())
            self.assertEqual(hashlib.sha256(data).hexdigest(),entry['sha256'])
        content=json.dumps(bim)
        self.assertNotIn('File.Contents',content);self.assertNotIn('SyntheticDataRoot',content)
        self.assertNotIn(str(self.root),content)
        moved=self.root.with_name('moved');self.root.rename(moved)
        report=json.loads((moved/'Showcase.Report/definition.pbir').read_text(encoding="utf-8"))
        target=(moved/'Showcase.Report'/report['datasetReference']['byPath']['path']).resolve()
        self.assertTrue((target/'model.bim').is_file())

    def test_source_stays_unchanged_and_connections_unrelated_objects_are_excluded(self):
        source=model();source['model']['dataSources']=[{'name':'SENTINEL_CONNECTION'}]
        source['model']['tables'][0]['partitions'][0]['source']={'type':'m','expression':'SENTINEL_SOURCE_QUERY'}
        source['model']['tables'].append({'name':'Unrelated','columns':[{'name':'Detail','dataType':'string'}]})
        before=copy.deepcopy(source);self.export(source)
        self.assertEqual(source,before)
        bim=self.read('Showcase.SemanticModel/model.bim');text=json.dumps(bim)
        for word in ['SENTINEL_CONNECTION','SENTINEL_SOURCE_QUERY','Unrelated']:self.assertNotIn(word,text)
        self.assertNotIn('dataSources',bim['model']);self.assertNotIn('roles',bim['model'])

    def test_unknown_filter_drill_total_and_fact_contexts_fail_without_output(self):
        specs=[matrix(context_complete=False),matrix(filters=[{'table':'Customer','column':'Region','values':['North']}]),
               matrix(filter_measures=[['Sales','Revenue']]),matrix(drill_states=[{'name':'collapsed','row_depth':1}]),
               matrix(grand_total=False),matrix(unsupported=['Visual calculation']),matrix(rows=[['Sales','OrderKey']]),
               matrix(values=[['Sales','Order calculation']])]
        for spec in specs:
            with self.subTest(spec=spec),self.assertRaises(ModelError):self.export(spec=spec)
            self.assertFalse(self.root.exists())

    def test_ambiguous_multifact_and_security_contracts_are_not_exported(self):
        for mode in ['ambiguous','security','multifact']:
            source=model();spec=matrix()
            if mode=='ambiguous':
                rel=copy.deepcopy(source['model']['relationships'][0]);rel['name']='Second path';source['model']['relationships'].append(rel)
            if mode=='security':source['model']['roles']=[{'name':'Restricted'}]
            if mode=='multifact':
                source['model']['tables'].append({'name':'Budget','columns':[{'name':'Amount','dataType':'double'}],
                    'measures':[{'name':'Budget','expression':'SUM(Budget[Amount])'}]})
                spec['values'].append(['Budget','Budget'])
            with self.subTest(mode=mode),self.assertRaises(ModelError):self.export(source,spec)
            self.assertFalse(self.root.exists())

    def test_selection_skips_blocked_matrix_but_explicit_choice_fails(self):
        specs=[matrix(id='opaque',values=[['Sales','Order calculation']]),matrix(id='supported')]
        result=export_project(model(),specs,self.root,config=self.config)
        self.assertEqual(result.manifest['matrix_id'],'supported')
        self.assertEqual(result.manifest['skipped_matrices'][0]['matrix_id'],'opaque')
        with self.assertRaises(ModelError):export_project(model(),specs,self.root.with_name('other'),matrix_id='opaque')
        with self.assertRaises(ModelError):export_project(model(),specs,self.root.with_name('other'),matrix_id='missing')

    def test_prefix_contexts_have_separate_baseline_candidate_and_blank_validation(self):
        result=self.export()
        self.assertEqual(len(result.manifest['cases']),6)
        for case in result.manifest['cases']:
            for kind,query in case['queries'].items():
                text=(self.root/query['file']).read_text(encoding="utf-8")
                self.assertEqual(hashlib.sha256(text.encode()).hexdigest(),query['sha256'])
                self.assertEqual(hashlib.sha256((self.root/query['file']).read_bytes()).hexdigest(),query['sha256'])
                if kind=='validation':
                    self.assertIn('OriginalBlank_1',text);self.assertIn('CandidateBlank_1',text)
                    self.assertIn(' == ',text);self.assertIn('Equal_2',text)
                if kind=='baseline':self.assertNotIn('Candidate Revenue',text)
                if kind=='candidate':self.assertIn('Candidate Revenue',text)
        self.assertEqual(result.manifest['engine_validation'],'not_run')
        self.assertEqual(result.manifest['performance_status'],'unknown')
        self.assertIsNone(result.manifest['predicted_speedup'])

    def test_failed_staging_and_existing_output_do_not_leave_partial_project(self):
        with patch('semantic_hotspots.project._EMBED_LIMIT',1),self.assertRaises(ModelError):self.export()
        self.assertFalse(self.root.exists())
        self.assertFalse(list(self.root.parent.glob('.showcase-*')))
        self.root.mkdir();marker=self.root/'keep.txt';marker.write_text('keep')
        with self.assertRaises(ModelError):self.export()
        self.assertEqual(marker.read_text(encoding="utf-8"),'keep')
        self.assertEqual(list(self.root.iterdir()),[marker])

    def test_unknown_key_types_and_malformed_or_extra_config_are_rejected(self):
        data=model();data['model']['tables'][0]['columns'][0].pop('dataType')
        with self.assertRaises(ModelError):self.export(data)
        for settings in [[],{'tables':None},{'workloads':[{'name':'extra'}]},{'matrices':[matrix()]}]:
            with self.subTest(config=settings),self.assertRaises(ModelError):
                export_project(model(),[matrix()],self.root,config=settings)
        self.assertFalse(self.root.exists())

    def test_casefold_bindings_and_generated_names_do_not_collide(self):
        data=model();data['model']['tables'][0]['measures'].append({'name':'Candidate Revenue','expression':'1'})
        data['model']['tables'].append({'name':'showcase summary','columns':[{'name':'X','dataType':'int64'}]})
        result=self.export(data,matrix(rows=[['customer','region']],columns=[['calendar','month']]))
        self.assertEqual(result.manifest['summary_table'],'Showcase Summary 2')
        self.assertEqual(result.manifest['changes'][0]['candidate']['name'],'Candidate Revenue 2')
        imported=load_matrices(self.root/'Showcase.Report')[0]
        self.assertEqual(imported.rows,[['Customer','Region']])

    def test_snowflake_dimension_paths_survive_projection(self):
        data=model()
        data['model']['tables'][1]['columns'].append({'name':'CountryKey','dataType':'int64'})
        data['model']['tables'].append({'name':'Country','columns':[{'name':'Key','dataType':'int64'},{'name':'Name','dataType':'string'}]})
        data['model']['relationships'].append({'name':'CustomerCountry','fromTable':'Customer','fromColumn':'CountryKey','toTable':'Country','toColumn':'Key'})
        result=self.export(data,matrix(rows=[['Country','Name']]))
        raw=self.read('Showcase.SemanticModel/model.bim')['model']
        self.assertIn('Country',{t['name'] for t in raw['tables']})
        self.assertIn('CustomerCountry',{r['name'] for r in raw['relationships']})
        self.assertEqual({r['toTable'] for r in result.manifest['added_relationships']},{'Customer','Calendar'})

    def test_generated_names_and_source_types_remain_reviewable(self):
        self.export(spec=matrix(title='<script>unsafe</script>'))
        text=(self.root/'showcase.html').read_text(encoding="utf-8")
        self.assertIn('&lt;script&gt;',text);self.assertNotIn('<script>unsafe',text)
        self.assertEqual(load_model(self.root/'Showcase.SemanticModel/model.bim').measure('Candidate Revenue').metadata.get('description'),
                         'Unvalidated summary-table candidate for the declared showcase context only.')

    def test_cli_exports_or_reports_clean_error(self):
        source=self.root.parent/'source.bim';source.write_text(json.dumps(model()))
        specs=self.root.parent/'matrices.json';specs.write_text(json.dumps([matrix()]))
        with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            code=main(['project',str(source),'--matrices',str(specs),'--fact-rows','16','--dimension-rows','4','-o',str(self.root)])
            self.assertEqual(code,0)
            self.assertEqual(main(['project',str(source),'--matrices',str(specs),'--matrix','missing','-o',str(self.root.with_name('bad'))]),2)
