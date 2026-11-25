"""Bounded characteristic-point calculation and attractive axis ticks."""
import math
from study_plot_expr import compile_expression


def nice_ticks(lo, hi, count=6):
    raw=(hi-lo)/count
    if raw<=0 or not math.isfinite(raw):
        return []
    unit=10**math.floor(math.log10(raw))
    step=unit*next(n for n in (1,2,2.5,5,10) if n*unit>=raw)
    return [0.0 if abs(i*step)<step*1e-8 else i*step for i in range(math.ceil(lo/step-1e-10),math.floor(hi/step+1e-10)+1)]


def markers(original, xs, ys):
    if len(xs)<3 or max(ys)==min(ys): return []
    explicit=original.get('points')
    fn=None
    try:
        fn=compile_expression(original['series'][0]['expr'])
    except (KeyError, TypeError, ValueError, SyntaxError, IndexError):
        pass
    if not fn: return []  # no invented points for tabular data
    result=[]
    lo,hi=min(xs),max(xs)
    def add(a,name):
        if not lo<=a<=hi or len(result)>=8: return
        try: b=fn(a)
        except (ValueError,ArithmeticError): return
        if any(abs(a-x)<(hi-lo)/1000 for x,_,_ in result): return
        result.append((a,b,name))
    if isinstance(explicit,list):
        for p in explicit[:8]:
            if not isinstance(p,dict) or type(p.get('x')) not in (int,float) or not math.isfinite(p['x']): continue
            name=p.get('label','Точка')
            if not isinstance(name,str) or len(name)>55: name='Точка'
            add(p['x'],name)
        if result: return sorted(result)
    for i in range(len(xs)-1):
        a,b=xs[i],xs[i+1]; fa,fb=ys[i],ys[i+1]
        if fa==0: add(a,'Нуль')
        if fa*fb<0:
            for _ in range(30):
                m=(a+b)/2; fm=fn(m)
                if fa*fm<=0: b=m
                else: a=m; fa=fm
            add((a+b)/2,'Нуль')
    if ys[-1]==0: add(xs[-1],'Нуль')
    for i in range(1,len(xs)-1):
        peak=ys[i]>ys[i-1] and ys[i]>=ys[i+1]
        valley=ys[i]<ys[i-1] and ys[i]<=ys[i+1]
        if not (peak or valley): continue
        a,b=xs[i-1],xs[i+1]
        for _ in range(27):
            l=a+(b-a)/3; r=b-(b-a)/3
            if (fn(l)<fn(r))==peak: a=l
            else: b=r
        add((a+b)/2,'Максимум' if peak else 'Минимум')
    return sorted(result)
