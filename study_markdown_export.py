"""Export reader-facing Markdown without losing ordinary code or TeX."""
import json
import re

META_KEYS = {'subtitle','subject','format','outcomes','tags','goals','target_pages','document_kind'}


def is_metadata(obj):
    return isinstance(obj, dict) and len(META_KEYS.intersection(obj)) >= 2 and bool(set(obj) & {'outcomes','goals','target_pages','document_kind','subtitle'})


def load_json(raw):
    try:
        return json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return None


def scalar(value):
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value).replace('\n', ' ').replace('\r', ' ').strip()
    return ''


def code(value):
    value = scalar(value)
    # Markdown code spans preserve expression punctuation.
    fence = '`' * (max([len(s) for s in re.findall(r'`+', value)] or [0]) + 1)
    return fence + ' ' + value + ' ' + fence if '`' in value else fence + value + fence


def plot_description(obj):
    if not isinstance(obj, dict):
        return '**График.** Исходные данные не распознаны; изображение не построено.'
    title = scalar(obj.get('title')) or 'График'
    lines = ['**' + title.replace('*','') + '**']
    domain = obj.get('domain')
    if isinstance(domain, list) and len(domain) == 2:
        lines.append('Диапазон аргумента: ' + code(domain[0]) + ' … ' + code(domain[1]) + '.')
    series = obj.get('series')
    if not isinstance(series, list):
        series = []
    for item in series:
        if not isinstance(item, dict):
            continue
        name = scalar(item.get('name')) or 'Кривая'
        expr = scalar(item.get('expr'))
        if expr:
            try:
                from study_typography import expression_tex
                expr = linear_math(expression_tex(expr)) or expr
            except (ImportError, ValueError, SyntaxError, RecursionError, OverflowError):
                pass
        lines.append('- ' + code(name.replace("'", '′') + (' = ' + expr if expr else '')))
        points = item.get('points', [])
        if isinstance(points, list):
            for pt in points:
                if isinstance(pt, dict) and scalar(pt.get('x')):
                    lines.append('  - ' + (scalar(pt.get('label')) or 'Точка') + ': x = ' + code(pt['x']))
    if isinstance(obj.get('points'), list):
        for pt in obj['points']:
            if isinstance(pt, dict) and scalar(pt.get('x')):
                lines.append('- ' + (scalar(pt.get('label')) or 'Точка') + ': x = ' + code(pt['x']))
    # Preserve tabulated data as a real table, not internal JSON.
    xs = obj.get('x')
    sampled = [s for s in series if isinstance(s, dict) and isinstance(s.get('y'), list)]
    if isinstance(xs, list) and sampled and all(len(s['y']) == len(xs) for s in sampled):
        def cell(v): return scalar(v).replace('|', r'\|')
        lines += ['', '| x | ' + ' | '.join(cell(s.get('name')) or 'y' for s in sampled) + ' |',
                  '| ' + ' | '.join(['---']*(len(sampled)+1)) + ' |']
        lines += ['| ' + ' | '.join([cell(x)] + [cell(s['y'][i]) for s in sampled]) + ' |' for i,x in enumerate(xs)]
    lines.append('')
    lines.append('*Данные для графика; изображение в Markdown не встроено.*')
    return '\n'.join(lines)


def diagram_description(obj):
    if not isinstance(obj, dict):
        return '**Схема.** Исходные данные не распознаны.'
    title = scalar(obj.get('title')) or 'Схема'
    root = scalar(obj.get('root'))
    branches = obj.get('branches', [])
    if not isinstance(branches, list): branches = []
    return '\n'.join(['**' + title.replace('*','') + '**'] + ([root] if root else []) + ['- '+scalar(b) for b in branches if scalar(b)])


FENCE = re.compile(r'^\s*(`{3,}|~{3,})([\w+#.-]*)\s*$')


def clean_markdown(source):
    text = str(source or '').replace('\r\n', '\n').replace('\r', '\n').lstrip('\ufeff').strip('\n')
    lines = text.split('\n')
    out, i, math_end = [], 0, None
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        # Don't interpret fences or copy artefacts inside a displayed equation.
        if math_end:
            out.append(line)
            if math_end in line: math_end = None
            i += 1
            continue
        if stripped.startswith('$$') and '$$' not in stripped[2:]:
            math_end = '$$'
        elif stripped.startswith(r'\[') and r'\]' not in stripped[2:]:
            math_end = r'\]'
        if math_end or stripped.startswith(('$$', r'\[')):
            out.append(line); i += 1; continue
        m = FENCE.match(line)
        if m:
            fence, lang = m.group(1), m.group(2).lower()
            j = i + 1
            closing = re.compile(r'^\s*'+re.escape(fence[0])+r'{'+str(len(fence))+r',}\s*$')
            while j < len(lines) and not closing.match(lines[j]): j += 1
            body = '\n'.join(lines[i+1:j])
            obj = load_json(body) if lang in ('study','plot','diagram','json','') else None
            # Model sometimes appends one stray fence at EOF, e.g. ``` or ``` followed by a stray token.
            if j == len(lines) and i == len(lines)-1:
                i += 1
                continue
            if lang == 'study' or lang in ('json','') and is_metadata(obj):
                pass
            elif lang == 'plot' or (lang in ('json','') and isinstance(obj, dict) and isinstance(obj.get('series'), list) and ('domain' in obj or 'x' in obj)):
                out.extend(['', plot_description(obj), ''])
            elif lang == 'diagram' or (lang in ('json','') and isinstance(obj, dict) and 'root' in obj and isinstance(obj.get('branches'), list)):
                out.extend(['', diagram_description(obj), ''])
            else:
                out.extend(lines[i:min(j+1,len(lines))])
            i = j+1; continue
        # Recognize bare complete JSON only when the object has a reserved schema.
        if stripped.startswith('{'):
            tail = '\n'.join(lines[i:]).lstrip()
            try:
                obj, end = json.JSONDecoder().raw_decode(tail)
                remainder = tail[end:]
                boundary = not remainder or remainder.startswith('\n') or not remainder.split('\n',1)[0].strip()
                if boundary and (is_metadata(obj) or isinstance(obj, dict) and (isinstance(obj.get('series'), list) and ('domain' in obj or 'x' in obj) or 'root' in obj and isinstance(obj.get('branches'), list))):
                    if not is_metadata(obj):
                        out.extend(['', plot_description(obj) if 'series' in obj else diagram_description(obj), ''])
                    i += tail[:end].count('\n')+1
                    continue
            except (ValueError, RecursionError):
                pass
        # Telegram clipboard hard breaks and escaped list numbering, outside code/math.
        if re.fullmatch(r'[─━]{3,}\\?', stripped): line = '---'
        elif stripped == '\\': line = ''
        else:
            if line.endswith('\\') and not line.endswith('\\\\'): line = line[:-1]
            line = re.sub(r'^(\s*\d+)\\\.(?=\s)', r'\1.', line)
        out.append(line)
        i += 1
    # Do not collapse blank lines inside code; only strip outer whitespace.
    return '\n'.join(out).strip() + '\n'


def markdown_bytes(source):
    return reader_markdown(source).encode('utf-8')

# Telegram's Markdown viewer does not typeset TeX. Use unambiguous Unicode
# linear math for supported expressions, and retain unsupported TeX verbatim.
_SYMBOLS = dict(zip('alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa lambda mu nu xi pi rho sigma tau phi varphi chi psi omega Gamma Delta Theta Lambda Pi Sigma Phi Psi Omega'.split(),
                    'α β γ δ ε ε ζ η θ ϑ ι κ λ μ ν ξ π ρ σ τ φ ϕ χ ψ ω Γ Δ Θ Λ Π Σ Φ Ψ Ω'.split()))
_SYMBOLS.update(dict(zip('infty to rightarrow leftarrow leftrightarrow implies Rightarrow Leftrightarrow le leq ge geq ne neq approx equiv pm mp times cdot div in notin cup cap subset subseteq forall exists partial nabla sum prod int oint dots ldots cdots prime ne nier nearrow searrow perp parallel'.split(),
                        '∞ → → ← ↔ ⇒ ⇒ ⇔ ≤ ≤ ≥ ≥ ≠ ≠ ≈ ≡ ± ∓ × · ÷ ∈ ∉ ∪ ∩ ⊂ ⊆ ∀ ∃ ∂ ∇ ∑ ∏ ∫ ∮ … … ⋯ ′ ≠ ≠ ↗ ↘ ⊥ ∥'.split())))
_FUNCS = set('lim sin cos tan tg cot ctg arcsin arccos arctan log ln exp max min sup inf det gcd'.split())
_SUP = dict(zip('0123456789+-−=()nx', '⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾ⁿˣ'))
_SUB = dict(zip('0123456789+-−=()iaenx', '₀₁₂₃₄₅₆₇₈₉₊₋₋₌₍₎ᵢₐₑₙₓ'))
_BLACKBOARD = {'R':'ℝ','N':'ℕ','Z':'ℤ','Q':'ℚ','C':'ℂ'}


def linear_math(tex):
    """Conservative parser; unsupported structures preserve their original TeX."""
    s=tex.strip()
    if not s or len(s)>3500: return None
    pos=0
    def skip():
        nonlocal pos
        while pos<len(s) and s[pos].isspace(): pos+=1
    def atom(depth):
        nonlocal pos
        if depth>40: raise ValueError('nesting')
        skip()
        if pos>=len(s): raise ValueError('missing argument')
        if s[pos]=='{':
            pos+=1; value=sequence(depth+1,True)
            if pos>=len(s) or s[pos]!='}': raise ValueError('unclosed group')
            pos+=1; return value
        if s[pos]=='\\': return command(depth+1)
        c=s[pos];pos+=1
        if c in '}_^&$': raise ValueError('unexpected token')
        return {'-':'−',"'":'′'}.get(c,c)
    def group_text():
        nonlocal pos
        skip()
        if pos>=len(s) or s[pos]!='{': raise ValueError('expected group')
        pos+=1;end=s.find('}',pos)
        if end<0 or any(c in s[pos:end] for c in '{}\\'): raise ValueError('complex text')
        v=s[pos:end];pos=end+1;return v
    def command(depth):
        nonlocal pos
        m=re.match(r'\\([A-Za-z]+|.)',s[pos:])
        if not m: raise ValueError('bad command')
        name=m[1];pos+=len(m[0])
        if name in _SYMBOLS: return _SYMBOLS[name]
        if name in _FUNCS: return name
        if name in ('frac','dfrac','tfrac'):
            a=atom(depth);b=atom(depth)
            if not a or not b: raise ValueError('empty fraction')
            return f'({a})/({b})'
        if name=='sqrt':
            skip();root=''
            if pos<len(s) and s[pos]=='[':
                end=s.find(']',pos)
                if end<0: raise ValueError('root')
                root=s[pos+1:end];pos=end+1
                if root not in ('2','3','4'): raise ValueError('complex root')
            return {'':'√','2':'√','3':'∛','4':'∜'}[root]+'('+atom(depth)+')'
        if name in ('text','mathrm','operatorname'): return group_text()
        if name=='mathbb':
            value=group_text()
            if value not in _BLACKBOARD: raise ValueError('unknown mathbb')
            return _BLACKBOARD[value]
        if name in ('left','right','big','Big','bigl','bigr','displaystyle','textstyle','limits','nolimits'):
            if pos<len(s) and s[pos]=='.':pos+=1
            return ''
        if name in ('quad','qquad',',',';',':',' '):return ' '
        if name=='!':return ''
        if name in ('{','}','%','_','#','|'):return {'|':'‖'}.get(name,name)
        raise ValueError('unsupported TeX: '+name)
    def sequence(depth=0,group=False):
        nonlocal pos
        if depth>40:raise ValueError('nesting')
        out=[]
        while pos<len(s):
            c=s[pos]
            if c=='}':
                if group:break
                raise ValueError('unexpected closing group')
            if c in '^_':
                pos+=1;arg=atom(depth+1);mp=_SUP if c=='^' else _SUB
                if not arg:raise ValueError('empty script')
                out.append(''.join(mp[x] for x in arg) if all(x in mp for x in arg) else c+'('+arg+')')
            elif c=='{':out.append('('+atom(depth+1)+')')
            elif c=='\\':out.append(command(depth+1))
            elif c in '&$':raise ValueError('complex expression')
            else:out.append({'-':'−',"'":'′'}.get(c,c));pos+=1
        return ''.join(out)
    try:
        value=sequence()
        return re.sub(r'[ \t]+',' ',value).strip() or None
    except (ValueError,RecursionError,IndexError):return None


_MATH_TOKEN=re.compile(r'(`+)([^\n]*?)\1|\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([^\n]+?)\\\)|(?<!\\)\$(?![\s$])([^$\n]+?)(?<!\s)\$')


def reader_markdown(source):
    """Only the exported file gets linear Unicode; original PDF source stays TeX."""
    text=clean_markdown(source)
    # Split around real code fences. Never rewrite code examples or escaped literal math.
    lines=text.splitlines(keepends=True);out=[];buf=[];fence=None
    def flush():
        if not buf:return
        def repl(m):
            if m[1]:return m[0]
            tex=next(g for g in m.groups()[2:] if g is not None)
            value=linear_math(tex)
            if value is None:
                # Code blocks keep unsupported multiline TeX intact, including its line breaks.
                if m[3] is not None or m[4] is not None:
                    delim='`'*max(3,1+max([len(x) for x in re.findall(r'`+',tex)] or [0]))
                    return '\n'+delim+'text\n'+tex.strip()+'\n'+delim+'\n'
                return code(tex)
            if m[3] is not None or m[4] is not None:
                return '\n\n'+code(value)+'\n\n'
            return code(value)
        out.append(_MATH_TOKEN.sub(repl,''.join(buf)));buf.clear()
    for line in lines:
        m=FENCE.match(line.rstrip('\n'))
        if fence:
            out.append(line)
            if re.fullmatch(r'\s*'+re.escape(fence[0])+r'{'+str(len(fence))+r',}\s*',line):fence=None
        elif m:
            flush();fence=m[1];out.append(line)
        else:buf.append(line)
    flush()
    return ''.join(out).rstrip()+'\n'
