"""Optional local ADOMD client orchestration; the DAX engine is not emulated."""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

from .model import ModelError


def local_server(value):
    """Accept loopback plus an explicit port, never a connection string."""
    match = re.fullmatch(r'(localhost|127\.0\.0\.1|\[::1\]):([0-9]{1,5})', str(value), re.I)
    if not match or not 1 <= int(match[2]) <= 65535:
        raise ModelError('The ADOMD backend needs a local endpoint such as localhost:PORT')
    return f'{match[1].lower()}:{int(match[2])}'


def _timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ModelError('timeout must be finite and positive')
    return value


class EngineRunner:
    """Build isolated worker commands while preserving the legacy dscmd path."""

    def __init__(self, *, server, database, engine='auto', dscmd=None,
                 adomd_path=None, dotnet_runtime='netfx', cold=0, trace_verified=False):
        if engine not in {'auto', 'adomd', 'dscmd'}:
            raise ModelError('engine must be auto, adomd or dscmd')
        self.engine = ('dscmd' if dscmd else 'adomd') if engine == 'auto' else engine
        if not server or not database or not isinstance(database, str) or '\x00' in database:
            raise ModelError('Provide an explicit server and database for the loaded model')
        if dotnet_runtime not in {'netfx', 'coreclr'}:
            raise ModelError('dotnet_runtime must be netfx or coreclr')
        self.server, self.database = str(server), database
        self.adomd_path, self.dotnet_runtime = adomd_path, dotnet_runtime
        if self.engine == 'adomd':
            self.server = local_server(server)
            if dscmd is not None:
                raise ModelError('--dscmd cannot be combined with the ADOMD backend')
            if cold:
                raise ModelError('ADOMD currently supports warm iterations only; cold cache clearing needs the optional dscmd backend')
            if trace_verified:
                raise ModelError('ADOMD records client elapsed time, not server traces; omit --trace-verified')
            self.executable = None
        else:
            if adomd_path is not None or dotnet_runtime != 'netfx':
                raise ModelError('ADOMD assembly/runtime options require the ADOMD backend')
            self.executable = Path(dscmd).resolve() if dscmd is not None else None
            if self.executable is None or not self.executable.is_file():
                raise ModelError('dscmd must be a path to an installed DAX Studio dscmd.exe 3.6+')

    def command(self, operation, output, query_file, *, cold=0, warm=3, timeout=300):
        if operation not in {'csv', 'benchmark'}:
            raise ModelError('Unsupported engine operation')
        if self.engine == 'dscmd':
            command = [str(self.executable), operation, str(output)]
        else:
            command = [sys.executable, '-m', 'semantic_hotspots._adomd_worker', operation,
                       '--output', str(output), '--timeout', str(_timeout(timeout)),
                       '--dotnet-runtime', self.dotnet_runtime]
            if self.adomd_path is not None:
                command += ['--adomd-path', str(self.adomd_path)]
        command += ['--server', self.server, '--database', self.database, '--file', str(query_file)]
        if operation == 'benchmark':
            command += ['--cold', str(cold), '--warm', str(warm)]
        return command


def discover_desktop_ports(local_app_data=None):
    """Best-effort discovery from Desktop workspace files, not a stable public API.

    Entries can be stale. This neither selects a model nor contacts an endpoint.
    """
    base = Path(local_app_data or os.environ.get('LOCALAPPDATA', ''))
    if local_app_data is None and not os.environ.get('LOCALAPPDATA'):
        return []
    ports = set()
    for product in ['Power BI Desktop', 'Power BI Desktop Store App']:
        workspace = base/'Microsoft'/product/'AnalysisServicesWorkspaces'
        try:
            files = workspace.glob('AnalysisServicesWorkspace*/Data/msmdsrv.port.txt')
            for path in files:
                try:
                    with path.open('rb') as stream:
                        raw = stream.read(128)
                    encoding = 'utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else ('utf-16-le' if b'\x00' in raw else 'utf-8-sig')
                    value = raw.decode(encoding).strip().strip('\x00')
                    if re.fullmatch(r'[0-9]{1,5}', value) and 1 <= int(value) <= 65535:
                        ports.add(int(value))
                except (OSError, UnicodeError):
                    continue
        except OSError:
            continue
    return [f'localhost:{port}' for port in sorted(ports)]


def desktop_sessions(*, server=None, adomd_path=None, dotnet_runtime='netfx', timeout=30):
    """List catalogs at discovered local endpoints; never auto-select a database."""
    if sys.platform != 'win32':
        raise ModelError('Desktop session discovery and the optional ADOMD backend currently require Windows')
    timeout = _timeout(timeout)
    if dotnet_runtime not in {'netfx', 'coreclr'}:
        raise ModelError('dotnet_runtime must be netfx or coreclr')
    results = []
    for endpoint in [local_server(server)] if server else discover_desktop_ports():
        command = [sys.executable, '-m', 'semantic_hotspots._adomd_worker', 'catalogs',
                   '--server', endpoint, '--timeout', str(timeout), '--dotnet-runtime', dotnet_runtime]
        if adomd_path is not None:
            command += ['--adomd-path', str(adomd_path)]
        try:
            process = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                     errors='replace', timeout=timeout, check=False)
            if process.returncode:
                raise ModelError(process.stderr.strip() or 'ADOMD catalog discovery failed')
            catalogs = json.loads(process.stdout)
            if not isinstance(catalogs, list) or not all(isinstance(c, str) for c in catalogs):
                raise ValueError('Unexpected catalog response')
            results.append({'server': endpoint, 'databases': catalogs, 'error': None})
        except (OSError, ValueError, ModelError, subprocess.TimeoutExpired) as exc:
            results.append({'server': endpoint, 'databases': [], 'error': str(exc)})
    return results


def query_to_csv(query_file, output, *, server, database, adomd_path=None,
                 dotnet_runtime='netfx', timeout=300):
    """Execute one local DAX result set; an existing output is never overwritten."""
    timeout = _timeout(timeout)
    query_file, output = Path(query_file).resolve(), Path(output).resolve()
    if not query_file.is_file():
        raise ModelError('Query file does not exist')
    if output.exists():
        raise ModelError('Query output must not already exist')
    runner = EngineRunner(server=server, database=database, engine='adomd',
                          adomd_path=adomd_path, dotnet_runtime=dotnet_runtime)
    command = runner.command('csv', output, query_file, timeout=timeout)
    try:
        process = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                 errors='replace', timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ModelError(f'ADOMD query failed or timed out: {exc}') from exc
    if process.returncode or not output.is_file():
        raise ModelError(process.stderr.strip() or 'ADOMD query did not produce a complete CSV')
    return output
