"""Explicit installation of a pinned official client, never model/data upload."""
from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from .model import ModelError

CLIENT_VERSION = '19.118.3'
CLIENT_SHA256 = 'fb16292dcc7c9b37d6c21b64e8cd6bab4a336df561d3c1ef437476aaa109afff'
CLIENT_URL = ('https://api.nuget.org/v3-flatcontainer/microsoft.analysisservices.adomdclient/'
              + CLIENT_VERSION + '/microsoft.analysisservices.adomdclient.' + CLIENT_VERSION + '.nupkg')


def client_directory():
    base = os.environ.get('LOCALAPPDATA')
    return Path(base)/'SemanticHotspots'/'adomd'/CLIENT_VERSION if base else None


def _installed(root):
    try:
        manifest = json.loads((root/'installed.json').read_text(encoding='utf-8'))
        if manifest['package_sha256'] != CLIENT_SHA256:
            return False
        if not {'Microsoft.AnalysisServices.AdomdClient.dll', 'Microsoft.AnalysisServices.SPClient.Interfaces.dll', 'msasxpress.dll'}.issubset(manifest['files']):
            return False
        for name, digest in manifest['files'].items():
            if Path(name).name != name or hashlib.sha256((root/name).read_bytes()).hexdigest() != digest:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def install_adomd(directory=None):
    """Download/check/extract official x64 .NET Framework client on Windows.

    This is an explicit network operation. No model or source data is sent.
    Existing unknown or modified installations are not overwritten. Python.NET
    remains a pip extra; this function never invokes pip or installs an engine.
    """
    if sys.platform != 'win32' or platform.machine().casefold() not in {'amd64', 'x86_64'} or sys.maxsize <= 2**32:
        raise ModelError('Automatic client setup requires x64 Python on x64 Windows')
    destination = Path(directory).resolve() if directory is not None else client_directory()
    if destination is None:
        raise ModelError('LOCALAPPDATA is unavailable; provide an installation directory')
    destination = destination.resolve()
    if destination.exists():
        if _installed(destination):
            return destination/'Microsoft.AnalysisServices.AdomdClient.dll'
        raise ModelError('Client directory exists but is unknown or changed; choose a new directory')
    with urllib.request.urlopen(CLIENT_URL, timeout=60) as response:
        package = response.read(64*1024*1024+1)
    if hashlib.sha256(package).hexdigest() != CLIENT_SHA256:
        raise ModelError('Official client package checksum mismatch; no files installed')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.adomd-', dir=destination.parent) as temporary:
        staging = Path(temporary)/'client';staging.mkdir()
        digests = {}
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            for member in archive.infolist():
                name = member.filename
                allowed = ((name.startswith('lib/net472/') and name.count('/') == 2) or
                           (name.startswith('runtimes/win-x64/native/') and name.count('/') == 3))
                if not allowed or not name.endswith('.dll'):
                    continue
                basename = name.rsplit('/',1)[-1]
                if '\\' in basename or basename in digests or member.file_size > 64*1024*1024:
                    raise ModelError('Unexpected client package entry')
                payload = archive.read(member)
                (staging/basename).write_bytes(payload)
                digests[basename] = hashlib.sha256(payload).hexdigest()
        (staging/'installed.json').write_text(json.dumps({'version':CLIENT_VERSION, 'source':CLIENT_URL,
                                                       'package_sha256':CLIENT_SHA256, 'files':digests},indent=2),encoding='utf-8')
        if not _installed(staging):
            raise ModelError('Official package is missing required client dependencies')
        # No DLL has been loaded in this process, so Windows can move the files.
        os.rename(staging, destination)
    return destination/'Microsoft.AnalysisServices.AdomdClient.dll'
