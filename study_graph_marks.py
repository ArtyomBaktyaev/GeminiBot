"""Bounded characteristic-point calculation with refined roots and extrema."""
import math
from study_plot_expr import compile_expression
from study_polish import clean_point_label


def nice_ticks(lo, hi, count=6):
    raw=(hi-lo)/count
    if raw<=0 or not math.isfinite(raw):
        return []
    unit=10**math.floor(math.log10(raw))
    step=unit*next(n for n in (1,2,2.5,5,10) if n*unit>=raw)
    return [0.0 if abs(i*step)<step*1e-8 else i*step for i in range(math.ceil(lo/step-1e-10),math.floor(hi/step+1e-10)+1)]


def markers(original, xs, ys):
    if len(xs)<3 or max(ys)==min(ys): return []
    try:
        fn=compile_expression(original['series'][0]['expr'])
    except (KeyError, TypeError, ValueError, SyntaxError, IndexError):
        return []
    lo,hi=min(xs),max(xs)
    result=[]
    span=hi-lo
    yscale=max(1, max(abs(y) for y in ys))
    root_tol=1e-9*yscale
    roots=[]; extrema=[]
    def snap(a):
        if abs(a)<1e-7 and lo<=0<=hi: return 0.0
        if abs(a-round(a))<1e-7 and lo<=round(a)<=hi: return float(round(a))
        return a
    def record(a,name,target):
        try:
            a=snap(a); b=fn(a)
        except (ValueError,ArithmeticError): return
        if name=='Нуль':
            if abs(b)>root_tol: return  # Reject sign jumps at poles.
            b=0.0
        if any(abs(a-x)<span/10000 for x,_,_ in target): return
        target.append((a,b,name))
    for i in range(len(xs)-1):
        a,b=xs[i],xs[i+1]; fa,fb=ys[i],ys[i+1]
        if fa==0: record(a,'Нуль',roots)
        if fa*fb<0:
            try:
                for _ in range(42):
                    m=(a+b)/2; fm=fn(m)
                    if fa*fm<=0: b=m
                    else: a=m; fa=fm
                record((a+b)/2,'Нуль',roots)
            except (ValueError, ArithmeticError): pass
    if ys[-1]==0: record(xs[-1],'Нуль',roots)
    for i in range(1,len(xs)-1):
        peak=ys[i]>ys[i-1] and ys[i]>=ys[i+1]
        valley=ys[i]<ys[i-1] and ys[i]<=ys[i+1]
        if not (peak or valley): continue
        a,b=xs[i-1],xs[i+1]
        try:
            for _ in range(42):
                l=a+(b-a)/3; r=b-(b-a)/3
                if (fn(l)<fn(r))==peak: a=l
                else: b=r
            record((a+b)/2,'Максимум' if peak else 'Минимум',extrema)
            if valley and abs(fn((a+b)/2))<=root_tol: record((a+b)/2,'Нуль',roots)
            if peak and abs(fn((a+b)/2))<=root_tol: record((a+b)/2,'Нуль',roots)
        except (ValueError,ArithmeticError): pass
    def add(a,b,name):
        if lo<=a<=hi and len(result)<8 and not any(abs(a-x)<span/10000 for x,_,_ in result):
            result.append((a,b,name))
    explicit=original.get('points')
    if isinstance(explicit,list):
        for p in explicit[:8]:
            if not isinstance(p,dict) or type(p.get('x')) not in (int,float) or not math.isfinite(p['x']) or not lo<=p['x']<=hi: continue
            name=p.get('label','Точка')
            if not isinstance(name,str) or len(name)>55: name='Точка'
            name=clean_point_label(name); a=float(p['x'])
            key=name.casefold()
            candidates=roots if 'нуль' in key or 'корень' in key else [v for v in extrema if v[2].casefold()==key]
            if candidates:
                x,y,_=min(candidates,key=lambda v:abs(v[0]-a))
                if abs(x-a)<=max(span/80,1e-6):
                    add(x,y,name);continue
            try: b=fn(a)
            except (ValueError,ArithmeticError): continue
            if candidates or 'нуль' in key or 'корень' in key or key in ('максимум','минимум'):
                # Do not claim a point is a root/extremum if refinement cannot verify it.
                name='Точка'
            add(a,b,name)
        if result: return sorted(result)
    for a,b,name in sorted(roots+extrema): add(a,b,name)
    return sorted(result)
