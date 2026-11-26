"""Full integration test, runs against the actual cloned project's PDF renderer."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from io import BytesIO
from pypdf import PdfReader
from study_markdown import parse_markdown
from study_layout import stamp_page_target
from study_polish import stamp_profile
from study_render_worker import render_bounded
from study_deadline import PdfBudget, size_seconds
from study_clean import normalize_blocks
from study_pdf import document_html

ROOT=Path(__file__).parent
TITLE='Производная функции: теория и практика'
source=(ROOT/'fixtures/study_regression.md').read_text(encoding='utf8')
request='Создай учебное пособие PDF на 5–6 страниц с графиками.'
blocks=parse_markdown(stamp_profile(stamp_page_target(source,request),request))
html=document_html(TITLE,blocks)
assert html.count('<section class="chapter"')==3,'Expected three real chapters'
assert 'Привет!' not in html and 'Вот доработанный вариант' not in html
assert 'class="code-lang">study' not in html
assert html.count('class="editorial-figure"')==3
assert 'figure.editorial-figure svg {' not in html, 'Global chart SVG style is still present'
budget = PdfBudget(size_seconds(request))
data = asyncio.run(budget.run(lambda: asyncio.to_thread(render_bounded, TITLE, blocks)))
assert budget.seconds == 600
reader=PdfReader(BytesIO(data)); count=len(reader.pages)
assert 5<=count<=8,f'Unexpected page count: {count}'
found={key.lstrip('/'):reader.get_destination_page_number(dest) for key,dest in reader.named_destinations.items()}
assert all('sec-'+str(n) in found for n in (1,2,3)),found
pages=[found['sec-'+str(n)] for n in (1,2,3)]
assert pages[0]>=2 and pages[0]<pages[1]<pages[2],pages
text='\n'.join(p.extract_text() or '' for p in reader.pages)
assert 'Привет!' not in text and 'Вот доработанный вариант' not in text
assert 'document-source.md' in reader.attachments
md=reader.attachments['document-source.md'][0].decode('utf8')
assert 'Привет!' not in md and '# Оглавление' not in md and md.count('```plot')==3
assert len(reader.pages[1].get('/Annots',[]))>=3,'Missing TOC links'
Path(sys.argv[1]).write_bytes(data)
print(f'PASS: {count} pages; 3 chapters on pages {[p+1 for p in pages]}; 3 actual plots; clean source and TOC links.')
