"""Fictional client contracts; these tests do not claim real DAX execution."""
from __future__ import annotations

import contextlib
import csv
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from semantic_hotspots import (ModelError, desktop_sessions, discover_desktop_ports,
                               query_to_csv, read_benchmark_csv, run_benchmarks, simulate)
from semantic_hotspots.cli import main
from semantic_hotspots.engine import EngineRunner, local_server
from semantic_hotspots import _adomd_worker as worker


class Reader:
    def __init__(self, headers, rows, kinds=None, extra=False, fail=False):
        self.headers, self.rows, self.kinds = headers, rows, kinds or ['System.Int64']*len(headers)
        self.extra, self.fail = extra, fail
        self.index, self.disposed = -1, False
        self.FieldCount = len(headers)

    def GetName(self, i): return self.headers[i]
    def GetFieldType(self, i): return SimpleNamespace(FullName=self.kinds[i])
    def Read(self):
        self.index += 1
        if self.fail and self.index == 1: raise RuntimeError('Fictional reader failure')
        return self.index < len(self.rows)
    def IsDBNull(self, i): return self.rows[self.index][i] is None
    def GetValue(self, i): return self.rows[self.index][i]
    def NextResult(self): return self.extra
    def Dispose(self): self.disposed = True


class Connection:
    def __init__(self, readers):
        self.readers, self.commands = iter(readers), []
        self.disposed = self.opened = False

    def Open(self): self.opened = True
    def CreateCommand(self):
        reader = next(self.readers)
        command = SimpleNamespace(ExecuteReader=lambda: reader, disposed=False)
        command.Dispose = lambda: setattr(command, 'disposed', True)
        self.commands.append(command)
        return command
    def Dispose(self): self.disposed = True


class EngineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def run_worker(self, readers, operation='csv', **options):
        conn = Connection(readers)
        captured = []
        def factory(text):
            captured.append(text)
            return conn
        path = self.root/'result.csv'
        worker.execute(factory, operation=operation, output=path, query='EVALUATE ROW("Value", 1)',
                       server='localhost:12345', database='Synthetic', timeout=1.5, **options)
        return conn, path, captured

    def test_worker_exports_values_blanks_booleans_and_disposes(self):
        reader = Reader(['Original', 'Candidate', 'Equal', 'Blank'], [[1.25, None, True, False]],
                        ['System.Double', 'System.Decimal', 'System.Boolean', 'System.Boolean'])
        conn, path, capture = self.run_worker([reader])
        with path.open(newline='', encoding='utf-8') as stream:
            self.assertEqual(list(csv.reader(stream)), [['Original','Candidate','Equal','Blank'], ['1.25','','1','0']])
        self.assertTrue(conn.opened and conn.disposed and reader.disposed and conn.commands[0].disposed)
        self.assertEqual(conn.commands[0].CommandTimeout, 2)
        self.assertIn('Initial Catalog="Synthetic";', capture[0])

    def test_worker_client_timings_remain_client_even_if_trace_asserted(self):
        _, path, _ = self.run_worker([Reader(['Value'], [[1]]), Reader(['Value'], [[2]])], operation='benchmark', warm=2)
        rows = read_benchmark_csv(path, trace_verified=True)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r['rows'] == 1 and r['duration_ms'] >= 0 for r in rows))
        self.assertTrue(all(r['timing_source'].startswith('client elapsed') for r in rows))
        self.assertTrue(all(r['cpu_ms'] is None and r['formula_engine_ms'] is None for r in rows))

    def test_partial_and_multiple_results_never_commit_success_csv(self):
        for reader in [Reader(['Value'], [[1],[2]], fail=True), Reader(['Value'], [[1]], extra=True), Reader(['X','X'], [[1,2]])]:
            conn = Connection([reader])
            with self.subTest(reader=reader), self.assertRaises((ModelError, RuntimeError)):
                worker.execute(lambda _:conn, operation='csv', output=self.root/'fail.csv',
                               query='EVALUATE ROW("Value",1)', server='localhost:123', database='Synthetic', timeout=1)
            self.assertFalse((self.root/'fail.csv').exists())
            self.assertTrue(conn.disposed and reader.disposed and conn.commands[0].disposed)
            self.assertFalse(list(self.root.glob('.adomd-*')))

    def test_failed_later_iteration_discards_whole_workload(self):
        with self.assertRaises(RuntimeError):
            self.run_worker([Reader(['Value'], [[1]]), Reader(['Value'], [[1],[2]], fail=True)], operation='benchmark', warm=2)
        self.assertFalse((self.root/'result.csv').exists())

    def test_outputs_cannot_be_overwritten_even_if_created_during_execution(self):
        output = self.root/'out.csv'
        with self.assertRaises(OSError):
            with worker.atomic_csv(output) as writer:
                writer.writerow(['Fictional'])
                output.write_text('Keep existing')
        self.assertEqual(output.read_text(), 'Keep existing')
        self.assertFalse(list(self.root.glob('.adomd-*')))

    def test_only_dax_queries_and_quoted_database_properties(self):
        for text in ['<Execute/>', 'SELECT * FROM Anything', 'DEFINE MEASURE T[X] = 1', '', '/* broken']:
            with self.subTest(text=text), self.assertRaises(ModelError): worker.dax_query(text)
        self.assertIn('EVALUATE', worker.dax_query('/* note */ DEFINE MEASURE T[X]=1 EVALUATE ROW("X",[X])'))
        result = worker.connection_string('localhost:123', 'Synthetic";Password=fictional;')
        self.assertIn('Initial Catalog="Synthetic"";Password=fictional;";', result)
        for server in ['remote:123', 'localhost', 'localhost:0', 'localhost:65536', 'localhost:123;Password=x', '127.0.0.2:123']:
            with self.subTest(server=server), self.assertRaises(ModelError): local_server(server)

    def test_backend_selection_and_guarded_options(self):
        direct = EngineRunner(server='localhost:123', database='Synthetic')
        argv = direct.command('benchmark', 'out.csv', 'query.dax', warm=2)
        self.assertEqual(argv[:4], [sys.executable, '-m', 'semantic_hotspots._adomd_worker', 'benchmark'])
        self.assertIn('--file', argv)
        for options in [dict(cold=1), dict(trace_verified=True), dict(dscmd='unused'), dict(dotnet_runtime='unknown')]:
            with self.subTest(options=options), self.assertRaises(ModelError):
                EngineRunner(server='localhost:123', database='Synthetic', engine='adomd', **options)
        exe = self.root/'client.exe'; exe.touch()
        legacy = EngineRunner(server='server', database='Synthetic', dscmd=exe)
        self.assertEqual(legacy.command('csv','out.csv','query.dax')[:3], [str(exe),'csv','out.csv'])

    def test_port_discovery_deduplicates_encodings_and_ignores_malformed_files(self):
        for i, raw in enumerate([b'12345\n', '12345'.encode('utf-16'), b'54321\x00', b'65536', b'abc', b'1234'*40]):
            path = self.root/'Microsoft'/'Power BI Desktop'/'AnalysisServicesWorkspaces'/f'AnalysisServicesWorkspace{i}'/'Data'/'msmdsrv.port.txt'
            path.parent.mkdir(parents=True)
            path.write_bytes(raw)
        self.assertEqual(discover_desktop_ports(self.root), ['localhost:12345'])
        path = self.root/'Microsoft'/'Power BI Desktop Store App'/'AnalysisServicesWorkspaces'/'AnalysisServicesWorkspace7'/'Data'/'msmdsrv.port.txt'
        path.parent.mkdir(parents=True);path.write_bytes('54321'.encode('utf-16-le'))
        self.assertEqual(discover_desktop_ports(self.root), ['localhost:12345','localhost:54321'])

    def test_discovery_returns_catalogs_errors_and_enforces_timeout(self):
        with patch('semantic_hotspots.engine.sys.platform', 'win32'), patch('semantic_hotspots.engine.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([],0,'["Synthetic"]','')
            self.assertEqual(desktop_sessions(server='localhost:123')[0]['databases'], ['Synthetic'])
            self.assertEqual(run.call_args.kwargs['timeout'], 30)
            run.side_effect = subprocess.TimeoutExpired('fixture',1)
            self.assertIsNotNone(desktop_sessions(server='localhost:123')[0]['error'])

    def test_catalog_reader_is_closed_and_dll_lookup_is_explicit(self):
        conn = Connection([Reader(['CATALOG_NAME'], [['Synthetic'],['Synthetic'],[None]])])
        self.assertEqual(worker.catalogs(lambda _:conn, 'localhost:123', 1), ['Synthetic'])
        self.assertTrue(conn.disposed and conn.commands[0].disposed)
        dll = self.root/'Microsoft.AnalysisServices.AdomdClient.dll';dll.touch()
        self.assertEqual(worker.find_adomd(self.root),dll)
        with self.assertRaises(ModelError): worker.find_adomd(self.root/'missing.dll')

    def test_query_worker_failure_and_timeout_are_not_success(self):
        query = self.root/'query.dax';query.write_text('EVALUATE ROW("Value",1)')
        for effect in [subprocess.CompletedProcess([],2,'','Fictional failure'), subprocess.TimeoutExpired('fixture',1)]:
            with patch('semantic_hotspots.engine.subprocess.run', side_effect=effect if isinstance(effect,Exception) else None,
                       return_value=effect), self.assertRaises(ModelError):
                query_to_csv(query,self.root/'out.csv',server='localhost:123',database='Synthetic')

    def test_query_command_exports_elapsed_csv_without_a_dscmd_path(self):
        query = self.root/'query.dax';query.write_text('EVALUATE ROW("Value",1)')
        def fake(argv, **kwargs):
            self.assertEqual(argv[3],'benchmark')
            self.assertEqual(argv[argv.index('--warm')+1],'2')
            Path(argv[argv.index('--output')+1]).write_text('Sequence,Cache,TotalDuration_ms,RowCount,Error,TimingSource\n1,Warm,2,1,,client_elapsed\n')
            return subprocess.CompletedProcess(argv,0,'','')
        output = self.root/'timing.csv'
        with patch('semantic_hotspots.engine.subprocess.run',side_effect=fake), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['query',str(query),'--server','localhost:123','--database','Synthetic',
                                   '--benchmark','--warm','2','-o',str(output)]),0)
        self.assertEqual(read_benchmark_csv(output)[0]['cpu_ms'],None)
        with self.assertRaises(ModelError):
            query_to_csv(query,output,server='localhost:123',database='Synthetic',benchmark=True)

    def test_direct_backend_drives_existing_benchmark_collection(self):
        model = {'model':{'tables':[{'name':'Sales','columns':[{'name':'Amount','dataType':'double'}],
                                    'measures':[{'name':'Total','expression':'SUM(Sales[Amount])'}]}]}}
        bundle = simulate(model,self.root/'bundle',config={'fact_rows':10})
        def fake(argv, **kwargs):
            self.assertIn('semantic_hotspots._adomd_worker',argv)
            output = Path(argv[argv.index('--output')+1])
            output.write_text('Sequence,Cache,TotalDuration_ms,RowCount,Error,TimingSource\n1,Warm,2,1,,client_elapsed\n')
            return subprocess.CompletedProcess(argv,0,'','')
        with patch('semantic_hotspots.benchmark.subprocess.run',side_effect=fake):
            result = run_benchmarks(bundle,server='localhost:123',database='Synthetic',warm=1)
        self.assertTrue(result.cases and all(c['phases']['warm']['successes'] == 1 for c in result.cases))
        capture = json.loads((bundle.directory/'benchmark-results/runner.json').read_text())
        self.assertEqual(capture['engine'],'adomd')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['benchmark',str(bundle.directory),'--collect-only']),0)


if __name__ == '__main__':
    unittest.main()
