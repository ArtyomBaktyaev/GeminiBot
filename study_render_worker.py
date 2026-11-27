"""Keep PDF jobs bounded and isolated from Telegram's event loop and process."""
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from threading import BoundedSemaphore

_SLOT=BoundedSemaphore(1)
LOG=logging.getLogger('study.pdf')
from study_runtime_diag import memory_status
from study_deadline import CURRENT, PdfExpired
from study_layout import page_target
from study_chromium import MemoryPressure, check_memory, communicate_guarded, memory_snapshot, MIB
from study_memory_relief import reclaim_if_small


def _memory_failure(budget, exc):
    LOG.warning("PDF memory guard: %s; %s", exc, memory_status())
    message = "На сервере недостаточно оперативной памяти для безопасной вёрстки PDF. Готовый PDF не создан."
    if budget:
        budget.failure_reason = message
        raise PdfExpired(budget.stage) from exc
    raise RuntimeError(message) from exc


def _terminate(proc):
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if proc.poll() is None:
            proc.kill()
    try:
        proc.communicate(timeout=5)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def render_bounded(title, blocks):
    start=time.monotonic()
    target = page_target(blocks)
    five_minutes = target is not None and 6 <= target[1] <= 8
    budget = CURRENT.get()
    if budget:
        budget.enter("очередь рендера")
    wait = budget.available(budget.render_end - budget.seconds * .10) if budget else 30
    if five_minutes:
        wait = min(wait, 300)
    if not _SLOT.acquire(timeout=wait):
        if budget:
            raise PdfExpired(budget.stage)
        raise RuntimeError('Генератор PDF занят. Повтори запрос немного позже.')
    try:
        if budget:
            budget.enter("вёрстка PDF")
            budget.available(budget.render_end)
        if five_minutes and time.monotonic() >= start + 300:
            if budget:
                raise PdfExpired(budget.stage)
            raise RuntimeError('Истекло время ожидания в очереди PDF.')
        reclaim_if_small()
        LOG.info('PDF worker start; queue %.1fs; %s',time.monotonic()-start,memory_status())
        executable=Path(sys.executable)
        if executable.name.lower()=='pythonw.exe': executable=executable.with_name('python.exe')
        args=[str(executable),str(Path(__file__).resolve()),'--worker']
        payload = json.dumps([title, blocks], ensure_ascii=False).encode('utf-8')
        try:
            check_memory()
        except MemoryPressure as e:
            _memory_failure(budget, e)
        deadline = start + 300 if five_minutes else time.monotonic() + 90
        if budget:
            deadline = min(deadline, budget.render_end) if five_minutes else budget.render_end
        for attempt in range(2):
            baseline = memory_snapshot()
            proc=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                  start_new_session=(os.name=='posix'))
            LOG.info('PDF worker spawned pid=%s; attempt=%s',proc.pid,attempt+1)
            try:
                timeout = max(0, deadline - time.monotonic())
                output,_=communicate_guarded(proc, payload, timeout)
            except (subprocess.TimeoutExpired, MemoryPressure) as e:
                LOG.error('PDF worker stopped; pid=%s; reason=%s; %s',proc.pid,type(e).__name__,memory_status())
                _terminate(proc)
                if isinstance(e, MemoryPressure):
                    # A single retry, only after freeing pages, with demonstrably
                    # lower baseline memory and enough original deadline left.
                    if attempt == 0 and baseline and deadline-time.monotonic()>30:
                        reclaim_if_small()
                        time.sleep(1)
                        recovered = memory_snapshot()
                        if recovered and recovered.limit==baseline.limit and not recovered.pressured and recovered.working_set+8*MIB <= baseline.working_set:
                            LOG.info('PDF memory retry; working set fell by at least 8 MiB; same document and deadline')
                            continue
                    _memory_failure(budget, e)
                if budget:
                    raise PdfExpired(budget.stage) from e
                raise RuntimeError('Время вёрстки PDF истекло; генерация остановлена без отправки повреждённого файла.') from e
            except BaseException:
                _terminate(proc)
                raise
            if proc.returncode or not output.startswith(b'%PDF-') or not output.rstrip().endswith(b'%%EOF'):
                LOG.error('PDF worker failed; exit=%s; output bytes=%d; %s',proc.returncode,len(output),memory_status())
                raise RuntimeError('Ошибка генератора PDF; подробности в журнале Render (PDF worker).')
            break
        LOG.info('PDF worker done %.1fs; %d bytes',time.monotonic()-start,len(output))
        return output
    finally: _SLOT.release()


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,format='%(asctime)s | %(levelname)s | %(message)s',stream=sys.stderr)
    if sys.argv[1:]!=['--worker']: raise SystemExit('Internal PDF worker')
    LOG.info('PDF child importing renderer; %s',memory_status())
    from study_pdf import render_pdf
    title,blocks=json.loads(sys.stdin.buffer.read())
    sys.stdout.buffer.write(render_pdf(title,blocks))
