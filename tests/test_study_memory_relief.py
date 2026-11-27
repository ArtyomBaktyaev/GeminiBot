import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from study_file_cache import DiskFileCache
from study_memory_relief import trim_free_memory


class DiskCacheTests(unittest.TestCase):
    def test_data_not_in_python_cache_and_private_owner_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            c=DiskFileCache(directory=root)
            payload=b'x' * (1024*1024)
            key=c.store(payload,'test.pdf','application/pdf','user1')
            entry=c.get(key)
            self.assertEqual(entry['owner'],'user1')
            self.assertNotIn('data',entry)
            self.assertEqual(entry['path'].read_bytes(),payload)
            self.assertEqual(c.total_bytes,len(payload))
            self.assertNotEqual(entry['path'].name,'test.pdf')
            c.close()
            self.assertFalse(entry['path'].exists())

    def test_count_and_size_eviction(self):
        with tempfile.TemporaryDirectory() as root:
            c=DiskFileCache(directory=root,max_count=2,max_bytes=12)
            a=c.store(b'abcdef','a','x','1');p=c.get(a)['path']
            b=c.store(b'abcdef','b','x','2')
            d=c.store(b'abcdef','d','x','1')
            self.assertIsNone(c.get(a));self.assertFalse(p.exists())
            self.assertEqual(c.total_bytes,12)
            self.assertIsNotNone(c.get(b));self.assertIsNotNone(c.get(d))
            c.close()

    def test_expiry_and_oversize(self):
        with tempfile.TemporaryDirectory() as root:
            c=DiskFileCache(directory=root,ttl=5,max_bytes=4)
            with patch('study_file_cache.time.time',return_value=100):
                a=c.store(b'ab','a','x','1');p=c.get(a)['path']
                with self.assertRaises(ValueError):c.store(b'abcde','b','x','1')
            with patch('study_file_cache.time.time',return_value=106):
                self.assertIsNone(c.get(a));self.assertFalse(p.exists())
            c.close()

    def test_trim_preserves_live_data(self):
        data=bytearray(b'x'*1024*1024)
        self.assertIsInstance(trim_free_memory(),bool)
        self.assertEqual(data,b'x'*1024*1024)

    def test_isolated_pdf_count(self):
        from io import BytesIO
        from pypdf import PdfWriter
        out=BytesIO();w=PdfWriter()
        for _ in range(3):w.add_blank_page(595,842)
        w.write(out)
        worker=Path(__file__).resolve().parents[1]/'study_pdf_notice_worker.py'
        if not worker.exists():worker=Path(__file__).with_name('study_pdf_notice_worker.py')
        result=subprocess.run([sys.executable,str(worker)],input=out.getvalue(),capture_output=True,check=True,timeout=10)
        self.assertEqual(result.stdout.strip(),b'3')


if __name__=='__main__':unittest.main()
