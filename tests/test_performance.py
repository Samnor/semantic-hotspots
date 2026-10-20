"""Invented event trees using Microsoft's documented event names and fields."""
import copy
import unittest

from semantic_hotspots import read_performance_analyzer, ModelError
from test_matrices import model
from semantic_hotspots import parse_model


def recording():
    return {'version':'1.0.0','events':[
        {'id':'v','name':'Visual Container Lifecycle','component':'Report Canvas',
         'start':'2025-01-01T00:00:00Z','end':'2025-01-01T00:00:01Z',
         'metrics':{'visualId':'monthly-margin','visualTitle':'Fictional revenue'}},
        {'id':'q','parentId':'v','name':'Execute DAX Query','component':'DSE',
         'start':'2025-01-01T00:00:00.100Z','end':'2025-01-01T00:00:00.300Z',
         'metrics':{'QueryText':'EVALUATE ROW("A", Sales[Revenue], "B", Sales[Margin])'}},
        {'id':'as','parentId':'q','name':'Execute Query','component':'AS',
         'start':'2025-01-01T00:00:00.110Z','end':'2025-01-01T00:00:00.290Z'},
        {'id':'render','parentId':'v','name':'Render','component':'Report Canvas',
         'start':'2025-01-01T00:00:00.300Z','end':'2025-01-01T00:00:01Z'}]}


class PerformanceTests(unittest.TestCase):
    def test_dax_context_is_not_summed_with_visual_render_or_nested_engine_events(self):
        result=read_performance_analyzer(parse_model(model()),recording())
        self.assertEqual(len(result['queries']),1)
        query=result['queries'][0]
        self.assertEqual(query['duration_ms'],200)
        self.assertEqual(query['visual_id'],'monthly-margin')
        self.assertEqual(len(query['measure_ids']),2)
        self.assertIn('first row',result['interpretation'])

    def test_failed_canceled_and_query_scoped_contexts_are_not_false_measure_costs(self):
        for key in ['Error','Canceled']:
            data=recording();data['events'][1]['metrics'][key]=True
            result=read_performance_analyzer(parse_model(model()),data)
            self.assertIsNone(result['queries'][0]['duration_ms'])
        data=recording();data['events'][1]['metrics']['QueryText']='DEFINE MEASURE Sales[Local] = 1 EVALUATE ROW("A",Sales[Local])'
        result=read_performance_analyzer(parse_model(model()),data)
        self.assertEqual(result['queries'][0]['measure_ids'],[])
        self.assertTrue(result['queries'][0]['diagnostics'])

    def test_duplicate_cycles_and_invalid_clock_values_are_rejected(self):
        for mode in ['duplicate','cycle','reverse','naive','version']:
            data=recording()
            if mode=='duplicate':data['events'].append(copy.deepcopy(data['events'][0]))
            if mode=='cycle':data['events'][0]['parentId']='q'
            if mode=='reverse':data['events'][1]['end']='2024-01-01T00:00:00Z'
            if mode=='naive':data['events'][1]['start']='2025-01-01T00:00:00'
            if mode=='version':data['version']='999'
            with self.subTest(mode=mode),self.assertRaises(ModelError):
                read_performance_analyzer(parse_model(model()),data)

    def test_missing_parents_truncation_and_missing_text_are_explicit(self):
        data=recording();data['events'][1]['parentId']='missing';data['events'][1]['metrics']={}
        data['events'].append({'id':'truncated','name':'Metrics Truncated','component':'DSE','start':'2025-01-01T00:00:00Z'})
        result=read_performance_analyzer(parse_model(model()),data)
        self.assertIsNone(result['queries'][0]['visual_id'])
        self.assertTrue(result['queries'][0]['diagnostics'])
        self.assertEqual(len(result['diagnostics']),2)


if __name__=='__main__':unittest.main()
