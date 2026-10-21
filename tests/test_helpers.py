"""Only actual, explicitly supplied fictional metadata can fill helper gaps."""
import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from semantic_hotspots import (create_measure, parse_model, ModelError, Entity, Model,
                               helper_requirements, recover_helpers, recover_helpers_from_desktop)
from semantic_hotspots.studio import Studio
from test_optimizer import source


def partial():
    data=source('SUMX(Sales,Sales[Amount])+[Helper]')
    data['model']['tables'][0]['measures'].append({'name':'Helper'})
    return data


def supplement():
    data=partial();data['model']['tables'][0]['measures'][-1]['expression']='[Leaf]'
    data['model']['tables'][0]['measures'].extend([{'name':'Leaf','expression':'YEAR(TODAY())'}, {'name':'Unrelated','expression':'99'}])
    return data


class HelperRecoveryTests(unittest.TestCase):
    def test_actual_transitive_definitions_fill_only_reachable_gaps_in_copy(self):
        original=parse_model(partial());before=copy.deepcopy(original)
        recovered=recover_helpers(original,supplement(),'Total')
        self.assertEqual(original,before);self.assertFalse(recovered.remaining)
        self.assertEqual({recovered.model.entities[i].name for i in recovered.recovered},{'Helper','Leaf'})
        self.assertNotIn('Unrelated',{m.name for m in recovered.model.measures})
        self.assertEqual(recovered.model.measure('Total').expression,before.measure('Total').expression)
        result=create_measure(recovered.model,'Total',validation_dates=['2024-01-01'])
        self.assertEqual(result.data['outcome'],'rewritten');self.assertTrue(result.data['validation_available'])

    def test_unknown_reference_can_be_bound_using_real_supplement_catalog(self):
        data=source('[Helper]')
        extra={'model':{'tables':[{'name':'Sales','measures':[{'name':'Helper','expression':'2'}]}]}}
        recovered=recover_helpers(data,extra,'Total')
        self.assertFalse(helper_requirements(recovered.model,'Total'))

    def test_no_definition_is_invented_and_existing_dax_and_source_file_stay_unchanged(self):
        data=partial()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'model.bim';path.write_text(json.dumps(data));before=path.read_bytes()
            recovered=recover_helpers(path,partial(),'Total')
            self.assertEqual(recovered.recovered,[]);self.assertEqual(len(recovered.remaining),1)
            self.assertIsNone(recovered.model.measure('Helper').expression)
            self.assertEqual(path.read_bytes(),before)

    def test_conflicting_root_and_existing_definitions_and_live_anchor_are_rejected(self):
        data=supplement();data['model']['tables'][0]['measures'][0]['expression']='0'
        with self.assertRaisesRegex(ModelError,'root definition differs'):recover_helpers(partial(),data,'Total')
        data=supplement();data['model']['tables'][0]['measures'][1]['expression']='0'
        with self.assertRaisesRegex(ModelError,'Conflicting existing'):recover_helpers(supplement(),data,'Total')
        with self.assertRaisesRegex(ModelError,'selected root definition'):
            recover_helpers(partial(),{'model':{'tables':[]}},'Total',require_root_match=True)

    def test_ambiguous_incoming_bare_name_is_not_arbitrarily_selected(self):
        data=source('[New helper]')
        extra={'model':{'tables':[{'name':t,'measures':[{'name':'New helper','expression':'1'}]} for t in ['One','Two']]}}
        with self.assertRaisesRegex(ModelError,'ambiguous'):recover_helpers(data,extra,'Total')

    def test_calculated_table_and_context_markers_survive_partial_import(self):
        original=parse_model(source('[Helper]'))
        extra=Model();table=extra.add_table('Dates');extra.entities[table.id].expression='CALENDAR(TODAY(),TODAY())'
        extra.add(Entity('column','Dates','Day',metadata={'data_type':'datetime'}))
        extra.add(Entity('measure','Sales','Helper','MAX(Dates[Day])'))
        extra.add(Entity('calculation_item','Group','Item','SELECTEDMEASURE()'))
        recovered=recover_helpers(original,extra,'Total').model
        self.assertEqual(recovered.entities[table.id].expression,extra.entities[table.id].expression)
        self.assertTrue(any(e.kind=='calculation_item' for e in recovered.entities.values()))
        self.assertFalse(create_measure(recovered,'Total').data['validation_available'])

    def test_physical_objects_cannot_silently_become_calculated(self):
        for kind in ['table','column']:
            original=parse_model(source());extra=copy.deepcopy(original)
            entity=next(e for e in extra.entities.values() if e.kind==kind)
            entity.expression='TODAY()'
            with self.assertRaisesRegex(ModelError,'physical object'):recover_helpers(original,extra,'Total')

    def test_unrelated_format_markers_do_not_leave_dangling_owners(self):
        extra=parse_model(supplement())
        unrelated=extra.measure('Unrelated')
        extra.add(Entity('format_string','Sales','Unrelated (format string)','"0"',metadata={'for_measure':unrelated.id}))
        recovered=recover_helpers(partial(),extra,'Total')
        self.assertFalse(recovered.remaining)
        self.assertNotIn(unrelated.id,recovered.model.entities)
        self.assertFalse(any(e.kind=='format_string' for e in recovered.model.entities.values()))

    def test_desktop_read_only_info_views_require_matching_root_and_preserve_formats(self):
        original=parse_model(partial());root=original.measure('Total').expression
        rows={'TABLES':[{'Name':'Sales','Expression':'','CalculationGroupPrecedence':''}],
              'COLUMNS':[{'Table':'Sales','Name':'Amount','DataType':'Decimal Number','Expression':'','SourceColumn':'Amount'}],
              'MEASURES':[{'Table':'Sales','Name':'Total','Expression':root},
                          {'Table':'Sales','Name':'Helper','Expression':'YEAR(TODAY())','FormatStringDefinition':'"0"'}]}
        queries=[]
        def query(path,output,**kwargs):
            text=Path(path).read_text();kind=text.split('INFO.VIEW.')[1].split('(')[0];queries.append(text)
            with Path(output).open('w',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=sorted({k for r in rows[kind] for k in r}));writer.writeheader();writer.writerows(rows[kind])
        with patch('semantic_hotspots.engine.query_to_csv',side_effect=query):
            recovered=recover_helpers_from_desktop(original,'Total',server='localhost:55555',database='explicit')
        self.assertEqual(len(queries),3);self.assertFalse(recovered.remaining)
        self.assertEqual(recovered.model.measure('Helper').expression,'YEAR(TODAY())')
        self.assertFalse(any(e.kind=='format_string' for e in recovered.model.entities.values()))
        self.assertEqual(recovered.model.measure('Helper').metadata['format_string'],'0')
        self.assertIsNone(original.measure('Helper').expression)
        rows['MEASURES'][0]['Expression']=''
        with patch('semantic_hotspots.engine.query_to_csv',side_effect=query),self.assertRaisesRegex(ModelError,'root definition'):
            recover_helpers_from_desktop(original,'Total',server='localhost:55555',database='explicit')
        with self.assertRaises(ModelError):recover_helpers_from_desktop(original,'Total',server='remote.example:123',database='x')

    def test_studio_export_recovery_recreates_selected_measure_with_fixed_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'input.bim';path.write_text(json.dumps(partial()));before=path.read_bytes()
            (root/'extra.bim').write_text(json.dumps(supplement()))
            studio=Studio(root,root/'out');studio.dispatch('inspect',{'model':'input.bim'})
            owner=studio.model.measure('Total').id
            studio.dispatch('create-measure',{'measure':owner,'validation_dates':['2024-02-29']})
            self.assertFalse(studio.snapshot()['measure_review']['created']['validation_available'])
            self.assertIsNone(studio.snapshot()['measure_review']['candidates'][0]['validation'])
            with self.assertRaises(ModelError):studio.dispatch('recover-helpers',{'measure':'wrong','path':'extra.bim'})
            with self.assertRaises(ModelError):studio.dispatch('recover-helpers',{'measure':owner,'path':'../outside.bim'})
            with self.assertRaises(ModelError):studio.dispatch('recover-helpers',{'measure':owner,'server':'localhost:123','database':'not-discovered'})
            studio.dispatch('recover-helpers',{'measure':owner,'path':'extra.bim'})
            review=studio.snapshot()['measure_review']
            self.assertTrue(review['created']['validation_available'])
            self.assertEqual(review['created']['date_controls']['dates'],['2024-02-29'])
            self.assertEqual(review['recovery']['recovered_count'],2)
            self.assertEqual(path.read_bytes(),before)
            self.assertIsNotNone(review['candidates'][0]['validation'])

    def test_whitespace_helpers_are_missing_and_only_real_definitions_fill_them(self):
        data=partial();data['model']['tables'][0]['measures'][-1]['expression']='  \n '
        result=create_measure(data,'Total')
        self.assertFalse(result.data['validation_available'])
        self.assertEqual({i['code'] for i in result.data['dependency_issues']},{'missing_definition'})
        recovered=recover_helpers(data,supplement(),'Total')
        self.assertEqual({recovered.model.entities[i].name for i in recovered.recovered},{'Helper','Leaf'})
        self.assertFalse(recovered.remaining)
