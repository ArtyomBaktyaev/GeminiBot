"""Linux cgroup diagnostics; best effort and no user/request data."""
from pathlib import Path
from study_chromium import memory_snapshot


def memory_status():
    snap = memory_snapshot()
    root = (snap.directory if snap else None) or Path("/sys/fs/cgroup")
    values = []
    for name in ("memory.current", "memory.max", "memory.peak", "memory.events", "memory.usage_in_bytes", "memory.limit_in_bytes"):
        try:
            value = (root / name).read_text(encoding="ascii").strip().replace("\n", "; ")
            values.append(name + "=" + value)
        except (OSError, UnicodeError):
            pass
    # Per-process RSS is diagnostic only. Shared mappings may appear in
    # several processes. The guard always uses group accounting.
    try:
        pids = (root / "cgroup.procs").read_text().split() if snap and snap.constrained else []
    except OSError:
        pids = []
    for pid in pids[:30]:
        try:
            status = (Path('/proc') / pid / 'status').read_text()
            fields = dict(line.split(':', 1) for line in status.splitlines() if ':' in line)
            values.append(f"pid={pid} {fields['Name'].strip()} RSS={fields['VmRSS'].strip()}")
        except (OSError, KeyError, UnicodeError):
            continue
    return " | ".join(values) or "cgroup counters unavailable"
