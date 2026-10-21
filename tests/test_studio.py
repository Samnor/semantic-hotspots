"""Local workflow and HTTP-boundary regressions with synthetic fixtures."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from semantic_hotspots import ModelError
from semantic_hotspots.studio import make_server


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.server=make_server(self.root,self.root/'output')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.origin=f'http://127.0.0.1:{self.server.server_port}'
        self.headers={'Authorization':'Bearer '+self.server.studio.token,'Origin':self.origin,'Content-Type':'application/json'}

    def request(self,method,path,body=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        conn.request(method,path,json.dumps(body) if body is not None else None,headers or {})
        response=conn.getresponse();data=response.read();status=response.status;h=dict(response.getheaders());conn.close()
        return status,data,h

    def test_session_origin_and_host_protect_workspace_and_actions(self):
        self.assertEqual(self.request('GET','/')[0],200)
        self.assertEqual(self.request('GET','/api/state')[0],403)
        self.assertEqual(self.request('GET','/api/state',headers=self.headers)[0],200)
        self.assertEqual(self.request('POST','/api/session',{},self.headers)[0],200)
        wrong=dict(self.headers,Origin='https://elsewhere.example')
        self.assertEqual(self.request('POST','/api/action',{'action':'demo'},wrong)[0],403)
        wrong=dict(self.headers,Host='elsewhere.example')
        self.assertEqual(self.request('GET','/api/state',headers=wrong)[0],403)
        cookie=self.request('POST','/api/session',{},self.headers)[2]['Set-Cookie']
        self.assertIn('HttpOnly',cookie);self.assertIn('SameSite=Strict',cookie)

    def test_browser_is_confined_and_does_not_serve_workspace_files(self):
        (self.root/'input.bim').write_text('{}')
        self.assertEqual(self.request('GET','/api/browse?path=..',headers=self.headers)[0],400)
        status,body,_=self.request('GET','/api/browse',headers=self.headers)
        self.assertEqual(status,200);self.assertEqual(json.loads(body)['entries'][0]['name'],'input.bim')
        self.assertEqual(self.request('GET','/input.bim',headers=self.headers)[0],404)
        studio=self.server.studio
        key,root=studio.new_artifact();root.mkdir();(root/'report.html').write_text('Synthetic')
        path=f'/artifacts/{key}/report.html'
        self.assertEqual(self.request('GET',path)[0],403)
        cookie={'Cookie':'studio='+studio.token}
        self.assertEqual(self.request('GET',path,headers=cookie)[0],200)
        self.assertEqual(self.request('GET',f'/artifacts/{key}/../input.bim',headers=cookie)[0],400)

    def test_bundled_demo_drives_analysis_generation_and_portable_download(self):
        studio=self.server.studio
        studio.dispatch('demo',{})
        state=studio.snapshot()
        self.assertTrue(state['measures']);self.assertTrue(state['matrices'])
        eligible=[m for m in state['matrices'] if m['eligible']]
        blocked=[m for m in state['matrices'] if not m['eligible']]
        self.assertTrue(eligible);self.assertTrue(blocked)
        with self.assertRaises(ModelError):studio.dispatch('generate',{'matrix':blocked[0]['id']})
        owner=studio.model.measure('Revenue').id
        studio.dispatch('measure',{'measure':owner,'landing_matrix':'monthly-margin'})
        studio.dispatch('generate',{'measure':owner,'matrix':'monthly-margin','fact_rows':50,'dimension_rows':4})
        project=studio.snapshot()['project']
        self.assertTrue(Path(project['directory'],'Showcase.pbip').is_file())
        self.assertEqual(project['manifest']['engine_validation'],'not_run')
        self.assertTrue(studio.artifact(project['key'],'showcase.zip').is_file())
        with self.assertRaises(ModelError):studio.dispatch('run',{'server':'localhost:123','database':'Unknown'})

    def test_matrix_review_keeps_unsupported_context_and_cannot_bypass_guards(self):
        studio=self.server.studio;studio.dispatch('demo',{})
        original=studio.specs[0].to_dict();original['unsupported']=['Unknown visual calculation']
        original['context_complete']=False
        from semantic_hotspots import MatrixSpec
        studio.specs=[MatrixSpec.from_dict(original)]
        with self.assertRaises(ModelError):studio.dispatch('matrix',{'id':original['id']})
        studio.dispatch('matrix',{'id':original['id'],'reviewed':True})
        self.assertFalse(studio.snapshot()['matrices'][0]['eligible'])
        self.assertIn('Unknown visual calculation',studio.snapshot()['matrices'][0]['blocked_reason'])

    def test_recorded_contexts_rank_first_without_inventing_matrix_title_matches(self):
        from test_performance import recording
        from semantic_hotspots import read_performance_analyzer
        studio=self.server.studio;studio.dispatch('demo',{})
        studio.performance=read_performance_analyzer(studio.model,recording())
        studio.analyze_current()
        state=studio.snapshot()
        self.assertEqual(state['measures'][0]['recorded_dax_context_ms'],200)
        self.assertEqual(state['matrices'][0]['id'],'monthly-margin')
        self.assertEqual(state['matrices'][0]['recorded_dax_context_ms'],200)
        studio.performance['queries'][0]['visual_id']='unmatched'
        studio.analyze_current()
        self.assertTrue(all(m['recorded_dax_context_ms'] is None for m in studio.snapshot()['matrices']))

    def test_discovery_hints_match_all_measure_definitions_without_auto_selection(self):
        studio=self.server.studio;studio.dispatch('demo',{})
        owner=studio.model.measure('Revenue').id
        studio.dispatch('measure',{'measure':owner,'landing_matrix':'monthly-margin'})
        studio.dispatch('generate',{'measure':owner,'matrix':'monthly-margin','fact_rows':30})
        project=studio.snapshot()['project']
        bim=json.loads(Path(project['directory'],'Showcase.SemanticModel/model.bim').read_text())
        def query(file,output,**kwargs):
            import csv
            with Path(output).open('w',newline='',encoding='utf-8') as stream:
                writer=csv.writer(stream)
                if Path(file).stem=='TABLES':
                    writer.writerow(['[ID]','[Name]']);writer.writerows((i,t['name']) for i,t in enumerate(bim['model']['tables']))
                else:
                    writer.writerow(['[TableID]','[Name]','[Expression]'])
                    writer.writerows((i,m['name'],m['expression']) for i,t in enumerate(bim['model']['tables']) for m in t.get('measures',[]))
        sessions=[{'server':'localhost:123','databases':['Fictional'],'error':None}]
        with patch('semantic_hotspots.studio.desktop_sessions',return_value=sessions),patch('semantic_hotspots.studio.query_to_csv',side_effect=query):
            studio.dispatch('discover',{})
        state=studio.snapshot()
        self.assertTrue(state['sessions'][0]['models'][0]['matches_project'])
        self.assertIsNone(state['result'])

    def test_progress_checkpoints_are_observed_work_and_elapsed_time_freezes(self):
        studio=self.server.studio;entered=threading.Event();release=threading.Event()
        self.addCleanup(release.set)
        now=[10.0]
        def work(action,data):
            now[0]=12
            studio.report({'stage':'rewrites','label':'Check rewrite proposals',
                           'completed':2,'total':10,'unit':'measures','detail':'Fictional measure'})
            entered.set();release.wait(10)
        with patch('semantic_hotspots.studio.time.monotonic',side_effect=lambda:now[0]),patch.object(studio,'dispatch',side_effect=work):
            worker=studio.start('inspect',{})
            self.assertTrue(entered.wait(2))
            with self.assertRaises(ModelError):studio.start('demo',{})
            now[0]=17
            snapshot=studio.snapshot();op=snapshot['operation']
            self.assertTrue(snapshot['busy']);self.assertEqual(op['completed'],2)
            self.assertEqual(op['elapsed_seconds'],7);self.assertEqual(op['stage_elapsed_seconds'],5)
            self.assertEqual(op['seconds_since_update'],5)
            self.assertEqual(next(s for s in op['stages'] if s['id']=='load')['status'],'completed')
            snapshot['operation']['completed']=9
            self.assertEqual(studio.snapshot()['operation']['completed'],2)
            now[0]=19;release.set();worker.join(2);self.assertFalse(worker.is_alive())
            now[0]=50
            finished=studio.snapshot()
            self.assertFalse(finished['busy']);self.assertEqual(finished['operation']['status'],'completed')
            self.assertEqual(finished['operation']['elapsed_seconds'],9)
            self.assertIn('Analysis complete',finished['operation']['message'])

    def test_failure_preserves_stage_and_new_attempt_resets_operation(self):
        studio=self.server.studio
        with patch.object(studio,'dispatch',side_effect=ModelError('Fictional input is missing')):
            worker=studio.start('inspect',{});worker.join(2)
        failed=studio.snapshot();first=failed['operation']['id']
        self.assertFalse(failed['busy']);self.assertEqual(failed['operation']['status'],'failed')
        self.assertEqual(failed['operation']['stages'][0]['status'],'failed')
        self.assertIn('Fictional input is missing',failed['error'])
        self.assertIsNone(failed['operation']['total'])
        worker=studio.start('demo',{});worker.join(10)
        self.assertFalse(worker.is_alive());done=studio.snapshot()
        self.assertIsNone(done['error']);self.assertNotEqual(first,done['operation']['id'])
        self.assertEqual(done['operation']['status'],'completed')
        self.assertTrue(all(s['status']=='completed' for s in done['operation']['stages']))
        self.assertEqual(done['operation']['inventory']['measures'],len(studio.model.measures))

    def test_analysis_and_matrix_guards_reuse_one_request_analysis(self):
        from semantic_hotspots.analyzer import analyze
        studio=self.server.studio
        with patch('semantic_hotspots.studio.analyze',wraps=analyze) as inspect, \
             patch('semantic_hotspots.analyzer.analyze',side_effect=AssertionError('Unexpected repeated analysis')):
            studio.dispatch('demo',{})
        self.assertEqual(inspect.call_count,1)
        state=studio.snapshot()
        self.assertTrue(any(m['eligible'] for m in state['matrices']))
        self.assertTrue(any(not m['eligible'] for m in state['matrices']))

    def test_one_measure_workflow_needs_no_matrix_and_retains_only_selected_proposals(self):
        studio=self.server.studio
        with self.assertRaises(ModelError):studio.dispatch('measure',{'measure':'anything'})
        studio.dispatch('demo',{})
        studio.specs=[]
        original=[m.expression for m in studio.model.measures]
        owner=studio.model.measure('Revenue iterator','Sales')
        from semantic_hotspots.optimizer import plan_rewrites
        self.assertIsNone(studio.analysis.rewrite_plan)
        with patch('semantic_hotspots.focus.plan_rewrites',wraps=plan_rewrites) as plan:
            studio.dispatch('measure',{'measure':owner.id})
        self.assertEqual(plan.call_count,1)
        self.assertEqual(plan.call_args.kwargs['measure'],owner.id)
        result=studio.snapshot()['measure_review']
        self.assertEqual(result['measure_id'],owner.id);self.assertEqual(len(result['candidates']),1)
        self.assertEqual(result['candidates'][0]['after'],'SUM(Sales[Amount])')
        self.assertIsNone(studio.snapshot()['project'])
        status,body,_=self.request('GET',result['candidates'][0]['validation'],headers=self.headers)
        self.assertEqual(status,200);self.assertIn(b'ISBLANK',body)
        studio.dispatch('measure-proposal',{'measure':owner.id,'expression':'SUM(Sales[Amount]) + 0'})
        custom=studio.snapshot()['measure_review']['custom'];self.assertIn('/proposal/',custom['url'])
        with self.assertRaises(ModelError):studio.dispatch('measure-proposal',{'measure':'another','expression':'1'})
        studio.dispatch('measure',{'measure':studio.model.measure('Revenue').id})
        self.assertFalse(studio.snapshot()['measure_review']['candidates'])
        self.assertIsNone(studio.snapshot()['measure_review']['custom'])
        self.assertEqual([m.expression for m in studio.model.measures],original)
        studio.dispatch('demo',{});self.assertIsNone(studio.snapshot()['measure_review'])

    def test_optional_landing_selection_is_root_scoped_and_clearing_removes_project(self):
        studio=self.server.studio;studio.dispatch('demo',{})
        owner=studio.model.measure('Revenue iterator').id
        studio.dispatch('measure',{'measure':owner})
        self.assertIsNone(studio.snapshot()['measure_review']['landing_visual'])
        with self.assertRaises(ModelError):studio.dispatch('generate',{'matrix':'iterator-review'})
        studio.dispatch('measure',{'measure':owner,'landing_matrix':'iterator-review'})
        review=studio.snapshot()['measure_review']
        self.assertEqual(review['landing_visual']['spec']['values'],[['Sales','Revenue iterator']])
        self.assertTrue(review['candidates'][0]['landing_queries'])
        studio.dispatch('generate',{'measure':owner,'matrix':'iterator-review','fact_rows':20})
        self.assertEqual(len(studio.snapshot()['project']['manifest']['changes']),1)
        studio.dispatch('measure',{'measure':owner,'landing_matrix':None})
        self.assertIsNone(studio.snapshot()['project']);self.assertIsNone(studio.snapshot()['result'])
        self.assertIsNone(studio.snapshot()['measure_review']['landing_visual'])
        self.assertIsNone(studio.snapshot()['measure_review']['candidates'][0]['landing_queries'])

    def test_optional_matrix_import_failure_leaves_measure_analysis_available(self):
        from test_landing import source
        (self.root/'model.bim').write_text(json.dumps(source()),encoding='utf-8')
        (self.root/'bad.json').write_text('{',encoding='utf-8')
        studio=self.server.studio;studio.dispatch('inspect',{'model':'model.bim','matrices':'bad.json'})
        self.assertTrue(studio.snapshot()['matrix_warning']);self.assertFalse(studio.specs)
        studio.dispatch('measure',{'measure':studio.model.measure('Iterator').id})
        self.assertTrue(studio.snapshot()['measure_review']['candidates'])

    def test_landing_choices_use_bound_value_slots_and_reject_unrelated_visuals(self):
        from semantic_hotspots import MatrixSpec
        studio=self.server.studio;studio.dispatch('demo',{})
        owner=studio.model.measure('Revenue iterator').id
        specs={s.id:s.to_dict() for s in studio.specs}
        # Case-insensitive binding, plus a second visual of the same selected measure.
        first=specs['iterator-review'];first['values']=[['sales','REVENUE ITERATOR']]
        second=dict(first,id='another-iterator',page_name='Overview')
        # A helper dependency or visual filter does not mean the measure is displayed.
        unrelated=specs['monthly-margin'];unrelated['filter_measures']=[['Sales','Revenue iterator']]
        unknown=dict(first,id='unknown-binding',values=[['Missing','Revenue iterator']])
        studio.specs=[MatrixSpec.from_dict(s) for s in [first,second,unrelated,unknown]]
        studio.analyze_current()
        choices=[c for c in studio.snapshot()['matrices'] if owner in c['measure_ids']]
        self.assertEqual({c['id'] for c in choices},{'iterator-review','another-iterator'})
        self.assertTrue(any('Overview' in c['visual_label'] for c in choices))
        studio.dispatch('measure',{'measure':owner})
        for action,data in [('measure',{'landing_matrix':'monthly-margin'}),
                            ('matrix',{'id':'monthly-margin','reviewed':True})]:
            with self.subTest(action=action),self.assertRaisesRegex(ModelError,'directly display'):
                studio.dispatch(action,dict(data,measure=owner))
        for choice in choices:
            studio.dispatch('measure',{'measure':owner,'landing_matrix':choice['id']})
            self.assertEqual(studio.snapshot()['measure_review']['landing_visual']['matrix_id'],choice['id'])
        studio.dispatch('measure',{'measure':owner})
        self.assertIsNone(studio.snapshot()['measure_review']['landing_visual'])

    def test_duplicate_visual_titles_are_disambiguated_without_title_based_binding(self):
        from semantic_hotspots import MatrixSpec
        studio=self.server.studio;studio.dispatch('demo',{})
        first=studio.specs[0].to_dict();second=dict(first,id='same-page/second-visual')
        studio.specs=[MatrixSpec.from_dict(first),MatrixSpec.from_dict(second)]
        studio.analyze_current()
        choices=studio.snapshot()['matrices']
        self.assertEqual(len({c['visual_label'] for c in choices}),2)
        self.assertTrue(all(c['id'] in c['visual_label'] for c in choices))

    def test_create_new_measure_is_one_action_with_zero_or_multiple_visuals(self):
        from semantic_hotspots import MatrixSpec
        studio=self.server.studio;studio.dispatch('demo',{})
        owner=studio.model.measure('Revenue iterator').id
        second=next(s.to_dict() for s in studio.specs if s.id=='iterator-review')
        second['id']='iterator-second';second['title']='Second iterator context'
        studio.specs.append(MatrixSpec.from_dict(second));studio.analyze_current()
        original=[(e.id,e.expression) for e in studio.model.entities.values()]
        studio.dispatch('create-measure',{'measure':owner,'landing_matrices':[]})
        result=studio.snapshot()['measure_review'];self.assertIsNotNone(result['created'])
        self.assertFalse(result['landing_visuals']);self.assertEqual(result['created']['expression'],'SUM(Sales[Amount])')
        self.assertNotEqual(result['created']['measure_id'],owner)
        self.assertEqual(self.request('GET',result['created']['dax'],headers=self.headers)[0],200)
        studio.dispatch('create-measure',{'measure':owner,'landing_matrices':['iterator-review','iterator-second']})
        result=studio.snapshot()['measure_review'];self.assertEqual(len(result['landing_visuals']),2)
        self.assertIsNone(result['landing_visual']);self.assertTrue(result['created']['contexts'])
        self.assertEqual(self.request('GET',result['created']['zip'],headers=self.headers)[0],200)
        self.assertIsNone(studio.snapshot()['project'])
        with self.assertRaises(ModelError):studio.dispatch('generate',{'measure':owner,'matrix':'iterator-review'})
        self.assertEqual([(e.id,e.expression) for e in studio.model.entities.values()],original)
        studio.dispatch('create-measure',{'measure':owner,'landing_matrices':[]})
        self.assertFalse(studio.snapshot()['measure_review']['landing_visuals'])

    def test_create_measure_rejects_unrelated_duplicate_unknown_and_malformed_contexts(self):
        studio=self.server.studio;studio.dispatch('demo',{})
        owner=studio.model.measure('Revenue iterator').id
        for ids in [['monthly-margin'],['missing'],['iterator-review','iterator-review'],'iterator-review',[None]]:
            with self.subTest(ids=ids),self.assertRaises(ModelError):
                studio.dispatch('create-measure',{'measure':owner,'landing_matrices':ids})
        self.assertIsNone(studio.snapshot()['measure_review'])


if __name__=='__main__':unittest.main()
