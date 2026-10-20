"""Check publishable archives without exposing matching sensitive content in logs."""
from __future__ import annotations

import re
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

BLOCKED_PARTS = {'local-validation', 'private', 'local-data', 'exports', 'reports',
                 'benchmark-results', 'rewrite-experiment', 'experiment-results', '__pycache__', '.git', '.venv', 'venv'}
BLOCKED_FILES = {'RELEASE-VERIFICATION.json', '.env', '.DS_Store', 'Thumbs.db'}
PATTERNS = {
    'personal filesystem path': re.compile(rb'(?:/(?:Users|home)/[A-Za-z0-9_.-]+/|/(?:Volumes)/[^\s\x22\x27]+/|[A-Za-z]:\\Users\\[A-Za-z0-9_.-]+\\)'),
    'GitHub access token': re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})'),
    'private key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'cloud access key': re.compile(rb'(?:AKIA|ASIA)[A-Z0-9]{16}'),
}


def check_entry(name, data):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or BLOCKED_PARTS.intersection(path.parts) or path.name in BLOCKED_FILES:
        raise ValueError(f'Non-distributable file: {name}')
    if any(part.startswith('.venv') for part in path.parts) or path.suffix in {'.pyc', '.pem', '.key', '.pbix', '.pbit'}:
        raise ValueError(f'Non-distributable file: {name}')
    for label, pattern in PATTERNS.items():
        if pattern.search(data):
            raise ValueError(f'Potential {label} in {name}; review locally before publishing')


def check_archive(path):
    count = 0
    if path.suffix == '.whl':
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise ValueError('Corrupt wheel archive')
            if archive.comment:
                raise ValueError('Unexpected wheel archive comment')
            for member in archive.infolist():
                check_entry(member.filename, archive.read(member))
                if member.extra:
                    raise ValueError(f'Unexpected ZIP extra metadata in {member.filename}')
                count += 1
    else:
        with tarfile.open(path) as archive:
            for member in archive:
                if member.uname not in {'', 'root'} or member.gname not in {'', 'root'} or member.uid or member.gid:
                    raise ValueError('Source archive has non-normalized ownership metadata')
                if member.issym() or member.islnk():
                    raise ValueError('Unexpected archive link')
                data = archive.extractfile(member).read() if member.isfile() else b''
                check_entry(member.name, data)
                count += 1
    return count


def main(argv=None):
    paths = [Path(p) for p in (argv if argv is not None else sys.argv[1:])]
    if not paths:
        paths = sorted(Path('dist').glob('*.whl')) + sorted(Path('dist').glob('*.tar.gz'))
    if not paths:
        raise SystemExit('No distribution archives found')
    for path in paths:
        print(f'{path.name}: checked {check_archive(path)} archive entries')


if __name__ == '__main__':
    main()
