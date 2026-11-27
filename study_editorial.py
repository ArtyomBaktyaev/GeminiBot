"""Cheap, conservative chapter planning for generated educational notes."""
import re


def grouping_allowed(request):
    # Do not override a supplied outline or an explicit pagination rule.
    request = str(request or '')
    return not bool(re.search(
        r'(?im)^\s*#{1,6}\s+|\b(?:глава|раздел)\s*\d+|'
        r'\b\d+\s+(?:глав\w*|раздел\w*)\b|'
        r'кажд\w*\s+(?:глав\w*|раздел\w*).{0,60}(?:новой|отдельной)\s+страниц|'
        r'(?:не\s+объединяй|не\s+меняй\s+структуру)|'
        r'(?:график\w*|правил\w*|задач\w*)\s+(?:долж\w*\s+быть\s+)?(?:в\s+)?отдельн\w*\s+(?:глав\w*|раздел\w*)', request))


def _headings(body):
    level = min((b.get('lvl', 1) for b in body if b.get('t') == 'h'), default=1)
    return level, [i for i,b in enumerate(body) if b.get('t')=='h' and b.get('lvl',1)==level]


def _kind(title):
    s = title.casefold().replace('ё','е')
    if re.search(r'ответ|решени[яйе]', s): return 'answers'
    if re.search(r'^(?:введение\b|понятие\s+производной|определение\s+производной|геометрический\s+смысл\s+производной)', s): return 'intro'
    if re.search(r'^(?:основные\s+)?правила\s+дифференцирования|^таблица\s+производных', s): return 'rules'
    if re.search(r'^графи(?:ки|ческий\s+анализ|ческая\s+иллюстрация)|^визуализация', s): return 'graphs'
    if re.search(r'^задачи\s+(?:для\s+)?(?:самопроверки|самостоятельного\s+решения)', s): return 'tasks'
    return ''


def _short_pair(blocks):
    # Only an inexpensive preflight heuristic, not a physical page count.
    chars=maths=figures=0
    for b in blocks:
        if b['t']=='code':
            if b.get('lang') not in ('plot','diagram'): return False
            figures+=1
        elif b['t']=='table':
            return False
        else:
            chars+=len(b.get('text',''))
            maths+=int(b['t']=='math')
    return chars <= (1900 if figures else 3400) and figures <= 2 and maths <= 4


def compact_chapters(body, target, allowed=False):
    """Join only adjacent, recognised short topics; never remove body blocks."""
    result=[dict(b) for b in body]
    if not allowed or not target or not 5 <= target[1] <= 10:
        return result
    level, heads=_headings(result)
    if len(heads)<5 or level>=3: return result
    pairs={('intro','rules'):'Теория производных', ('graphs','tasks'):'Графики и задачи для самопроверки'}
    # Prefer genuine chapters; target includes cover, TOC and longer chapters.
    desired=max(3,target[1]-4)
    i=0
    while len(heads)>desired and i+1<len(heads):
        a,b=heads[i:i+2]
        end=heads[i+2] if i+2<len(heads) else len(result)
        title=pairs.get((_kind(result[a]['text']),_kind(result[b]['text'])))
        if title and _short_pair(result[a:end]):
            # Parent and original subsection titles are all kept. Do not
            # renumber list items or touch formulas, graphs or other content.
            for j in range(a,end):
                if result[j]['t']=='h': result[j]['lvl']=min(3,result[j]['lvl']+1)
            result.insert(a,{'t':'h','lvl':level,'text':title})
            level,heads=_headings(result)
        i+=1
    return result


def align_cover(meta, body):
    """Use real chapter names in the template when model goals disagree."""
    _,heads=_headings(body)
    if not heads: return
    names=[body[i]['text'] for i in heads]
    def key(s): return re.sub(r'[^\w]+',' ',s.casefold()).strip()
    goals=meta.get('goals')
    if goals and [key(g[0]) for g in goals] != [key(n) for n in names[:3]]:
        # The existing renderer automatically creates linked cover rows.
        meta.pop('goals',None)
    figures=sum(b.get('t')=='code' and b.get('lang') in ('plot','diagram') for b in body)
    count=f'Разделов — {len(names)}, иллюстраций — {figures}.'
    # Do not duplicate a count on repeated normalization.
    outcomes=re.sub(r'^Разделов — \d+, иллюстраций — \d+\.\s*','',meta.get('outcomes','')).strip()
    # Always show the actual number, so the three-item cover preview isn't
    # mistaken for an exhaustive TOC. Preserve the model's outcome text.
    if len(names)>3 or 'outcomes' in meta:
        meta['outcomes']=count+(' '+outcomes if outcomes else '')
