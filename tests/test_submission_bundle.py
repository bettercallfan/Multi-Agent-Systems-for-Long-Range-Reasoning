import hashlib
from pathlib import Path
import tempfile
import unittest
import zipfile

from deployment.bootstrap import unpack
from deployment.verify_bundle import verify


class SubmissionBundleTests(unittest.TestCase):
    def test_bootstrap_preserves_existing_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'system_source').mkdir()
            with self.assertRaises(SystemExit):unpack(root)

    def test_bootstrap_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with zipfile.ZipFile(root/'system_source.zip','w') as z:z.writestr('../escape.txt','bad')
            with self.assertRaises(ValueError):unpack(root)
            self.assertFalse((root/'system_source').exists())

    def test_bootstrap_extracts_normal_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with zipfile.ZipFile(root/'system_source.zip','w') as z:z.writestr('main.py','print(1)')
            dest=unpack(root)
            self.assertEqual((dest/'main.py').read_text(),'print(1)')
            self.assertTrue((dest/'outputs/dashboard_jobs').is_dir())

    def test_checksum_detects_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'交付物.txt';path.write_bytes(b'original')
            (root/'SHA256SUMS.txt').write_text(hashlib.sha256(b'original').hexdigest()+'  交付物.txt\n',encoding='utf-8')
            self.assertTrue(verify(root));path.write_bytes(b'changed');self.assertFalse(verify(root))


if __name__=='__main__':unittest.main()
