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


def render_bounded(title, blocks):
    start=time.monotonic()
    if not _SLOT.acquire(timeout=30):
        raise RuntimeError('Генератор PDF занят. Повтори запрос немного позже.')
    try:
        LOG.info('PDF worker start; queue %.1fs',time.monotonic()-start)
        executable=Path(sys.executable)
        if executable.name.lower()=='pythonw.exe': executable=executable.with_name('python.exe')
        args=[str(executable),str(Path(__file__).resolve()),'--worker']
        proc=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                              start_new_session=(os.name=='posix'))
        try:
            output,_=proc.communicate(json.dumps([title,blocks],ensure_ascii=False).encode('utf-8'),timeout=90)
        except subprocess.TimeoutExpired as e:
            if os.name=='posix':
                try: os.killpg(proc.pid,signal.SIGKILL)
                except ProcessLookupError: pass
            else:
                subprocess.run(['taskkill','/F','/T','/PID',str(proc.pid)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10,check=False)
                if proc.poll() is None: proc.kill()
            proc.communicate()
            raise RuntimeError('PDF не удалось сверстать за 90 секунд; генерация остановлена без отправки повреждённого файла.') from e
        if proc.returncode or not output.startswith(b'%PDF-'):
            raise RuntimeError('Ошибка генератора PDF; подробности в журнале Render (PDF worker).')
        LOG.info('PDF worker done %.1fs; %d bytes',time.monotonic()-start,len(output))
        return output
    finally: _SLOT.release()


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,format='%(asctime)s | %(levelname)s | %(message)s',stream=sys.stderr)
    if sys.argv[1:]!=['--worker']: raise SystemExit('Internal PDF worker')
    from study_pdf import render_pdf
    title,blocks=json.loads(sys.stdin.buffer.read())
    sys.stdout.buffer.write(render_pdf(title,blocks))
