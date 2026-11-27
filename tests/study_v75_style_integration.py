"""Run only in disposable test environment. No network calls or real bot credentials."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
MODE=sys.argv[1]; OUT=Path(sys.argv[2]); OUT.mkdir(parents=True,exist_ok=True)


def manifest(pdf):
    from pypdf import PdfReader
    reader=PdfReader(pdf)
    names={k.lstrip('/'):reader.get_destination_page_number(dest) for k,dest in reader.named_destinations.items()}
    assert len(reader.pages) in range(5,9),len(reader.pages)
    assert all('sec-'+str(n) in names for n in (1,2,3)),names
    assert names['sec-1']<names['sec-2']<names['sec-3'],names
    assert 'document-source.md' in reader.attachments
    source=reader.attachments['document-source.md'][0].decode('utf8')
    assert source.count('```plot')==3,'Missing source graph data'
    assert len(reader.pages[1].get('/Annots',[]))>=3,'Missing TOC links'
    return {'pages':len(reader.pages),'names':names,
            'annotation_counts':[len(p.get('/Annots',[])) for p in reader.pages],
            'source':source,'attachments':sorted(reader.attachments.keys())}


def generate(mode):
    if mode=='baseline':os.environ['STUDY_PDF_TRANSPORT']='playwright'
    elif mode=='direct':os.environ['STUDY_PDF_TRANSPORT']='direct'
    else:os.environ['STUDY_PDF_TRANSPORT']='auto'
    # Import the real application in a temporary directory with dummy credentials,
    # exactly as existing repository tests do, without polling or API requests.
    from test_study_export import bot, ext_webapp
    from study_chromium import memory_snapshot, MIB
    from study_deadline import PdfBudget, size_seconds
    from study_layout import stamp_page_target
    from study_polish import stamp_profile
    from study_markdown import parse_markdown
    from study_pdf import document_html
    source=(ROOT/'tests/fixtures/study_regression.md').read_text(encoding='utf8')
    request='Создай учебное пособие PDF на 5–6 страниц с графиками.'
    source=stamp_profile(stamp_page_target(source,request),request)
    title='Производная функции: теория и практика'
    blocks=parse_markdown(source)
    # Verify structural parity without holding two large HTML documents at once.
    html=document_html(title,blocks,external_fonts=(mode!='baseline'))
    assert html.count('<section class="chapter"')==3
    assert html.count('class="editorial-figure"')==3
    del html
    import gc;gc.collect()
    start=memory_snapshot()
    assert start is not None,'Cgroup memory counters unavailable'
    if mode=='limited':assert start.limit==512*MIB,start.limit
    peak=[start.working_set];stop=threading.Event()
    def sample():
        while not stop.wait(.025):
            snap=memory_snapshot()
            if snap:peak[0]=max(peak[0],snap.working_set)
    monitor=threading.Thread(target=sample,daemon=True);monitor.start()
    budget=PdfBudget(size_seconds(request))
    began=time.monotonic()
    try:
        data, filename, mime=asyncio.run(budget.run(lambda: asyncio.to_thread(bot.build_document,'pdf',title,source)))
    finally:
        stop.set();monitor.join()
    assert mime=='application/pdf' and data.startswith(b'%PDF-') and data.rstrip().endswith(b'%%EOF')
    target=OUT/(mode+'.pdf');target.write_bytes(data)
    facts=manifest(target)
    stats={'baseline_mib':round(start.working_set/MIB,2),'peak_mib':round(peak[0]/MIB,2),
           'delta_mib':round((peak[0]-start.working_set)/MIB,2),'limit_mib':round(start.limit/MIB,2),
           'seconds':round(time.monotonic()-began,2),'pages':facts['pages']}
    (OUT/(mode+'.json')).write_text(json.dumps(stats,ensure_ascii=False,indent=2),encoding='utf8')
    print(mode,stats,flush=True)
    if mode=='limited':assert peak[0] < start.limit-32*MIB,'Insufficient memory margin'


def compare(a,b):
    import fitz
    facts1,facts2=manifest(a),manifest(b)
    assert facts1==facts2,'Page count, source, navigation or annotations changed'
    with fitz.open(a) as x,fitz.open(b) as y:
        for i,(p,q) in enumerate(zip(x,y),1):
            pa=p.get_pixmap(matrix=fitz.Matrix(1.25,1.25),alpha=False)
            pb=q.get_pixmap(matrix=fitz.Matrix(1.25,1.25),alpha=False)
            assert (pa.width,pa.height)==(pb.width,pb.height),f'Page {i}: geometry differs'
            assert pa.samples==pb.samples,f'Page {i}: raster mismatch. No deployment.'
    print('PASS: exact raster equality, page count, navigation and source attachments.',flush=True)


if MODE=='compare':
    for m in ('baseline','direct'):
        subprocess.run([sys.executable,__file__,m,str(OUT)],check=True,timeout=300)
    compare(OUT/'baseline.pdf',OUT/'direct.pdf')
    a=json.loads((OUT/'baseline.json').read_text());b=json.loads((OUT/'direct.json').read_text())
    assert b['delta_mib']+8<a['delta_mib'],f'No demonstrated memory reduction: {a} vs {b}'
    print('PASS: lower peak working-set increase measured.',a['delta_mib'],'->',b['delta_mib'],'MiB',flush=True)
elif MODE in ('baseline','direct','limited'):
    generate(MODE)
    if MODE=='limited':
        compare(OUT/'baseline.pdf',OUT/'limited.pdf')
        print('PASS: 512 MiB integration with bot imported, same PDF appearance.',flush=True)
else:raise SystemExit('Unknown mode')
