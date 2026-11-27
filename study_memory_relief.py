"""Return only free allocator pages; never drop conversation state or change cgroup limits."""
import gc
import logging
import sys

LOG = logging.getLogger('study.pdf')


def trim_free_memory():
    if not sys.platform.startswith('linux'):
        return False
    gc.collect()
    try:
        import ctypes
        trim = ctypes.CDLL(None).malloc_trim
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        return bool(trim(0))
    except (AttributeError, OSError):
        return False


def reclaim_if_small():
    from study_chromium import memory_snapshot, MIB
    before = memory_snapshot()
    if before is None or not before.constrained:
        return
    trim_free_memory()
    after = memory_snapshot()
    if after is not None:
        LOG.info('PDF allocator trim; working set %.1f -> %.1f MiB; total %.1f -> %.1f MiB',
                 before.working_set/MIB, after.working_set/MIB,
                 before.current/MIB, after.current/MIB)
