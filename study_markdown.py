"""Shared Markdown block and inline parsing for document exports."""
import re


INLINE_RE = re.compile(
    r"\$(?!\s)[^$\n]+?(?<!\s)\$|\\\(.+?\\\)|\*\*.+?\*\*|__.+?__|`[^`\n]+`|\[[^\]\n]+\]\(https?://[^\s)]+\)"
    r"|(?<![\w*])\*(?!\s)[^*\n]+?(?<!\s)\*(?![\w*])")
LINK_PARTS_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def inline_tokens(text: str) -> list:
    """Разбор **жирного**, *курсива*, `кода` и [ссылок](url) -> [(вид, текст, url)]."""
    out, pos = [], 0
    for m in INLINE_RE.finditer(text):
        if m.start() > pos:
            out.append(("t", text[pos:m.start()], ""))
        tok = m.group(0)
        if tok.startswith(("*", "__")):
            inner = tok[2:-2] if tok.startswith(("**", "__")) else tok[1:-1]
            style = "b" if tok.startswith(("**", "__")) else "i"
            for k, t, u in inline_tokens(inner):  # внутри жирного/курсива может быть `код`, формула или ссылка
                out.append((k if k in ("c", "a", "m") else style, t, u))
        elif tok.startswith("$"):
            out.append(("m", tok[1:-1], ""))
        elif tok.startswith("\\("):
            out.append(("m", tok[2:-2], ""))
        elif tok.startswith("`"):
            out.append(("c", tok[1:-1], ""))
        elif tok.startswith("["):
            lm = LINK_PARTS_RE.match(tok)
            out.append(("a", lm.group(1), lm.group(2)))
        pos = m.end()
    if pos < len(text):
        out.append(("t", text[pos:], ""))
    return out


def parse_markdown(md: str) -> list:
    """Markdown -> блоки: h(lvl,text), p, li(lvl,ordered,num,text), code, table(rows), quote, hr."""
    lines = (md or "").replace("\r\n", "\n").replace("\t", "    ").split("\n")
    blocks: list = []
    para: list = []

    def flush():
        if para:
            blocks.append({"t": "p", "text": " ".join(x.strip() for x in para)})
            para.clear()

    def split_row(row: str) -> list:
        row = row.strip()
        if row.startswith("|"):
            row = row[1:]
        if row.endswith("|"):
            row = row[:-1]
        return [c.strip() for c in row.split("|")]

    sep_re = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
    i = 0
    while i < len(lines):
        line = lines[i]
        st = line.strip()
        if st.startswith("```"):
            language = st[3:].strip().lower()
            flush()
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            blocks.append({"t": "code", "lang": language, "text": "\n".join(code)})
            continue
        if not st:
            flush()
            i += 1
            continue
        if st.startswith("$$") or st.startswith("\\["):
            flush()
            open_t, close_t = ("$$", "$$") if st.startswith("$$") else ("\\[", "\\]")
            body = st[len(open_t):]
            if close_t in body:
                expr = body[:body.index(close_t)]
                i += 1
            else:
                parts = [body]
                i += 1
                while i < len(lines) and close_t not in lines[i]:
                    parts.append(lines[i])
                    i += 1
                if i < len(lines):
                    parts.append(lines[i].split(close_t)[0])
                    i += 1
                expr = " ".join(x.strip() for x in parts)
            if expr.strip():
                blocks.append({"t": "math", "text": expr.strip()})
            continue
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", st)
        if m:
            flush()
            blocks.append({"t": "h", "lvl": min(len(m.group(1)), 3), "text": m.group(2)})
            i += 1
            continue
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", st):
            flush()
            blocks.append({"t": "hr"})
            i += 1
            continue
        if "|" in st and i + 1 < len(lines) and sep_re.match(lines[i + 1]):
            flush()
            rows = [split_row(st)]
            i += 2
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                rows.append(split_row(lines[i]))
                i += 1
            width = max(len(r) for r in rows)
            rows = [r + [""] * (width - len(r)) for r in rows]
            blocks.append({"t": "table", "rows": rows})
            continue
        mli = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", line)
        if mli:
            flush()
            marker = mli.group(2)
            ordered = marker[0].isdigit()
            blocks.append({"t": "li", "lvl": min(len(mli.group(1)) // 2, 2), "ordered": ordered,
                           "num": int(re.match(r"\d+", marker).group(0)) if ordered else 0, "text": mli.group(3)})
            i += 1
            continue
        if st.startswith(">"):
            flush()
            blocks.append({"t": "quote", "text": st.lstrip("> ").strip()})
            i += 1
            continue
        para.append(st)
        i += 1
    flush()
    return blocks
