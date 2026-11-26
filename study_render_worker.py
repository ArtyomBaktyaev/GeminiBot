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
from study_chromium import MemoryPressure, check_memory, communicate_guarded


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
    budget = CURRENT.get()
    if budget:
        budget.enter("очередь рендера")
    wait = budget.available(budget.render_end - budget.seconds * .10) if budget else 30
    if not _SLOT.acquire(timeout=wait):
        if budget:
            raise PdfExpired(budget.stage)
        raise RuntimeError('Генератор PDF занят. Повтори запрос немного позже.')
    try:
        if budget:
            budget.enter("вёрстка PDF")
            budget.available(budget.render_end)
        LOG.info('PDF worker start; queue %.1fs; %s',time.monotonic()-start,memory_status())
        executable=Path(sys.executable)
        if executable.name.lower()=='pythonw.exe': executable=executable.with_name('python.exe')
        args=[str(executable),str(Path(__file__).resolve()),'--worker']
        payload = json.dumps([title, blocks], ensure_ascii=False).encode('utf-8')
        try:
            check_memory()
        except MemoryPressure as e:
            _memory_failure(budget, e)
        proc=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                              start_new_session=(os.name=='posix'))
        LOG.info('PDF worker spawned pid=%s',proc.pid)
        try:
            timeout = max(0, budget.render_end - time.monotonic()) if budget else 90
            output,_=communicate_guarded(proc, payload, timeout)
        except (subprocess.TimeoutExpired, MemoryPressure) as e:
            LOG.error('PDF worker stopped; pid=%s; reason=%s; %s',proc.pid,type(e).__name__,memory_status())
            _terminate(proc)
            if isinstance(e, MemoryPressure):
                _memory_failure(budget, e)
            if budget:
                raise PdfExpired(budget.stage) from e
            raise RuntimeError('PDF не удалось сверстать за 90 секунд; генерация остановлена без отправки повреждённого файла.') from e
        except BaseException:
            _terminate(proc)
            raise
        if proc.returncode or not output.startswith(b'%PDF-') or not output.rstrip().endswith(b'%%EOF'):
            LOG.error('PDF worker failed; exit=%s; output bytes=%d; %s',proc.returncode,len(output),memory_status())
            raise RuntimeError('Ошибка генератора PDF; подробности в журнале Render (PDF worker).')
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
