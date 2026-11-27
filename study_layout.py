"""Safe, local MathJax and bounded PDF layout adjustments."""
import json
import os
from pathlib import Path
import re
from html import escape
from io import BytesIO

PAGE_RE = re.compile(r"(?<!\d)(\d{1,2})(?:\s*[-–—]\s*(\d{1,2}))?\s*(?:страниц\w*|стр\.(?!\w)|pages?\b)", re.I)


def requested_page_range(text):
    # Do not mistake the numbered page-by-page outline for the total volume.
    matches = list(PAGE_RE.finditer(text or ""))
    if not matches:
        return None
    m = matches[0]
    a, b = int(m[1]), int(m[2] or m[1])
    if not 1 <= a <= b <= 30:
        return None
    # Common explicit allowance for one extra page, e.g. 5 pages, sixth allowed.
    if a == b and (re.search(r"(?:допустим\w*|можно|разрешен\w*)\s+(?:ещ[её]\s+)?(?:одна\s+страниц\w*|шестая\s+страниц\w*)", text or "", re.I) and a == 5):
        b = 6
    return (a, b)


def stamp_page_target(content, request):
    target = requested_page_range(request)
    if not target:
        return content
    m = re.match(r"\A\s*```study\s*\n(.*?)\n```\s*", content, re.S | re.I)
    meta = {}
    if m:
        try:
            candidate = json.loads(m[1])
            if isinstance(candidate, dict):
                meta = candidate
                content = content[m.end():]
        except ValueError:
            pass
    from study_editorial import grouping_allowed
    meta["target_pages"] = list(target)
    meta["auto_group"] = grouping_allowed(request)
    return "```study\n" + json.dumps(meta, ensure_ascii=False) + "\n```\n\n" + content


def page_target(blocks):
    if not blocks or blocks[0].get("lang") != "study":
        return None
    try:
        v = json.loads(blocks[0]["text"]).get("target_pages")
        if isinstance(v, list) and len(v) == 2 and all(type(n) is int for n in v) and 1 <= v[0] <= v[1] <= 30:
            return tuple(v)
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def page_budget_hint(request):
    target = requested_page_range(request)
    if not target:
        return ""
    a, b = target
    n = max(1, b - (2 if b > 2 else 0))
    return (f"\nБЮДЖЕТ PDF: {a}–{b} страниц ВСЕГО, включая титул и содержание. "
            f"Ориентир для основного текста — до {n*320} слов, меньше при многочисленных формулах и графиках. "
            "Сохраняй все запрошенные элементы, убери вводную воду, повторные формулировки и лишние формулы. "
            "Не повторяй название и аннотацию в основном тексте. Формулы с нескольких связанных строк объединяй в aligned. "
            "Не создавай отдельные главы только ради страниц. Не обещай точное число страниц до вёрстки. "
            + ("Для 6–8 страниц, если пользователь не задал иной план, используй 3–4 крупные главы: "
               "короткие определения и правила внутри общей теории, графики и задания внутри практики, "
               "решения для самопроверки — отдельной главой. Короткие темы оформляй ##, не #. "
               if 6 <= b <= 8 else ""))


def volume_issues(request, words, blocks):
    target = requested_page_range(request)
    if not target:
        return []
    a,b = target
    body = max(1,b-(2 if b>2 else 0))
    visual = sum(b.get("t") == "math" or b.get("lang") in ("plot", "diagram") for b in blocks)
    # Soft content budget; actual page count is measured separately.
    cap = max(220, body * 380 - min(body * 130, visual * 7))
    if words > cap * 1.2:
        return [f"Превышен бюджет объёма: {words} слов при ориентире {cap}. Сожми повторы и пояснения, сохрани все необходимые доказательства, задачи и графики"]
    return []


def math_slot(tex, display, fallback):
    return (f'<span class="study-math" data-tex="{escape(tex, quote=True)}" '
            f'data-display="{int(bool(display))}">{fallback}</span>')


def mathjax_prepare(page, address, *, compact=False):
    asset = Path(os.environ.get("STUDY_MATHJAX_JS") or Path(__file__).with_name("study_print") / "mathjax-svg.js")
    if not asset.is_file():
        raise RuntimeError("MathJax не установлен: запусти python install_study_mathjax.py")
    url = address.rsplit("/",1)[0] + "/mathjax-svg.js"
    page.route(url, lambda route: route.fulfill(content_type="application/javascript", path=asset))
    page.evaluate("""(cache) => {
      window.MathJax = {
        startup: {typeset:false},
        tex: {packages:['base','ams','boldsymbol','mathtools','textmacros'], maxBuffer:10000, maxMacros:500},
        svg: {fontCache:cache}, options: {enableMenu:false}
      };
    }""", "local" if compact else "none")
    page.add_script_tag(url=url)
    page.unroute(url)
    page.wait_for_function("typeof MathJax.tex2svg === 'function'", timeout=15000)
    return page.evaluate(r"""async () => {
      await MathJax.startup.promise;
      let errors = 0;
      for(const el of document.querySelectorAll('.study-math')) {
        const tex = el.dataset.tex || '';
        // No HTML, network resources, macro definitions, or executable commands in TeX.
        if(tex.length>3500 || /\\(?:require|href|url|html\w*|include\w*|input|def|gdef|edef|xdef|newcommand|renewcommand|csname|class|style|unicode|bbox)\b/i.test(tex)) {
          errors++; continue;
        }
        try {
          const svg = MathJax.tex2svg(tex,{display:el.dataset.display==='1'});
          if(svg.querySelector('[data-mml-node="merror"], [data-mjx-error]')) throw Error('TeX');
          for (const hidden of svg.querySelectorAll("mjx-assistive-mml")) hidden.remove();
          svg.setAttribute("aria-label", tex);
          if (el.closest('figure.editorial-figure')) {
            const vector=svg.querySelector('svg');
            if(vector){
              const w=vector.getAttribute('width'), h=vector.getAttribute('height');
              if(w&&h&&/^[\d.]+ex$/.test(w)&&/^[\d.]+ex$/.test(h)){
                vector.style.setProperty('--study-svg-w',w);
                vector.style.setProperty('--study-svg-h',h);
                vector.setAttribute('data-study-math-size','1');
              }
            }
          }
          el.replaceChildren(svg);
        } catch(e) { errors++; }
      }
      return errors;
    }""")


LAYOUT_JS = r"""() => {

    // Keep the statement, short solution and answer together, without locking long solutions.
    for(const chapter of document.querySelectorAll('.chapter')) {
      for(const el of [...chapter.children]) {
        if(!el.classList.contains('example-block') || el.parentElement!==chapter) continue;
        const first=el.nextElementSibling;
        if(!first || !/^Решение\s*[:.]/i.test(first.textContent.trim())) continue;
        const nodes=[el]; let current=first;
        while(current && nodes.length<60) {
          if(current.matches('h1,h2,h3,.section-opener,.example-block,.theorem-block,.def-block,.proof-block,.figures,hr')) break;
          const next=current.nextElementSibling; nodes.push(current);
          if(current.classList.contains('callout-conclusion')) break;
          current=next;
        }
        const wrap=document.createElement('div');wrap.className='solution-group';
        el.before(wrap);for(const n of nodes) wrap.append(n);
      }
    }
    for(const node of document.querySelectorAll('.solution-group')) {
      const h=node.getBoundingClientRect().height;
      node.classList.toggle('keep-small',h>0&&h<340);
    }
    for(const proof of document.querySelectorAll('.proof-block')) {
      const children=[...proof.children];
      if(children.length>1 && children.at(-1).getBoundingClientRect().height<100) {
        children.at(-2).classList.add('keep-next');
      }
    }

    for(const node of document.querySelectorAll('.chapter p, .proof-block p, .list-row')) {
        const next=node.nextElementSibling;
        if(next && next.classList.contains('math-display') && /[:：]\s*$/.test(node.textContent.trim())) node.classList.add('keep-next');
    }
    for(const node of document.querySelectorAll('.proof-block,.def-block,.theorem-block,.example-block,table.editorial-table')) {
        const h=node.getBoundingClientRect().height;
        node.classList.toggle('keep-small',h>0 && h<300);
    }
    for(const expr of document.querySelectorAll('.study-math')) {
        expr.style.zoom='';
        const box=expr.closest('.math-expr,td,th,p,.list-row,.step-item,h2,h3,.legend,.point-key,figcaption,.chapter');
        if(!box) continue;
        const style=getComputedStyle(box);
        const width=box.clientWidth-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight);
        const actual=expr.getBoundingClientRect().width;
        if(width>0 && actual>width) expr.style.zoom=Math.max(.35,width/actual);
    }
}"""


def print_fitted(page, target, pdf_reader):
    import time, logging
    log=logging.getLogger('study.pdf')
    passes=0
    def render():
        from study_runtime_diag import memory_status
        nonlocal passes
        passes+=1
        t=time.monotonic()
        page.evaluate(LAYOUT_JS)
        # A regression guard catches oversized graph formulas before printing.
        bad=page.evaluate("""() => [...document.querySelectorAll('figure .study-math mjx-container > svg')].filter(s=>{const r=s.getBoundingClientRect();return r.height>60||r.width>s.closest('figure').clientWidth+2}).length""")
        if bad: raise RuntimeError(f'Неверный размер математических подписей на графиках: {bad}')
        result=page.pdf(prefer_css_page_size=True, print_background=True, outline=True)
        log.info('PDF print pass %d: %.1fs, %d bytes; %s',passes,time.monotonic()-t,len(result),memory_status())
        return result
    page.evaluate("() => delete document.body.dataset.density")
    data=render()
    def page_count(pdf):
        with pdf_reader(BytesIO(pdf)) as reader:
            return len(reader.pages)

    if target and (normal_count := page_count(data)) > target[1]:
        page.evaluate("() => document.body.dataset.density='compact'")
        candidate=render()
        if page_count(candidate)<normal_count:
            data=candidate
        else:
            page.evaluate("() => delete document.body.dataset.density")
            # Existing data is already the normal layout; only reprint if the TOC changes.
        del candidate
    # Resolve contents after density is final; repeat if page locations change.
    for _ in range(2):
        with pdf_reader(BytesIO(data)) as draft:
            pages={k.lstrip('/'):str(draft.get_destination_page_number(v)+1).zfill(2) for k,v in draft.named_destinations.items()}
        changed=page.locator('.toc-page').evaluate_all("""(nodes,pages)=>{
          let changed=false;
          for(const n of nodes){const number=pages[n.dataset.target]; if(!number) throw Error('Не найдена страница раздела '+n.dataset.target); if(n.textContent!==number){n.textContent=number;changed=true;}}
          return changed;
        }""", pages)
        if not changed: break
        data=render()
    return data
