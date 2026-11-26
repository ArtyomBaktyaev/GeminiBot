"""Best-effort PDF worker memory protection on small Linux cgroups."""
from dataclasses import dataclass
from pathlib import Path
import subprocess
import time

MIB = 1024 * 1024

@dataclass(frozen=True)
class MemorySnapshot:
    current: int
    limit: int
    inactive_file: int = 0

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


def memory_snapshot(root=Path('/sys/fs/cgroup')):
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
        return MemorySnapshot(current, limit, max(0, min(current,inactive)))
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
