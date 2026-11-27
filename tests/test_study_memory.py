import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from study_chromium import MIB, MemoryPressure, MemorySnapshot, check_memory, chromium_args, communicate_guarded, memory_snapshot

class MemoryTests(unittest.TestCase):
    def test_child_with_unlimited_memory_inherits_service_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            child = parent / 'worker'
            child.mkdir()
            for directory, cap, used in ((parent, str(512*MIB), 460*MIB), (child, 'max', 5*MIB)):
                (directory/'memory.current').write_text(str(used))
                (directory/'memory.max').write_text(cap)
            with patch('study_chromium._v2_directories', return_value=(child, parent)):
                snap = memory_snapshot()
                self.assertEqual(snap.directory, parent)
                self.assertTrue(snap.pressured)

    def test_cache_and_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'memory.current').write_text(str(490*MIB))
            (root/'memory.max').write_text(str(512*MIB))
            (root/'memory.stat').write_text(f'inactive_file {64*MIB}\n')
            self.assertFalse(memory_snapshot(root).pressured)
            (root/'memory.stat').write_text('inactive_file 0\n')
            self.assertTrue(memory_snapshot(root).pressured)
            (root/'memory.max').write_text('max')
            self.assertIsNone(memory_snapshot(root))

    def test_threshold_and_flags(self):
        self.assertFalse(MemorySnapshot(447*MIB,512*MIB).pressured)
        self.assertTrue(MemorySnapshot(448*MIB,512*MIB).pressured)
        with patch('study_chromium.memory_snapshot',return_value=MemorySnapshot(448*MIB,512*MIB)):
            with self.assertRaises(MemoryPressure): check_memory()
            self.assertIn('--renderer-process-limit=1',chromium_args())
        with patch('study_chromium.memory_snapshot',return_value=MemorySnapshot(700*MIB,1024*MIB)):
            self.assertEqual(chromium_args(),[])
            check_memory()

    def test_communicate_keeps_output(self):
        with patch('study_chromium.memory_snapshot',return_value=MemorySnapshot(200*MIB,512*MIB)):
            with subprocess.Popen([sys.executable,'-c','import sys,time; x=sys.stdin.buffer.read(); time.sleep(.35); sys.stdout.buffer.write(x)'],stdin=subprocess.PIPE,stdout=subprocess.PIPE) as proc:
                out,_=communicate_guarded(proc,b'test',4)
                self.assertEqual(out,b'test')

    def test_stops_on_pressure(self):
        with subprocess.Popen([sys.executable,'-c','import time; time.sleep(10)'],stdin=subprocess.PIPE,stdout=subprocess.PIPE) as proc:
            try:
                with patch('study_chromium.memory_snapshot',side_effect=[MemorySnapshot(200*MIB,512*MIB),MemorySnapshot(200*MIB,512*MIB),MemorySnapshot(450*MIB,512*MIB)]):
                    with self.assertRaises(MemoryPressure): communicate_guarded(proc,b'',3)
            finally:
                proc.kill();proc.wait()

if __name__=='__main__': unittest.main()
