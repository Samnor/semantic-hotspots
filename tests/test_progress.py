"""Progress observations must not change analysis results or manufacture work."""
import tempfile
import unittest
from pathlib import Path

from semantic_hotspots import analyze, analyze_matrices, ModelError, parse_model
from test_matrices import model, matrix


class ProgressTests(unittest.TestCase):
    def test_callbacks_preserve_results_and_report_real_units(self):
        source=parse_model(model());events=[]
        plain=analyze(source);observed=analyze(source,progress=events.append)
        self.assertEqual(plain.to_dict(),observed.to_dict())
        self.assertTrue({'load','dependencies','ranking','rewrites'} <= {e['stage'] for e in events})
        for event in events:
            if event['total'] is not None:
                self.assertGreaterEqual(event['completed'],0)
                self.assertLessEqual(event['completed'],event['total'])
        rewrites=[e for e in events if e['stage']=='rewrites']
        self.assertEqual(rewrites[-1]['completed'],len(source.measures))
        self.assertTrue(all(e['total']==len(source.measures) for e in rewrites))
        with tempfile.TemporaryDirectory() as tmp:
            files=observed.write(tmp,progress=events.append)
            self.assertTrue(files['html'].is_file())
            self.assertTrue(any(e['stage']=='reports' and e['unit']=='measure chains' for e in events))
        expected=analyze_matrices(source,[matrix()]);events=[]
        reused=analyze_matrices(source,[matrix()],analysis=observed,progress=events.append)
        self.assertEqual(expected.to_dict(),reused.to_dict())
        self.assertEqual(events[-1]['completed'],1)
        self.assertTrue(all(e['stage']=='matrices' for e in events))
        with self.assertRaises(ModelError):analyze_matrices(parse_model(model()),[matrix()],analysis=observed)
