"""Best-effort PDF worker memory protection on small Linux cgroups."""
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import subprocess
import time

MIB = 1024 * 1024

@dataclass(frozen=True)
class MemorySnapshot:
    current: int
    limit: int
    inactive_file: int = 0
    directory: Path | None = None

    @property
    def working_set(self):
        return max(0, self.current - self.inactive_file)

    @property
    def constrained(self):
        return 0 < self.limit <= 768 * MIB

    @property
    def pressured(self):
        reserve = max(48*MIB, self.limit//8)
        return self.constrained and self.working_set >= max(0, self.limit-reserve)


@lru_cache(maxsize=1)
def _v2_directories(membership):
    """Resolve proc membership against each visible cgroup2 mount root."""
    path = Path(membership)
    if not path.is_absolute() or '..' in path.parts:
        return ()
    try:
        lines = Path('/proc/self/mountinfo').read_text().splitlines()
    except OSError:
        return ()
    directories = []
    for line in lines:
        mount, _, fs = line.partition(' - ')
        if not fs.startswith('cgroup2 '):
            continue
        fields = mount.split()
        mount_root, point = (Path(s.replace(r'\040', ' ')) for s in fields[3:5])
        if path.is_relative_to(mount_root):
            leaf = point / path.relative_to(mount_root)
        else:
            leaf = point / path.relative_to('/')
        while leaf.is_relative_to(point):
            directories.append(leaf)
            if leaf == point:
                break
            leaf = leaf.parent
    return tuple(dict.fromkeys(directories))


def memory_snapshot(root=None):
    if root is None:
        root = Path('/sys/fs/cgroup')
        try:
            membership = next(line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines()
                              if line.startswith('0::'))
        except (OSError, StopIteration):
            membership = ''
        # A parent may own the service limit while a child reports "max".
        snaps = [s for p in _v2_directories(membership) if (s := _snapshot(p)) is not None]
        if snaps:
            small = [s for s in snaps if s.constrained]
            return min(small or snaps, key=lambda s: s.limit - max(48*MIB, s.limit//8) - s.working_set)
    return _snapshot(root)


def _snapshot(root):
    root = Path(root)
    for directory, used, cap, keys in (
        (root, 'memory.current', 'memory.max', ('inactive_file',)),
        (root/'memory', 'memory.usage_in_bytes', 'memory.limit_in_bytes', ('total_inactive_file', 'inactive_file')),
        (root, 'memory.usage_in_bytes', 'memory.limit_in_bytes', ('total_inactive_file', 'inactive_file')),
    ):
        try:
            current = int((directory/used).read_text().strip())
            limit = int((directory/cap).read_text().strip())
            if current < 0 or not 0 < limit < (1<<60):
                continue
        except (OSError, ValueError, UnicodeError):
            continue
        try:
            stats = dict(line.split() for line in (directory/'memory.stat').read_text().splitlines())
            inactive = next((int(stats[k]) for k in keys if k in stats), 0)
        except (OSError, ValueError, UnicodeError):
            inactive = 0
        return MemorySnapshot(current, limit, max(0, min(current,inactive)), directory)
    return None


def chromium_args():
    snap = memory_snapshot()
    # Do not use --single-process, --no-sandbox, or a hard V8 heap cap.
    return ['--disable-gpu', '--renderer-process-limit=1'] if snap and snap.constrained else []


class MemoryPressure(RuntimeError):
    pass


def check_memory():
    snap = memory_snapshot()
    if snap and snap.pressured:
        raise MemoryPressure(f'working set {snap.working_set//MIB} MiB; limit {snap.limit//MIB} MiB')


def communicate_guarded(proc, payload, timeout):
    """Poll while the child renders; preserve input/output across timeouts."""
    snap = memory_snapshot()
    if snap is None or not snap.constrained:
        return proc.communicate(payload, timeout=timeout)
    end = time.monotonic()+timeout
    pending = payload
    while True:
        if proc.poll() is None:
            check_memory()
        remaining = end-time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(proc.args, timeout)
        try:
            return proc.communicate(pending, timeout=min(.15,remaining))
        except subprocess.TimeoutExpired:
            pending = None
