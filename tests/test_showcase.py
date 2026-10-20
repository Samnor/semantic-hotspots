"""Invented showcase execution evidence; no live engine or private fixtures."""
import copy
import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from semantic_hotspots import export_project, run_showcase, ModelError
from semantic_hotspots.showcase import _verdict
from test_matrices import model, matrix


class ShowcaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = export_project(model(), [matrix()], self.root/'project', config={'fact_rows':32,'dimension_rows':4})
        self.bim = json.loads(self.project.model_path.read_text())
        self.calls = []
        self.mode = None

    def engine(self, query, output, **kwargs):
        self.calls.append((Path(query).name, kwargs))
        name = Path(query).stem
        fields, rows = [], []
        tables = self.bim['model']['tables']
        if name.startswith('loaded-tables'):
            fields = ['[ID]', '[Name]']
            rows = [[i, t['name']] for i,t in enumerate(tables)]
        elif name.startswith('loaded-measures'):
            fields = ['[TableID]', '[Name]', '[Expression]']
            rows = [[i,m['name'],m['expression']] for i,t in enumerate(tables) for m in t.get('measures',[])]
            if self.mode == 'wrong-model':
                rows[0][2] = '999'
            if self.mode == 'changed-engine' and name.endswith('final'):
                rows[0][2] = '999'
        elif name.startswith('row-counts'):
            source = json.loads((self.project.directory/'simulation/simulation.json').read_text())
            physical = [t for t in source['tables'] if t['kind']=='synthetic-physical']
            fields = [f'[Physical_{i}]' for i in range(len(physical))]+['[SummaryRows]']
            rows = [[t['rows'] for t in physical]+[8]]
            if self.mode == 'unrefreshed':
                rows[0][0] = 0
        elif name.endswith('validation'):
            fields = [f'[{key}_{i}]' for i in (1,2) for key in ['Original','Candidate','Equal','OriginalBlank','CandidateBlank']]
            rows = [['100','100','1','0','0','0.4','0.4','1','0','0']]
            if self.mode == 'failed':
                rows[0][2] = '0'
            if self.mode == 'allblank':
                rows = [['','','1','1','1']*2]
            if self.mode == 'oneblank':
                rows[0][5:] = ['','','1','1','1']
            if self.mode == 'empty':
                rows = []
            if self.mode == 'badflag':
                rows[0][2] = 'true'
            if self.mode == 'malformed':
                fields[-1] = fields[0]
        else:
            self.assertTrue(kwargs['benchmark'])
            fields = ['Sequence','Cache','TotalDuration_ms','Error','RowCount','TimingSource']
            duration = 20 if name.endswith('baseline') else 8
            rows = [[i,'Warm',duration+(i%2)/10,'',1,'client_elapsed'] for i in range(1,kwargs['warm']+1)]
            if self.mode == 'partial-timing':
                rows.pop()
            if self.mode == 'wrong-rows':
                rows[2][4] = 2
            if self.mode == 'query-error':
                raise ModelError('engine unavailable')
            if self.mode == 'changed-files':
                manifest = self.project.directory/'showcase.json'
                manifest.write_text(manifest.read_text()+' ')
        with Path(output).open('w',newline='',encoding='utf-8') as stream:
            writer=csv.writer(stream);writer.writerow(fields);writer.writerows(rows)
        return Path(output)

    def run_case(self):
        with patch('semantic_hotspots.showcase.query_to_csv',side_effect=self.engine):
            return run_showcase(self.project.directory,server='localhost:123',database='Fictional',results_directory=self.root/'results',warm=5)

    def test_all_validation_precedes_timing_and_evidence_is_retained(self):
        result = self.run_case()
        self.assertEqual(result.data['status'],'completed')
        self.assertEqual(result.data['performance_status'],'observed_faster')
        self.assertEqual(result.data['validation_status'],'passed_tested_cases')
        kinds = [name for name,_ in self.calls]
        first_timing = next(i for i,(name,kw) in enumerate(self.calls) if kw.get('benchmark'))
        self.assertEqual(sum(name.endswith('validation.dax') for name in kinds[:first_timing]),6)
        self.assertEqual(kinds[first_timing:first_timing+4],['context-0-baseline.dax','context-0-candidate.dax','context-1-candidate.dax','context-1-baseline.dax'])
        for record in result.data['runs']:
            self.assertEqual(record['output_sha256'],hashlib.sha256((result.directory/record['output']).read_bytes()).hexdigest())
        self.assertTrue(result.report_path.is_file())
        self.assertIn('not a statistical significance test',result.report_path.read_text())
        timing=result.data['cases'][0]['timings']['baseline']
        self.assertEqual(timing['samples'],5)
        self.assertEqual(timing['excluded_warmup_sequence'],1)

    def test_bad_bindings_unrefreshed_empty_blank_and_malformed_values_withhold_timings(self):
        for mode in ['wrong-model','unrefreshed','failed','allblank','oneblank','empty','badflag','malformed']:
            with self.subTest(mode=mode):
                self.mode=mode;self.calls=[]
                out=self.root/'results'
                if out.exists():
                    import shutil
                    shutil.rmtree(out)
                result=self.run_case()
                self.assertNotEqual(result.data['status'],'completed')
                self.assertEqual(result.data['performance_status'],'withheld')
                self.assertFalse(any(k.get('benchmark') for _,k in self.calls))
                self.assertTrue((out/'results.json').is_file())

    def test_partial_failed_different_row_timings_and_source_edits_are_not_results(self):
        original=(self.project.directory/'showcase.json').read_bytes()
        for mode in ['partial-timing','wrong-rows','query-error','changed-files','changed-engine']:
            with self.subTest(mode=mode):
                self.mode=mode
                import shutil
                shutil.rmtree(self.root/'results',ignore_errors=True)
                result=self.run_case()
                self.assertEqual(result.data['status'],'failed')
                self.assertEqual(result.data['performance_status'],'withheld')
                self.assertIn('error',result.data)
                self.assertTrue(all(c['performance_status']=='withheld' for c in result.data['cases']))
                (self.project.directory/'showcase.json').write_bytes(original)

    def test_tampered_queries_model_and_paths_fail_before_engine_contact(self):
        path=self.project.directory/'showcase.json'
        original=json.loads(path.read_text())
        for mode in ['query','path','model']:
            modified=copy.deepcopy(original)
            if mode=='query':modified['cases'][0]['queries']['validation']['sha256']='0'*64
            if mode=='path':modified['cases'][0]['queries']['validation']['file']='../outside.dax'
            if mode=='model':modified['model_sha256']='0'*64
            path.write_text(json.dumps(modified))
            with patch('semantic_hotspots.showcase.query_to_csv') as engine,self.assertRaises(ModelError):
                self.run_case()
            engine.assert_not_called()
        path.write_text(json.dumps(original))
        self.run_case()
        with self.assertRaises(ModelError):self.run_case()

    def test_noisy_or_tiny_timings_stay_inconclusive(self):
        def timing(median,lo,hi):return {'median_ms':median,'min_ms':lo,'max_ms':hi}
        self.assertEqual(_verdict(timing(10,5,20),timing(5,4,12)),'inconclusive')
        self.assertEqual(_verdict(timing(.5,.4,.6),timing(.1,.09,.12)),'inconclusive')
        self.assertEqual(_verdict(timing(10,9,11),timing(20,19,21)),'observed_slower')

    def test_arguments_fail_before_writes(self):
        for kwargs in [{'warm':1},{'warm':True},{'server':'remote:123'},{'database':''},{'timeout':float('nan')}]:
            options={'server':'localhost:123','database':'Fictional','warm':5,**kwargs}
            with self.assertRaises(ModelError):run_showcase(self.project.directory,**options)
        self.assertFalse((self.project.directory/'showcase-results').exists())

    def test_lost_contexts_and_rehashed_noncanonical_queries_are_rejected(self):
        manifest=self.project.directory/'showcase.json'
        original=json.loads(manifest.read_text())
        changed=copy.deepcopy(original);changed['cases'].pop()
        manifest.write_text(json.dumps(changed))
        with self.assertRaises(ModelError):self.run_case()
        manifest.write_text(json.dumps(original))
        entry=original['cases'][0]['queries']['validation']
        query=self.project.directory/entry['file'];query.write_text('EVALUATE ROW("Equal_1",1)\n')
        entry['sha256']=hashlib.sha256(query.read_bytes()).hexdigest()
        manifest.write_text(json.dumps(original))
        with self.assertRaises(ModelError):self.run_case()


if __name__=='__main__':unittest.main()
