"""Data-only vector graphics for printed study notes."""
from html import escape
import math


def label(value, limit=130):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("Недопустимая подпись рисунка")
    return escape(value)


def numbers(values):
    if not isinstance(values, list) or not 2 <= len(values) <= 600:
        raise ValueError("Нужно от 2 до 600 точек")
    if not all(type(v) in (int, float) and abs(v) <= 1e12 and math.isfinite(v) for v in values):
        raise ValueError("Ожидались конечные координаты")
    return values


def plot(data):
    xs = numbers(data["x"])
    series = data["series"]
    if not isinstance(series, list) or not 1 <= len(series) <= 5:
        raise ValueError("Ожидалось от одной до пяти кривых")
    curves = [(label(s["name"], 40), numbers(s["y"])) for s in series]
    if any(len(ys) != len(xs) for _, ys in curves):
        raise ValueError("Число координат x и y различается")
    lo, hi = min(xs), max(xs)
    if lo == hi:
        raise ValueError("Диапазон x пуст")
    low = min(min(ys) for _, ys in curves)
    high = max(max(ys) for _, ys in curves)
    if low == high:
        low, high = low - 1, high + 1
    span = high - low
    step = 10 ** math.floor(math.log10(span / 5))
    step *= next(k for k in (1, 2, 5, 10) if step * k >= span / 5)
    low, high = math.floor(low / step) * step, math.ceil(high / step) * step
    def x(v):
        return 38 + (v - lo) / (hi - lo) * 254
    def y(v):
        return 122 - (v - low) / (high - low) * 107
    parts = ['<svg viewBox="0 0 310 148" role="img" aria-label="График по численным данным">']
    for i in range(round((high - low) / step) + 1):
        val = low + step * i
        pos = y(val)
        parts.append(f'<path d="M38 {pos:.2f}h254" stroke="#CFDCD5" stroke-dasharray="2 2"/>'
                     f'<text x="34" y="{pos+3:.2f}" text-anchor="end">{val:.2g}</text>')
    for i in range(4):
        val = lo + (hi - lo) * i / 3
        parts.append(f'<text x="{x(val):.2f}" y="139" text-anchor="middle">{val:.3g}</text>')
    parts.append('<path d="M38 12V122H292" fill="none" stroke="#68756F"/>')
    colors = ("#18221F", "#245E51", "#52997E", "#68756F", "#327E8A")
    dashes = ("none", "6 3", "2 2", "8 2 2 2", "3 5")
    keys = []
    for i, (name, ys) in enumerate(curves):
        points = " ".join(f"{x(a):.2f},{y(b):.2f}" for a, b in zip(xs, ys))
        parts.append(f'<polyline points="{points}" fill="none" stroke="{colors[i]}" '
                     f'stroke-dasharray="{dashes[i]}" stroke-width="1.8"/>')
        keys.append(f'<span><svg viewBox="0 0 20 5"><path d="M0 2h20" stroke="{colors[i]}" '
                    f'stroke-width="2" stroke-dasharray="{dashes[i]}"/></svg>{name}</span>')
    parts.append('</svg><div class="legend">' + "".join(keys) + '</div>')
    return "".join(parts)


def diagram(data):
    branches = data["branches"]
    if not isinstance(branches, list) or not 1 <= len(branches) <= 8:
        raise ValueError("Ожидалось от одной до восьми ветвей")
    items = "".join(f'<li>{label(branch)}</li>' for branch in branches)
    return f'<div class="diagram-root">{label(data["root"])}</div><ul class="branches">{items}</ul>'
