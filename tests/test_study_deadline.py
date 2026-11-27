import asyncio
from contextlib import asynccontextmanager
import subprocess
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from study_deadline import CURRENT, PdfBudget, PdfExpired, size_seconds
from study_web_jobs import JobStore
from study_render_worker import render_bounded
from test_study_export import bot, ext_webapp  # imports with isolated offline credentials


class Sizing(unittest.TestCase):
    def test_boundaries_and_structure(self):
        for prompt, expected in [
            ("PDF 3 страницы", 300), ("PDF на 3–4 страницы", 600),
            ("10 pages", 600), ("10–11 pages", 900), ("120 страниц", 900),
            ("900 слов", 300), ("901 слово", 600), ("3 000 слов", 600),
            ("3,001 words", 900), ("5 тыс. слов", 900),
            ("PDF: 12 глав", 900), ("PDF: формулы, таблицы и графики", 600),
            ("PDF кратко. " + "контекст " * 5000, 300),
        ]:
            with self.subTest(prompt=prompt[:40]):
                self.assertEqual(size_seconds(prompt), expected)
        self.assertEqual(size_seconds(source="a " * 3001), 900)
        self.assertEqual(size_seconds(source="# Раздел\n" * 10), 900)

    def test_short_word_target_is_a_draft(self):
        from study_quality import document_issues, attach_warning
        text = "Текст " * 23
        issues = document_issues("PDF на 1000–1500 слов", text)
        args = {"caption": "Готов полный PDF" * 100}
        attach_warning(args, issues)
        self.assertTrue(args["_pdf_incomplete"])
        self.assertTrue(args["caption"].startswith("⚠️ Черновик"))
        self.assertFalse(document_issues("PDF до 1000 слов", text))

    def test_exact_endpoints_and_reserves(self):
        for seconds in (300, 600, 900):
            budget = PdfBudget(seconds, started=1000)
            self.assertEqual(budget.end, 1000 + seconds)
            self.assertLess(budget.model_end, budget.render_end)
            self.assertLess(budget.render_end, budget.work_end)
            self.assertLess(budget.work_end, budget.end)
            for endpoint in (budget.model_end, budget.render_end, budget.work_end, budget.end):
                with patch("study_deadline.time.monotonic", return_value=endpoint - .01):
                    self.assertAlmostEqual(budget.available(endpoint), .01)
                with patch("study_deadline.time.monotonic", return_value=endpoint):
                    with self.assertRaises(PdfExpired):
                        budget.available(endpoint)

    def test_renderer_never_starts_after_queue_allocation(self):
        budget = PdfBudget(300, started=time.monotonic() - 280)
        with patch("study_render_worker.subprocess.Popen") as spawn:
            token = CURRENT.set(budget)
            try:
                with self.assertRaises(PdfExpired):
                    render_bounded("test", [])
            finally:
                CURRENT.reset(token)
            spawn.assert_not_called()

    def test_partial_worker_output_rejected(self):
        process = Mock(returncode=0)
        process.communicate.return_value = (b"%PDF-1.4 truncated", None)
        with patch("study_render_worker.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "генератора"):
                render_bounded("test", [])

    def test_worker_timeout_releases_slot_and_discards_bytes(self):
        budget = PdfBudget(300)
        process = Mock(pid=123)
        process.communicate.side_effect = [subprocess.TimeoutExpired("worker", 1), (b"partial", None)]
        with patch("study_render_worker.subprocess.Popen", return_value=process), \
             patch("study_render_worker.subprocess.run"), patch("study_render_worker.os.killpg", create=True):
            token = CURRENT.set(budget)
            try:
                with self.assertRaises(PdfExpired):
                    render_bounded("test", [])
                self.assertLess(process.communicate.call_args_list[0].kwargs["timeout"], budget.seconds)
            finally:
                CURRENT.reset(token)


class DeadlineFlows(unittest.IsolatedAsyncioTestCase):
    async def test_ingress_time_and_no_late_commit(self):
        budget = PdfBudget(300, started=time.monotonic() - 287.95)
        committed = []
        async def slow():
            self.assertIs(await asyncio.to_thread(CURRENT.get), budget)
            await asyncio.sleep(.3)
            committed.append(True)
        with self.assertRaises(PdfExpired):
            await budget.run(slow)
        self.assertFalse(committed)
        self.assertIsNone(CURRENT.get())

    async def test_web_job_publishes_owner_scoped_draft(self):
        jobs = JobStore()
        req = AsyncMock()
        req.json.return_value = {"text": "PDF на 12 страниц"}
        owner = (9, "user")
        async def expired(*args):
            b = CURRENT.get()
            b.draft = "# Проверенный фрагмент"
            b.enter("очередь рендера")
            raise PdfExpired(b.stage)
        with patch.object(ext_webapp, "_STUDY_JOBS", jobs), \
             patch.object(ext_webapp, "_job_owner", return_value=owner), \
             patch.object(ext_webapp, "_api_ask", expired):
            ack = await ext_webapp.api_ask_start(req, {"id": 9})
            self.assertEqual(ack["budget_ms"], 900000)
            self.assertLessEqual(ack["remaining_ms"], ack["budget_ms"])
            key = ack["job_id"]
            await jobs.items[key]["task"]
            result = jobs.status(owner, key)
            self.assertEqual(result["state"], "error")
            self.assertIn("15 мин", result["result"]["error"])
            self.assertIn("очередь", result["result"]["error"])
            self.assertIn("фрагмент", result["result"]["draft"])
            self.assertNotIn("result", jobs.status((10, "other"), key))
            self.assertNotIn("files", result["result"])

    async def test_retry_then_repair_share_one_model_deadline(self):
        pool = bot.KeyPool(["offline-a", "offline-b"])
        call = SimpleNamespace(name="create_file", args={"format": "pdf", "content": "Черновик"})
        def response(part):
            return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]), finish_reason="STOP")])
        original = response(SimpleNamespace(function_call=call))
        repaired = response(SimpleNamespace(text="# Результат\n\n| A | B |\n|---|---|\n| 1 | 2 |"))
        limits = []
        replies = iter([TimeoutError(), original, repaired])
        async def model(**kwargs):
            limits.append(kwargs["config"].http_options.timeout / 1000)
            value = next(replies)
            if isinstance(value, Exception):
                raise value
            return value
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=model)))
        budget = PdfBudget(600, started=time.monotonic()-220)
        with patch.object(pool, "client", return_value=client):
            await budget.run(lambda: bot.generate_reply([], "PDF с таблицей", [], False, pool=pool))
        self.assertEqual(len(limits), 3)
        self.assertTrue(all(0 < n <= budget.model_end-budget.started-220 for n in limits))
        self.assertIn("Результат", call.args["content"])
        self.assertFalse(call.args.get("_pdf_incomplete"))

    async def test_implicit_pdf_from_web_request_through_real_renderer(self):
        from io import BytesIO
        from pypdf import PdfReader
        jobs = JobStore()
        owner = (9, "test-pdf")
        account = {"id": owner[1], "name": "Test"}
        request = AsyncMock()
        request.json.return_value = {"text": "Подготовь пособие на 12 разделов"}
        fn = SimpleNamespace(name="create_file", args={"format": "pdf", "content": "# Проверка\n\nEND_SENTINEL"})
        candidate = SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(function_call=fn)]))
        pool = bot.KeyPool(["offline"])
        model = AsyncMock(return_value=SimpleNamespace(candidates=[candidate]))
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=model)))
        with patch.object(ext_webapp, "_STUDY_JOBS", jobs), patch.object(ext_webapp, "APP_CHATS", {}), \
             patch.object(ext_webapp, "get_request_account", return_value=account), \
             patch.object(ext_webapp, "get_account_pool", return_value=(pool, True)), \
             patch.object(pool, "client", return_value=client), patch.object(bot, "save_json"), \
             patch.object(bot, "HISTORY", {"items": []}), \
             patch.object(ext_webapp, "_store_generated_file", return_value="result") as save:
            ack = await ext_webapp.api_ask_start(request, {"id": 9})
            key = ack["job_id"]
            await jobs.items[key]["task"]
            status = jobs.status(owner, key)
            self.assertEqual(status["state"], "done", status.get("result"))
            self.assertEqual(status["budget_ms"], 900000)
            self.assertEqual(status["result"]["files"][0]["id"], "result")
            data, _, mime = save.call_args.args
            self.assertEqual(mime, "application/pdf")
            self.assertIn("END_SENTINEL", "".join(p.extract_text() for p in PdfReader(BytesIO(data)).pages))

    async def test_telegram_unconfirmed_upload_is_not_retried(self):
        @asynccontextmanager
        async def silent(*args):
            yield
        msg = SimpleNamespace(chat=SimpleNamespace(id=7), message_id=4, text="Мой текст",
                              reply=AsyncMock(), reply_document=Mock())
        async def recovery(doc, **kwargs):
            self.assertEqual(doc.filename, "Черновик.md")
            self.assertEqual(doc.data.decode().strip(), msg.text)
        msg.reply_document.side_effect = lambda doc, **kw: recovery(doc, **kw) if doc.filename.endswith(".md") else object()
        call = SimpleNamespace(data="doc_pdf:4", message=msg, answer=AsyncMock())
        transport = AsyncMock(side_effect=TimeoutError)
        with patch.object(bot, "chat_action", silent), patch.object(bot, "bot", transport), \
             patch.object(bot, "build_document", return_value=(b"%PDF", "x.pdf", "application/pdf")):
            await bot.cb_generate_doc(call)
        transport.assert_awaited_once()
        self.assertLessEqual(transport.await_args.kwargs["request_timeout"], 288)
        msg.reply.assert_awaited_once()
        self.assertIn("не подтвердил", msg.reply.await_args.args[0])
        self.assertEqual(msg.reply_document.call_count, 2)
