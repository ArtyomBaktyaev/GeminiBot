"""Normalize reserved document metadata and obvious assistant wrappers before printing."""
import json
import re
from study_markdown_export import is_metadata, load_json


def _key(s):
    return re.sub(r'[^\w]+', ' ', str(s).casefold()).strip()


def _meta(raw):
    try:
        obj=json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(obj,dict): return {}
    result={}
    for k in ('subtitle','subject','format','outcomes','tags','document_kind'):
        v=obj.get(k)
        if isinstance(v,list) and k in ('outcomes','tags'):
            v=' · '.join(x for x in v[:8] if isinstance(x,str))
        if isinstance(v,str) and v.strip(): result[k]=v.strip()[:1000]
    if result.get('format','').casefold() in ('pdf','пдф'): result.pop('format')
    goals=[]
    if isinstance(obj.get('goals'),list):
        for g in obj['goals'][:3]:
            if isinstance(g,dict): g=[g.get('name',g.get('title','')),g.get('description','')]
            if isinstance(g,list) and len(g)==2 and all(isinstance(v,str) for v in g) and g[0].strip():
                goals.append([g[0][:120],g[1][:220]])
            elif isinstance(g,str) and g.strip(): goals.append([g[:160],''])
    if goals: result['goals']=goals
    p=obj.get('target_pages')
    if isinstance(p,list) and len(p)==2 and all(type(n) is int for n in p) and 1<=p[0]<=p[1]<=30: result['target_pages']=p
    return result


def _intro(b):
    if b.get('t')!='p': return False
    t=b.get('text','').strip().lstrip('*')
    return bool(re.match(r'(?i)^(?:привет[!,\s]|(?:вот|ниже)\s+(?:(?:полностью|обновл[её]нный|доработанный|исправленный|готовый|подробный)\s+)*(?:вариант|материал|текст|пособие)\s*[:.!]?\s*$|(?:конечно|хорошо)[!,]\s+(?:вот|сейчас|я\s+подготов))',t))


def _outro(b):
    if b.get('t')!='p': return False
    t=b.get('text','').strip().lstrip('*')
    return bool(re.match(r'(?i)^(?:[А-ЯЁа-яё]{2,24},\s*)?(?:если\s+(?:нужно|хочешь|понадобится|у тебя будут вопросы)|как\s+будешь\s+готов|дай\s+знать|могу\s+(?:также|ещ[её]|дополнительно|оформить))\b',t) and re.search(r'(?i)\b(?:пиши|дай знать|напиши|могу|оформлю|помогу|добавить|подправить)\b',t))


def normalize_blocks(blocks,title=''):
    meta={}; body=[]
    for b in blocks:
        if b.get('t')=='code' and b.get('lang','') in ('study','json',''):
            obj = load_json(b.get('text',''))
            if b.get('lang')=='study' or is_metadata(obj):
                for k,v in _meta(b.get('text','')).items(): meta.setdefault(k,v)
                continue
            if isinstance(obj,dict) and isinstance(obj.get('series'),list) and ('domain' in obj or 'x' in obj):
                b = dict(b,lang='plot')
            elif isinstance(obj,dict) and 'root' in obj and isinstance(obj.get('branches'),list):
                b = dict(b,lang='diagram')
        body.append(dict(b))
    # The outer title is supplied by the API, not a body section.
    while body and (body[0]['t']=='hr' or _intro(body[0]) or (body[0]['t']=='h' and title and _key(body[0].get('text'))==_key(title))): body.pop(0)
    while body and (body[-1]['t']=='hr' or _outro(body[-1])): body.pop()
    # A second title is only removed if it introduces front matter, not a substantive chapter.
    front={'аннотация','цели изучения','цели обучения','оглавление','содержание'}
    if len(body)>1 and body[0]['t']=='h' and body[1]['t']=='h' and _key(body[1].get('text')) in front: body.pop(0)
    intro=[]
    while body:
        if body[0]['t']=='hr': body.pop(0);continue
        if body[0]['t']!='h': break
        h=_key(body[0].get('text'))
        if h not in front: break
        end=1
        while end<len(body) and body[end]['t']!='h': end+=1
        section=[b for b in body[1:end] if b['t']!='hr']
        if h in ('оглавление','содержание'):
            # Remove only a generated front-matter index, not an actual section of prose.
            headings={_key(b.get('text','')) for b in body[end:] if b['t']=='h'}
            def index_entry(b):
                text=b.get('text','')
                return b['t'] in ('li','p') and bool(re.search(r'(?i)\bстр\.?\s*\d+|\]\(#',text) or _key(text) in headings)
            if section and all(index_entry(b) for b in section):
                del body[:end];continue
            break
        if h=='аннотация' and section and all(b['t']=='p' for b in section):
            summary=' '.join(b['text'] for b in section)
            # Keep long annotation or unmatched content within the first substantive chapter.
            if len(summary)<=260: meta['subtitle']=summary
            else:
                meta['subtitle']=summary[:256].rsplit(' ',1)[0]+'…'
                intro.extend(section)
            del body[:end];continue
        if h in ('цели изучения','цели обучения') and section and all(b['t']=='li' for b in section):
            meta.setdefault('goals',[[b['text'][:160],''] for b in section[:3]])
            if len(section)>3 or any(len(b['text'])>160 for b in section): intro.extend(section)
            del body[:end];continue
        break
    if intro:
        i=1 if body and body[0]['t']=='h' else 0
        body[i:i]=intro
    # Style named theorem statements as real theorem blocks; retain their names and text.
    for b in body:
        if b['t']=='p':
            b['text']=re.sub(r'^\*\*Теорема\s+([^:*\n]{1,70}):\*\*\s*',r'Теорема (\1): ',b['text'],count=1,flags=re.I)
    # Drop only an obvious standalone divider before a new chapter, avoiding trailing rules.
    body=[b for i,b in enumerate(body) if not (b['t']=='hr' and (i==len(body)-1 or body[i+1]['t']=='h'))]
    return ([{'t':'code','lang':'study','text':json.dumps(meta,ensure_ascii=False,sort_keys=True)}] if meta else [])+body
