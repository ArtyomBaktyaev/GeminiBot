"""Linux cgroup diagnostics; best effort and no user/request data."""
from pathlib import Path


def memory_status():
    root = Path("/sys/fs/cgroup")
    values = []
    for name in ("memory.current", "memory.max", "memory.events", "memory/memory.usage_in_bytes", "memory/memory.limit_in_bytes"):
        try:
            value = (root / name).read_text(encoding="ascii").strip().replace("\n", "; ")
            values.append(name + "=" + value)
        except (OSError, UnicodeError):
            pass
    return " | ".join(values) or "cgroup counters unavailable"
