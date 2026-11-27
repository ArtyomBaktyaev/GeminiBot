"""Several real PDF exports in one application process, with warm cache and 512 MiB.
No credentials or model calls. Run in disposable Linux cgroup v2 container.
"""
import asyncio
from pathlib import Path
from io import BytesIO
import os
import sys
import time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from test_study_export import bot,ext_webapp
from study_deadline import PdfBudget
from study_layout import stamp_page_target
from study_polish import stamp_profile,pdf_notice
from study_chromium import memory_snapshot,MIB
from study_markdown import parse_markdown

snap=memory_snapshot();assert snap and snap.limit==512*MIB and snap.directory
peak=snap.directory/'memory.peak';events=snap.directory/'memory.events'
def counters():return dict(line.split() for line in events.read_text().splitlines())
before=counters()
# Several accounts have previously generated files, and chats are not cleared.
for account in range(3):
    for n in range(5):ext_webapp.GENERATED_FILES.store(b'x'*(4*MIB),'previous.pdf','application/pdf',str(account))
    ext_webapp.APP_CHATS[str(account)]=[{'text':'Материал '*2000,'parts':[],'file_names':[]} for _ in range(24)]
assert ext_webapp.GENERATED_FILES.total_bytes==60*MIB
assert all('data' not in e for e in ext_webapp.GENERATED_FILES.entries.values())
base=(ROOT/'tests/fixtures/study_regression.md').read_text(encoding='utf-8')
extra='\n\n## Дополнительные вычисления\n\n'+'\n\n'.join('**Шаг:** Для проверки подставим значение $x='+str(i)+'$ в формулу $f(x)=x^3-3x$.\n\n$$f('+str(i)+')='+str(i**3-3*i)+'$$' for i in range(1,42))
source=base+extra+'\n\nEND_SENTINEL\n'
request='Подготовь PDF на 6–8 страниц с графиками.'
source=stamp_profile(stamp_page_target(source,request),request)
assert len(parse_markdown(source))>=107
out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=True)
for i in range(3):
    started=time.monotonic()
    data,filename,mime=asyncio.run(PdfBudget(600).run(lambda:asyncio.to_thread(bot.build_document,'pdf','Производная: нагрузочная проверка',source)))
    assert mime=='application/pdf' and data.startswith(b'%PDF-') and data.rstrip().endswith(b'%%EOF')
    # No persistent parent PDF parser from this diagnostic.
    pdf_notice(data,source)
    (out/f'load-{i+1}.pdf').write_bytes(data)
    del data
    s=memory_snapshot();print('run',i+1,'seconds',round(time.monotonic()-started,2),'current MiB',round(s.current/MIB,1),'peak MiB',round(int(peak.read_text())/MIB,1),flush=True)
    assert int(peak.read_text())<512*MIB,'Hit the hard memory limit'
    assert all(counters().get(k,'0')==before.get(k,'0') for k in ('oom','oom_kill','max')),'Memory limit hit'
ext_webapp.GENERATED_FILES.close()
print('PASS: three consecutive guarded renders, warm disk cache, no memory-limit events.')
