"""One monotonic clock per PDF request, also inherited by asyncio.to_thread."""
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
import logging
import re
import time
import uuid

LOG = logging.getLogger("study.deadline")
CURRENT = ContextVar("pdf_budget", default=None)
# A request can become a PDF only after the model selects create_file.
LATE_PDF = ContextVar("late_pdf", default=None)


async def run_unclassified(started, work, recover, announce=None):
    budget = None
    timer = asyncio.timeout(None)
    token = CURRENT.set(None)

    def activate(request, source):
        nonlocal budget
        if budget is None:
            budget = PdfBudget(size_seconds(request, source), started, draft=source[:80000])
            CURRENT.set(budget)
            if announce:
                announce(budget)
            budget.enter("получен черновик Gemini")
            loop = asyncio.get_running_loop()
            timer.reschedule(loop.time() + budget.available(budget.work_end))
        return budget

    pending = LATE_PDF.set(activate)
    try:
        try:
            async with timer:
                result = await work()
                if budget:
                    budget.available(budget.work_end)
                return result
        except TimeoutError:
            if not timer.expired():
                raise
        except PdfExpired:
            if budget is None:
                raise
        return await recover(budget)
    finally:
        LATE_PDF.reset(pending)
        CURRENT.reset(token)

NUMBER = r"(?<!\d)(\d{1,3}(?:[ \u00a0\u202f,]\d{3})+|\d{1,9})"
RANGE = NUMBER + r"(?:\s*(?:[-–—]|до|to)\s*" + NUMBER + r")?\s*"
PAGES = re.compile(RANGE + r"(?:стр\.|страниц\w*\b|pages?\b)", re.I)
WORDS = re.compile(RANGE + r"(тыс(?:яч\w*)?\.?|[kк])?\s*(?:слов\w*|words?)\b", re.I)
SECTIONS = re.compile(RANGE + r"(?:глав\w*|раздел\w*|задач\w*|график\w*|chapters?|sections?|problems?|plots?)\b", re.I)
OUTLINE = re.compile(r"(?m)^\s*(?:#{1,4}\s+|\d+[.)]\s+|[-*]\s+)")
FEATURES = (r"доказательств|proof", r"график|plot|chart", r"таблиц|table",
            r"схем|diagram", r"задач|упражнен|exercise", r"формул|formula")


def quantity(pattern, text):
    values = []
    for match in pattern.finditer(text):
        n = int(re.sub(r"\D", "", match[2] or match[1]))
        values.append(n * (1000 if pattern is WORDS and match[3] else 1))
    return max(values, default=0)


def minimum_words(request):
    for match in WORDS.finditer(request):
        prefix = request[:match.start()].casefold().rstrip()
        if re.search(r"(?:\bдо|не более|не больше|up to|at most|max(?:imum)?)$", prefix):
            continue
        number = int(re.sub(r"\D", "", match[1]))
        return number * (1000 if match[3] else 1)
    return 0


def size_seconds(request="", source=""):
    """Choose the largest signal. Source means existing Markdown being exported."""
    pages = quantity(PAGES, request)
    words = max(quantity(WORDS, request), len(source.split()))
    figures = len(re.findall(r"(?im)^\s*\x60{3}(?:plot|diagram)\b", source))
    equations = len(re.findall(r"(?m)^\s*\$\$[^\n]+\$\$\s*$", source))
    structure = max(quantity(SECTIONS, request), len(OUTLINE.findall(source or request)),
                    sum(bool(re.search(pattern, request, re.I)) for pattern in FEATURES),
                    figures + equations)
    if pages > 10 or words > 3000 or structure >= 10:
        return 900
    if pages > 3 or words > 900 or structure >= 3:
        return 600
    return 300


class PdfExpired(RuntimeError):
    pass


@dataclass
class PdfBudget:
    seconds: int
    started: float = field(default_factory=time.monotonic)
    trace: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    stage: str = "приём запроса"
    draft: str = ""
    upload_pending: bool = False
    delivered: int = 0
    failure_reason: str = ""

    @property
    def end(self):
        return self.started + self.seconds

    # Leave time for sending a terminal notice (and for the next miniapp poll).
    @property
    def work_end(self):
        return self.end - 12

    @property
    def render_end(self):
        return self.work_end - self.seconds * .08

    @property
    def model_end(self):
        return self.render_end - self.seconds * .20

    def available(self, until):
        left = until - time.monotonic()
        if left <= 0:
            raise PdfExpired(self.stage)
        return left

    def enter(self, stage):
        self.stage = stage
        LOG.info("PDF %s stage=%s remaining=%.1fs", self.trace, stage, self.end - time.monotonic())

    def message(self):
        if self.failure_reason:
            return self.failure_reason
        text = (f"Время на этап «{self.stage}» исчерпано. Общий лимит PDF — "
                f"{self.seconds // 60} мин.")
        if self.upload_pending:
            return f"Telegram не подтвердил доставку PDF. Общий лимит — {self.seconds // 60} мин. Проверь чат перед повтором."
        if self.delivered:
            return text + f" Telegram уже подтвердил отправку файлов: {self.delivered}."
        return text + " Готовый PDF по этому запросу не подтверждён."

    async def run(self, work):
        token = CURRENT.set(self)
        LOG.info("PDF %s start budget=%ds", self.trace, self.seconds)
        try:
            timer = asyncio.timeout(self.available(self.work_end))
            try:
                async with timer:
                    result = await work()
                    self.available(self.work_end)
                    return result
            except TimeoutError as exc:
                if timer.expired():
                    raise PdfExpired(self.stage) from exc
                raise
        finally:
            LOG.info("PDF %s stop stage=%s elapsed=%.1fs", self.trace, self.stage,
                     time.monotonic() - self.started)
            CURRENT.reset(token)
