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
        proc=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                              start_new_session=(os.name=='posix'))
        LOG.info('PDF worker spawned pid=%s',proc.pid)
        try:
            timeout = max(0, budget.render_end - time.monotonic()) if budget else 90
            output,_=proc.communicate(payload, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            LOG.error('PDF worker timeout; pid=%s; %s',proc.pid,memory_status())
            if os.name=='posix':
                try: os.killpg(proc.pid,signal.SIGKILL)
                except ProcessLookupError: pass
            else:
                subprocess.run(['taskkill','/F','/T','/PID',str(proc.pid)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10,check=False)
                if proc.poll() is None: proc.kill()
            proc.communicate()
            if budget:
                raise PdfExpired(budget.stage) from e
            raise RuntimeError('PDF не удалось сверстать за 90 секунд; генерация остановлена без отправки повреждённого файла.') from e
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
