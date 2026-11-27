"""Run this inside a disposable 512 MiB Docker container, not production."""
import asyncio
from io import BytesIO
import os
from pathlib import Path
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from test_study_export import bot
from study_chromium import memory_snapshot, MIB
from study_deadline import PdfBudget
from study_layout import stamp_page_target
from study_polish import stamp_profile
from test_study_editorial_integration import SOURCE
from pypdf import PdfReader

# Use the same data through the real bot builder and guarded renderer.
# There are no API calls or real credentials; this is not a live Telegram test.
request='Подготовь PDF на 6–8 страниц. Решения помести в отдельный раздел.'
source=stamp_profile(stamp_page_target(SOURCE,request),request)
start=memory_snapshot()
assert start and start.limit==512*MIB,start
assert start.directory is not None and (start.directory/'memory.peak').exists(), 'Require cgroup v2 peak counter'
peak_file=start.directory/'memory.peak'
events_file=start.directory/'memory.events'
def events(): return dict(line.split() for line in events_file.read_text().splitlines())
before=events(); peak=[start.working_set];stop=threading.Event()
def sample():
    while not stop.wait(.025):
        s=memory_snapshot()
        if s: peak[0]=max(peak[0],s.working_set)
monitor=threading.Thread(target=sample,daemon=True);monitor.start()
t=time.monotonic()
try:
    data, filename, mime=asyncio.run(PdfBudget(600).run(lambda: asyncio.to_thread(bot.build_document,'pdf','Проверка редакционной структуры',source)))
finally:
    stop.set();monitor.join()
assert mime=='application/pdf' and data.startswith(b'%PDF-') and data.rstrip().endswith(b'%%EOF')
reader=PdfReader(BytesIO(data))
assert 6<=len(reader.pages)<=8,len(reader.pages)
text='\n'.join(p.extract_text() or '' for p in reader.pages)
assert 'END_SENTINEL' in text
assert 'Листинг' not in text
assert 'document-source.md' in reader.attachments
md=reader.attachments['document-source.md'][0].decode('utf8')
assert md.count('```plot')==1
assert '## Введение в дифференциальное исчисление' in md
assert '## Задачи для самопроверки' in md
assert '# Ответы и решения к задачам для самопроверки' in md
names={k.lstrip('/'):reader.get_destination_page_number(v) for k,v in reader.named_destinations.items()}
assert all('sec-'+str(i) in names for i in range(1,9)),names
assert names['sec-8']>names['sec-7'],names
assert len(reader.pages[1].get('/Annots',[]))>=8
assert all(events().get(k,'0')==before.get(k,'0') for k in ('oom','oom_kill'))
actual=int(peak_file.read_text())
assert actual<start.limit and peak[0]<448*MIB,(actual,peak[0])
out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=True);(out/'editorial.pdf').write_bytes(data)
print(f'PASS: {len(reader.pages)} pages, 8 heading destinations, no empty listing; cgroup peak {actual/MIB:.1f} MiB, sampled working set {peak[0]/MIB:.1f} MiB, {time.monotonic()-t:.1f} s',flush=True)
