"""Independent six-stage fixtures; engine evidence is collected separately."""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from semantic_hotspots import create_measure, parse_model, ModelError
from semantic_hotspots.graph import DependencyGraph
from semantic_hotspots.cli import main
from semantic_hotspots.production_chain import CHAIN_MEASURES, production_chain_model, create_production_chain_lab
from semantic_hotspots.production_lab import production_data
from production_chain_oracle import expected_chain


def tiny_data():
    return {'Periods': [[1, '2024-01-02'], [2, '2024-01-04']],
            'Calendar': [['2024-01-01', 1, 1], ['2024-01-02', 1, 1],
                         ['2024-01-03', 2, 1], ['2024-01-04', 2, 1]],
            'Site': [[1, 'North']], 'Product': [[1, 'Widget']], 'Metrics': [[1]],
            'Production Daily': [['2024-01-01', 1, 1, 10, 0, 0, 1],
                                 ['2024-01-02', 1, 1, 20, 0, 0, 1],
                                 ['2024-01-03', 1, 1, 5, 0, 0, 1],
                                 ['2024-01-04', 1, 1, 0, 0, 0, 1]]}


class ProductionChainTests(unittest.TestCase):
    def test_exactly_five_transitive_helpers_and_six_sequential_stages(self):
        model = parse_model(production_chain_model()); graph = DependencyGraph(model)
        self.assertEqual(len(model.measures), 6)
        for i, name in enumerate(CHAIN_MEASURES):
            owner = model.measure(name)
            helpers = {n for n in graph.dependency_ids(owner.id) if model.entities[n].kind == 'measure'}
            self.assertEqual(helpers, {model.measure(n).id for n in CHAIN_MEASURES[:i]})
            if i:self.assertIn(model.measure(CHAIN_MEASURES[i-1]).id, graph.dependencies[owner.id])
        self.assertEqual(len(model.tables), 6)

    def test_generated_root_is_changed_and_all_five_original_helpers_are_unchanged(self):
        document = production_chain_model(); frozen = copy.deepcopy(document)
        result = create_measure(document, CHAIN_MEASURES[-1]).data
        self.assertEqual(result['outcome'], 'rewritten'); self.assertTrue(result['validation_available'])
        self.assertEqual(document, frozen)
        evidence = result['chain_specialization']; self.assertTrue(evidence['source_helpers_unchanged'])
        self.assertEqual(len(evidence['expanded_helpers']), 5)
        self.assertNotEqual(result['expression'], parse_model(document).measure(CHAIN_MEASURES[-1]).expression)
        self.assertIn("SUM('Production Daily'[ProducedUnits])", result['expression'])
        self.assertNotIn("SUMX(VALUES('Production Daily'[Day])", result['expression'])
        # The nonadditive time-context iterator and previous-calendar-day lookup
        # must remain: summing leaf forecasts cannot replace cumulative totals.
        self.assertIn("SUMX(VALUES('Calendar'[Day])", result['expression'])
        self.assertIn("REMOVEFILTERS('Periods')", result['expression'])
        self.assertIn('currentDay - 1', result['expression'])

    def test_oracle_has_hand_calculated_days_periods_and_nonadditive_total(self):
        data = tiny_data()
        self.assertEqual(expected_chain(data), [35, None, -10, -10, -2.5, -35])
        self.assertEqual(expected_chain(data, {'days': ['2024-01-02']}), [20, 10, 10, 10, 10, 140])
        self.assertEqual(expected_chain(data, {'days': ['2024-01-03']}), [5, -15, -15, -15, -15, -210])
        first, second = expected_chain(data, {'periods': [1]}), expected_chain(data, {'periods': [2]})
        self.assertEqual(first, [30, None, 10, 10, 5, 70])
        self.assertEqual(second, [5, None, -20, -10, -2.5, -35])
        self.assertNotEqual(first[-1] + second[-1], expected_chain(data)[-1])
        # First day of a selected period still sees the previous period's day.
        self.assertEqual(expected_chain(data, {'periods': [2], 'days': ['2024-01-03']})[1], -15)

    def test_missing_day_is_not_previous_observation_and_zero_is_not_blank(self):
        data = tiny_data(); data['Production Daily'].pop(1)
        self.assertIsNone(expected_chain(data, {'days': ['2024-01-03']})[1])
        self.assertEqual(expected_chain(tiny_data(), {'days': ['2024-01-04']})[1], -5)
        data['Production Daily'][1][3] = None
        self.assertIsNone(expected_chain(data, {'days': ['2024-01-03']})[1])
        for context in [{'sites': []}, {'days': []}, {'periods': [999]}, {'products': [999]}]:
            self.assertEqual(expected_chain(data, context), [None] * 6)

    def test_production_rows_include_blank_gap_correction_and_grand_total_cases(self):
        data = production_data(days=56, sites=3, products=4)
        self.assertEqual(expected_chain(data, {'families': ['Empty']}), [None] * 6)
        self.assertEqual(expected_chain(data, {'days': [None]})[0], 11)
        self.assertEqual(expected_chain(data, {'days': [None]})[1:], [None] * 5)
        self.assertTrue(any(r[3] is not None and r[3] < 0 for r in data['Production Daily']))
        self.assertIsNotNone(expected_chain(data)[-1])

    def test_cli_exports_fixture_without_claiming_execution(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['production-chain-lab', '--days', '28', '-o', str(Path(tmp)/'chain')]), 0)
            self.assertIn('have not been tested by this export', output.getvalue())

    def test_export_has_seven_measures_one_matrix_two_dags_and_embedded_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = create_production_chain_lab(Path(tmp)/'chain', days=28)
            spec = json.loads((root/'chain-lab.json').read_text())
            self.assertEqual(spec['transitive_helper_count'], 5)
            document = json.loads((root/'Showcase.SemanticModel/model.bim').read_text())
            self.assertEqual(len(parse_model(document).measures), 7)
            self.assertEqual(len(list((root/'Showcase.Report').rglob('visual.json'))), 1)
            for table in document['model']['tables']:
                self.assertIn('Binary.FromText', table['partitions'][0]['source']['expression'])
            self.assertTrue((root/'new-measure/dags/report.html').exists())
            self.assertTrue((root/'new-measure/validate-total.dax').exists())
            self.assertEqual(spec['correctness'], 'not_run')
            definitions = json.loads((root/'measure-definitions.json').read_text())
            self.assertEqual(len(definitions['originals']), 6)
            query = (root/'validate-chain.dax').read_text()
            self.assertEqual(query.count("MEASURE 'Metrics'["), 7)
            self.assertIn('EVALUATE ROW(', query)
            with self.assertRaises(ModelError):create_production_chain_lab(root)


if __name__ == '__main__': unittest.main()
