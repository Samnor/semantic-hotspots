"""Isolated optional .NET client. Only this subprocess initializes Python.NET."""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .dax import tokenize
from .engine import local_server
from .model import ModelError

TIMING_SOURCE = 'client_elapsed'
_DLL = 'Microsoft.AnalysisServices.AdomdClient.dll'
_DLL_DIRECTORIES = []


def find_adomd(path=None):
    if path is not None:
        result = Path(path).resolve()
        if result.is_dir():
            result /= _DLL
        if not result.is_file():
            raise ModelError('--adomd-path must name an installed ADOMD DLL or its directory')
        return result
    from .client_setup import client_directory, _installed
    cached = client_directory()
    if cached is not None and _installed(cached):
        return cached/_DLL
    for variable in ['ProgramW6432', 'ProgramFiles']:
        base = os.environ.get(variable)
        if base:
            result = Path(base)/'Microsoft Power BI Desktop'/'bin'/_DLL
            if result.is_file():
                return result.resolve()
    raise ModelError('ADOMD client DLL not found. Supply --adomd-path to an installed Microsoft ADOMD client. No DLL is downloaded or bundled.')


def load_adomd(path=None, runtime='netfx'):
    if sys.platform != 'win32':
        raise ModelError('The optional ADOMD backend currently requires Windows')
    if runtime not in {'netfx', 'coreclr'}:
        raise ModelError('Unsupported .NET runtime')
    try:
        from pythonnet import load
    except ImportError as exc:
        raise ModelError('Install the optional client with python -m pip install "semantic-hotspots[engine]" (or the release wheel with [engine])') from exc
    dll = find_adomd(path)
    try:
        load(runtime)
        sys.path.insert(0, str(dll.parent))
        if hasattr(os, 'add_dll_directory'):
            _DLL_DIRECTORIES.append(os.add_dll_directory(str(dll.parent)))
        import clr
        clr.AddReference(str(dll))
        from Microsoft.AnalysisServices.AdomdClient import AdomdConnection
        return AdomdConnection
    except Exception as exc:
        raise ModelError('Unable to load Microsoft ADOMD. Match the DLL, Python architecture and .NET runtime; use --dotnet-runtime coreclr for a .NET Core DLL. '+str(exc)) from exc


def connection_string(server, database=None, timeout=300):
    server = local_server(server)
    # Quote values with ADO.NET rules; database names cannot add properties.
    def quoted(value):
        return '"'+str(value).replace('"', '""')+'"'
    result = f'Data Source={quoted(server)};Integrated Security=SSPI;Connect Timeout={max(1, math.ceil(timeout))};Application Name="semantic-hotspots";'
    if database is not None:
        if not isinstance(database, str) or not database or '\x00' in database:
            raise ModelError('Provide an explicit database')
        result += 'Initial Catalog='+quoted(database)+';'
    return result


def dax_query(text):
    tokens = tokenize(text)
    if (not tokens or tokens[0].kind != 'NAME' or tokens[0].value.upper() not in {'DEFINE', 'EVALUATE'}
            or not any(t.kind == 'NAME' and t.value.upper() == 'EVALUATE' for t in tokens)):
        raise ModelError('Only DAX DEFINE/EVALUATE queries are accepted; XMLA/model commands are unsupported')
    return text


@contextmanager
def connection(factory, server, database, timeout):
    conn = factory(connection_string(server, database, timeout))
    try:
        conn.Open()
        yield conn
    finally:
        conn.Dispose()


def cell(reader, index):
    if reader.IsDBNull(index):
        return ''
    value = reader.GetValue(index)
    # Invariant round-trippable text, never locale-dependent decimal commas or
    # a float conversion of a fixed decimal. BLANK flags come from DAX itself.
    kind = str(reader.GetFieldType(index).FullName)
    if kind == 'System.Boolean':
        return '1' if bool(value) else '0'
    if kind in {'System.Double', 'System.Single', 'System.Decimal', 'System.DateTime'}:
        if kind in {'System.Double', 'System.Single'} and isinstance(value, (float, int)):
            return repr(value)
        from System.Globalization import CultureInfo
        pattern = 'R' if kind in {'System.Double', 'System.Single'} else 'o' if kind == 'System.DateTime' else 'G'
        return value.ToString(pattern, CultureInfo.InvariantCulture)
    return str(value)


def consume(conn, query, timeout, writer=None):
    """Time execution plus full reading/conversion, excluding CSV disk writes."""
    command = conn.CreateCommand()
    reader = None
    try:
        command.CommandText = query
        command.CommandTimeout = max(1, math.ceil(timeout))
        started = time.perf_counter_ns()
        reader = command.ExecuteReader()
        headers = [str(reader.GetName(i)) for i in range(reader.FieldCount)]
        if not headers or len(headers) != len(set(headers)):
            raise ModelError('Expected a result set with unique column names')
        if writer is not None:
            writer.writerow(headers)
        count = 0
        disk_ns = 0
        while reader.Read():
            values = [cell(reader, i) for i in range(reader.FieldCount)]
            count += 1
            if writer is not None:
                disk_start = time.perf_counter_ns()
                writer.writerow(values)
                disk_ns += time.perf_counter_ns()-disk_start
        if reader.NextResult():
            raise ModelError('Multiple result sets are unsupported; submit one EVALUATE query at a time')
        elapsed = (time.perf_counter_ns()-started-disk_ns)/1_000_000
        return count, elapsed
    finally:
        try:
            if reader is not None:
                reader.Dispose()
        finally:
            command.Dispose()


@contextmanager
def atomic_csv(output):
    output = Path(output)
    if output.exists():
        raise ModelError('Engine output must not already exist')
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.adomd-', suffix='.tmp', dir=output.parent)
    staging = Path(name)
    try:
        with os.fdopen(descriptor, 'w', newline='', encoding='utf-8') as stream:
            yield csv.writer(stream)
        # Windows rename refuses an existing destination, including on volumes
        # without hard links. POSIX rename would overwrite, so use a link there.
        if os.name == 'nt':
            os.rename(staging, output)
        else:
            os.link(staging, output)
    finally:
        staging.unlink(missing_ok=True)


def execute(factory, *, operation, output, query, server, database, timeout, warm=3, cold=0):
    if operation not in {'csv', 'benchmark'}:
        raise ModelError('Unsupported execution operation')
    query = dax_query(query)
    if cold or isinstance(warm, bool) or not isinstance(warm, int) or warm < 1:
        raise ModelError('ADOMD currently needs warm >= 1 and cold = 0')
    with connection(factory, server, database, timeout) as conn:
        with atomic_csv(output) as writer:
            if operation == 'csv':
                consume(conn, query, timeout, writer)
            else:
                writer.writerow(['Sequence', 'Cache', 'TotalDuration_ms', 'RowCount', 'Error', 'TimingSource'])
                for sequence in range(1, warm+1):
                    # No cache clearing or discarded priming iteration: Warm
                    # means an unchanged cache, including the first iteration.
                    count, elapsed = consume(conn, query, timeout)
                    writer.writerow([sequence, 'Warm', repr(elapsed), count, '', TIMING_SOURCE])


def catalogs(factory, server, timeout):
    with connection(factory, server, None, timeout) as conn:
        schema = None
        try:
            # Use XMLA Discover rather than routing DMV SQL through Execute:
            # Desktop can otherwise parse the statement as DAX.
            schema = conn.GetSchemaDataSet('DBSCHEMA_CATALOGS', None)
            tables = list(schema.Tables)
            if len(tables) != 1:
                raise ModelError('Unexpected catalog schema tables')
            return sorted({str(row['CATALOG_NAME']) for row in tables[0].Rows
                           if str(row['CATALOG_NAME'])})
        finally:
            if schema is not None:
                schema.Dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Internal ADOMD subprocess worker')
    parser.add_argument('operation', choices=['csv', 'benchmark', 'catalogs'])
    parser.add_argument('--server', required=True)
    parser.add_argument('--database')
    parser.add_argument('--file', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--adomd-path', type=Path)
    parser.add_argument('--dotnet-runtime', choices=['netfx', 'coreclr'], default='netfx')
    parser.add_argument('--timeout', type=float, default=300)
    parser.add_argument('--cold', type=int, default=0)
    parser.add_argument('--warm', type=int, default=3)
    args = parser.parse_args(argv)
    try:
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise ModelError('timeout must be finite and positive')
        local_server(args.server)
        if args.operation != 'catalogs' and (not args.database or args.file is None or args.output is None):
            raise ModelError('Execution needs --database, --file and --output')
        query = dax_query(args.file.read_text(encoding='utf-8-sig')) if args.operation != 'catalogs' else None
        factory = load_adomd(args.adomd_path, args.dotnet_runtime)
        if args.operation == 'catalogs':
            print(json.dumps(catalogs(factory, args.server, args.timeout), ensure_ascii=True))
        else:
            execute(factory, operation=args.operation, output=args.output, query=query, server=args.server,
                    database=args.database, timeout=args.timeout, warm=args.warm, cold=args.cold)
        return 0
    except Exception as exc:
        print(f'ADOMD: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
