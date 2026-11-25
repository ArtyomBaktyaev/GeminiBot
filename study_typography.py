"""Safe mathematical figure labels and locally installed print fonts."""
import ast
import base64
from functools import lru_cache
from html import escape
from pathlib import Path
import re

from study_layout import math_slot

FUNCS = {'sin','cos','tan','exp','log','sqrt','abs','asin','acos','atan'}
GREEK = {'pi':r'\pi','e':'e'}
LHS = r"(?:[A-Za-z](?:'{1,3})?\([A-Za-z]\)|[A-Za-z])"
EQUATION = re.compile(r'(?<![\w\\])('+LHS+r')\s*=\s*([A-Za-z0-9.()+*/^−+\-\s]+)')
SIMPLE = re.compile(r"(?<![\w\\])"+LHS+r"(?!\w)")


def expression_tex(expr, implicit=False):
    if not isinstance(expr, str) or len(expr)>180: raise ValueError('Invalid expression')
    expr=expr.strip().replace('−','-').replace('^','**')
    if implicit:
        expr=re.sub(r'(?<=\d)(?=[A-Za-z(])','*',expr)
        expr=re.sub(r'(?<=\))(?=[A-Za-z0-9(])','*',expr)
    tree=ast.parse(expr,mode='eval')
    if len(list(ast.walk(tree)))>80: raise ValueError('Expression too complex')
    def render(n, parent=0):
        if isinstance(n,ast.Constant) and type(n.value) in (int,float):
            s=str(n.value); p=100
            if 'e' in s.lower():
                mant,exp=re.split('[eE]',s);s=mant+r'\times 10^{'+str(int(exp))+'}'
        elif isinstance(n,ast.Name) and n.id in ('x','pi','e','t','y'):
            s=GREEK.get(n.id,n.id);p=100
        elif isinstance(n,ast.UnaryOp) and isinstance(n.op,(ast.UAdd,ast.USub)):
            p=30;s=('+' if isinstance(n.op,ast.UAdd) else '-')+render(n.operand,p)
        elif isinstance(n,ast.BinOp):
            op=n.op
            if isinstance(op,ast.Pow):
                p=40;s=render(n.left,41)+'^{'+render(n.right)+'}'
            elif isinstance(op,ast.Div):
                p=100;s=r'\frac{'+render(n.left)+'}{'+render(n.right)+'}'
            elif isinstance(op,ast.Mult):
                p=20
                # Juxtapose ordinary polynomial factors, retain explicit dot otherwise.
                simple=isinstance(n.left,ast.Constant) and isinstance(n.right,(ast.Name,ast.BinOp,ast.Call))
                dot='' if simple and not isinstance(n.right,ast.Constant) else r'\cdot '
                s=render(n.left,p)+dot+render(n.right,p+1)
            elif isinstance(op,(ast.Add,ast.Sub)):
                p=10;s=render(n.left,p)+('+' if isinstance(op,ast.Add) else '-')+render(n.right,p+1)
            else: raise ValueError('Unsupported operation')
        elif isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id in FUNCS and len(n.args)==1 and not n.keywords:
            v=render(n.args[0]); f=n.func.id;p=100
            if f=='sqrt': s=r'\sqrt{'+v+'}'
            elif f=='abs': s=r'\left|'+v+r'\right|'
            else: s='\\'+({'asin':'arcsin','acos':'arccos','atan':'arctan','log':'ln'}.get(f,f))+r'\left('+v+r'\right)'
        else: raise ValueError('Unsupported expression')
        return r'\left('+s+r'\right)' if p<parent else s
    return render(tree.body)


def slot(tex, fallback=None):
    return math_slot(tex, False, escape(tex if fallback is None else fallback))


def rich_figure_label(value):
    """Keep prose escaped; accept explicit TeX or bounded legacy equations."""
    value=str(value)
    parts=[]
    for i,chunk in enumerate(re.split(r'\$([^$\n]{1,350})\$',value)):
        if i%2:
            parts.append(slot(chunk));continue
        pos=0
        for m in EQUATION.finditer(chunk):
            rhs=m[2].rstrip()
            try: tex=m[1]+'='+expression_tex(rhs,implicit=True)
            except (ValueError,SyntaxError,OverflowError,RecursionError): continue
            end=m.start(2)+len(rhs)
            parts.append(_symbols(chunk[pos:m.start()]));parts.append(slot(tex,chunk[m.start():end]));pos=end
        parts.append(_symbols(chunk[pos:]))
    return ''.join(parts)


def _symbols(text):
    # Only function-style names, not single Latin letters in ordinary prose.
    out=[];pos=0
    for m in re.finditer(r"(?<![\w\\])[A-Za-z](?:'{1,3})?\([A-Za-z]\)(?!\w)",text):
        out.append(escape(text[pos:m.start()]));out.append(slot(m[0]));pos=m.end()
    out.append(escape(text[pos:]));return ''.join(out)


def series_label(name, expr=None):
    name=str(name).strip()
    if expr is not None:
        try: rhs=expression_tex(expr)
        except (ValueError,SyntaxError,OverflowError,RecursionError): return rich_figure_label(name)
        # The plotted expression is authoritative; a model-supplied equation may be stale.
        m=re.search(r'(?<!\w)('+LHS+r')\s*(?:=|$)',name.strip('$'))
        if m:
            prefix=name.strip('$')[:m.start()].strip()
            lhs=m[1]
        else:
            prefix=name;lhs='y'
        return (escape(prefix)+' · ' if prefix else '')+slot(lhs+'='+rhs, lhs+'='+str(expr))
    return rich_figure_label(name)


def point_label(name, a, b):
    from study_polish import coord
    def tex(v): return coord(v).replace('≈',r'\approx ').replace('−','-')
    return rich_figure_label(name)+' '+slot(r'\left('+tex(a)+r';\,'+tex(b)+r'\right)', f'({coord(a)}; {coord(b)})')


@lru_cache(maxsize=1)
def print_font_css():
    root=Path(__file__).with_name('study_print')/'typefaces'
    faces=[('Inter.ttf','Study Inter','100 900','normal'),('Inter-Italic.ttf','Study Inter','100 900','italic'),('Manrope.ttf','Study Manrope','200 800','normal')]
    rules=[]
    for file,family,weight,style in faces:
        p=root/file
        if not p.is_file() or p.stat().st_size>2_000_000: continue
        data=base64.b64encode(p.read_bytes()).decode('ascii')
        rules.append(f'@font-face{{font-family:"{family}";font-style:{style};font-weight:{weight};font-display:block;src:url(data:font/ttf;base64,{data}) format("truetype");}}')
    return '\n'.join(rules)


def ordered_source(title, blocks):
    """Reconstruct a logical-order Markdown companion from parsed blocks."""
    out=['# '+str(title)]
    for b in blocks:
        kind=b.get('t'); text=str(b.get('text',''))
        if kind=='h': out.append('#'*max(1,min(6,int(b.get('lvl',1))))+' '+text)
        elif kind=='math': out.append('$$\n'+text+'\n$$')
        elif kind=='code':
            if b.get('lang')!='study': out.append('```'+str(b.get('lang',''))+'\n'+text+'\n```')
        elif kind=='table' and b.get('rows'):
            rows=b['rows']; out.append('\n'.join(['| '+' | '.join(map(str,rows[0]))+' |','| '+' | '.join(['---']*len(rows[0]))+' |']+['| '+' | '.join(map(str,r))+' |' for r in rows[1:]]))
        elif kind=='li': out.append(('  '*int(b.get('lvl',0)))+(str(b.get('num',1))+'. ' if b.get('ordered') else '- ')+text)
        elif kind=='quote': out.append('> '+text)
        elif kind=='hr': out.append('---')
        elif text: out.append(text)
    return '\n\n'.join(out)+'\n'
