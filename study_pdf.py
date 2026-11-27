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

from contextvars import ContextVar
_DEFER_MATHML = ContextVar("study_defer_mathml", default=False)
from pypdf import PdfReader

from study_markdown import inline_tokens, parse_markdown
from study_figures import plot, diagram, label
from study_layout import math_slot, mathjax_prepare, page_target, print_fitted
from study_polish import document_kind, clean_summary
from study_clean import normalize_blocks
from study_typography import rich_figure_label, print_font_css, ordered_source, print_font_resources
from study_math_text import mark_formula_locations, add_formula_text

ASSETS = Path(__file__).with_name("study_print")
SLOT = BoundedSemaphore(1)
MATH_TAGS = set("math mrow mi mn mo ms mtext mspace mfrac msqrt mroot mstyle merror mpadded mphantom mfenced menclose msub msup msubsup munder mover munderover mmultiscripts mprescripts none mtable mtr mtd maligngroup malignmark".split())
MATH_ATTRS = set("display mathvariant stretchy fence separator accent accentunder lspace rspace movablelimits form mathsize linethickness columnalign rowalign columnspacing rowspacing notation width height depth minsize maxsize scriptlevel displaystyle".split())


@lru_cache(maxsize=256)
def _legacy_formula(tex, display=False):
    # Rebuild converter output as presentational MathML only.
    if len(tex) <= 2200 and not re.search(r"\\(?:def|gdef|newcommand|renewcommand|href|url|style|class|html|include|input|require|unicode)\b", tex):
        try:
            from latex2mathml.converter import convert
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


def formula(tex, display=False):
    if _DEFER_MATHML.get():
        return math_slot(tex, display, escape(tex)).replace('class="study-math"','class="study-math" data-deferred-fallback="1"',1)
    return math_slot(tex, display, _legacy_formula(tex, display))


def inline(text):
    chunks = []
    for kind, value, url in inline_tokens(text):
        safe = escape(re.sub(r"(?<!\\)\*{2,}", "", value) if kind == "t" else value)
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
    # Short, wide sign/comparison tables become readable vertical records.
    if 6 <= len(rows[0]) <= 8 and 2 <= len(rows) <= 6:
        rows = [list(row) for row in zip(*rows)]
    showcase = len(rows) == 2 and len(rows[0]) <= 3 and all(c.startswith("$") and c.endswith("$") for c in rows[1])
    style = "formula-showcase" if showcase else "editorial-table"
    header = "".join(f"<th>{render(c)}</th>" for c in rows[0])
    body = "".join("<tr>" + "".join(f"<td>{render(c)}</td>" for c in row) + "</tr>" for row in rows[1:])
    widths = []
    if not showcase and len(rows[0]) > 1:
        n = len(rows[0])
        weights = [max(7, min(48, max(len(str(row[i])) for row in rows if i < len(row)))) ** .65 for i in range(n)]
        floor = min(12, 65/n)
        widths = [floor + (100-n*floor)*w/sum(weights) for w in weights]
    cols = '<colgroup>' + ''.join(f'<col style="width:{w:.2f}%">' for w in widths) + '</colgroup>' if widths else ''
    return f'<table class="{style}">{cols}<thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>'


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


def document_html(title, blocks, *, external_fonts=False):
    rich = inline

    title = readable_title(title)
    blocks = normalize_blocks(blocks,title)
    target = page_target(blocks)
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
    kind_profile = meta.get("document_kind")
    if kind_profile not in ("book", "lab", "cheat", "solution", "note"):
        # Tables keep cells in rows. Count them when choosing the compact layout.
        profile_text = "\n".join(
            "\n".join(" | ".join(row) for row in b["rows"]) if b["t"] == "table"
            else b.get("text", "") for b in blocks
        )
        kind_profile = document_kind("", title, profile_text)
    short = (target[1] <= 2) if target else kind_profile in ("cheat", "solution", "note")
    profile_label = {"book":"Учебное пособие", "lab":"Лабораторная работа", "cheat":"Шпаргалка", "solution":"Решение задачи", "note":"Учебная заметка"}[kind_profile]
    if short:
        # One main section in compact documents; user headings become subsections.
        for b in blocks:
            if b["t"] == "h": b["lvl"] = 2
        blocks.insert(0, {"t":"h", "lvl":1, "text":"Материал" if kind_profile == "book" else profile_label})
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
                label(data["title"])  # validate before rendering
                caption = rich_figure_label(data["title"])
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
                sections.append(("" if major == 1 else "</section>") + f'<section class="chapter" >'
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
            if name.lower() == "теорема":
                while i < len(blocks):
                    item = blocks[i]
                    if item["t"] == "li":
                        marker = str(item["num"]) + "." if item["ordered"] else "•"
                        sections.append(f'<p class="theorem-step"><b>{marker}</b> {rich(item["text"])}</p>')
                    elif item["t"] == "math":
                        equation += 1
                        sections.append(display_math(item["text"], major, equation))
                    elif item["t"] == "p" and re.match(r"^(?:Тогда|Следовательно|Существует|Иными словами)", item["text"], re.I):
                        sections.append(f'<p>{rich(item["text"])}</p>')
                    else:
                        break
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
    summary = clean_summary(summary, title, [e[1] for e in entries if e[3]])
    if len(summary) > 260:
        summary = summary[:256].rsplit(" ", 1)[0] + "…"
    fields = [("Дисциплина", meta.get("subject", title)), ("Формат", meta.get("format", profile_label)),
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
    css = print_font_css(external_fonts) + "\n" + "\n".join((ASSETS / f).read_text(encoding="utf8") for f in ("template.css", "print.css"))
    font = ASSETS.parent / "fonts" / "DejaVuSans.ttf"
    if font.is_file() and (external_fonts or any("math-greek-capital" in part for part in sections)):
        source = "/fonts/DejaVuSans.ttf" if external_fonts else "data:font/ttf;base64," + base64.b64encode(font.read_bytes()).decode("ascii")
        css += "\n@font-face{font-family:StudyGreek;src:url(" + source + ") format('truetype');} math .math-greek-capital{font-family:StudyGreek,sans-serif!important;}"
    notice = '<p class="volume-note" hidden></p>'
    cover_section = f'<section class="cover">{cover}{notice}</section>'
    nav_section = f'<section class="navigation">{nav}</section>'
    if short:
        cover_section = f'<header class="short-title"><div class="document-kind">{escape(profile_label)}</div><h1>{rich(title)}</h1>{notice}</header>'
        nav_section = ''
    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>{escape(title)}</title>'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'self\'; style-src \'unsafe-inline\'; font-src data: \'self\'">'
            f'<style>{css}</style></head><body>{cover_section}{nav_section}{"".join(sections)}</body></html>')


def display_math(tex, section, number):
    return (f'<div class="math-display"><span class="math-expr">{formula(tex, True)}</span>'
            f'<span class="math-num">({section}.{number})</span></div>')


def render_pdf(title, blocks):
    import time, logging
    log=logging.getLogger('study.pdf')
    from study_runtime_diag import memory_status
    from study_chromium import chromium_args, memory_snapshot
    from study_direct_chromium import direct_page
    from contextlib import contextmanager
    import gc, sys
    start=time.monotonic()
    title=readable_title(title)
    blocks=normalize_blocks(blocks,title)
    setting = os.environ.get("STUDY_PDF_TRANSPORT", "auto").lower()
    if setting not in ("auto", "direct", "playwright"):
        raise ValueError("Invalid STUDY_PDF_TRANSPORT")
    snap = memory_snapshot()
    use_direct = sys.platform.startswith("linux") and (setting == "direct" or (setting == "auto" and snap is not None and snap.constrained))
    if setting == "direct" and not use_direct:
        raise RuntimeError("Direct Chromium transport is Linux-only")
    token = _DEFER_MATHML.set(use_direct)
    try:
        html = document_html(title, blocks, external_fonts=use_direct)
    finally:
        _DEFER_MATHML.reset(token)
    if use_direct:
        _legacy_formula.cache_clear()
        print_font_css.cache_clear()
        gc.collect()
    log.info('PDF HTML prepared: %.1fs; %d blocks',time.monotonic()-start,len(blocks))
    # One browser at a time on small Render instances, and no profile shared between users.
    import playwright
    if (Path(playwright.__file__).parent / "driver/package/.local-browsers").is_dir():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
    @contextmanager
    def original_page():
      from playwright.sync_api import sync_playwright
      with sync_playwright() as engine:
        executable = os.environ.get("STUDY_CHROMIUM") or None
        log.info("PDF launching Chromium; %s", memory_status())
        try:
            browser = engine.chromium.launch(executable_path=executable, timeout=25000, args=chromium_args())
        except Exception as exc:
            if executable or not os.environ.get("RENDER") or "Executable doesn't exist" not in str(exc):
                raise
            import subprocess
            import sys
            subprocess.run([sys.executable, "-m", "playwright", "install", "--only-shell", "chromium"],
                           check=True, timeout=180)
            browser = engine.chromium.launch(timeout=25000, args=chromium_args())
        log.info("PDF Chromium ready; %s", memory_status())
        try:
            yield browser.new_page(offline=True, service_workers="block")
        finally:
            browser.close()
    LOG_MODE = "direct CDP (no Node driver)" if use_direct else "Playwright"
    log.info("PDF transport=%s; %s", LOG_MODE, memory_status())
    with SLOT, (direct_page(args=chromium_args()) if use_direct else original_page()) as page:
        page.route("**/*", lambda route: route.abort())
        if use_direct:
            for url, resource in print_font_resources().items():
                page.route("https://study.invalid" + url, lambda route, resource=resource: route.fulfill(content_type="font/ttf", path=resource))
        page.emulate_media(media="print")
        # A document URL lets Chromium classify fragment links as PDF
        # destinations. The route is fulfilled from memory, even offline.
        address = "https://study.invalid/document"
        page.route(address, lambda route: route.fulfill(content_type="text/html; charset=utf-8", body=html))
        page.goto(address, wait_until="load", timeout=15000)
        page.unroute(address)
        del html
        page.evaluate("() => document.fonts.ready.then(() => true)")
        if use_direct:
            gc.collect()
        log.info("PDF fonts ready; %s", memory_status())
        t_math=time.monotonic()
        errors = mathjax_prepare(page, address, compact=use_direct)
        if use_direct and errors:
            # The original MathML fallback is restored only where MathJax failed.
            # Successful MathJax output is not changed.
            missing = page.locator('.study-math[data-deferred-fallback="1"]:not(:has(mjx-container svg))').evaluate_all(
                "nodes => nodes.map(n=>[n.dataset.tex,n.dataset.display==='1'])")
            fallbacks = [_legacy_formula(tex, display) for tex,display in missing]
            page.locator('.study-math[data-deferred-fallback="1"]:not(:has(mjx-container svg))').evaluate_all(
                "(nodes, htmls) => nodes.forEach((n,i)=>{n.innerHTML=htmls[i]})", fallbacks)
            page.evaluate("() => document.fonts.ready.then(() => true)")
        log.info("PDF MathJax: %.1fs, %d fallback formulas",time.monotonic()-t_math,errors)
        if errors:
            # Preserve formulas we cannot render instead of quietly deleting them.
            page.evaluate("""(n)=>{const note=document.querySelector('.volume-note');if(note){note.hidden=false;note.textContent=`${n} формул не удалось обработать MathJax. Для них сохранён исходник или резервная вёрстка.`;}}""", errors)
        prefix, formulas = mark_formula_locations(page)
        data = print_fitted(page, page_target(blocks), PdfReader)
    # Chromium and Playwright are gone before pypdf clones the output.
    # This lowers peak memory without changing the PDF appearance.
    log.info("PDF browser closed; %s", memory_status())
    t_text=time.monotonic()
    try:
        result=add_formula_text(data, formulas, prefix, ordered_source(title, blocks))
    except Exception:
        log.exception("PDF searchable layer failed; removing temporary markers")
        from study_math_text import strip_formula_links
        result=strip_formula_links(data, prefix)
    log.info("PDF source finalization: %.1fs; total %.1fs; %s",time.monotonic()-t_text,time.monotonic()-start,memory_status())
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Напечатать учебное пособие из Markdown.")
    parser.add_argument("markdown", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--title", required=True)
    args = parser.parse_args()
    args.output.write_bytes(render_pdf(args.title, parse_markdown(args.markdown.read_text(encoding="utf8"))))
