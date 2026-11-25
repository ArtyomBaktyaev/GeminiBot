"""Document profiles, clean graph labels and non-destructive PDF diagnostics."""
import json
import math
import re
from io import BytesIO
from study_markdown import parse_markdown


def document_kind(request, title='', content=''):
    text = (str(request or '') + ' ' + str(title or '')).casefold()
    if re.search(r'пособи|учебник|курс лекций|многостраничн|доклад', text):
        return 'book'
    if re.search(r'лабораторн|лабораторная|отч[её]т по лаб', text):
        return 'lab'
    if re.search(r'шпаргал|памятк|краткий конспект', text):
        return 'cheat'
    if re.search(r'решени[ея] (?:одной )?задач|реши задач|реши уравн|реши пример', text):
        return 'solution'
    if len(content) < 2600:
        return 'note'
    return 'book'


def stamp_profile(content, request):
    m = re.match(r'\A\s*```study\s*\n(.*?)\n```\s*', content, re.S | re.I)
    meta = {}
    if m:
        try:
            candidate = json.loads(m[1])
            if isinstance(candidate, dict):
                meta = candidate
                content = content[m.end():]
        except ValueError:
            pass
    meta['document_kind'] = document_kind(request, content=content)
    return '```study\n'+json.dumps(meta, ensure_ascii=False)+'\n```\n\n'+content


def pdf_notice(data, content):
    from study_layout import page_target
    from pypdf import PdfReader
    target = page_target(parse_markdown(content))
    if not target:
        return ''
    count = len(PdfReader(BytesIO(data)).pages)
    if target[0] <= count <= target[1]:
        return ''
    wanted = str(target[0]) if target[0] == target[1] else f'{target[0]}–{target[1]}'
    return f'Объём: {count} стр. вместо {wanted}; материал сохранён без обрезания.'


def add_notice(caption, notice):
    if not notice:
        return caption
    return (str(caption or '')[:max(0, 895-len(notice))]+'\n'+notice).strip()


def clean_point_label(name):
    # Model labels sometimes already contain coordinates. Render only computed values.
    name = re.sub(r'\s*\([^()]*[;,][^()]*\)\s*$', '', str(name)).strip()
    return {'max':'Максимум','min':'Минимум'}.get(name.casefold(),name) or 'Точка'


def coord(value):
    if abs(value) < 1e-7:
        return '0'
    if abs(value-round(value)) < 1e-7:
        return str(round(value)).replace('-', '−')
    return '≈'+f'{value:.5g}'.replace('-', '−')


def clean_summary(summary, title, headings):
    def norm(s):
        return re.sub(r'[^\w]+',' ',s.casefold()).strip()
    s, t = norm(summary), norm(title)
    if not s or s == t or s in t or t in s and len(s) < len(t)+25:
        return 'Материал охватывает: ' + '; '.join(headings[:3]) + '.' if headings else ''
    return summary
