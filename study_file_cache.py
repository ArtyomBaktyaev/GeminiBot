"""Ephemeral, account-scoped file cache, with payloads on disk rather than the Python heap."""
import tempfile
from pathlib import Path
import time
import uuid


class DiskFileCache:
    def __init__(self, *, max_count=60, ttl=7200, max_bytes=256*1024*1024, directory=None):
        self.max_count, self.ttl, self.max_bytes = max_count, ttl, max_bytes
        self.directory = directory
        self._root = None
        self.entries = {}
        self.total_bytes = 0

    def _folder(self):
        if self._root is None:
            self._root = tempfile.TemporaryDirectory(prefix='gemini-file-cache-', dir=self.directory)
        return Path(self._root.name)

    def _remove(self, key):
        item = self.entries.pop(key, None)
        if item:
            self.total_bytes -= item['size']
            item['path'].unlink(missing_ok=True)

    def store(self, data, name, mime, owner):
        now = time.time()
        if len(data) > self.max_bytes:
            raise ValueError('Файл превышает допустимый размер временного хранилища')
        for key, entry in list(self.entries.items()):
            if now-entry['ts'] > self.ttl:
                self._remove(key)
        while self.entries and (len(self.entries) >= self.max_count or self.total_bytes+len(data) > self.max_bytes):
            self._remove(min(self.entries, key=lambda k:self.entries[k]['ts']))
        key = uuid.uuid4().hex
        path = self._folder()/key
        try:
            # Use a private temporary directory, exclusive creation, and opaque filenames.
            with path.open('xb') as f:
                f.write(data)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        self.entries[key] = dict(owner=owner, name=name, mime=mime, ts=now, path=path, size=len(data))
        self.total_bytes += len(data)
        return key

    def get(self, key, default=None):
        item = self.entries.get(key)
        if item is None:
            return default
        if time.time()-item['ts'] > self.ttl:
            self._remove(key)
            return default
        return item

    def close(self):
        if self._root is not None:
            self._root.cleanup()
            self._root = None
        self.entries.clear()
        self.total_bytes = 0
