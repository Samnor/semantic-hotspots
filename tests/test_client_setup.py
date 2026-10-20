"""Synthetic package bytes test extraction boundaries without a network call."""
import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from semantic_hotspots import install_adomd, ModelError
from semantic_hotspots.client_setup import _installed


class ClientSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'client'
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:
            for name in ['lib/net472/Microsoft.AnalysisServices.AdomdClient.dll',
                         'lib/net472/Microsoft.AnalysisServices.SPClient.Interfaces.dll',
                         'runtimes/win-x64/native/msasxpress.dll',
                         'runtimes/win-x86/native/msasxpress.dll','../../outside.dll']:
                z.writestr(name,b'Fictional assembly placeholder '+name.encode())
        self.package=stream.getvalue()

    def install(self,checksum=None):
        with patch('semantic_hotspots.client_setup.sys.platform','win32'),patch('semantic_hotspots.client_setup.platform.machine',return_value='AMD64'),patch('semantic_hotspots.client_setup.CLIENT_SHA256',checksum or hashlib.sha256(self.package).hexdigest()),patch('semantic_hotspots.client_setup.urllib.request.urlopen',return_value=io.BytesIO(self.package)):
            result=install_adomd(self.root)
            self.assertTrue(_installed(self.root))
            return result

    def test_pinned_package_whitelist_reuse_and_tampering(self):
        path=self.install()
        self.assertTrue(path.is_file())
        self.assertEqual(len(list(self.root.glob('*.dll'))),3)
        self.assertIn(b'win-x64', (self.root/'msasxpress.dll').read_bytes())
        self.assertFalse((self.root.parent/'outside.dll').exists())
        self.assertEqual(self.install(),path)
        path.write_bytes(b'changed')
        with self.assertRaises(ModelError):self.install()

    def test_bad_checksum_and_existing_unknown_directories_leave_no_install(self):
        with self.assertRaises(ModelError):self.install('0'*64)
        self.assertFalse(self.root.exists())
        self.root.mkdir();(self.root/'keep').write_text('unchanged')
        with self.assertRaises(ModelError):self.install()
        self.assertEqual((self.root/'keep').read_text(),'unchanged')


if __name__=='__main__':unittest.main()
