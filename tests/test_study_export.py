"""Offline tests. Run from the repo root with python -m unittest discover -s tests."""
import asyncio
from io import BytesIO
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from pypdf import PdfReader
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Import in an empty directory, independently of real .env, keys or bot state.
with TemporaryDirectory() as scratch:
    from contextlib import chdir
    with chdir(scratch), patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "100000:offline", "GEMINI_KEYS": "",
                                                 "GEMINI_API_KEY": "", "HTTP_PROXY": "", "HTTPS_PROXY": ""}):
        import bot
        import ext_webapp
import study_pdf


class Exports(unittest.TestCase):
    def test_source_and_tool(self):
        for alias in ("tex", "latex"):
            result, name, mime = bot.build_document(alias, "A & B", r"$$\frac{1}{2}$$")
            self.assertTrue(name.endswith(".tex"))
            self.assertEqual(mime, "application/x-tex")
            self.assertIn(rb"\begin{document}", result)
            self.assertIn(rb"\frac{1}{2}", result)
        tool = next(t for t in bot.build_post_tool([]).function_declarations if t.name == "create_file")
        self.assertIn("tex", tool.parameters.properties["format"].enum)
        keys = bot.make_response_action_kb(1).inline_keyboard[0]
        self.assertEqual([k.text for k in keys], ["📄 PDF", "📝 Word", "📐 LaTeX (.tex)"])

    def test_web_export(self):
        request = AsyncMock()
        request.json.return_value = {"format": "tex", "content": r"$a^2$", "title": "Формула"}
        with patch.object(ext_webapp, "_store_generated_file", return_value="test") as save:
            res = asyncio.run(ext_webapp.api_export(request, {"id": 1}))
        self.assertTrue(res["ok"])
        data, name, mime = save.call_args.args
        self.assertTrue(name.endswith(".tex"))
        self.assertIn(b"$a^2$", data)
        self.assertEqual(mime, "application/x-tex")

    def test_telegram_button_exports_full_original(self):
        message = SimpleNamespace(chat=SimpleNamespace(id=42), message_id=52,
                                  text="Последняя часть", reply_document=AsyncMock())
        call = SimpleNamespace(data="doc_tex:51", message=message, answer=AsyncMock())
        cache = {(42, 52): {"text": r"Исходное решение $\frac{1}{2}$"}}
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def silent(*args):
            yield
        with patch.dict(bot.RECENT_ANSWERS, cache), patch.object(bot, "chat_action", silent):
            asyncio.run(bot.cb_generate_doc(call))
        document = message.reply_document.call_args.args[0]
        self.assertTrue(document.filename.endswith(".tex"))
        self.assertIn(rb"\frac{1}{2}", document.data)

    def test_modes(self):
        short, long = [bot.build_system_instruction([], True, account={"answer_mode": mode})
                       for mode in ("short", "detailed")]
        self.assertIn("одну строку", short)
        self.assertNotIn("обоснуй переходы", short)
        self.assertIn("обоснуй переходы", long)

    def test_safe_markup_math_and_figures(self):
        blocks = bot.parse_markdown("# Заголовок\n\n<widget>текст</widget>\n\n$$\\frac{1}{2}$$\n\n"
                                    + (ROOT / "examples/taylor.md").read_text(encoding="utf8"))
        html = study_pdf.document_html("Курс", blocks)
        self.assertIn("&lt;widget&gt;", html)
        self.assertIn("<mfrac>", html)
        self.assertIn("<polyline", html)
        self.assertIn('class="branches"', html)
        self.assertIn('class="kw"', html)
        result = study_pdf.document_html("Раздел", [{"t": "code", "lang": "plot", "text": '{"x":[1]}'}])
        self.assertIn("code-body", result)
        self.assertNotIn("<polyline", result)

    def test_long_pdf(self):
        rows = "\n".join(f"| {i} | Запись {i} |" for i in range(160))
        code = "\n".join(f"print({i})" for i in range(125))
        text = (f"# Теория\n\n| Номер | Запись |\n|---|---|\n{rows}\n\n"
                f"# Практика\n\n~~~python\n{code}\n~~~\n\nEND_SENTINEL\n\n[Источник](https://example.org/)").replace("~~~", chr(96)*3)
        data, name, mime = bot.build_document("pdf", "Длинный конспект", text)
        doc = PdfReader(BytesIO(data))
        self.assertEqual(mime, "application/pdf")
        self.assertTrue(name.endswith(".pdf"))
        self.assertGreater(len(doc.pages), 6)
        pages = [p.extract_text() for p in doc.pages]
        full = "\n".join(pages)
        for part in ("Запись 159", "print(124)", "END_SENTINEL"):
            self.assertIn(part, full)
        destinations = {k.lstrip("/"): doc.get_destination_page_number(v) + 1 for k,v in doc.named_destinations.items()}
        self.assertGreater(destinations["sec-2"], destinations["sec-1"])
        self.assertIn(f'{destinations["sec-2"]:02}', pages[1])
        self.assertIn(f'из {len(pages)}', pages[-1])
        links = [ref.get_object() for page in doc.pages for ref in page.get("/Annots", [])]
        self.assertTrue(any(a.get("/A", {}).get("/URI") == "https://example.org/" for a in links))

    def test_mini_app_null_and_tour(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=os.environ.get("STUDY_CHROMIUM") or None)
            try:
                page = browser.new_page(offline=True)
                page.route("**/*", lambda r: r.abort())
                page.route("https://study.invalid/app", lambda r: r.fulfill(content_type="text/html; charset=utf-8",
                           body=(ROOT / "webapp/index.html").read_text(encoding="utf8")))
                page.goto("https://study.invalid/app")
                rendered = page.evaluate("h('div', {}, null, [false, undefined, 'A', 0]).textContent")
                self.assertEqual(rendered, "A0")
                page.evaluate("openGuide()")
                self.assertEqual(page.locator(".tour-layer").count(), 1)
                page.locator(".tour-skip").click()
                self.assertEqual(page.locator(".tour-layer").count(), 0)
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
