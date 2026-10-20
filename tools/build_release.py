"""Build portable distributions with reproducible, anonymous archive metadata."""
from __future__ import annotations

import gzip
import os
import subprocess
import sys
import tarfile
from pathlib import Path

if __package__:
    from .check_distribution import check_archive
else:
    from check_distribution import check_archive


def normalize_source_archive(path, epoch):
    temporary = path.with_suffix(path.suffix + '.tmp')
    try:
        with tarfile.open(path) as original, temporary.open('wb') as raw:
            with gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=epoch) as compressed:
                with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as output:
                    for member in original:
                        member.uid = member.gid = 0
                        member.uname = member.gname = ''
                        member.mtime = epoch
                        member.pax_headers = {k: v for k, v in member.pax_headers.items()
                                              if k not in {'uid', 'gid', 'uname', 'gname', 'atime', 'ctime', 'mtime'}}
                        output.addfile(member, original.extractfile(member) if member.isfile() else None)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    root = Path(__file__).resolve().parent.parent
    epoch = int(os.environ.get('SOURCE_DATE_EPOCH', '1767225600'))
    environment = dict(os.environ, SOURCE_DATE_EPOCH=str(epoch))
    subprocess.run([sys.executable, '-m', 'build'], cwd=root, env=environment, check=True)
    artifacts = sorted((root/'dist').glob('*.whl')) + sorted((root/'dist').glob('*.tar.gz'))
    for path in artifacts:
        if path.name.endswith('.tar.gz'):
            normalize_source_archive(path, epoch)
        print(f'{path.name}: checked {check_archive(path)} archive entries')


if __name__ == '__main__':
    main()
