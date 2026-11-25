"""Print the original Gemini for Study styles with Chromium's paged layout."""
from datetime import date
from functools import lru_cache
from html import escape
from io import BytesIO
import json
import keyword
import os
from pathlib import Path
import re
from string import Template
from threading import BoundedSemaphore
import tokenize
import unicodedata
import base64
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from latex2mathml.converter import convert
from playwright.sync_api import sync_playwright
from pypdf import PdfReader

from study_markdown import inline_tokens, parse_markdown
from study_figures import plot, diagram, label

ASSETS = Path(__file__).with_name("study_print")
SLOT = BoundedSemaphore(1)
MATH_TAGS = set("math mrow mi mn mo ms mtext mspace mfrac msqrt mroot mstyle merror mpadded mphantom mfenced menclose msub msup msubsup munder mover munderover mmultiscripts mprescripts none mtable mtr mtd maligngroup malignmark".split())
MATH_ATTRS = set("display mathvariant stretchy fence separator accent accentunder lspace rspace movablelimits form mathsize linethickness columnalign rowalign columnspacing rowspacing notation width height depth minsize maxsize scriptlevel displaystyle".split())


@lru_cache(maxsize=256)
def formula(tex, display=False):
    # Rebuild converter output as presentational MathML only.
    if len(tex) <= 2200 and not re.search(r"\\(?:def|gdef|newcommand|renewcommand|href|url|style|class|html|include|input|require|unicode)\b", tex):
        try:
            root = ET.fromstring(convert(tex, display="block" if display else "inline"))
            for node in root.iter():
                node.tag = node.tag.rsplit("}", 1)[-1]
                if node.tag not in MATH_TAGS:
                    raise ValueError("Unsupported MathML element")
                node.attrib = {k: v for k, v in node.attrib.items() if k in MATH_ATTRS}
                # Greek capitals such as mathematical italic Delta are absent in some Render fonts.
                if node.tag == "mi" and node.text:
                    normalized = unicodedata.normalize("NFKC", node.text)
                    if normalized == "∆":
                        normalized = "Δ"
                    if len(normalized) == 1 and "GREEK CAPITAL" in unicodedata.name(normalized, ""):
                        node.text = normalized
                        node.set("mathvariant", "normal")
                        node.set("class", "math-greek-capital")
            return ET.tostring(root, encoding="unicode", method="html")
        except Exception:
            pass
    return '<code class="math-source">' + escape(tex) + '</code>'


def inline(text):
    chunks = []
    for kind, value, url in inline_tokens(text):
        safe = escape(value)
        if kind == "m":
            safe = formula(value)
        elif kind in ("b", "i", "c"):
            tag = {"b": "strong", "i": "em", "c": "code"}[kind]
            safe = f"<{tag}>{safe}</{tag}>"
        elif kind == "a":
            try:
                parts = urlsplit(url)
                if parts.scheme.lower() in ("http", "https") and parts.hostname:
                    safe = f'<a href="{escape(url, quote=True)}">{safe}</a>'
            except ValueError:
                pass
        chunks.append(safe)
    return "".join(chunks)


def editorial_table(rows, render):
    if not rows:
        return ""
    # Very wide tables read as records; avoid shrinking or clipping columns.
    if max(map(len, rows)) > 8:
        return "".join(editorial_table([["Поле", "Значение"], *zip(rows[0], row)], render)
                       for row in rows[1:]) or editorial_table([["Поле"], *[[x] for x in rows[0]]], render)
    showcase = len(rows) == 2 and len(rows[0]) <= 3 and all(c.startswith("$") and c.endswith("$") for c in rows[1])
    style = "formula-showcase" if showcase else "editorial-table"
    header = "".join(f"<th>{render(c)}</th>" for c in rows[0])
    body = "".join("<tr>" + "".join(f"<td>{render(c)}</td>" for c in row) + "</tr>" for row in rows[1:])
    return f'<table class="{style}"><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>'


def code_block(block):
    raw_lines = block["text"].split("\n")
    markup = [escape(line) for line in raw_lines]
    if block.get("lang") in ("py", "python", "python3"):
        marks = {}
        classes = {tokenize.STRING: "str", tokenize.COMMENT: "cm", tokenize.NUMBER: "num"}
        try:
            for token in tokenize.tokenize(BytesIO(block["text"].encode()).readline):
                row, start = token.start
                end_row, end = token.end
                style = "kw" if keyword.iskeyword(token.string) else classes.get(token.type)
                if style and row == end_row:
                    marks.setdefault(row - 1, []).append((start, end, style))
        except (tokenize.TokenError, SyntaxError):
            pass
        for row, marks_on_line in marks.items():
            line, pieces, pos = raw_lines[row], [], 0
            for start, end, style in marks_on_line:
                pieces.extend([escape(line[pos:start]), f'<span class="{style}">{escape(line[start:end])}</span>'])
                pos = end
            markup[row] = "".join(pieces) + escape(line[pos:])
    lines = "".join(f'<span class="code-line"><span class="ln">{i:02}</span>{line or " "}</span>'
                    for i, line in enumerate(markup, 1))
    return ('<div class="code-container"><div class="code-header">Листинг'
            f'<span class="code-lang">{escape(block.get("lang", ""))}</span></div>'
            f'<pre class="code-body">{lines}</pre></div>')


CALLOUTS = {"определение": "def-block", "теорема": "theorem-block", "доказательство": "proof-block",
            "пример": "example-block", "важно": "callout-important", "запомни": "callout-remember",
            "вывод": "callout-conclusion"}
CALLOUT = re.compile(r"^(?:\*\*)?(" + "|".join(CALLOUTS) + r")(?:\s+(\d+(?:\.\d+)*))?(?:\s*\(([^)\n]{1,100})\))?(?:\*\*)?\s*[:.]\s*(?:\*\*)?\s*(.*)$", re.I | re.S)


def readable_title(title):
    title = str(title).strip()
    # Only normalize filename-like prose; leave mathematical subscripts alone.
    if "$" not in title and "\\(" not in title and "_" in title and " " not in title:
        title = title.replace("_", " ")
    return title or "Учебный материал"


def heading_text(text):
    return re.sub(r"^\s*\d+(?:\.\d+)*(?:[.)]\s+|\s+(?=[А-ЯA-Z]))", "", str(text)).strip()


def title_key(text):
    return re.sub(r"[\W_]+", " ", readable_title(text).casefold()).strip()


def opener(num, title, kicker, anchor=""):
    return (f'<div class="section-opener" id="{anchor}"><span class="section-big-num">{num}</span>'
            f'<div class="section-opener-meta"><div class="section-kicker">{kicker}</div>'
            f'<h2>{title}</h2></div></div>')


def document_html(title, blocks):
    rich = inline

    title = readable_title(title)
    blocks = [dict(b) for b in blocks]
    for b in blocks:
        if b["t"] == "h":
            b["text"] = heading_text(b["text"])
    meta = {}
    if blocks and blocks[0].get("lang") == "study":
        try:
            spec = json.loads(blocks[0]["text"])
            if isinstance(spec, dict):
                meta = {k: v for k, v in spec.items() if isinstance(v, str)}
                goals = spec.get("goals")
                if isinstance(goals, list) and all(
                    isinstance(g, list) and len(g) == 2 and all(isinstance(t, str) for t in g) for g in goals
                ):
                    meta["goals"] = goals[:3]
                blocks.pop(0)
        except ValueError:
            pass
    if blocks and blocks[0]["t"] == "h" and title_key(blocks[0]["text"]) == title_key(title):
        blocks.pop(0)
    # Drop empty subtitle headings and move a leading annotation onto the cover.
    while len(blocks) > 1 and blocks[0]["t"] == "h" and re.match(r"^(?:учебное пособие|учебный конспект)(?: для|$)", blocks[0]["text"], re.I) and blocks[1]["t"] == "h":
        blocks.pop(0)
    if len(blocks) > 1 and blocks[0]["t"] == "h" and blocks[0]["text"].casefold() == "аннотация" and blocks[1]["t"] == "p":
        meta.setdefault("subtitle", blocks[1]["text"])
        if len(blocks[1]["text"]) <= 260:
            del blocks[:2]
    level = min((b["lvl"] for b in blocks if b["t"] == "h"), default=1)
    if not blocks or blocks[0]["t"] != "h":
        blocks.insert(0, {"t": "h", "lvl": level, "text": "Основной материал"})
    entries, sections, figures = [], [], []
    major = minor = equation = figure = 0

    def flush_figures():
        if figures:
            sections.append('<div class="figures">' + "".join(figures) + '</div>')
            figures.clear()

    i = 0
    while i < len(blocks):
        b = blocks[i]
        i += 1
        kind, txt = b["t"], b.get("text", "")
        lang = b.get("lang", "")
        if kind == "code" and lang in ("plot", "diagram"):
            try:
                data = json.loads(txt)
                drawing = (plot if lang == "plot" else diagram)(data)
                caption = label(data["title"])
            except (KeyError, TypeError, ValueError, ArithmeticError, RecursionError):
                flush_figures()
                sections.append(code_block(b))
                continue
            figure += 1
            figures.append(f'<figure class="editorial-figure">{drawing}<figcaption>'
                           f'<b class="fig-label">Рис. {figure}.</b> {caption}</figcaption></figure>')
            if len(figures) == 2:
                flush_figures()
            continue
        flush_figures()
        if kind == "h":
            top = b["lvl"] == level or not major
            if top:
                major += 1
                minor = equation = 0
            else:
                minor += 1
            num = f"{major:02}" if top else f"{major}.{minor}"
            anchor = f"sec-{len(entries)+1}"
            entries.append((num, txt, anchor, top))
            if top:
                sections.append(("" if major == 1 else "</section>") + f'<section class="chapter" style="page:chapter{major}">'
                                + opener(num, rich(txt), "Раздел " + num, anchor))
            else:
                sections.append(f'<h3 id="{anchor}"><span class="sub-num">{num}</span><span>{rich(txt)}</span></h3>')
        elif kind in ("p", "quote"):
            m = CALLOUT.match(txt)
            if not m:
                tag = "blockquote" if kind == "quote" else "p"
                sections.append(f'<{tag}>{rich(txt)}</{tag}>')
                continue
            name, number, subject, body = m.groups()
            style = CALLOUTS[name.lower()]
            tag = "callout-tag" if style.startswith("callout-") else "block-kicker"
            label_text = escape(name + (" " + number if number else "") + (" · " + subject if subject else ""))
            sections.append(f'<div class="{style}"><span class="{tag}">{label_text}</span> {rich(body)}')
            while i < len(blocks) and blocks[i]["t"] == "math":
                equation += 1
                sections.append(display_math(blocks[i]["text"], major, equation))
                i += 1
            if name.lower() == "пример":
                while i < len(blocks) and blocks[i]["t"] == "li" and blocks[i]["ordered"]:
                    step = blocks[i]
                    sections.append(f'<div class="step-item"><span class="step-big-num">{step["num"]}</span>'
                                    f'<span>{rich(step["text"])}</span></div>')
                    i += 1
            if name.lower() == "доказательство":
                # Keep consecutive proof steps and the concluding paragraph together.
                while i < len(blocks):
                    step = blocks[i]
                    if step["t"] in ("p", "quote") and CALLOUT.match(step["text"]):
                        break
                    if step["t"] not in ("p", "quote", "li", "math"):
                        break
                    if step["t"] == "math":
                        equation += 1
                        sections.append(display_math(step["text"], major, equation))
                    elif step["t"] == "li":
                        marker = str(step["num"]) + "." if step["ordered"] else "•"
                        sections.append(f'<p class="proof-step"><b>{marker}</b> {rich(step["text"])}</p>')
                    else:
                        sections.append(f'<p>{rich(step["text"])}</p>')
                    i += 1
                    if step.get("text", "").rstrip().endswith(("□", "■", "∎")):
                        break
            sections.append('</div>')
        elif kind == "math":
            equation += 1
            sections.append(display_math(txt, major, equation))
        elif kind == "code":
            sections.append(code_block(b))
        elif kind == "table":
            sections.append(editorial_table(b["rows"], rich))
        elif kind == "li":
            marker = str(int(b["num"])) + "." if b["ordered"] else "•"
            sections.append(f'<div class="list-row" style="margin-left:{int(b["lvl"])*12}px">'
                            f'<b>{marker}</b> {rich(txt)}</div>')
        elif kind == "hr":
            sections.append("<hr>")
    flush_figures()
    sections.append("</section>")
    chapters = [e for e in entries if e[3]]
    year = str(date.today().year)
    overview = []
    for n, name, anchor, _ in chapters[:3]:
        children = [e[1] for e in entries if not e[3] and e[0].split(".")[0] == str(int(n))]
        detail = rich(" · ".join(children[:2])) if children else ""
        overview.append(f'<li class="workflow-item {"is-featured" if len(overview)==1 else ""}">'
                        f'<span class="workflow-num">{n}</span><div><a class="workflow-title" href="#{anchor}">'
                        f'{rich(name)}</a> <span class="workflow-desc">{detail}</span></div></li>')
    if meta.get("goals"):
        overview = [f'<li class="workflow-item {"is-featured" if i == 2 else ""}"><span class="workflow-num">{i:02}</span>'
                    f'<div><span class="workflow-title">{rich(goal)}</span> <span class="workflow-desc">— {rich(detail)}</span></div></li>'
                    for i, (goal, detail) in enumerate(meta["goals"], 1)]
    summary = meta.get("subtitle") or next((b["text"] for b in blocks if b["t"] == "p"), "")
    if len(summary) > 260:
        summary = summary[:256].rsplit(" ", 1)[0] + "…"
    fields = [("Дисциплина", meta.get("subject", title)), ("Формат", meta.get("format", "Учебный конспект")),
              ("Содержание", meta.get("outcomes", f"Разделов — {len(chapters)}, иллюстраций — {figure}."))]
    cover = Template((ASSETS / "cover.html").read_text(encoding="utf8")).substitute(
        year=year, title=rich(title), subtitle=rich(summary), overview="".join(overview),
        tags=rich(meta.get("tags", " · ".join(n for n, yes in (("Теория", True), ("Формулы", any(b["t"]=="math" for b in blocks)), ("Практикум", bool(figure))) if yes))),
        metadata="".join(f'<tr><td class="col-key">{k}</td><td>{rich(v)}</td></tr>' for k,v in fields))
    toc = []
    for num, name, anchor, top in entries:
        toc.append(f'<li class="toc-item {"toc-major" if top else "toc-sub"}"><a class="toc-link" href="#{anchor}">'
                   f'<span class="toc-num">{num}</span><span>{rich(name)}</span></a>'
                   f'<span class="toc-page" data-target="{anchor}">00</span></li>')
    guide = editorial_table([["Элемент оформления", "Назначение в учебном материале"],
                             ["Определение", "Вводит обозначения и задаёт точный смысл нового понятия."],
                             ["Теорема и доказательство", "Формулируют условия и объясняют, как из них получить утверждение."],
                             ["Пример и шаги решения", "Показывают применение метода к конкретной задаче."],
                             ["Вывод", "Собирает результаты, которые пригодятся в следующей задаче."]], escape)
    nav = (opener("00", "Содержание модуля", "Структура учебного пособия", "contents")
           + '<p>Выберите раздел или подраздел, чтобы перейти к нему. Справа указан номер страницы в этом издании.</p>'
           + '<div><div class="toc-title">Разделы и подразделы</div><ul class="toc-list">' + "".join(toc) + '</ul></div>'
           + '<h3><span class="sub-num">§</span>Условные обозначения и навигация по блокам</h3>' + guide)
    css = "\n".join((ASSETS / f).read_text(encoding="utf8") for f in ("template.css", "print.css"))
    font = ASSETS.parent / "fonts" / "DejaVuSans.ttf"
    if font.is_file() and any("math-greek-capital" in part for part in sections):
        encoded = base64.b64encode(font.read_bytes()).decode("ascii")
        css += "\n@font-face{font-family:StudyGreek;src:url(data:font/ttf;base64," + encoded + ") format('truetype');} math .math-greek-capital{font-family:StudyGreek,sans-serif!important;}"
    for num, name, _, _ in chapters:
        # Encode all characters of the heading, including quotes and '<', inside the CSS string.
        value = "".join("\\" + format(ord(ch), "x") + " " for ch in name[:48].upper())
        css += f'\n@page chapter{int(num)} {{ @top-right {{ content: "{num} · {value}"; }} }}'
    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>{escape(title)}</title>'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; font-src data:">'
            f'<style>{css}</style></head><body><section class="cover">{cover}</section>'
            f'<section class="navigation">{nav}</section>{"".join(sections)}</body></html>')


def display_math(tex, section, number):
    return (f'<div class="math-display"><span class="math-expr">{formula(tex, True)}</span>'
            f'<span class="math-num">({section}.{number})</span></div>')


def render_pdf(title, blocks):
    html = document_html(title, blocks)
    # One browser at a time on small Render instances, and no profile shared between users.
    import playwright
    if (Path(playwright.__file__).parent / "driver/package/.local-browsers").is_dir():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
    with SLOT, sync_playwright() as engine:
        executable = os.environ.get("STUDY_CHROMIUM") or None
        try:
            browser = engine.chromium.launch(executable_path=executable)
        except Exception as exc:
            if executable or not os.environ.get("RENDER") or "Executable doesn't exist" not in str(exc):
                raise
            import subprocess
            import sys
            subprocess.run([sys.executable, "-m", "playwright", "install", "--only-shell", "chromium"],
                           check=True, timeout=180)
            browser = engine.chromium.launch()
        try:
            page = browser.new_page(offline=True, service_workers="block")
            page.route("**/*", lambda route: route.abort())
            page.emulate_media(media="print")
            # A document URL lets Chromium classify fragment links as PDF
            # destinations. The route is fulfilled from memory, even offline.
            address = "https://study.invalid/document"
            page.route(address, lambda route: route.fulfill(content_type="text/html; charset=utf-8", body=html))
            page.goto(address, wait_until="load", timeout=15000)
            page.evaluate("() => document.fonts.ready")
            # MathML is atomic in Chromium. Fit long expressions to the actual
            # column (including table cells) before measuring destinations.
            page.evaluate("""() => {
                for (const expr of document.querySelectorAll('math')) {
                    const box = expr.closest('.math-expr, td, th, p, li, section');
                    if (!box) continue;
                    const style = getComputedStyle(box);
                    const width = box.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
                    const actual = Math.max(expr.scrollWidth, expr.getBoundingClientRect().width);
                    if (width > 0 && actual > width) expr.style.zoom = width / actual;
                }
            }""")
            def print_pages():
                return page.pdf(prefer_css_page_size=True, print_background=True, outline=True)
            # Page numbers come from the actual link destinations in the first PDF.
            draft = PdfReader(BytesIO(print_pages()))
            pages = {k.lstrip("/"): str(draft.get_destination_page_number(v) + 1).zfill(2)
                     for k, v in draft.named_destinations.items()}
            page.locator(".toc-page").evaluate_all("""(nodes, pages) => {
                for (const n of nodes) {
                    const number = pages[n.dataset.target];
                    if (!number) throw Error("Не найдена страница раздела " + n.dataset.target);
                    n.textContent = number;
                }
            }""", pages)
            return print_pages()
        finally:
            browser.close()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Напечатать учебное пособие из Markdown.")
    parser.add_argument("markdown", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--title", required=True)
    args = parser.parse_args()
    args.output.write_bytes(render_pdf(args.title, parse_markdown(args.markdown.read_text(encoding="utf8"))))
