"""Bounded, data-only checks for educational PDFs; no model-supplied code execution."""
import json
import re
from study_markdown import parse_markdown
from study_figures import plot, diagram, label
from study_layout import volume_issues, requested_page_range

PDF_RE = re.compile(r"\b(?:pdf|пдф)\b", re.I)
PAGE_RE = re.compile(r"(\d{1,2})(?:\s*[-–—]\s*\d{1,2})?\s*(?:страниц\w*|стр\.?|pages?)\b", re.I)
GRAPH_RE = re.compile(r"\b(?:график\w*|plot|charts?)\b", re.I)

PDF_INSTRUCTIONS = r"""
ПРАВИЛА УЧЕБНЫХ PDF (в дополнение к выбранному режиму ответа):
- Явное требование пользователя о глубине и объёме имеет приоритет. Сложное пособие — не краткий пересказ.
- Различай типы документов: пособие — главы и подразделы; решение одной задачи — условие, решение, проверка и ответ; шпаргалка — компактные правила и примеры; лабораторная — цель, методика, исходные данные, расчёты, результаты и вывод. Не выдумывай исходные экспериментальные данные.
- Каждый крупный раздел начинается с новой страницы, поэтому не превращай короткие абзацы в отдельные главы. В кратком документе используй подзаголовки. Аннотация должна раскрывать содержание, а не повторять заголовок.
- Перед выдачей проверяй каждую формулу подстановкой или независимым пересчётом, где это возможно; сверяй условия теорем, знаки неравенств, область определения, численные ответы, графики и табличные значения. Не называй непроверенное вычисление проверенным.
- Для задач используй «Пример: Задача ...», затем отдельный абзац «Решение: ...» и «Вывод: Ответ ...».
- Заголовок title: обычный текст с пробелами, не имя файла. Не повторяй title как первый заголовок; начни с 3–6 содержательных разделов #, внутри ##. Никакой ручной нумерации заголовков: PDF проставляет её сам. Не добавляй пустые разделы, отдельные заголовки «Аннотация» и «Учебное пособие»; аннотация передаётся метаданными.
- В начале можно поместить блок study с JSON: subtitle, subject, format, outcomes, tags и goals (список пар название/описание). Затем содержательная часть Markdown.
- Определение: ..., Теорема: ..., Доказательство: ..., Пример: ..., Важно: ..., Вывод: ... — каждый смысловой блок начинай с нового абзаца именно с такой меткой. Доказательство продолжай абзацами, формулами и шагами вплоть до вывода. Не называй теоремой утверждение без условий.
- Объясняй конкретные вычисления и логические переходы. В подробном пособии формулируй определения, доказывай запрошенные теоремы, раскрывай выбор метода, показывай все вычисления и проверяй результат. Не считай список формул доказательством. Для каждой запрошенной задачи с ответом дай решение или ответ в соответствии с запросом.
- Если запрошены графики, ОБЯЗАТЕЛЬНО передай реальные блоки plot, а не только листинг Python. Синтаксис: три обратные кавычки и plot, затем JSON вида {"title":"Функция и производная","domain":[-2,2],"series":[{"name":"f(x)","expr":"x**3-3*x"},{"name":"f'(x)","expr":"3*x**2-3"}]} и закрывающие три кавычки. Выражения только с x, pi, e, числами, + - * / ** и функциями sin, cos, tan, exp, log, sqrt, abs. Для табличных данных допустим x:[...], series:[{"name":"...","y":[...]}]. Если графиков несколько, включи все необходимые кривые или отдельные блоки. Не выдумывай данные. Для схемы используй блок diagram с JSON title, root, branches. Не заменяй запрошенный рисунок программным кодом.
- В названиях графиков и подписях математические выражения заключай в $...$; названия кривых должны соответствовать их expr. Математические обозначения и степени не записывай как обычный текст.
- В plot подпись точки должна быть только названием («Нуль», «Максимум», «Точка касания»), без координат: они вычисляются автоматически. В plot для первой кривой добавляй при необходимости points: [{"x":1,"label":"Точка касания"}]; координата y вычисляется автоматически. Если точек нет, движок отметит обнаруженные нули и экстремумы первой аналитической кривой.
- Используй LaTeX $...$ и отдельные $$...$$ для формул. В частности, используй \Delta x или h вместо неоднозначного Unicode. Для систем/многострочных формул используй aligned, а не склеивание уравнений.
- Если просят краткую шпаргалку, соблюдай краткость; если просят подробный документ или конкретное число страниц, заполняй объём реальными разъяснениями, а не титулами, пустыми страницами или повторами. Перед завершением сверь ВСЕ пункты запроса, в том числе графики, таблицы, примеры, доказательства, решения и ответы.
"""


def pdf_request(text):
    return bool(PDF_RE.search(text or ""))


def requested_min_pages(text):
    m = PAGE_RE.search(text or "")
    return min(20, int(m.group(1))) if m else 0


def requested_curves(text):
    if re.search(r"(?:без|не нужны|не надо|не добавляй)\s+график", text, re.I):
        return 0
    if not GRAPH_RE.search(text):
        return 0
    if re.search(r"перв\w*\s+и\s+втор\w*\s+производн", text, re.I):
        return 3
    m = re.search(r"([2-5])\s+график", text, re.I)
    return int(m.group(1)) if m else 1


def document_issues(request, content):
    request, content = str(request or ""), str(content or "")
    blocks = parse_markdown(content)
    prose = "\n".join(b.get("text", "") for b in blocks if b["t"] in ("p", "quote", "li"))
    words = len(re.findall(r"\b[^\W\d_]{2,}\b", prose))
    issues = volume_issues(request, words, blocks)
    pages = requested_min_pages(request)
    # This is a lower bound on substance, not a claim about the actual page count.
    if pages >= 5 and words < max(250, (pages - 2) * 180):
        issues.append(f"Недостаточно содержательного текста: {words} слов; для запрошенного объёма ориентир не менее {(pages-2)*180} слов без титула, кода и оглавления")
    curves = diagrams = 0
    for b in blocks:
        if b["t"] == "code" and b.get("lang") in ("plot", "diagram"):
            try:
                spec = json.loads(b["text"])
                label(spec["title"])
                if b["lang"] == "plot":
                    plot(spec)
                    curves += len(spec["series"])
                else:
                    diagram(spec)
                    diagrams += 1
            except (ValueError, KeyError, TypeError, ArithmeticError, RecursionError):
                issues.append("Есть некорректный блок графика или схемы; исправь его данные")
    need = requested_curves(request)
    if curves < need:
        issues.append(f"Нет всех запрошенных кривых: {curves} из {need}")
    if re.search(r"\bсхем\w*\b", request, re.I) and not re.search(r"без\s+схем", request, re.I) and not diagrams:
        issues.append("Нет запрошенной визуальной схемы diagram")
    if re.search(r"\bтаблиц\w*\b", request, re.I) and not re.search(r"без\s+таблиц", request, re.I) and not any(b["t"] == "table" for b in blocks):
        issues.append("Нет запрошенной таблицы")
    if re.search(r"доказательств", request, re.I) and not re.search(r"без\s+доказательств", request, re.I) and not re.search(r"доказательств", prose, re.I):
        issues.append("Нет запрошенных доказательств")
    if re.search(r"(?:самостоятельн\w*\s+решени|задач\w*).{0,65}(?:с\s+(?:подробными\s+)?(?:ответ|решени))", request, re.I | re.S):
        # A separate answer/solution heading or repeated explicit answer labels.
        if not re.search(r"(?im)^#{1,6}\s+[^\n]*(?:ответы|решения)|(?:ответ|решение)\s*[:.]", content):
            issues.append("Нет ответов или решений для самостоятельных задач")
    if len(content) > 80000:
        issues.append("Документ превышает допустимые 80000 символов; сократи повторы, не содержание")
    return list(dict.fromkeys(issues))


def response_text(resp):
    parts = getattr(getattr(resp.candidates[0], "content", None), "parts", None) or []
    text = "\n".join(p.text for p in parts if getattr(p, "text", None) and not getattr(p, "thought", False)).strip()
    if text.startswith("```markdown\n") and text.endswith("\n```"):
        text = text[len("```markdown\n"):-len("\n```")].strip()
    return text


def attach_warning(args, issues):
    if not issues:
        return
    warning = "Автопроверка выявила неполноту: " + "; ".join(issues) + "."
    args["caption"] = (str(args.get("caption") or "") + "\n⚠️ " + warning).strip()[:900]
    # Diagnostics belong in the caption, not in the educational material.
