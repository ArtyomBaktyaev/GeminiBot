"""Add invisible, searchable LaTeX source to the final PDF only.
Temporary link annotations locate each SVG without relying on screen coordinates.
"""
from io import BytesIO
import re
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (NameObject, DictionaryObject, ArrayObject, DecodedStreamObject,
                          FloatObject, NumberObject, TextStringObject)

PREFIX = 'https://study.invalid/math-source/'


def add_formula_text(data, formulas, prefix=PREFIX, reading_order=None):
    if not formulas and not reading_order:
        return data
    reader = PdfReader(BytesIO(data))
    writer = PdfWriter(clone_from=reader)
    font = writer._add_object(DictionaryObject({NameObject('/Type'):NameObject('/Font'),
          NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica'),
          NameObject('/Encoding'):NameObject('/WinAnsiEncoding')}))
    for page in writer.pages:
        annots = page.get('/Annots')
        if not annots:
            continue
        kept, commands = ArrayObject(), []
        for ref in annots:
            annot = ref.get_object()
            action = annot.get('/A') or {}
            uri = str(action.get('/URI',''))
            if not uri.startswith(prefix):
                kept.append(ref)
                continue
            key = uri[len(prefix):]
            tex = formulas.get(key)
            if not tex:
                continue
            x0,y0,x1,y1 = map(float,annot['/Rect'])
            # Text rendering mode 3: nonpainting text, no visual alteration.
            clean = re.sub(r'\s+', ' ', tex).strip()[:3500]
            if not clean or x1<=x0 or y1<=y0:
                continue
            raw = clean.encode('cp1252', errors='replace').hex()
            actual = ('\ufeff'+clean).encode('utf-16-be').hex()
            size = min(10.0,max(2.0,y1-y0))
            scale = min(100.0,max(0.1,(x1-x0)*100/(size*max(1,len(clean))*.65)))
            commands.append(f'/Span << /ActualText <{actual}> >> BDC BT /StudySource {size:.4f} Tf 3 Tr {scale:.4f} Tz 1 0 0 1 {x0:.4f} {y0:.4f} Tm <{raw}> Tj ET EMC')
        if len(kept) != len(annots):
            if kept: page[NameObject('/Annots')]=kept
            else: page.pop('/Annots',None)
        if not commands:
            continue
        resources=page['/Resources'].get_object()
        fonts=resources.get('/Font')
        if fonts is None:
            fonts=DictionaryObject();resources[NameObject('/Font')]=fonts
        fonts.get_object()[NameObject('/StudySource')]=font
        stream=DecodedStreamObject();stream.set_data(('q\n'+'\n'.join(commands)+'\nQ\n').encode('ascii'))
        old=page.get('/Contents')
        if old is None: refs=ArrayObject()
        elif isinstance(old.get_object(),ArrayObject): refs=ArrayObject(old.get_object())
        else: refs=ArrayObject([page.raw_get('/Contents')])
        refs.append(writer._add_object(stream));page[NameObject('/Contents')]=refs
    # Preserve exact full source, including Unicode, for readers without ActualText support.
    writer.add_attachment('formula-source.txt', ('\n\n'.join(formulas.values())).encode('utf-8'))
    if reading_order:
        writer.add_attachment('document-source.md', reading_order.encode('utf-8'))
    output=BytesIO();writer.write(output);return output.getvalue()


def mark_formula_locations(page):
    from uuid import uuid4
    prefix = PREFIX+uuid4().hex+'/'
    formulas = page.evaluate(r'''prefix => {
      const formulas={};
      let i=0;
      for(const el of document.querySelectorAll('.study-math')) {
        if(!el.querySelector('mjx-container svg')) continue;
        const tex=el.dataset.tex||'';
        if(!tex) continue;
        const id=String(i++);
        formulas[id]=tex;
        if(el.closest('a')) continue;
        el.style.position='relative';
        const a=document.createElement('a');
        a.className='study-formula-location';
        a.href=prefix+id;
        a.setAttribute('aria-hidden','true');
        a.style.cssText='position:absolute;inset:0;display:block;pointer-events:none;';
        el.append(a);
      }
      return formulas;
    }''', prefix)
    return prefix, formulas
