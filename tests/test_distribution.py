"""Checks for portable distributions and accidental local-data inclusion."""
import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.build_release import normalize_source_archive
from tools.check_distribution import check_archive, check_entry


class DistributionTests(unittest.TestCase):
    def test_source_and_fictional_examples_are_allowed(self):
        check_entry('src/package/model.py', b'class Model: pass')
        check_entry('examples/retail.bim', b'{"name":"Fictional"}')

    def test_local_files_and_unsafe_archive_paths_are_rejected(self):
        for path in ['private/model.bim', 'local-validation/capture.json', '.venv/lib/module.py',
                     'src/__pycache__/model.pyc', '.env', '../outside.txt', '/absolute/file.txt',
                     'RELEASE-VERIFICATION.json', 'model.pbix']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                check_entry(path, b'')

    def test_home_paths_and_credential_patterns_are_rejected(self):
        # Construct fictional sensitive-looking samples without embedding them in source.
        samples = [('/'.join(['', 'home', 'example', 'project']) + '/').encode(),
                   ('/'.join(['', 'Users', 'example', 'project']) + '/').encode(),
                   b'ghp_' + b'A' * 36,
                   b'AKIA' + b'B' * 16,
                   b'-----BEGIN ' + b'PRIVATE KEY-----']
        for data in samples:
            with self.subTest(length=len(data)), self.assertRaises(ValueError):
                check_entry('package/file.txt', data)

    def test_guard_source_does_not_trigger_its_own_patterns(self):
        path = Path(__file__).resolve().parent.parent/'tools/check_distribution.py'
        check_entry('tools/check_distribution.py', path.read_bytes())

    def test_wheel_archive_is_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'example.whl'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('package/__init__.py', '# Fictional package')
            self.assertEqual(check_archive(path), 1)
            with zipfile.ZipFile(path, 'a') as archive:
                archive.writestr('private/model.bim', '{}')
            with self.assertRaises(ValueError):
                check_archive(path)

    def test_source_ownership_and_timestamp_are_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'example.tar.gz'
            with tarfile.open(path, 'w:gz') as archive:
                member = tarfile.TarInfo('package/README.md')
                member.uid = member.gid = 42
                member.uname = member.gname = 'example-account'
                member.size = 7
                member.pax_headers = {'uid': '42', 'uname': 'example-account', 'atime': '1.5'}
                archive.addfile(member, io.BytesIO(b'Example'))
            with self.assertRaises(ValueError):
                check_archive(path)
            normalize_source_archive(path, 1767225600)
            self.assertEqual(check_archive(path), 1)
            with tarfile.open(path) as archive:
                member = archive.next()
                self.assertEqual((member.uid, member.gid, member.uname, member.gname), (0, 0, '', ''))
                self.assertEqual(member.mtime, 1767225600)
                self.assertNotIn('atime', member.pax_headers)

    def test_source_archive_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'example.tar.gz'
            with tarfile.open(path, 'w:gz') as archive:
                member = tarfile.TarInfo('package/link')
                member.type = tarfile.SYMTYPE
                member.linkname = '../../outside'
                archive.addfile(member)
            with self.assertRaises(ValueError):
                check_archive(path)
