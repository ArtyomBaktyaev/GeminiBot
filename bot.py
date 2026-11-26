# -*- coding: utf-8 -*-
"""
Академический Telegram-ассистент: aiogram 3 + Gemini (google-genai).

Что умеет:
  * ответы Gemini в группе с топиками и в личке (контекст ветки = скользящее окно);
  * публикация напоминаний в нужный топик через Function Calling + список задач (/tasks);
  * фото, документы (PDF / DOCX / текст) и голосовые сообщения;
  * пул API-ключей Gemini с автоматической ротацией (429 / 401 / 403 / 5xx);
  * безопасное форматирование (Markdown -> Telegram HTML) и разбивка длинных ответов;
  * мини веб-сервер для Render health check (/health);
  * необязательное облачное сохранение JSON-файлов в GitHub Gist
    (на бесплатном Render диск стирается при каждом перезапуске).

Конфигурация — только через переменные окружения или файл .env (см. .env.example).
"""
import asyncio
import hashlib
import importlib
import html
import io
import json
import logging
import os
import random
import re
import socket
import sys
import time

# Обход блокировок ТСПУ/РКН и сетевого таймаута семафора (WinError 121) к api.telegram.org:
# Адрес 149.154.166.110 часто глушится, тогда как рабочий Anycast-узел 149.154.167.220 доступен моментально.
_orig_getaddrinfo = socket.getaddrinfo


def _telegram_dns_patch(host, port, *args, **kwargs):
    res = _orig_getaddrinfo(host, port, *args, **kwargs)
    if host == "api.telegram.org":
        new_res = []
        for item in res:
            family, type_, proto, canonname, sockaddr = item
            if sockaddr and sockaddr[0] == "149.154.166.110":
                new_sockaddr = ("149.154.167.220", sockaddr[1])
                new_res.append((family, type_, proto, canonname, new_sockaddr))
            else:
                new_res.append(item)
        return new_res
    return res


socket.getaddrinfo = _telegram_dns_patch
import zipfile
import xml.etree.ElementTree as ET
from collections import OrderedDict
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
from study_markdown import inline_tokens, parse_markdown

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramConflictError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)
from google import genai
from google.genai import types as genai_types
from PIL import Image, ImageOps

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("academic-bot")


# =====================================================================
# 1. КОНФИГУРАЦИЯ
# =====================================================================
def _load_dotenv(path: str = ".env") -> None:
    """Мини-загрузчик .env (без внешних зависимостей). Реальные env имеют приоритет."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def _env_list(name: str) -> list:
    raw = os.environ.get(name, "")
    return [x.strip() for x in re.split(r"[,\s;]+", raw) if x.strip()]


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_KEYS = _env_list("GEMINI_KEYS") or _env_list("GEMINI_API_KEY")
TARGET_CHAT_ID = _env_int("TARGET_CHAT_ID", 0)
ADMIN_IDS = {int(x) for x in _env_list("ADMIN_IDS") if x.lstrip("-").isdigit()}
# Группы, подключённые заранее через переменную (остальные подключаются командой /addgroup внутри группы)
ALLOWED_CHAT_IDS = {int(x) for x in _env_list("ALLOWED_CHAT_IDS") if x.lstrip("-").isdigit()}

_env_models = _env_list("GEMINI_MODELS")
if _env_models:
    MODEL_CASCADE = _env_models
else:
    MODEL_CASCADE = [
        "gemini-3.8-flash",
        "gemini-3.6-flash",
        "gemini-3.1-pro",
        "gemini-3-flash",
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash-lite",
        "gemini-2.5-flash",
        "gemini-4-hydrogen",
        "gemini-4-argon",
    ]

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", (MODEL_CASCADE[0] if MODEL_CASCADE else "gemini-3.8-flash")).strip()
HISTORY_WINDOW = _env_int("HISTORY_WINDOW", 20)      # сколько текстовых реплик помнить в ветке
MAX_MEDIA_PER_THREAD = _env_int("MAX_MEDIA_PER_THREAD", 3)  # сколько вложений держим в RAM на ветку
MEDIA_BUDGET_BYTES = _env_int("MEDIA_BUDGET_MB", 60) * 1024 * 1024  # общий лимит RAM на вложения
MAX_THREADS_IN_RAM = _env_int("MAX_THREADS_IN_RAM", 40)
IMG_MAX_SIDE = 1600
INLINE_PDF_LIMIT = 6 * 1024 * 1024      # PDF меньше — шлём inline, больше — через File API
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024    # лимит скачивания файла ботом в Telegram
TG_LIMIT = 4096                         # лимит одного сообщения Telegram
CHUNK_LIMIT = 3000                      # запас под HTML-теги и экранирование
PORT = _env_int("PORT", 10000)
DAILY_HOUR = _env_int("DAILY_HOUR", 9)  # во сколько (по BOT_TZ) слать ежедневные напоминания
REQUEST_TIMEOUT_SEC = _env_int("REQUEST_TIMEOUT_SEC", 25)  # таймаут на одну попытку
TOTAL_BUDGET_SEC = _env_int("TOTAL_BUDGET_SEC", 48)        # не держим запрос по 2 минуты

MODEL_DESCRIPTIONS = {
    "gemini-3.8-flash": ("Gemini 3.8 Flash", "Флагман скорости · Reasoning и глубокие рассуждения"),
    "gemini-3.6-flash": ("Gemini 3.6 Flash", "Быстрая и точная · Оптимальный баланс"),
    "gemini-3.1-pro": ("Gemini 3.1 Pro", "Флагман Pro · Сложная математика, код и анализ"),
    "gemini-3-flash": ("Gemini 3 Flash", "Базовая 3.0 · Быстрый отклик"),
    "gemini-3.5-flash-lite": ("Gemini 3.5 Flash Lite", "Высокий лимит · Минимальная задержка"),
    "gemini-3.1-flash-lite": ("Gemini 3.1 Flash Lite", "Легковесная · Экономичный расход"),
    "gemini-2.5-flash": ("Gemini 2.5 Flash", "Стабильная 2.5 · Проверенная рабочая модель"),
    "gemini-4-hydrogen": ("Gemini 4 Hydrogen", "Флагман следующего поколения"),
    "gemini-4-argon": ("Gemini 4 Argon", "Продвинутая логика и reasoning"),
}

AVAILABLE_MODELS = []
seen_models = set()
for m_id in MODEL_CASCADE + list(MODEL_DESCRIPTIONS.keys()):
    if m_id not in seen_models:
        seen_models.add(m_id)
        name, desc = MODEL_DESCRIPTIONS.get(m_id, (m_id, "Модель Gemini"))
        AVAILABLE_MODELS.append({"id": m_id, "name": name, "desc": desc})

if not TELEGRAM_BOT_TOKEN:
    sys.exit("Для запуска нужен TELEGRAM_BOT_TOKEN.")

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ.get("BOT_TZ", "Europe/Moscow"))
except Exception:  # нет tzdata
    TZ = timezone.utc

TOPICS_FILE = "topics.json"
SETTINGS_FILE = "user_settings.json"
TASKS_FILE = "tasks.json"
SCHEDULE_FILE = "schedule.json"  # запланированные напоминания и таймеры
GROUPS_FILE = "groups.json"  # {chat_id: {title, enabled, topics: {имя: thread_id}}}
HISTORY_FILE = "history.json"  # последние запросы (для мини-приложения)
GUEST_FILE = "guests.json"
STATE_FILES = (GROUPS_FILE, TOPICS_FILE, SETTINGS_FILE, TASKS_FILE, SCHEDULE_FILE, HISTORY_FILE, GUEST_FILE)

DEFAULT_SETTINGS = {
    "user_profile": "Студент технического вуза, изучаю математику, программирование, английский язык.",
    "custom_instructions": "Объясняй понятно, по делу, при решении задач давай пошаговый ход мысли.",
    "answer_mode": "detailed",  # detailed | short
    "preferred_model": GEMINI_MODEL,
    "model_fallback": True,     # автоматически спускаться на 3.6 -> 3.5-lite и ниже при исчерпании квот
    "media_enabled": True,      # слать стикер/GIF вместе с напоминанием
    "reminder_media": [],       # [{"kind": "sticker"|"animation", "file_id": ..., "uid": ...}]
}

esc = lambda s: html.escape(str(s), quote=False)  # noqa: E731


# =====================================================================
# 1b. РАСШИРЕНИЯ (плагины ext_*.py): расписание, учёба, мини-приложение
# =====================================================================
PLUGIN_TOOLS: list = []      # описания функций для Gemini
PLUGIN_HANDLERS: dict = {}   # имя функции -> async (message, scope, is_private, args, origin) -> str
PLUGIN_PROMPT: list = []     # callables(scope, is_private) -> str: правила и контекст для системного промпта
PLUGIN_TICKS: list = []      # async callables, вызываются планировщиком примерно раз в 30 секунд
WEB_SETUP: list = []         # callables(app): подключение маршрутов aiohttp
API_ROUTES: list = []        # (метод, путь, async handler(request, user) -> dict) для мини-приложения
PLUGIN_LOADERS: list = []    # callables(): загрузка состояния модуля (после восстановления из Gist)


# =====================================================================
# 2. ХРАНИЛИЩЕ JSON (+ опциональная синхронизация с GitHub Gist)
# =====================================================================
GIST_ID = os.environ.get("GIST_ID", "").strip()
GIST_TOKEN = os.environ.get("GIST_TOKEN", "").strip()
GIST_ENABLED = bool(GIST_ID and GIST_TOKEN)
_gist_task: Optional[asyncio.Task] = None
_gist_dirty = False


def _gist_headers() -> dict:
    return {
        "Authorization": f"Bearer {GIST_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def gist_pull() -> None:
    """При старте подтягиваем сохранённое состояние из Gist (если настроен)."""
    restoring_large_file = False
    if not GIST_ENABLED:
        return
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.get(f"https://api.github.com/gists/{GIST_ID}", headers=_gist_headers()) as r:
                if r.status != 200:
                    log.warning("Gist pull: HTTP %s", r.status)
                    return
                data = await r.json()
            restored = {}
            for name in STATE_FILES:
                entry = (data.get("files") or {}).get(name)
                if not entry:
                    continue
                content = entry.get("content")
                restoring_large_file = bool(entry.get("truncated"))
                if restoring_large_file:
                    raw_url = entry.get("raw_url", "")
                    url = urlsplit(raw_url)
                    if url.scheme != "https" or url.hostname != "gist.githubusercontent.com" or GIST_ID not in url.path.split("/"):
                        raise ValueError("Некорректный адрес файла резервной копии")
                    async with s.get(raw_url, allow_redirects=False) as raw:
                        if raw.status != 200:
                            raise RuntimeError(f"Не удалось восстановить {name}: HTTP {raw.status}")
                        content = await raw.text()
                if content:
                    json.loads(content)
                    restored[name] = content
                restoring_large_file = False
        for name, content in restored.items():
            tmp = name + ".restore.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(tmp, name)
        log.info("☁️ Состояние загружено из Gist")
    except Exception as e:
        log.warning("Не удалось восстановить резервную копию: %s", type(e).__name__)
        if restoring_large_file and not os.path.exists(name):
            raise RuntimeError(f"Не восстановлен {name}; локальной копии нет") from e


async def gist_push() -> None:
    try:
        files = {}
        for name in STATE_FILES:
            if os.path.exists(name):
                with open(name, "r", encoding="utf-8") as fh:
                    files[name] = {"content": fh.read() or "{}"}
        if not files:
            return
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.patch(
                f"https://api.github.com/gists/{GIST_ID}",
                headers=_gist_headers(),
                json={"files": files},
            ) as r:
                if r.status != 200:
                    log.warning("Gist push: HTTP %s", r.status)
    except Exception as e:
        log.warning("Gist push failed: %s", e)


async def _drain_gist_pushes() -> None:
    global _gist_dirty
    while _gist_dirty:
        _gist_dirty = False
        await gist_push()


def schedule_gist_push() -> None:
    """Coalesce saves before uploading and upload again if a save arrives in flight."""
    global _gist_dirty, _gist_task
    if not GIST_ENABLED:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _gist_dirty = True
    if _gist_task is None or _gist_task.done():
        async def upload_after_delay():
            await asyncio.sleep(5)
            await _drain_gist_pushes()

        _gist_task = loop.create_task(upload_after_delay())


async def flush_gist() -> None:
    global _gist_dirty, _gist_task
    if not GIST_ENABLED:
        return
    _gist_dirty = True
    if _gist_task is None or _gist_task.done():
        _gist_task = asyncio.create_task(_drain_gist_pushes())
    await _gist_task


def save_json(filepath: str, data: Any) -> None:
    tmp = filepath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, filepath)  # атомарная запись — файл не бьётся при падении
    schedule_gist_push()


def load_json(filepath: str, default: Any) -> Any:
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Не удалось прочитать %s: %s", filepath, e)
            return default
    save_json(filepath, default)
    return default


GROUPS: dict = {}  # темы хранятся отдельно для каждой группы
USER_SETTINGS: dict = {}
GUESTS: dict = {}
CURRENT_ACCOUNT: ContextVar[Optional[dict]] = ContextVar("current_account", default=None)
CURRENT_GUEST: ContextVar[Optional[int]] = ContextVar("current_guest", default=None)
TASKS: dict = {"next_id": 1, "items": []}
SCHEDULE: dict = {"next_id": 1, "items": []}
HISTORY: dict = {"items": []}
HISTORY_LIMIT = 150


def get_plugin_state(section: str, shared: dict, factory: Callable[[], dict]) -> dict:
    uid = CURRENT_GUEST.get()
    if uid is None:
        return shared
    guest = GUESTS.setdefault(str(uid), {})
    if not isinstance(guest.get(section), dict):
        guest[section] = factory()
    return guest[section]


def profile_plugin_state(section, shared, factory):
    state = get_plugin_state(section, shared, factory)
    account = CURRENT_ACCOUNT.get()
    uid = CURRENT_GUEST.get()
    accounts = USER_SETTINGS.get("accounts", {})
    legacy_id = next((aid for aid, acc in accounts.items() if uid is None or acc.get("tg_id") == uid), None)
    aid = account["id"] if account else legacy_id
    if isinstance(state.get(aid), dict):
        return state[aid]
    if aid == legacy_id or legacy_id is None:
        return state
    return state.setdefault(aid, factory())


def save_plugin_state(filepath: str, shared: dict) -> None:
    if CURRENT_GUEST.get() is None:
        save_json(filepath, shared)
    else:
        save_json(GUEST_FILE, GUESTS)


def load_state() -> None:
    GUESTS.clear()
    guests = load_json(GUEST_FILE, {})
    if isinstance(guests, dict):
        GUESTS.update(guests)
    GROUPS.clear()
    if os.path.exists(GROUPS_FILE):
        GROUPS.update(load_json(GROUPS_FILE, {}))
    else:  # миграция: старый topics.json -> основная группа
        legacy = {"английский": 9, "english": 9}
        if os.path.exists(TOPICS_FILE):
            legacy = load_json(TOPICS_FILE, legacy)
        GROUPS[str(TARGET_CHAT_ID)] = {"title": "Основная группа", "enabled": True, "topics": legacy}
        save_json(GROUPS_FILE, GROUPS)
    for cid in {TARGET_CHAT_ID, *ALLOWED_CHAT_IDS}:
        GROUPS.setdefault(str(cid), {"title": f"Группа {cid}", "enabled": True, "topics": {}})
    USER_SETTINGS.clear()
    USER_SETTINGS.update(load_json(SETTINGS_FILE, dict(DEFAULT_SETTINGS)))
    for k, v in DEFAULT_SETTINGS.items():
        USER_SETTINGS.setdefault(k, json.loads(json.dumps(v)))
    t = load_json(TASKS_FILE, {"next_id": 1, "items": []})
    TASKS["next_id"] = int(t.get("next_id", 1))
    TASKS["items"] = list(t.get("items", []))
    prune_tasks()
    sc = load_json(SCHEDULE_FILE, {"next_id": 1, "items": []})
    SCHEDULE["next_id"] = int(sc.get("next_id", 1))
    SCHEDULE["items"] = list(sc.get("items", []))
    HISTORY["items"] = list(load_json(HISTORY_FILE, {"items": []}).get("items", []))[-HISTORY_LIMIT:]
    for loader in PLUGIN_LOADERS:
        try:
            loader()
        except Exception:
            log.exception("plugin loader")


def log_history(src: str, who: str, prompt: str, answer: str) -> None:
    """Короткая запись о запросе (для вкладки «История» в мини-приложении)."""
    HISTORY["items"].append({"ts": time.time(), "acc": (CURRENT_ACCOUNT.get() or {}).get("id", ""), "src": src, "who": who[:40], "q": (prompt or "")[:600],
                             "a": (answer or "")[:1500]})
    del HISTORY["items"][:-HISTORY_LIMIT]
    save_json(HISTORY_FILE, HISTORY)


def is_group_enabled(cid: int) -> bool:
    return cid == TARGET_CHAT_ID or cid in ALLOWED_CHAT_IDS or bool(GROUPS.get(str(cid), {}).get("enabled"))


def ensure_group(chat) -> dict:
    """Запись о группе (создаём при первом сообщении, обновляем название)."""
    g = GROUPS.get(str(chat.id))
    changed = False
    if g is None:
        g = GROUPS[str(chat.id)] = {"title": "", "enabled": False, "topics": {}}
        changed = True
    g.setdefault("topics", {})
    title = getattr(chat, "title", None)
    if title and g.get("title") != title:
        g["title"] = title
        changed = True
    if changed:
        save_json(GROUPS_FILE, GROUPS)
    return g


def group_title(cid: int) -> str:
    return (GROUPS.get(str(cid)) or {}).get("title") or f"Группа {cid}"


def enabled_groups() -> list:
    return [(int(k), v) for k, v in GROUPS.items() if is_group_enabled(int(k))]


def topics_overview_html(user_id: Optional[int] = None) -> str:
    is_main = is_primary_user(user_id)
    groups = enabled_groups()
    if not groups:
        return "Подключённых групп пока нет."
    blocks = []
    for cid, g in groups:
        # Чужие группы и чужие личные чаты видны ТОЛЬКО владельцу
        if not can_see_chat(user_id, cid):
            continue
        items = "\n".join(f"  • <b>{esc(n)}</b> (ID: <code>{tid}</code>)"
                          for n, tid in g.get("topics", {}).items()) or "  тем пока нет"
        al = g.get("aliases") or []
        al_line = f"\n  🏷 псевдонимы: {esc(', '.join(al))}" if al else ""
        blocks.append(f"📂 <b>{esc(g.get('title') or cid)}</b> (<code>{cid}</code>){al_line}\n{items}")
    return "\n\n".join(blocks) if blocks else "У вас нет подключенных групп."


def tasks_scope(chat, user_id: Optional[int] = None) -> Optional[int]:
    """В личке у владельца видны задачи всех групп, у остальных — только их личный user_id."""
    if chat.type == "private":
        return None if is_primary_user(user_id or chat.id) else chat.id
    return chat.id


# =====================================================================
# 3. ПУЛ КЛЮЧЕЙ GEMINI С КАСКАДОМ МОДЕЛЕЙ И РОТАЦИЕЙ
# =====================================================================
class KeyPool:
    def __init__(self, keys: list):
        self.keys = keys
        self.idx = 0
        self.cooldown: dict = {}      # (key_idx, model) -> timestamp окончания паузы
        self.dead_keys: dict = {}     # key_idx -> timestamp (для невалидных ключей 401/403)
        self.dead_models: dict = {}   # model -> timestamp окончания бана (TTL 30 мин)
        self._clients: dict = {}
        self.last_used_model: str = GEMINI_MODEL
        self.stats: dict = {
            i: {"ok": 0, "quota": 0, "auth": 0, "transient": 0, "fatal": 0, "lat": 0.0, "last_model": ""}
            for i in range(len(keys))
        }

    def update_keys(self, new_keys: list) -> None:
        """Динамически обновляет ключи пула без перезапуска процесса."""
        if not new_keys:
            return
        self.keys = new_keys
        self._clients.clear()
        self.idx = 0
        self.cooldown.clear()
        self.dead_keys.clear()
        self.stats = {
            i: {"ok": 0, "quota": 0, "auth": 0, "transient": 0, "fatal": 0, "lat": 0.0, "last_model": ""}
            for i in range(len(new_keys))
        }

    def is_model_dead(self, model: str) -> bool:
        until = self.dead_models.get(model, 0)
        return until > time.time()

    def mark_model_dead(self, model: str, ttl: int = 1800) -> None:
        self.dead_models[model] = time.time() + ttl

    def record(self, i: int, kind: str, latency: float = 0.0, model: str = "") -> None:
        st = self.stats.get(i)
        if not st:
            return
        if kind in st:
            st[kind] += 1
        if kind == "ok":
            st["lat"] = latency if st["ok"] == 1 else st["lat"] * 0.8 + latency * 0.2
            if model:
                st["last_model"] = model
                self.last_used_model = model

    def report(self) -> str:
        now = time.time()
        lines = []
        active_m = get_current_account_settings().get("preferred_model") or GEMINI_MODEL
        for i, st in self.stats.items():
            pause = max(
                int(self.dead_keys.get(i, 0) - now),
                int(self.cooldown.get((i, active_m), 0) - now),
                0,
            )
            errs = ", ".join(f"{k}:{st[k]}" for k in ("quota", "auth", "transient", "fatal") if st[k])
            m_tag = f" · 🤖 {st['last_model']}" if st.get("last_model") else ""
            lines.append(
                f"#{i + 1} · ✅ {st['ok']}"
                + (f" · ⏱ {st['lat']:.1f} c" if st["ok"] else "")
                + m_tag
                + (f" · ⚠️ {errs}" if errs else "")
                + (f" · ⏸ {pause} c" if pause else "")
            )
        return "\n".join(lines)

    def client(self, i: int) -> genai.Client:
        if i not in self._clients:
            # Отключаем встроенные повторы SDK (attempts=1), чтобы 429 не зависал на 22 секунды!
            try:
                http_kwargs: dict = {"timeout": max(REQUEST_TIMEOUT_SEC, 90) * 1000}
                if hasattr(genai_types, "HttpRetryOptions"):
                    http_kwargs["retry_options"] = genai_types.HttpRetryOptions(attempts=1)
                self._clients[i] = genai.Client(
                    api_key=self.keys[i],
                    http_options=genai_types.HttpOptions(**http_kwargs),
                )
            except Exception:
                self._clients[i] = genai.Client(api_key=self.keys[i])
        return self._clients[i]

    def get_model_order(self, pref_model: Optional[str] = None, allow_fallback: bool = True) -> list:
        pref = (pref_model or get_current_account_settings().get("preferred_model") or GEMINI_MODEL).strip()
        if pref in MODEL_CASCADE:
            idx = MODEL_CASCADE.index(pref)
            order = MODEL_CASCADE[idx:] + MODEL_CASCADE[:idx]
        else:
            order = [pref] + MODEL_CASCADE
        if not allow_fallback:
            return [pref]
        live_models = [m for m in order if not self.is_model_dead(m)]
        return live_models or [pref]

    def pick_for_model(self, model: str) -> Optional[int]:
        now = time.time()
        n = len(self.keys)
        for off in range(n):
            i = (self.idx + off) % n
            if self.dead_keys.get(i, 0) <= now and self.cooldown.get((i, model), 0) <= now:
                self.idx = (i + 1) % n  # сразу сдвигаем указатель на следующий проект (Round-Robin)
                return i
        return None

    def pick(self, pref_model: Optional[str] = None) -> Optional[int]:
        for m in self.get_model_order(pref_model=pref_model):
            idx = self.pick_for_model(m)
            if idx is not None:
                return idx
        return 0 if self.keys else None

    def penalize(self, i: int, seconds: int, model: Optional[str] = None, auth_fail: bool = False) -> None:
        until = time.time() + seconds
        if auth_fail:
            self.dead_keys[i] = until
        elif model:
            self.cooldown[(i, model)] = until
        else:
            for m in MODEL_CASCADE:
                self.cooldown[(i, m)] = until
        log.info(
            "🔄 Ключ #%d [%s] на паузе %d c -> переключаюсь дальше",
            i + 1, model or "все", seconds,
        )

    def min_wait(self, pref_model: Optional[str] = None) -> int:
        now = time.time()
        waits = []
        for m in self.get_model_order(pref_model=pref_model):
            for i in range(len(self.keys)):
                w = max(self.dead_keys.get(i, 0) - now, self.cooldown.get((i, m), 0) - now)
                if w <= 0:
                    return 0  # Есть полностью свободная пара (ключ, модель)!
                waits.append(int(w))
        return min(waits) if waits else 0


pool = KeyPool(GEMINI_KEYS)


def classify_error(e: Exception) -> str:
    """quota (429) | auth (401/403) | not_found (404) | transient (5xx/сеть) | fatal."""
    s = str(e)
    up = s.upper()
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if code == 429 or "RESOURCE_EXHAUSTED" in up or "QUOTA" in up or "RATE_LIMIT" in up:
        return "quota"
    if code == 404 or "NOT_FOUND" in up or "IS NOT FOUND" in up or "NOT SUPPORTED" in up:
        return "not_found"
    if code in (401, 403) or any(x in up for x in (
            "UNAUTHENTICATED", "API_KEY_INVALID", "API KEY NOT VALID",
            "PERMISSION_DENIED", "API KEY EXPIRED")):
        return "auth"
    if code in (500, 502, 503, 504) or any(x in up for x in (
            "UNAVAILABLE", "OVERLOADED", "DEADLINE_EXCEEDED", "INTERNAL")) \
            or isinstance(e, (asyncio.TimeoutError, aiohttp.ClientError)):
        return "transient"
    return "fatal"


# =====================================================================
# 4. ФОРМАТИРОВАНИЕ: Markdown (от Gemini) -> безопасный Telegram HTML
# =====================================================================
_CODE_BLOCK = re.compile(r"```[ \t]*[\w+#.-]*\n?(.*?)```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
RECENT_ANSWERS: dict = {}


def make_response_action_kb(msg_id: int) -> InlineKeyboardMarkup:
    """Инлайн-кнопки под ответом в ЛС: сделать красивый PDF, Word, LaTeX или открыть в приложении."""
    url = webapp_url()
    rows = [
        [
            InlineKeyboardButton(text="📄 PDF", callback_data=f"doc_pdf:{msg_id}"),
            InlineKeyboardButton(text="📝 Word", callback_data=f"doc_docx:{msg_id}"),
        ], [
            InlineKeyboardButton(text="📐 LaTeX (.tex)", callback_data=f"doc_tex:{msg_id}"),
            InlineKeyboardButton(text="Собрать TeX → PDF", callback_data=f"doc_texpdf:{msg_id}"),
        ]
    ]
    if url:
        rows.append([InlineKeyboardButton(text="📱 Открыть в приложении", web_app=WebAppInfo(url=url))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_markdown_tables(text: str) -> str:
    """Находит блоки таблиц Markdown и превращает их в моноширинный отформатированный текст."""
    if not text or "|" not in text:
        return text
    lines = text.split("\n")
    out = []
    in_table = False
    table_lines = []

    def flush_table():
        nonlocal table_lines
        if not table_lines:
            return
        if len(table_lines) >= 2:
            rows = []
            for tl in table_lines:
                if re.match(r"^\|?[\s\-:|]+\|?$", tl.strip()):
                    continue
                cells = [c.strip() for c in tl.strip("|").split("|")]
                rows.append(cells)
            if rows:
                num_cols = max(len(r) for r in rows)
                for r in rows:
                    while len(r) < num_cols:
                        r.append("")
                col_widths = [0] * num_cols
                for r in rows:
                    for idx, c in enumerate(r):
                        col_widths[idx] = max(col_widths[idx], len(c))
                tbl_out = []
                for r_idx, r in enumerate(rows):
                    line_str = " | ".join(c.ljust(col_widths[i]) for i, c in enumerate(r))
                    tbl_out.append(line_str)
                    if r_idx == 0:
                        tbl_out.append("-+-".join("-" * col_widths[i] for i in range(num_cols)))
                formatted_tbl = "\n".join(tbl_out)
                out.append(f"```text\n{formatted_tbl}\n```")
            else:
                out.extend(table_lines)
        else:
            out.extend(table_lines)
        table_lines = []

    for line in lines:
        if "|" in line and (line.strip().startswith("|") or line.strip().endswith("|") or " | " in line):
            in_table = True
            table_lines.append(line)
        else:
            if in_table:
                flush_table()
                in_table = False
            out.append(line)
    if in_table:
        flush_table()

    return "\n".join(out)


def md_to_html(text: str) -> str:
    """
    Конвертирует типичный Markdown от LLM в HTML, который понимает Telegram.
    Весь «сырой» текст экранируется, поэтому символы _ * [ ] ~ < > & не ломают разметку.
    """
    if not text:
        return ""
    text = text.replace("\x00", "")

    # 0. Таблицы -> аккуратные текстовые блоки кода
    text = format_markdown_tables(text)

    stash: list = []

    def hold(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    def _code_repl(m):
        lang = (m.group(1) or "").strip()
        code_content = esc(m.group(2).rstrip())
        if lang:
            return hold(f'<pre><code class="language-{esc(lang)}">{code_content}</code></pre>')
        return hold(f"<pre>{code_content}</pre>")

    text = re.sub(r"```[ \t]*([\w+#.-]*)\n?(.*?)```", _code_repl, text, flags=re.S)
    text = _INLINE_CODE.sub(lambda m: hold(f"<code>{esc(m.group(1))}</code>"), text)
    text = re.sub(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]|\\\((.+?)\\\)|\$(?!\s)([^$\n]+?)(?<!\s)\$",
                  lambda m: tex_plain(next(g for g in m.groups() if g is not None)), text, flags=re.S)
    text = esc(text)

    text = re.sub(r"(?m)^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", "──────────", text)          # ---
    text = re.sub(r"(?m)^#{1,6}\s+(.+?)\s*#*\s*$", r"<b>\1</b>", text)                 # # Заголовок
    text = re.sub(r"(?m)^(\s*)[*+-]\s+", r"\1• ", text)                                # списки
    # Безопасный жирный и курсив (не переходят через пустые строки!)
    text = re.sub(r"\*\*([^\n]+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__([^\n]+?)__", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r'\[([^\]\n]+)\]\((https?://[^\s)"]+)\)', r'<a href="\2">\1</a>', text)  # [t](url)

    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)


def strip_md(text: str) -> str:
    """Запасной вариант: убираем разметку и шлём чистым текстом."""
    return re.sub(r"\*\*|__|```|`", "", text or "")


def split_message(text: str, limit: int = CHUNK_LIMIT) -> list:
    """Режем по абзацам -> строкам -> словам; блоки кода ``` не остаются незакрытыми."""
    text = (text or "").strip() or "…"
    if len(text) <= limit:
        return [text]

    parts: list = []  # (кусок, разделитель перед ним)
    for bi, block in enumerate(text.split("\n\n")):
        sep = "\n\n" if bi else ""
        if len(block) <= limit:
            parts.append((block, sep))
            continue
        for li, line in enumerate(block.split("\n")):
            lsep = sep if li == 0 else "\n"
            while len(line) > limit:
                cut = line.rfind(" ", 0, limit)
                cut = cut if cut > limit // 2 else limit
                parts.append((line[:cut], lsep))
                lsep = "\n"
                line = line[cut:].lstrip()
            parts.append((line, lsep))

    chunks: list = []
    cur = ""
    for piece, sep in parts:
        if cur and len(cur) + len(sep) + len(piece) > limit:
            chunks.append(cur)
            cur = piece
        else:
            cur = f"{cur}{sep}{piece}" if cur else piece
    if cur:
        chunks.append(cur)

    fixed: list = []
    carry = False
    for c in chunks:
        if carry:
            c = "```\n" + c
        if c.count("```") % 2 == 1:
            c += "\n```"
            carry = True
        else:
            carry = False
        fixed.append(c)
    return fixed


async def _try_send(send: Callable, text: str, pm: Optional[str]):
    for _ in range(3):
        try:
            return await send(text, pm)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
    return await send(text, pm)


async def send_md(send: Callable, md_text: str, depth: int = 0):
    """send(text, parse_mode). HTML -> при отказе Telegram чистый текст. Никаких 'can't parse entities'."""
    html_text = md_to_html(md_text)
    if len(html_text) > TG_LIMIT and depth < 4 and len(md_text) > 200:
        for part in split_message(md_text, max(200, len(md_text) // 2 + 50)):
            await send_md(send, part, depth + 1)
        return
    try:
        return await _try_send(send, html_text[:TG_LIMIT], ParseMode.HTML)
    except TelegramBadRequest as e:
        log.warning("HTML отклонён Telegram (%s) — отправляю без разметки", e)
    return await _try_send(send, strip_md(md_text)[:TG_LIMIT], None)


async def reply_md(message: types.Message, text_md: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> list:
    """Ответ Markdown-текстом: режет на части и отправляет цепочкой, прикрепляя кнопки к последнему сообщению."""
    chunks = split_message(text_md, limit=CHUNK_LIMIT)
    sent_msgs = []
    for i, chunk in enumerate(chunks):
        is_last = (i == len(chunks) - 1)
        kb = reply_markup if is_last else None
        fn = message.reply if i == 0 else message.answer
        msg = await send_md(lambda t, pm, fn=fn, kb=kb: fn(t, parse_mode=pm, reply_markup=kb), chunk)
        if msg:
            sent_msgs.append(msg)
    return sent_msgs


async def safe_edit(msg: types.Message, text: str, kb: Optional[InlineKeyboardMarkup] = None) -> None:
    try:
        await msg.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            raise


# =====================================================================
# 5. ИНДИКАТОР «печатает… / отправляет документ…»
# =====================================================================
@asynccontextmanager
async def chat_action(chat_id: int, action: str = ChatAction.TYPING, thread_id: Optional[int] = None):
    async def _loop():
        while True:
            with suppress(Exception):
                await bot.send_chat_action(chat_id=chat_id, action=action, message_thread_id=thread_id)
            await asyncio.sleep(4.5)  # статус живёт ~5 c — обновляем чуть чаще

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


# =====================================================================
# 6. ПАМЯТЬ ВЕТОК: скользящее окно + лимиты по вложениям (экономим RAM на 512 МБ)
# =====================================================================
MEMORY: "OrderedDict[tuple, list]" = OrderedDict()
MEDIA_TYPES = ("part", "file", "doc")

# Последние присланные файлы (только Telegram file_id) — чтобы прикреплять их к публикациям в топики.
LAST_FILES: "OrderedDict[tuple, list]" = OrderedDict()


def remember_file(key: tuple, kind: str, file_id: str, name: str) -> None:
    lst = LAST_FILES.setdefault(key, [])
    LAST_FILES.move_to_end(key)
    lst.append({"kind": kind, "file_id": file_id, "name": name})
    del lst[:-5]
    while len(LAST_FILES) > MAX_THREADS_IN_RAM:
        LAST_FILES.popitem(last=False)


def last_files(key: tuple, n: int = 5) -> list:
    return list(LAST_FILES.get(key, []))[-n:]


def last_file(key: tuple) -> Optional[dict]:
    lst = LAST_FILES.get(key)
    return lst[-1] if lst else None


def mem_key(message: types.Message) -> tuple:
    return (message.chat.id, message.message_thread_id or 0)


def _release(item: dict) -> None:
    """Освобождаем вложение: байты уходят из RAM, файл в Gemini File API удаляется."""
    fobj = item.pop("file_obj", None)
    item.pop("part", None)
    if fobj is not None and getattr(fobj, "name", None):
        async def _del():
            with suppress(Exception):
                await pool.client(item.get("key_idx", 0)).aio.files.delete(name=fobj.name)
        with suppress(RuntimeError):
            asyncio.get_running_loop().create_task(_del())


def mem_items(key: tuple) -> list:
    if key in MEMORY:
        MEMORY.move_to_end(key)
        return MEMORY[key]
    return []


def mem_add(key: tuple, item: dict) -> None:
    item.setdefault("ts", time.time())
    items = MEMORY.setdefault(key, [])
    MEMORY.move_to_end(key)
    items.append(item)

    # 1) окно текстовых реплик
    texts = [it for it in items if it["type"] == "text"]
    for old in texts[:-HISTORY_WINDOW] if len(texts) > HISTORY_WINDOW else []:
        items.remove(old)
    # 2) лимит вложений на ветку
    media = [it for it in items if it["type"] in MEDIA_TYPES]
    for old in media[:-MAX_MEDIA_PER_THREAD] if len(media) > MAX_MEDIA_PER_THREAD else []:
        items.remove(old)
        _release(old)
    # 3) общий бюджет RAM на вложения
    while True:
        all_media = [(it["ts"], k, it) for k, lst in MEMORY.items() for it in lst if it["type"] in MEDIA_TYPES]
        if sum(it.get("size", 0) for _, _, it in all_media) <= MEDIA_BUDGET_BYTES or len(all_media) <= 1:
            break
        _, k, oldest = min(all_media, key=lambda x: x[0])
        MEMORY[k].remove(oldest)
        _release(oldest)
    # 4) лимит числа веток в RAM
    while len(MEMORY) > MAX_THREADS_IN_RAM:
        _, dropped = MEMORY.popitem(last=False)
        for it in dropped:
            if it["type"] in MEDIA_TYPES:
                _release(it)


def mem_clear(key: tuple) -> bool:
    LAST_FILES.pop(key, None)
    items = MEMORY.pop(key, None)
    if items is None:
        return False
    for it in items:
        if it["type"] in MEDIA_TYPES:
            _release(it)
    return True


def build_contents(history: list, key_idx: int, user_text: str, extra_parts: Optional[list]) -> list:
    contents: list = []
    for it in history:
        t = it["type"]
        if t in ("text", "doc"):
            contents.append(it["text"])
        elif t == "part" and it.get("part") is not None:
            contents.append(it["part"])
        elif t == "file" and it.get("file_obj") is not None:
            # файл из File API привязан к проекту конкретного ключа
            if it.get("key_idx") == key_idx:
                contents.append(it["file_obj"])
            else:
                contents.append(f"[Файл «{it.get('name', '?')}» недоступен при текущем ключе API]")
    if extra_parts:
        contents.extend(extra_parts)
    contents.append(f"Запрос пользователя: {user_text}")
    return contents


# =====================================================================
# 7. ЗАДАЧИ / ДЕДЛАЙНЫ (tasks.json)
# =====================================================================
def now_local() -> datetime:
    return datetime.now(TZ)


def parse_deadline(raw: Optional[str]) -> Optional[datetime]:
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            d = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if "%H" not in fmt:
            d = d.replace(hour=23, minute=59)
        return d.replace(tzinfo=TZ)
    return None


def prune_tasks() -> None:
    """Выполненные задачи старше 30 дней удаляем, чтобы файл не рос."""
    limit = time.time() - 30 * 86400
    TASKS["items"] = [t for t in TASKS["items"] if not (t.get("done") and t.get("done_at", 0) < limit)]


def add_task(chat_id: int, topic: str, thread_id: Optional[int], text: str, deadline: str, file_name: str = "",
             kind: str = "task", acc_id: str = "", user_id: Optional[int] = None) -> dict:
    task = {
        "id": TASKS["next_id"], "chat_id": chat_id, "topic": topic, "thread_id": thread_id, "text": text,
        "deadline": (deadline or "").strip(), "created": time.time(), "done": False, "file": file_name,
        "kind": kind, "acc": acc_id, "user_id": user_id or (chat_id if chat_id > 0 else 0),
    }
    TASKS["next_id"] += 1
    TASKS["items"].append(task)
    prune_tasks()
    save_json(TASKS_FILE, TASKS)
    return task


# ---------- планировщик напоминаний (ежедневные, «за N минут до», таймеры) ----------
def add_notification(task_id: int, ts: float, kind: str, chat_id: int, thread_id: Optional[int]) -> None:
    SCHEDULE["items"].append({"id": SCHEDULE["next_id"], "task_id": task_id, "ts": ts, "kind": kind,
                              "chat_id": chat_id, "thread_id": thread_id or 0})
    SCHEDULE["next_id"] += 1


def cancel_notifications(task_id: int) -> None:
    before = len(SCHEDULE["items"])
    SCHEDULE["items"] = [n for n in SCHEDULE["items"] if n["task_id"] != task_id]
    if len(SCHEDULE["items"]) != before:
        save_json(SCHEDULE_FILE, SCHEDULE)


def has_notifications(task_id: int) -> bool:
    return any(n["task_id"] == task_id for n in SCHEDULE["items"])


def _tz_name() -> str:
    key = getattr(TZ, "key", "UTC")
    return "МСК" if key == "Europe/Moscow" else key


def fmt_minutes(m: int) -> str:
    if m >= 60:
        h, r = divmod(m, 60)
        return f"{h} ч" + (f" {r} мин" if r else "")
    return f"{m} мин"


def plan_task_notifications(task: dict, dest: tuple, daily: bool, before: list) -> str:
    """Каждое утро до дедлайна; в день дедлайна — утром и за 3 часа; плюс «за N минут». Время — по BOT_TZ."""
    chat_id, thread_id = dest
    dl = parse_deadline(task.get("deadline"))
    if not dl:
        return "\n⚠️ Срок не указан — напоминания не запланированы. Назови срок, и я настрою их."
    now = now_local()
    before_list = set(before)
    if daily:
        before_list.add(180)
    before_times = {m: dl - timedelta(minutes=m) for m in before_list}
    cands = []  # (время, тип)
    for m, t in before_times.items():
        if t > now:
            cands.append((t, f"before{m}"))
    if daily:
        d = now.date()
        first_morning = datetime(d.year, d.month, d.day, DAILY_HOUR, 0, tzinfo=TZ)
        day = d if now < first_morning else d + timedelta(days=1)
        while day <= dl.date():
            morning = datetime(day.year, day.month, day.day, DAILY_HOUR, 0, tzinfo=TZ)
            near_before = any(abs((morning - t).total_seconds()) < 1200 for t in before_times.values())
            if morning < dl and not near_before:
                cands.append((morning, "daily"))
            day += timedelta(days=1)
    if not cands:
        return "\n⚠️ Подходящих моментов для напоминаний не осталось (срок слишком близко)."
    cands.sort(key=lambda x: x[0])
    for t, kind in cands:
        add_notification(task["id"], t.timestamp(), kind, chat_id, thread_id)
    save_json(SCHEDULE_FILE, SCHEDULE)
    return (f"\n🔔 Запланировано напоминаний: {len(cands)} "
            f"(ближайшее: {cands[0][0]:%d.%m %H:%M} {_tz_name()}, последнее: {cands[-1][0]:%d.%m %H:%M})")


def plan_timer(task: dict, dest: tuple, when: datetime, before: list) -> str:
    chat_id, thread_id = dest
    now = now_local()
    planned = []
    for m in sorted(set(before), reverse=True):
        t = when - timedelta(minutes=m)
        if t > now:
            add_notification(task["id"], t.timestamp(), f"before{m}", chat_id, thread_id)
            planned.append(f"за {fmt_minutes(m)} ({t:%H:%M})")
    add_notification(task["id"], when.timestamp(), "timer_due", chat_id, thread_id)
    save_json(SCHEDULE_FILE, SCHEDULE)
    extra = f"; предупреждения: {', '.join(planned)}" if planned else ""
    return f"сработает {when:%d.%m %H:%M} {_tz_name()}{extra}"


def notification_text(task: dict, kind: str) -> str:
    body = task["text"].strip()
    if len(body) > 700:
        body = body[:700] + "…"
    is_timer = task.get("kind") == "timer"
    dl = parse_deadline(task.get("deadline"))
    footer = "" if is_timer else "\n\n✅ Выполнил? Напиши: «Гемини, задание выполнено»."
    if kind == "timer_due":
        return f"⏰ **Время!**\n\n{body}"
    if kind.startswith("before"):
        try:
            left = fmt_minutes(int(kind[6:]))
        except ValueError:
            left = "некоторое время"
        if is_timer:
            return f"⏳ **Через {left}:** {body}"
        return f"⏰ **До дедлайна {left}!**\n\n{body}{footer}"
    # daily
    if dl:
        days = (dl.date() - now_local().date()).days
        if days <= 0:
            head = f"🚨 **Дедлайн сегодня — до {dl:%H:%M}!**"
        else:
            head = f"🔔 **Напоминание: осталось {days} дн.** (срок {dl:%d.%m.%Y})"
    else:
        head = "🔔 **Напоминание**"
    return f"{head}\n\n{body}{footer}"


async def fire_due() -> None:
    now = time.time()
    changed = False
    for n in list(SCHEDULE["items"]):
        if n["ts"] > now:
            continue
        SCHEDULE["items"].remove(n)
        changed = True
        task = next((t for t in TASKS["items"] if t["id"] == n["task_id"]), None)
        if not task or task.get("done"):
            continue
        if now - n["ts"] > 3 * 3600:  # бот спал/был выключен — устаревшее не шлём
            log.info("Пропускаю просроченное напоминание #%s (опоздание > 3 ч)", n["id"])
            continue
        text = notification_text(task, n["kind"])
        try:
            await send_md(
                lambda t, pm, n=n: bot.send_message(chat_id=n["chat_id"], message_thread_id=n.get("thread_id") or None,
                                                    text=t, parse_mode=pm),
                text)
        except Exception as e:
            log.warning("Напоминание #%s не отправлено: %s", n["id"], e)
            n["tries"] = n.get("tries", 0) + 1
            if n["tries"] < 3:
                n["ts"] = now + 120
                SCHEDULE["items"].append(n)
    if changed:
        save_json(SCHEDULE_FILE, SCHEDULE)


async def scheduler_loop() -> None:
    while True:
        try:
            await fire_due()
        except Exception:
            log.exception("scheduler error")
        for tick in PLUGIN_TICKS:
            try:
                await tick()
            except Exception:
                log.exception("plugin tick")
        await asyncio.sleep(30)


# ---------- навигация по задачам: группа › тема › задачи ----------
def chat_label(cid: int) -> str:
    if str(cid) in GROUPS:
        return group_title(cid)
    return "Личные напоминания" if cid > 0 else f"Чат {cid}"


def active_tasks(cid: Optional[int] = None, thread: Optional[int] = None) -> list:
    out = []
    for t in TASKS["items"]:
        if t.get("done"):
            continue
        if cid is not None and t.get("chat_id", TARGET_CHAT_ID) != cid:
            continue
        if thread is not None and (t.get("thread_id") or 0) != thread:
            continue
        out.append(t)
    return out


def topic_label(cid: int, thread: int, tasks: Optional[list] = None) -> str:
    for t in tasks or []:
        if t.get("topic"):
            return t["topic"]
    for name, tid in (GROUPS.get(str(cid)) or {}).get("topics", {}).items():
        if tid == thread:
            return name
    return "общая" if not thread else f"тема {thread}"


def _short(text: str, n: int = 40) -> str:
    return text if len(text) <= n else text[:n - 1] + "…"


def done_kb(task_id: int, n: int = 0) -> InlineKeyboardMarkup:
    label = "✅ Я сделал(а)" + (f" · {n}" if n else "")
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=f"dn_{task_id}")]])


def render_tasks_root(scope_chat: Optional[int] = None, user_id: Optional[int] = None) -> tuple:
    is_main = is_primary_user(user_id)
    if scope_chat is not None:
        return render_tasks_topics(scope_chat, False)

    # Обычным пользователям показываем только личные задачи (cid > 0), главному — все
    cids = [cid for cid, _ in enabled_groups() if can_see_chat(user_id, cid)]
    for t in active_tasks():
        c = t.get("chat_id", TARGET_CHAT_ID)
        if c not in cids and can_see_chat(user_id, c):
            cids.append(c)

    rows = []
    for cid in cids:
        if not can_see_chat(user_id, cid):
            continue
        n = len(active_tasks(cid))
        if n:
            rows.append([InlineKeyboardButton(text=f"📂 {_short(chat_label(cid))} ({n})", callback_data=f"tk_t_{cid}")])
    if not rows:
        return ("📋 <b>Активных задач нет.</b>\nКогда я опубликую напоминание или поставлю таймер, оно появится здесь.", None)
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="tk_g")])
    return "📋 <b>Задачи по группам.</b> Выбери группу:", InlineKeyboardMarkup(inline_keyboard=rows)


def render_tasks_topics(cid: int, private: bool) -> tuple:
    items = active_tasks(cid)
    back = [InlineKeyboardButton(text="⬅️ К группам", callback_data="tk_g")] if private else []
    if not items:
        return (f"📂 <b>{esc(chat_label(cid))}</b>\nАктивных задач нет.",
                InlineKeyboardMarkup(inline_keyboard=[back]) if back else None)
    by_thread: dict = {}
    for t in items:
        by_thread.setdefault(t.get("thread_id") or 0, []).append(t)
    rows = []
    for thread, ts in sorted(by_thread.items(), key=lambda kv: kv[0]):
        rows.append([InlineKeyboardButton(text=f"🏷 {_short(topic_label(cid, thread, ts), 30)} ({len(ts)})",
                                          callback_data=f"tk_l_{cid}_{thread}")])
    rows.append(back + [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"tk_t_{cid}")])
    return f"📂 <b>{esc(chat_label(cid))}</b>\nВыбери тему:", InlineKeyboardMarkup(inline_keyboard=rows)


def render_tasks_list(cid: int, thread: int, private: bool) -> tuple:
    items = active_tasks(cid, thread)
    if not items:
        return render_tasks_topics(cid, private)
    far = datetime(9999, 12, 31, tzinfo=TZ)
    items.sort(key=lambda t: (parse_deadline(t.get("deadline")) is None,
                              parse_deadline(t.get("deadline")) or far, t["id"]))
    shown = items[:15]
    lines = []
    for t in shown:
        raw = t.get("deadline")
        dl = ""
        if raw:
            d = parse_deadline(raw)
            if d:
                label = d.strftime("%d.%m.%Y" if len(raw) <= 10 else "%d.%m.%Y %H:%M")
                dl = f" · {'🔴 просрочено, было до' if d < now_local() else '⏰ до'} {label}"
            else:
                dl = f" · ⏰ {esc(raw)}"
        snippet = re.sub(r"\s+", " ", strip_md(t["text"])).strip()
        snippet = snippet[:120] + ("…" if len(snippet) > 120 else "")
        clip = f"\n📎 {esc(t['file'])}" if t.get("file") else ""
        if t.get("done_by"):
            names = list(t["done_by"].values())
            clip += f"\n👥 Сдали ({len(names)}): {esc(', '.join(names[:6]))}" + ("…" if len(names) > 6 else "")
        marks = ("⏲ " if t.get("kind") == "timer" else "") + ("🔔 " if has_notifications(t["id"]) else "")
        lines.append(f"<b>#{t['id']}</b> {marks}{dl.lstrip(' ·')}\n{esc(snippet)}{clip}")
    head = f"📋 <b>{esc(chat_label(cid))} › {esc(topic_label(cid, thread, shown))}</b>\n\n"
    text = head + "\n\n".join(lines)
    if len(items) > len(shown):
        text += f"\n\n…и ещё {len(items) - len(shown)}"
    buttons = [InlineKeyboardButton(text=f"✅ #{t['id']}", callback_data=f"td_{t['id']}_{cid}_{thread}") for t in shown]
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    rows.append([InlineKeyboardButton(text="⬅️ К темам", callback_data=f"tk_t_{cid}"),
                 InlineKeyboardButton(text="🔄 Обновить", callback_data=f"tk_l_{cid}_{thread}")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def render_tasks(scope_chat: Optional[int] = None) -> tuple:
    return render_tasks_root(scope_chat)


# =====================================================================
# 8. ВСПОМОГАТЕЛЬНОЕ: файлы, картинки, доступ
# =====================================================================
def shrink_image(raw: bytes) -> bytes:
    """Ресайз до 1600 px по большей стороне + JPEG q85 — экономит токены и трафик."""
    with Image.open(io.BytesIO(raw)) as img:
        img = ImageOps.exif_transpose(img)
        if max(img.size) > IMG_MAX_SIDE:
            img.thumbnail((IMG_MAX_SIDE, IMG_MAX_SIDE), Image.Resampling.LANCZOS)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, "JPEG", quality=85, optimize=True)
        return out.getvalue()


def docx_to_text(data: bytes) -> str:
    """Достаём текст из .docx без сторонних библиотек."""
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        info = z.getinfo("word/document.xml")
        if info.file_size > 30 * 1024 * 1024:
            raise ValueError("DOCX слишком большой для разбора")
        root = ET.fromstring(z.read("word/document.xml"))
    paras = ["".join(t.text or "" for t in p.iter(f"{ns}t")) for p in root.iter(f"{ns}p")]
    return "\n".join(paras).strip()


def clean_filename(filename: str) -> str:
    return re.sub(r'[\r\n\\/*?:"<>|]', "_", filename or "file").strip()[:80]


TEXT_EXT = (".txt", ".md", ".csv", ".json", ".py", ".js", ".html", ".xml", ".tex", ".log", ".c", ".cpp", ".java")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")


async def download_bytes(file_id: str) -> bytes:
    buf = io.BytesIO()
    await bot.download(file_id, destination=buf)
    return buf.getvalue()


def is_admin(user_id: Optional[int]) -> bool:
    return not ADMIN_IDS or (user_id is not None and user_id in ADMIN_IDS)


def is_primary_user(user_id: Optional[int]) -> bool:
    """Проверяет, является ли пользователь главным владельцем бота."""
    if not user_id:
        return False
    if ADMIN_IDS and user_id in ADMIN_IDS:
        return True
    # Проверяем по первому сохраненному аккаунту в базе профилей
    accs = USER_SETTINGS.get("accounts", {})
    if accs:
        first_acc = next(iter(accs.values()))
        if int(first_acc.get("tg_id") or 0) == user_id:
            return True
    # Если это самый первый запуск и аккаунтов еще нет
    if not accs and not ADMIN_IDS:
        return True
    return False


GUEST_CHECK = None  # ставится из ext_webapp: функция uid -> есть ли у гостя свой API-ключ


def account_owns_task(account, task):
    if not account:
        return False
    if task.get("acc"):
        return task["acc"] == account["id"]
    return account["id"] == next(iter(USER_SETTINGS.get("accounts", {})), None)


def can_see_chat(user_id: Optional[int], cid: int) -> bool:
    """Владелец видит все чаты; остальные — только свой личный (cid == их Telegram ID)."""
    return is_primary_user(user_id) or (user_id is not None and cid == user_id)


def msg_is_admin(message: types.Message) -> bool:
    """Анонимный админ группы (сообщение от имени чата) считается админом."""
    if message.sender_chat and message.sender_chat.id == message.chat.id:
        return True
    return is_admin(message.from_user.id if message.from_user else None)


def is_allowed(message: types.Message) -> bool:
    """Группа — только целевая; личка — только админы (если ADMIN_IDS задан)."""
    if message.chat.type == "private":
        uid = message.from_user.id if message.from_user else None
        if is_admin(uid):
            return True
        return bool(uid and GUEST_CHECK and GUEST_CHECK(uid))  # гость: только свой ключ, без чужих данных
    if not is_group_enabled(message.chat.id):
        return False
    ensure_group(message.chat)
    return True


async def sender_can_manage(message: types.Message) -> bool:
    """Кто может подключать группы и привязывать темы: админы бота / админы группы."""
    if message.sender_chat and message.sender_chat.id == message.chat.id:
        return True  # анонимный админ группы
    uid = message.from_user.id if message.from_user else None
    if ADMIN_IDS:
        return uid in ADMIN_IDS
    if uid is None:
        return False
    with suppress(Exception):
        member = await bot.get_chat_member(message.chat.id, uid)
        return member.status in ("administrator", "creator")
    return False


BOT_USERNAME = ""
BOT_ID = 0
TRIGGER_RE = re.compile(r"^\s*(?:гемини|gemini)\b[\s,:\-—]*", re.I)


def extract_query(text: str, is_group: bool) -> Optional[str]:
    """None — сообщение не адресовано боту."""
    if not is_group:
        return text.strip()
    m = TRIGGER_RE.match(text)
    if m:
        return text[m.end():].strip()
    if BOT_USERNAME and re.search(rf"@{re.escape(BOT_USERNAME)}\b", text, re.I):
        return re.sub(rf"@{re.escape(BOT_USERNAME)}\b", "", text, flags=re.I).strip(" ,:")
    return None


def caption_query(caption: str) -> str:
    q = extract_query(caption, True)
    return q if q is not None else caption.strip()


# =====================================================================
# 9. GEMINI: системный промпт, вызов с ротацией ключей, публикация в топики
# =====================================================================
def groups_block(scope: list, is_private: bool) -> str:
    """Описание групп и их тем для модели: темы разных групп не смешиваются."""
    if not scope:
        return "ГРУППЫ: подключённых групп пока нет."

    def topics_of(g: dict) -> str:
        return ", ".join(g.get("topics", {}).keys()) or "тем пока нет"

    if not is_private:
        cid, g = scope[0]
        return (f"ГРУППА: «{g.get('title') or cid}». ТЕМЫ ЭТОЙ ГРУППЫ: {topics_of(g)}. "
                "Публиковать можно только в эту группу.")
    def label(cid, g) -> str:
        al = g.get("aliases") or []
        return f"«{g.get('title') or cid}»" + (f" (также называется: {', '.join(al)})" if al else "")

    lines = [f"{i}) {label(cid, g)}: {topics_of(g)}" for i, (cid, g) in enumerate(scope, 1)]
    return ("ГРУППЫ И ИХ ТЕМЫ (темы разных групп разные, не путай их; публиковать можно в любую группу — "
            "если групп больше одной, ОБЯЗАТЕЛЬНО передавай group_name, а если непонятно какая — спроси):\n"
            + "\n".join(lines))


def tasks_block(scope: list) -> str:
    """Активные задачи (с номерами) для модели — чтобы она могла отметить нужную выполненной."""
    allowed = {cid for cid, _ in scope}
    items = [t for t in TASKS["items"]
             if not t.get("done") and t.get("chat_id", TARGET_CHAT_ID) in allowed][:30]
    if not items:
        return "АКТИВНЫЕ ЗАДАЧИ: нет."
    lines = []
    for t in items:
        grp = f"[{group_title(t.get('chat_id', TARGET_CHAT_ID))}] " if len(scope) > 1 else ""
        dl = f", срок {t['deadline']}" if t.get("deadline") else ""
        txt = t["text"].replace("\n", " ")[:80]
        lines.append(f"#{t['id']} {grp}тема «{t['topic']}»{dl}: {txt}")
    return "АКТИВНЫЕ ЗАДАЧИ:\n" + "\n".join(lines)


PERSONA_BLOCK = (
    "ХАРАКТЕР И МАНЕРА ОБЩЕНИЯ:\n"
    "- Говори как живой человек в переписке: тёпло, просто, на «ты». Подстраивайся под тон собеседника: "
    "на шутку можно ответить шуткой, на усталость — по-человечески поддержать, на сухой вопрос — ответить по делу.\n"
    "- На короткую или бытовую реплику отвечай коротко (1–3 предложения), без заголовков, списков и «простыней». "
    "Структуру и подробный разбор оставляй для учебных задач и просьб объяснить.\n"
    "- Не начинай с шаблонов вроде «Конечно!», «Отличный вопрос!», «Я рад помочь» и не заканчивай «Обращайся, если что». "
    "Сразу к сути. Не пересказывай вопрос заново.\n"
    "- Имя собеседника используй изредка и к месту, а не в каждом сообщении.\n"
    "- Эмодзи — редко и по настроению (не больше одного на сообщение), не как украшение каждой строки.\n"
    "- Можно иметь мнение: если спрашивают «как лучше?», выбери вариант и объясни почему, а не перечисляй всё подряд. "
    "Если не уверен или не знаешь — скажи прямо, без выдумок.\n"
    "- Помни, что было раньше в этом диалоге, и опирайся на это: не переспрашивай то, что уже говорили.\n"
    "- Если уместно, в конце предложи один конкретный следующий шаг или задай один встречный вопрос — но не всегда.\n"
    "- Живость не отменяет точности: в решениях задач, формулах, датах, дедлайнах и вызовах инструментов "
    "будь внимателен и аккуратен, тон — дружелюбный, содержание — строгое."
)


def build_system_instruction(scope: list, is_private: bool, current_topic: Optional[str] = None,
                             last_files_names: Optional[list] = None,
                             account: Optional[dict] = None,
                             user_name: Optional[str] = None) -> str:
    gblock = groups_block(scope, is_private)
    files_desc = ", ".join(f"{i}) {n}" for i, n in enumerate(last_files_names or [], 1)) or "нет"

    acc_settings = account or get_current_account_settings()
    answer_mode = acc_settings.get("answer_mode")
    if answer_mode == "short":
        mode = ("Краткий режим. Для каждой задачи дай ответ и при необходимости одну строку вычисления. "
                "Для вопроса по теории — определение и главную мысль в 1–3 предложениях.")
    else:
        mode = ("Подробный режим. Для учебной задачи назови идею решения и объясни выбор метода. "
                "Введи незнакомые обозначения, сформулируй нужную теорему и проверь её условия. "
                "Покажи промежуточные вычисления и обоснуй переходы, особенно сокращения и пределы. "
                "Проверь и сформулируй ответ. Для теории добавь небольшой разобранный пример.")

    user_prof = acc_settings.get('user_profile', '')
    user_inst = acc_settings.get('custom_instructions', '')
    user_readme = acc_settings.get('user_readme', '')
    readme_block = f"\nДОСЬЕ ПОЛЬЗОВАТЕЛЯ:\n{user_readme}\n" if user_readme else ""
    name_hint = f"Имя собеседника: {user_name}.\n" if user_name else ""

    base = (
        "Ты — личный помощник по учёбе в Telegram. Не «сервис» и не справочник, а толковый друг-однокурсник, "
        "который разбирается в предмете и общается по-человечески.\n"
        f"Сейчас: {now_local():%Y-%m-%d %H:%M} ({getattr(TZ, 'key', 'UTC')}).\n\n"
        f"{PERSONA_BLOCK}\n\n"
        f"{name_hint}"
        f"ИНФОРМАЦИЯ О ПОЛЬЗОВАТЕЛЕ:\n{user_prof}\n\n"
        f"ПЕРСОНАЛЬНЫЕ ИНСТРУКЦИИ:\n{user_inst}\n"
        f"{readme_block}\n"
        f"{mode}\n\n"
        f"{gblock}\n"
        f"ТЕКУЩАЯ ТЕМА: {current_topic or 'не определена'}.\n"
        f"ПОСЛЕДНИЕ ФАЙЛЫ/ФОТО В ЧАТЕ: {files_desc}.\n"
        f"{tasks_block(scope)}\n\n"
        "ПРАВИЛА ОФОРМЛЕНИЯ ДЛЯ TELEGRAM:\n"
        "- Пиши структурированно, живо, понятно, без душных вводных штампов и бюрократии.\n"
        "- Выделяй важные шаги и термины **жирным** шрифтом.\n"
        "- Код и терминальные команды оформляй в блоки ```язык ... ``` (с указанием языка: python, cpp, bash и т.д.).\n"
        "- Однострочный код — `в обратных кавычках`.\n"
        "- Таблицы делай в стандартном Markdown (бот автоматически красиво отформатирует их в ровную моноширинную сетку).\n"
        "- МАТЕМАТИКА: пиши формулы просто и читаемо. Внутри строк используй понятные символы ($x^2$, $v_0$) в LaTeX, включая дроби, суммы и интегралы. Большие выносные уравнения обособляй между $$...$$ отдельной строкой.\n"
        "- Итоговый ответ или ключевой вывод обязательно выделяй жирным шрифтом в конце разбора.\n"
        "- Если просят опубликовать объявление/напоминание в тему — вызывай send_group_topic_message "
        "(если назван срок, передай его в deadline в формате YYYY-MM-DD или YYYY-MM-DD HH:MM).\n"
        "- Если тема не названа, а текущая тема определена — публикуй в текущую.\n"
        "- Если для напоминания не хватает данных (нет текста или непонятна тема) — задай один короткий уточняющий вопрос: что напомнить, в какую тему и до какого срока.\n"
        "- Ты МОЖЕШЬ прикреплять файлы: передай attach_last_file=true (и attach_count — сколько последних файлов/фото, по умолчанию 1), и бот отправит их в тему.\n"
        "- Таймеры («через 3 часа», «сегодня в 18:00», «напомни мне об этом за 30 минут») — вызывай create_timer.\n"
        "- Если задание выполнено/сдано — вызови mark_tasks_done.\n"
        "- ФАЙЛЫ: если просят ответ в формате Word/ворд, Excel/эксель, PDF/пдф, LaTeX/латех (format=tex), сделать презентацию или файл — "
        "вызови create_file и положи ВЕСЬ ответ в content (Markdown). Делай документ красивым: заголовки, списки, таблицы. В caption дай короткую подпись."
    )
    extras = []
    for hook in PLUGIN_PROMPT:
        try:
            extra = hook(scope, is_private)
        except Exception:
            log.exception("plugin prompt hook")
            continue
        if extra:
            extras.append(extra)
    return base + ("\n\n" + "\n\n".join(extras) if extras else "")


def build_post_tool(scope: list) -> genai_types.Tool:
    titles = ", ".join(f"«{g.get('title') or cid}»" for cid, g in scope) or "нет"
    decls = [{
        "name": "send_group_topic_message",
        "description": "Публикует напоминание или объявление в конкретную тему группы Telegram.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "group_name": {"type": "STRING",
                               "description": f"Название группы. Доступны: {titles}. Пустая строка, если группа одна."},
                "topic_name": {"type": "STRING",
                               "description": "Название темы выбранной группы (только из её списка тем)."},
                "message_text": {"type": "STRING", "description": "Текст напоминания в Markdown, без HTML."},
                "deadline": {"type": "STRING",
                             "description": "Дедлайн YYYY-MM-DD или YYYY-MM-DD HH:MM; пустая строка, если срока нет."},
                "attach_last_file": {"type": "BOOLEAN",
                                     "description": "true — прикрепить к публикации последний файл/фото, присланный в чат."},
                "attach_count": {"type": "INTEGER",
                                 "description": "Сколько последних файлов/фото прикрепить (1–5), по умолчанию 1."},
                "only_file": {"type": "BOOLEAN",
                              "description": "true — напоминание уже опубликовано ранее, нужно только добавить файл (новую задачу не создавать)."},
                "remind_daily": {"type": "BOOLEAN",
                                 "description": "true — напоминать каждый день до дедлайна (утром; в день дедлайна утром и за 3 часа), пока задание не отмечено выполненным. Нужен deadline."},
                "remind_before_minutes": {"type": "ARRAY", "items": {"type": "INTEGER"},
                                          "description": "Дополнительные предупреждения за N минут до дедлайна, например [60, 30]."},
                "remind_target": {"type": "STRING",
                                  "description": "Куда слать повторные напоминания: пусто — в ту же тему; «лс» — админу в личку; «главная» — в общую ленту группы; или название темы."},
            },
            "required": ["topic_name", "message_text"],
        },
    }, {
        "name": "create_timer",
        "description": "Ставит таймер/одноразовое напоминание на время (например «через 3 часа» или «сегодня в 18:30») с предупреждениями заранее.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "text": {"type": "STRING", "description": "О чём напомнить (Markdown, с эмодзи)."},
                "in_minutes": {"type": "INTEGER", "description": "Через сколько минут сработать (если задано «через …»)."},
                "at": {"type": "STRING", "description": "Точное время YYYY-MM-DD HH:MM по текущему часовому поясу (если задано «в 18:30»)."},
                "remind_before_minutes": {"type": "ARRAY", "items": {"type": "INTEGER"},
                                          "description": "Предупредить заранее за N минут, например [30]."},
                "where": {"type": "STRING",
                          "description": "Куда слать: пусто — в этот же чат/тему; «лс»; «главная»; или название темы."},
                "group_name": {"type": "STRING", "description": "Группа, если нужна другая группа (из личных сообщений)."},
            },
            "required": ["text"],
        },
    }, {
        "name": "create_file",
        "description": "Создаёт файл PDF / Word (docx) / LaTeX (.tex) / Excel (xlsx) / PowerPoint (pptx) с готовым ответом и отправляет его в чат.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "format": {"type": "STRING", "enum": ["pdf", "docx", "xlsx", "pptx", "tex"],
                           "description": "Формат файла."},
                "title": {"type": "STRING", "description": "Название документа (оно же имя файла)."},
                "content": {"type": "STRING",
                            "description": "ПОЛНОЕ содержимое в Markdown. PDF: формулы $...$ и $$...$$, абзацы «Определение:», «Теорема:», «Доказательство:», «Пример:», «Вывод:». Для графика используй блок кода plot с JSON: title, x (массив чисел), series (массив объектов name и y той же длины). Для схемы используй блок diagram с JSON: title, root (исходный вопрос), branches (массив подписей альтернатив). Строй по данным задачи. tex: формулы LaTeX в Markdown. pdf/docx: заголовки #/##/###, абзацы, списки, таблицы | a | b |, блоки кода ```, **жирный**. xlsx: данные в таблицах Markdown (| a | b |), формулы начинаются с «=»; заголовок ## перед таблицей = имя листа. pptx: «# Название презентации», затем «## Слайд» и 3–6 коротких пунктов на слайд (таблицы и код допустимы)."},
                "caption": {"type": "STRING", "description": "Подпись к файлу: 1–2 строки о том, что внутри."},
                "where": {"type": "STRING",
                          "description": "Куда отправить файл: пусто — в этот же чат/тему; «лс»; «главная»; или название темы."},
                "group_name": {"type": "STRING", "description": "Группа, если файл нужно отправить в другую группу."},
            },
            "required": ["format", "title", "content"],
        },
    }, {
        "name": "mark_tasks_done",
        "description": "Отмечает задачи (напоминания) выполненными и убирает их из списка /tasks.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "task_ids": {"type": "ARRAY", "items": {"type": "INTEGER"},
                             "description": "Номера задач из списка активных задач."},
            },
            "required": ["task_ids"],
        },
    }]
    if not scope:
        decls = [decl for decl in decls if decl["name"] != "send_group_topic_message"]
    return genai_types.Tool(function_declarations=decls + PLUGIN_TOOLS)



async def improve_pdf_response(resp, user_text, contents, client, model_name, config, deadline=None):
    """At most one extra completion. Preserve the draft if the repair fails."""
    from study_quality import document_issues, response_text, attach_warning
    from study_layout import stamp_page_target
    from study_polish import stamp_profile
    candidate = resp.candidates[0] if resp.candidates else None
    parts = getattr(getattr(candidate, "content", None), "parts", None) or []
    repair_budget = 1
    for p in parts:
        call = getattr(p, "function_call", None)
        if not call or call.name != "create_file":
            continue
        args = dict(call.args or {})
        if str(args.get("format", "")).lower() not in ("pdf", "пдф"):
            continue
        original = str(args.get("content") or "")
        issues = document_issues(user_text, original)
        reason = str(getattr(candidate, "finish_reason", ""))
        if "MAX_TOKENS" in reason:
            issues.append("Первоначальная генерация достигла лимита токенов")
        if not issues:
            args["content"] = stamp_profile(stamp_page_target(original, user_text), user_text)
            call.args = args
            continue
        if not repair_budget or (deadline is not None and deadline-time.monotonic()<8):
            attach_warning(args, issues)
            args["content"] = stamp_profile(stamp_page_target(str(args.get("content") or ""), user_text), user_text)
            call.args = args
            continue
        repair_budget -= 1
        try:
            repair_config = genai_types.GenerateContentConfig(
                system_instruction=config.system_instruction,
                temperature=0.3, max_output_tokens=16384,
            )
            prompt = ("\nДоработай черновик учебного PDF. Возвращай только ПОЛНЫЙ исправленный Markdown, без общей внешней ограды кода и без вызова инструментов. "
                      "Не пиши приветствия, обращения, обещания, титул и оглавление. Начинай с единственного блока study или содержательного заголовка. "
                      "Сохрани правильные данные, добавь недостающие разделы, реальные plot/diagram, решения и объяснения. "
                      "Исходный запрос выше. Недочёты:\n- " + "\n- ".join(issues) + "\n\nЧЕРНОВИК:\n" + original[:40000])
            repair_started=time.monotonic()
            repair_timeout=min(45, max(1,deadline-time.monotonic())) if deadline is not None else 45
            log.info("PDF content repair start; budget %.1fs; %d issues",repair_timeout,len(issues))
            repaired = await asyncio.wait_for(client.aio.models.generate_content(
                model=model_name, contents=[*contents, prompt], config=repair_config), timeout=repair_timeout)
            log.info("PDF content repair done %.1fs",time.monotonic()-repair_started)
            new = response_text(repaired)
            if new and len(new) <= 80000:
                new_issues = document_issues(user_text, new)
                if "MAX_TOKENS" in str(getattr(repaired.candidates[0], "finish_reason", "")):
                    new_issues.append("Повторная генерация достигла лимита токенов")
                if len(new_issues) < len(issues) or (len(new_issues) == len(issues) and (
                    len(new) < len(original) if any("Превышен бюджет" in v for v in issues) else len(new) > len(original))):
                    args["content"] = new
                    issues = new_issues
        except Exception:
            log.warning("Не удалось дополнить PDF; сохраняем черновик", exc_info=True)
        attach_warning(args, issues)
        args["content"] = stamp_profile(stamp_page_target(str(args.get("content") or ""), user_text), user_text)
        call.args = args
    return resp


async def generate_reply(history: list, user_text: str, extra_parts: Optional[list], use_tools: bool,
                         current_topic: Optional[str] = None, last_files_names: Optional[list] = None,
                         scope: Optional[list] = None, is_private: bool = False,
                         *, pool: Optional[KeyPool] = None, account: Optional[dict] = None,
                         user_name: Optional[str] = None):
    from study_quality import pdf_request, PDF_INSTRUCTIONS
    from study_layout import page_budget_hint
    file_mode = bool(FILE_REQ_RE.search(user_text or "")) or pdf_request(user_text)
    target_pool = pool or globals()["pool"]
    pref_model = (account.get("preferred_model") if account else None) or GEMINI_MODEL
    allow_fallback = account.get("model_fallback", True) if account else True

    config = genai_types.GenerateContentConfig(
        system_instruction=build_system_instruction(
            scope or [], is_private, current_topic, last_files_names,
            account=account, user_name=user_name,
        ),
        tools=[build_post_tool(scope or [])] if use_tools else ([genai_types.Tool(function_declarations=[d for d in build_post_tool([]).function_declarations if d.name == "create_file"])] if file_mode else None),
        temperature=0.7,
    )
    if file_mode:
        config.temperature = 0.3
        config.max_output_tokens = 16384
        if pdf_request(user_text):
            config.system_instruction = str(config.system_instruction) + "\n" + PDF_INSTRUCTIONS + page_budget_hint(user_text)
    req_timeout = 90 if file_mode else REQUEST_TIMEOUT_SEC
    pdf_deadline = time.monotonic()+135 if pdf_request(user_text) else None
    if pdf_deadline is not None: log.info("PDF request started; Gemini total budget 135s")
    last_err = None

    for model_name in target_pool.get_model_order(pref_model=pref_model, allow_fallback=allow_fallback):
        timeouts_on_model = 0
        for _ in range(len(target_pool.keys)):
            if pdf_deadline is not None and pdf_deadline-time.monotonic()<2:
                raise RuntimeError("Превышено время ожидания Gemini для PDF. Повтори запрос или сократи объём.")
            idx = target_pool.pick_for_model(model_name)
            if idx is None:
                break  # на этой модели свободных ключей нет — мгновенно переходим к следующей модели

            contents = build_contents(history, idx, user_text, extra_parts)
            t0 = time.monotonic()
            try:
                resp = await asyncio.wait_for(
                    target_pool.client(idx).aio.models.generate_content(
                        model=model_name, contents=contents, config=config
                    ),
                    timeout=min(70, max(1,pdf_deadline-time.monotonic())) if pdf_deadline is not None else req_timeout,
                )
                target_pool.record(idx, "ok", time.monotonic() - t0, model=model_name)
                log.info(
                    "✅ %s ответила за %.1f c (ключ #%d, инструменты: %s)",
                    model_name, time.monotonic() - t0, idx + 1, "да" if use_tools else "нет",
                )
                if file_mode and pdf_request(user_text):
                    try:
                        await improve_pdf_response(resp, user_text, contents, target_pool.client(idx), model_name, config, pdf_deadline)
                    except Exception:
                        log.warning("PDF completeness check failed", exc_info=True)
                return resp
            except Exception as e:
                last_err = e
                kind = classify_error(e)
                target_pool.record(idx, kind)
                elapsed = time.monotonic() - t0
                log.warning(
                    "⚠️ %s error (%s, ключ #%d, %.1f c): %s",
                    model_name, kind, idx + 1, elapsed, str(e)[:180] or type(e).__name__,
                )
                if kind == "not_found":
                    target_pool.mark_model_dead(model_name)
                    break  # модели нет в API — сразу к следующей
                elif kind == "quota":
                    up_err = str(e).upper().replace("_", "").replace(" ", "")
                    is_daily = any(w in up_err for w in ("PERDAY", "RPD", "DAY"))
                    # При суточном лимите убираем ключ для этой модели на 30 мин, при минутном — на 75 сек
                    target_pool.penalize(idx, 1800 if is_daily else 75, model=model_name)
                elif kind == "auth":
                    target_pool.penalize(idx, 6 * 3600, auth_fail=True)
                elif kind == "transient":
                    is_timeout = isinstance(e, asyncio.TimeoutError)
                    # Зависший ключ баним на 3 минуты, чтобы следующий запрос не ждал его снова
                    target_pool.penalize(idx, 180 if is_timeout else 45, model=model_name)
                    if is_timeout:
                        timeouts_on_model += 1
                        if timeouts_on_model >= 2:
                            log.info("⚡ Модель %s зависает, переключаюсь на более быструю...", model_name)
                            break
                else:
                    raise

    wait_sec = target_pool.min_wait(pref_model=pref_model)
    if wait_sec > 0:
        raise RuntimeError(f"Все доступные ключи сейчас на паузе из-за лимитов Gemini. Подожди ~{wait_sec} сек.")
    raise RuntimeError(f"Не удалось получить ответ от Gemini ({type(last_err).__name__ if last_err else 'таймаут'}). Попробуй ещё раз.")


def resolve_group(scope: list, group_name: str):
    if not scope:
        return None
    if len(scope) == 1:
        return scope[0]
    gn = (group_name or "").lower().strip()
    if gn:
        for cid, g in scope:
            names = [(g.get("title") or "").lower()] + [a.lower() for a in g.get("aliases", [])]
            if any(n and (gn == n or gn in n or n in gn) for n in names):
                return cid, g
    return None


async def send_reminder_media(chat_id: int, thread_id: int) -> None:
    """Случайный стикер/GIF из коллекции активного аккаунта."""
    # Получаем настройки текущего аккаунта через ext_webapp, если он подключен
    settings = get_current_account_settings()
    media = settings.get("reminder_media") or []
    if not media or not settings.get("media_enabled", True):
        return
    item = random.choice(media)
    try:
        if item.get("kind") == "sticker":
            await bot.send_sticker(chat_id, item["file_id"], message_thread_id=thread_id)
        else:
            await bot.send_animation(chat_id, item["file_id"], message_thread_id=thread_id)
    except Exception as e:
        log.warning("Не удалось отправить стикер/GIF: %s", e)


async def execute_send(scope: list, group_name: str, topic_name: str, text: str,
                       files: Optional[list] = None, with_media: bool = True, task_id: Optional[int] = None) -> tuple:
    """Публикует текст (+ файлы, + стикер/GIF) в тему выбранной группы.
    Возвращает (ok, статус, thread_id, chat_id, название группы)."""
    target = resolve_group(scope, group_name)
    if target is None:
        names = ", ".join(f"«{g.get('title') or cid}»" for cid, g in scope) or "нет подключённых групп"
        return False, f"Не понял, в какую группу публиковать. Доступные: {names}", None, None, ""
    chat_id, g = target
    gtitle = g.get("title") or str(chat_id)
    topics = g.get("topics", {})
    key = (topic_name or "").lower().strip()
    thread_id = topics.get(key)
    if thread_id is None and key:
        for k, v in topics.items():
            if k in key or key in k:
                thread_id = v
                break
    if thread_id is None:
        known = ", ".join(list(topics.keys())[:10]) or "тем нет"
        return False, f"В группе «{gtitle}» нет темы «{topic_name}». Доступные: {known}", None, chat_id, gtitle
    markup_once = [done_kb(task_id)] if task_id else []

    async def _send_text(t, pm):
        msg = await bot.send_message(chat_id=chat_id, message_thread_id=thread_id, text=t, parse_mode=pm,
                                     reply_markup=markup_once[0] if markup_once else None)
        markup_once.clear()  # кнопка «Я сделал(а)» — только на первом сообщении
        return msg

    try:
        await send_md(_send_text, text)
    except Exception as e:
        return False, f"Ошибка при отправке: {e}", thread_id, chat_id, gtitle
    failed = []
    for f in files or []:
        try:
            if f["kind"] == "photo":
                await bot.send_photo(chat_id, f["file_id"], message_thread_id=thread_id)
            else:
                await bot.send_document(chat_id, f["file_id"], message_thread_id=thread_id)
        except Exception as e:
            failed.append(f"{f['name']} ({e})")
    if with_media:
        await send_reminder_media(chat_id, thread_id)
    if failed:
        return True, f"Текст опубликован, но не удалось прикрепить: {', '.join(failed)}", thread_id, chat_id, gtitle
    if files:
        return True, f"Опубликовано вместе с файлами: {', '.join(f['name'] for f in files)}!", thread_id, chat_id, gtitle
    return True, "Успешно опубликовано!", thread_id, chat_id, gtitle


PRIVATE_WORDS = ("лс", "личк", "личн", "мне", "private")
GENERAL_WORDS = ("главн", "общ", "general", "основн", "чат")


def _ints(val) -> list:
    out = []
    for x in val or []:
        with suppress(TypeError, ValueError):
            v = int(float(x))
            if 0 < v <= 60 * 24 * 14:
                out.append(v)
    return out


def resolve_destination(message: types.Message, scope: list, group_name: str, where: str, default: tuple) -> tuple:
    """Куда слать напоминания: пусто — туда же; «лс»; «главная/общий чат»; или название темы."""
    w = (where or "").lower().strip()
    if not w:
        return default
    if any(x in w for x in PRIVATE_WORDS):
        uid = message.chat.id if message.chat.type == "private" else (message.from_user.id if message.from_user else None)
        return (uid, None) if uid else default
    target = resolve_group(scope, group_name)
    if target:
        gcid, g = target
    elif str(default[0]) in GROUPS:
        gcid, g = default[0], GROUPS[str(default[0])]
    else:
        return default
    topics = g.get("topics", {})
    thread = topics.get(w)
    if thread is None:
        for k, v in topics.items():
            if k in w or w in k:
                thread = v
                break
    if thread is not None:
        return (gcid, thread)
    if any(x in w for x in GENERAL_WORDS):
        return (gcid, None)  # «Главная» (General) — без message_thread_id
    return default


def dest_label(dest: tuple) -> str:
    cid, thread = dest
    base = chat_label(cid)
    if cid > 0:
        return base
    return f"{base} › {topic_label(cid, thread or 0)}"


async def handle_create_timer(message: types.Message, scope: list, is_private: bool, args: dict, origin: tuple) -> str:
    if not is_private and not await sender_can_manage(message):
        await message.reply("⛔ Ставить таймеры в группе могут только администраторы.")
        return "[отказ: недостаточно прав]"
    text = str(args.get("text", "") or "").strip()
    when = None
    with suppress(TypeError, ValueError):
        if args.get("in_minutes") not in (None, ""):
            when = now_local() + timedelta(minutes=float(args["in_minutes"]))
    if when is None:
        when = parse_deadline(str(args.get("at", "") or ""))
    if not text or when is None or when <= now_local():
        await message.reply("Не понял, когда напомнить. Скажи, например: «через 3 часа» или «сегодня в 18:30».")
        return "[таймер не создан: не хватило данных]"
    before = _ints(args.get("remind_before_minutes"))
    dest = resolve_destination(message, scope, str(args.get("group_name", "") or ""),
                               str(args.get("where", "") or ""), origin)
    label = dest[1] and topic_label(dest[0], dest[1]) or "таймер"
    task = add_task(dest[0], label, dest[1], text, when.strftime("%Y-%m-%d %H:%M"), "", kind="timer")
    info = plan_timer(task, dest, when, before)
    msg = (f"⏲ **Таймер #{task['id']}** поставлен: {info}\nКуда: {dest_label(dest)}\n\n{text}\n\n"
           "Отменить: «Гемини, отмени таймер» или кнопка ✅ в /tasks.")
    await reply_md(message, msg)
    return msg[:300]


async def handle_mark_done(message: types.Message, scope: list, is_private: bool, args: dict) -> str:
    """Отмечает задачи выполненными (только админы). Возвращает краткое резюме для памяти."""
    if not is_private and not await sender_can_manage(message):
        await message.reply("⛔ Отмечать задачи выполненными могут только администраторы.")
        return "[отказ: недостаточно прав]"
    allowed = {cid for cid, _ in scope}
    ids = set()
    for x in args.get("task_ids") or []:
        with suppress(TypeError, ValueError):
            ids.add(int(float(x)))
    done = []
    for t in TASKS["items"]:
        if t["id"] in ids and not t.get("done") and t.get("chat_id", TARGET_CHAT_ID) in allowed:
            t["done"], t["done_at"] = True, time.time()
            cancel_notifications(t["id"])
            done.append(t)
    if done:
        save_json(TASKS_FILE, TASKS)
        lines = "\n".join(f"- **#{t['id']}** {t['topic']}: {t['text'].strip()[:60]}" for t in done)
        msg = f"✅ Отмечено выполненным и убрано из /tasks:\n{lines}"
    else:
        msg = "Не нашёл активных задач с такими номерами. Посмотри список: /tasks"
    await reply_md(message, msg)
    return msg[:300]


async def ask_gemini(
    message: types.Message,
    user_prompt: str,
    *,
    extra_parts: Optional[list] = None,
    force_tools: bool = False,
    remember_as: Optional[str] = None,
    pool: Optional[KeyPool] = None,
    account: Optional[dict] = None,
) -> None:
    key = mem_key(message)
    history = list(mem_items(key))
    is_private = message.chat.type == "private"
    _uid = message.from_user.id if message.from_user else None
    _uname = (message.from_user.first_name if message.from_user else "") or "Пользователь"

    # Подтягиваем персональный аккаунт и персональный пул ключей (BYOK)
    acc = account
    target_pool = pool
    if acc is None and "ext_webapp" in sys.modules:
        w = sys.modules["ext_webapp"]
        acc = w.get_or_create_account({"id": _uid, "first_name": _uname})
    if target_pool is None and acc is not None and "ext_webapp" in sys.modules:
        w = sys.modules["ext_webapp"]
        user_pool, is_custom = w.get_account_pool(acc)
        target_pool = user_pool
    if target_pool is None:
        target_pool = globals()["pool"]

    if is_private:
        scope = enabled_groups() if is_primary_user(_uid) else []
    else:
        scope = [(message.chat.id, ensure_group(message.chat))]
    use_tools = bool(scope)
    thread = message.message_thread_id
    lfs = last_files(key)
    current_topic = None
    if not is_private and thread:
        current_topic = next((n for n, t in scope[0][1].get("topics", {}).items() if t == thread), None)

    async with chat_action(message.chat.id, ChatAction.TYPING, message.message_thread_id):
        try:
            response = await generate_reply(
                history, user_prompt, extra_parts, use_tools, current_topic,
                [f["name"] for f in lfs], scope, is_private,
                pool=target_pool, account=acc, user_name=_uname,
            )
        except Exception as e:
            log.exception("ask_gemini failed")
            await message.reply("Что-то пошло не так на моей стороне 😕 Попробуй отправить ещё раз через минутку. "
                                "Если повторится — напиши мне, посмотрим логи.", parse_mode=None)
            return

    texts: list = []
    calls: list = []
    try:
        parts = response.candidates[0].content.parts or []
    except Exception:
        parts = []
    for p in parts:
        if getattr(p, "function_call", None):
            calls.append(p.function_call)
        elif getattr(p, "text", None):
            texts.append(p.text)

    reply_text = "\n".join(texts).strip()
    if reply_text:
        kb = make_response_action_kb(message.message_id) if is_private else None
        sent_msgs = await reply_md(message, reply_text, reply_markup=kb)
        if is_private and sent_msgs:
            last_msg = sent_msgs[-1]
            RECENT_ANSWERS[(message.chat.id, last_msg.message_id)] = {
                "text": reply_text,
                "user_id": _uid,
                "ts": time.time(),
            }
            if len(RECENT_ANSWERS) > 100:
                oldest = min(RECENT_ANSWERS.keys(), key=lambda k: RECENT_ANSWERS[k]["ts"])
                RECENT_ANSWERS.pop(oldest, None)

    origin = (message.chat.id, None if is_private else (message.message_thread_id or None))
    for call in calls:
        args = dict(call.args or {})
        if call.name in PLUGIN_HANDLERS:
            try:
                summary = await PLUGIN_HANDLERS[call.name](message, scope, is_private, args, origin)
            except Exception as e:
                log.exception("plugin tool %s", call.name)
                await message.reply(f"⚠️ Не получилось выполнить действие ({call.name}): {e}"[:600], parse_mode=None)
                summary = f"[ошибка {call.name}]"
            reply_text = reply_text or summary or f"[{call.name}]"
            continue
        if call.name == "mark_tasks_done":
            summary = await handle_mark_done(message, scope, is_private, args)
            reply_text = reply_text or summary
            continue
        if call.name == "create_file":
            summary = await handle_create_file(message, scope, is_private, args, origin)
            reply_text = reply_text or summary
            continue
        if call.name == "create_timer":
            summary = await handle_create_timer(message, scope, is_private, args, origin)
            reply_text = reply_text or summary
            continue
        topic = str(args.get("topic_name", ""))
        post = str(args.get("message_text", ""))
        deadline = str(args.get("deadline", "") or "")
        attach = bool(args.get("attach_last_file"))
        only_file = bool(args.get("only_file"))
        try:
            count = max(1, min(5, int(float(args.get("attach_count") or 1))))
        except (TypeError, ValueError):
            count = 1
        files = lfs[-count:] if attach else []
        group_name = str(args.get("group_name", "") or "")
        want_button = (bool(deadline.strip()) or bool(args.get("remind_daily"))) and not only_file
        ok, status, thread_id, cid, gtitle = await execute_send(
            scope, group_name, topic, post, files, with_media=not only_file,
            task_id=TASKS["next_id"] if want_button else None)
        report = f"{'✅' if ok else '❌'} {status}\n"
        if is_private and gtitle:
            report += f"Группа: **{gtitle}**\n"
        report += f"Тема: **{topic}**"
        if attach and not lfs:
            report += "\n⚠️ Файл не найден — пришли его в чат и повтори просьбу."
        if ok and not only_file:
            task = add_task(
                cid, topic, thread_id, post, deadline, ", ".join(f["name"] for f in files),
                acc_id=acc.get("id") if acc else "", user_id=_uid,
            )
            report += f"\nЗадача **#{task['id']}** сохранена в /tasks"
            if deadline:
                report += f" (дедлайн: {deadline})"
            daily = bool(args.get("remind_daily"))
            before = _ints(args.get("remind_before_minutes"))
            if daily or before:
                if is_private or await sender_can_manage(message):
                    dest = resolve_destination(message, scope, group_name,
                                               str(args.get("remind_target", "") or ""), (cid, thread_id))
                    report += plan_task_notifications(task, dest, daily, before)
                    report += f"\nНапоминания придут: {dest_label(dest)}"
                else:
                    report += "\n⚠️ Повторные напоминания могут настраивать только администраторы."
        await reply_md(message, f"{report}\n\n{post}")
        reply_text = reply_text or f"[опубликовано в «{topic}»]: {post[:300]}"

    if not reply_text and not calls:
        await message.reply("Тут я промолчал — похоже, сработал фильтр. Попробуй сказать иначе, и я отвечу.", parse_mode=None)
        return

    # запоминаем реплику пользователя и ответ ассистента (обрезаем — экономим RAM)
    who = (message.from_user.first_name if message.from_user else None) or "Участник"
    log_history("лс" if is_private else "группа", who, remember_as or user_prompt, reply_text)
    mem_add(key, {"type": "text", "text": f"{who}: {remember_as or user_prompt}"[:3000]})
    mem_add(key, {"type": "text", "text": f"Ассистент: {reply_text}"[:1500]})


# =====================================================================
# 9b. ГЕНЕРАЦИЯ ФАЙЛОВ: PDF / DOCX / XLSX / PPTX (ответ Gemini -> красивый документ)
# =====================================================================
FILE_REQ_RE = re.compile(r"\b(в\s+(?:ворд|word|docx|эксел|excel|xlsx|пдф|pdf|latex|латех|tex)|(?:latex|латех|tex)|сделай\s+(?:презентац|файл|pdf|docx|pptx|xlsx))\b", re.I)
FORMAT_ALIASES = {
    "pdf": "pdf", "пдф": "pdf",
    "docx": "docx", "doc": "docx", "word": "docx", "ворд": "docx",
    "xlsx": "xlsx", "xls": "xlsx", "excel": "xlsx", "эксель": "xlsx", "эксел": "xlsx",
    "pptx": "pptx", "ppt": "pptx", "powerpoint": "pptx", "presentation": "pptx", "презентация": "pptx",
    "tex": "tex", "latex": "tex", "латех": "tex", "текс": "tex",
}
FORMAT_MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "tex": "application/x-tex",
    "latex": "application/x-tex",
}
C_NAVY, C_BLUE, C_LIGHT, C_TEXT, C_CODE_BG = "1F3A5F", "2F80ED", "EEF3FA", "222B38", "F3F4F6"

# ---------- формулы LaTeX -> читаемый текст со степенями/индексами ----------
TEX_SYMBOLS = {
    "dots": "…", "ldots": "…", "cdots": "⋯", "vdots": "⋮", "ddots": "⋱",
    "implies": "⇒", "Rightarrow": "⇒", "Leftarrow": "⇐", "iff": "⇔", "Leftrightarrow": "⇔", "to": "→",
    "rightarrow": "→", "leftarrow": "←", "leftrightarrow": "↔", "mapsto": "↦", "uparrow": "↑", "downarrow": "↓",
    "times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓", "ast": "∗", "star": "⋆", "circ": "∘", "bullet": "•",
    "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "ne": "≠", "neq": "≠", "approx": "≈", "equiv": "≡", "sim": "∼",
    "simeq": "≃", "cong": "≅", "propto": "∝", "ll": "≪", "gg": "≫",
    "infty": "∞", "partial": "∂", "nabla": "∇", "emptyset": "∅", "varnothing": "∅", "forall": "∀", "exists": "∃",
    "neg": "¬", "lnot": "¬", "land": "∧", "wedge": "∧", "lor": "∨", "vee": "∨",
    "in": "∈", "notin": "∉", "ni": "∋", "subset": "⊂", "subseteq": "⊆", "supset": "⊃", "supseteq": "⊇",
    "cup": "∪", "cap": "∩", "setminus": "∖",
    "sum": "∑", "prod": "∏", "int": "∫", "oint": "∮", "iint": "∬", "iiint": "∭",
    "angle": "∠", "perp": "⊥", "parallel": "∥", "triangle": "△", "degree": "°", "prime": "′", "ell": "ℓ",
    "hbar": "ℏ", "Re": "ℜ", "Im": "ℑ", "aleph": "ℵ", "mid": "|", "vert": "|", "Vert": "‖",
    "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉", "langle": "⟨", "rangle": "⟩",
    "lbrace": "{", "rbrace": "}", "bmod": " mod ", "colon": ":", "dagger": "†",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ",
    "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν",
    "xi": "ξ", "pi": "π", "varpi": "ϖ", "rho": "ρ", "varrho": "ϱ", "sigma": "σ", "varsigma": "ς", "tau": "τ",
    "upsilon": "υ", "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ",
    "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}
TEX_FUNCS = {"sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh",
             "log", "ln", "lg", "exp", "lim", "max", "min", "sup", "inf", "det", "gcd", "arg", "deg", "dim", "ker"}
TEX_WRAP = {"text", "mathrm", "textbf", "mathbf", "mathit", "operatorname", "mathbb", "mathcal", "boldsymbol",
            "textit", "mbox", "overline", "underline", "vec", "hat", "bar", "tilde", "widehat", "mathsf", "mathtt"}
TEX_SKIP = {"left", "right", "big", "Big", "bigg", "Bigg", "bigl", "bigr", "displaystyle", "textstyle", "limits",
            "nolimits", "nonumber", "label"}
SUP_MAP = dict(zip("0123456789+-−=()niabcdefghjklmoprstuvwxyz",
                   "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾ⁿⁱᵃᵇᶜᵈᵉᶠᵍʰʲᵏˡᵐᵒᵖʳˢᵗᵘᵛʷˣʸᶻ"))
SUB_MAP = {**dict(zip("0123456789+-−=()", "₀₁₂₃₄₅₆₇₈₉₊₋₋₌₍₎")),
           **dict(zip("aehiklmnoprstuvx", "ₐₑₕᵢₖₗₘₙₒₚᵣₛₜᵤᵥₓ"))}


def tex_runs(expr: str) -> list:
    """LaTeX -> [(текст, режим)], режим: n (обычный) | sup (степень) | sub (индекс)."""
    runs: list = []

    def emit(t: str, mode: str = "n") -> None:
        if not t:
            return
        if runs and runs[-1][1] == mode:
            runs[-1] = (runs[-1][0] + t, mode)
        else:
            runs.append((t, mode))

    def flat(s: str) -> str:
        return tex_plain(s)  # вложенные степени/индексы сохраняем юникод-символами

    def group(s: str, i: int) -> tuple:
        depth = 0
        for j in range(i, len(s)):
            if s[j] == "{":
                depth += 1
            elif s[j] == "}":
                depth -= 1
                if depth == 0:
                    return s[i + 1:j], j + 1
        return s[i + 1:], len(s)

    def arg(s: str, i: int) -> tuple:
        while i < len(s) and s[i] == " ":
            i += 1
        if i >= len(s):
            return "", i
        if s[i] == "{":
            return group(s, i)
        if s[i] == "\\":
            m = re.match(r"\\([A-Za-z]+|.)", s[i:])
            return m.group(0), i + len(m.group(0))
        return s[i], i + 1

    def part(x: str) -> str:
        return x if re.fullmatch(r"[\w.,′]+", x, re.U) else f"({x})"

    s, i = expr.strip(), 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            m = re.match(r"\\([A-Za-z]+|.)", s[i:])
            name = m.group(1)
            i += len(m.group(0))
            if name in ("frac", "dfrac", "tfrac"):
                a, i = arg(s, i)
                b, i = arg(s, i)
                emit(f"{part(flat(a))}/{part(flat(b))}")
            elif name == "binom":
                a, i = arg(s, i)
                b, i = arg(s, i)
                emit(f"C({flat(a)}, {flat(b)})")
            elif name == "sqrt":
                root = ""
                if s[i:i + 1] == "[":
                    j = s.find("]", i)
                    if j > 0:
                        root, i = s[i + 1:j], j + 1
                a, i = arg(s, i)
                sign = {"": "√", "2": "√", "3": "∛", "4": "∜"}.get(root.strip(), "".join(SUP_MAP.get(ch, ch) for ch in root) + "√")
                emit(sign + part(flat(a)))
            elif name == "pmod":
                a, i = arg(s, i)
                emit(f" (mod {flat(a)})")
            elif name in TEX_WRAP:
                a, i = arg(s, i)
                emit(flat(a))
            elif name in TEX_SKIP:
                if s[i:i + 1] == ".":
                    i += 1
            elif name in ("begin", "end"):
                _, i = arg(s, i)
            elif name in (",", ";", ":", " ", "quad", "qquad", "enspace", "\\"):
                emit(" ")
            elif name == "!":
                pass
            elif name in TEX_SYMBOLS:
                emit(TEX_SYMBOLS[name])
            elif name in TEX_FUNCS:
                emit(name)
            elif name in ("%", "$", "#", "_", "{", "}", "&"):
                emit(name)
            else:
                emit(name)  # неизвестная команда — хотя бы слово
        elif c in "^_":
            a, i = arg(s, i + 1)
            emit(flat(a), "sup" if c == "^" else "sub")
        elif c in "{}":
            i += 1
        elif c == "&":
            emit(" ")
            i += 1
        elif c == "-":
            emit("−")
            i += 1
        elif c == "'":
            emit("′")
            i += 1
        else:
            emit(c)
            i += 1
        continue
    return runs


def tex_plain(expr: str) -> str:
    """Формула как обычная строка (для чата Telegram, Excel, заголовков): степени — юникод-надстрочные."""
    out = []
    for text, mode in tex_runs(expr):
        if mode == "n":
            out.append(text)
            continue
        mp = SUP_MAP if mode == "sup" else SUB_MAP
        if all(ch in mp for ch in text):
            out.append("".join(mp[ch] for ch in text))
        else:
            out.append(("^" if mode == "sup" else "_") + (text if len(text) == 1 else f"({text})"))
    return re.sub(r"[ \t]{2,}", " ", "".join(out)).strip()


def plain_text(text: str) -> str:
    return "".join(tex_plain(t) if k == "m" else t for k, t, _ in inline_tokens(text or ""))


def wrap_code(text: str, width: int = 86) -> str:
    import textwrap
    out = []
    for line in text.expandtabs(4).split("\n"):
        if len(line) <= width:
            out.append(line)
            continue
        indent = re.match(r"\s*", line).group(0)
        out.extend(textwrap.wrap(line, width, drop_whitespace=False, replace_whitespace=False,
                                 subsequent_indent=indent + "    ", break_long_words=True) or [""])
    return "\n".join(out)


def build_pdf(title: str, blocks: list) -> bytes:
    from study_render_worker import render_bounded
    return render_bounded(title, blocks)


LATEX_UNICODE_MAP = {
    # Греческие строчные
    "α": r"\ensuremath{\alpha}", "β": r"\ensuremath{\beta}", "γ": r"\ensuremath{\gamma}",
    "δ": r"\ensuremath{\delta}", "ε": r"\ensuremath{\varepsilon}", "ϵ": r"\ensuremath{\epsilon}",
    "ζ": r"\ensuremath{\zeta}", "η": r"\ensuremath{\eta}", "θ": r"\ensuremath{\theta}",
    "ϑ": r"\ensuremath{\vartheta}", "ι": r"\ensuremath{\iota}", "κ": r"\ensuremath{\kappa}",
    "λ": r"\ensuremath{\lambda}", "μ": r"\ensuremath{\mu}", "ν": r"\ensuremath{\nu}",
    "ξ": r"\ensuremath{\xi}", "π": r"\ensuremath{\pi}", "ϖ": r"\ensuremath{\varpi}",
    "ρ": r"\ensuremath{\rho}", "ϱ": r"\ensuremath{\varrho}", "σ": r"\ensuremath{\sigma}",
    "ς": r"\ensuremath{\varsigma}", "τ": r"\ensuremath{\tau}", "υ": r"\ensuremath{\upsilon}",
    "φ": r"\ensuremath{\varphi}", "ϕ": r"\ensuremath{\phi}", "χ": r"\ensuremath{\chi}",
    "ψ": r"\ensuremath{\psi}", "ω": r"\ensuremath{\omega}",
    # Греческие заглавные
    "Γ": r"\ensuremath{\Gamma}", "Δ": r"\ensuremath{\Delta}", "Θ": r"\ensuremath{\Theta}",
    "Λ": r"\ensuremath{\Lambda}", "Ξ": r"\ensuremath{\Xi}", "Π": r"\ensuremath{\Pi}",
    "Σ": r"\ensuremath{\Sigma}", "Υ": r"\ensuremath{\Upsilon}", "Φ": r"\ensuremath{\Phi}",
    "Ψ": r"\ensuremath{\Psi}", "Ω": r"\ensuremath{\Omega}",
    # Математические операторы и кванторы
    "∀": r"\ensuremath{\forall}", "∃": r"\ensuremath{\exists}", "∄": r"\ensuremath{\nexists}",
    "∈": r"\ensuremath{\in}", "∉": r"\ensuremath{\notin}", "∋": r"\ensuremath{\ni}",
    "⊂": r"\ensuremath{\subset}", "⊆": r"\ensuremath{\subseteq}", "⊄": r"\ensuremath{\not\subset}",
    "⊃": r"\ensuremath{\supset}", "⊇": r"\ensuremath{\supseteq}",
    "∪": r"\ensuremath{\cup}", "∩": r"\ensuremath{\cap}", "∖": r"\ensuremath{\setminus}",
    "∑": r"\ensuremath{\sum}", "∏": r"\ensuremath{\prod}", "∫": r"\ensuremath{\int}",
    "∬": r"\ensuremath{\iint}", "∭": r"\ensuremath{\iiint}", "∮": r"\ensuremath{\oint}",
    "√": r"\ensuremath{\sqrt{}}", "∞": r"\ensuremath{\infty}",
    "→": r"\ensuremath{\to}", "←": r"\ensuremath{\gets}", "↔": r"\ensuremath{\leftrightarrow}",
    "⇒": r"\ensuremath{\Rightarrow}", "⇐": r"\ensuremath{\Leftarrow}", "⇔": r"\ensuremath{\Leftrightarrow}",
    "≤": r"\ensuremath{\le}", "≥": r"\ensuremath{\ge}", "≠": r"\ensuremath{\neq}",
    "≈": r"\ensuremath{\approx}", "≡": r"\ensuremath{\equiv}", "±": r"\ensuremath{\pm}",
    "∓": r"\ensuremath{\mp}", "·": r"\ensuremath{\cdot}", "×": r"\ensuremath{\times}",
    "÷": r"\ensuremath{\div}", "°": r"\ensuremath{^\circ}", "∂": r"\ensuremath{\partial}",
    "∇": r"\ensuremath{\nabla}", "⊥": r"\ensuremath{\perp}", "∥": r"\ensuremath{\parallel}",
    "∠": r"\ensuremath{\angle}", "△": r"\ensuremath{\triangle}", "∼": r"\ensuremath{\sim}",
    "∝": r"\ensuremath{\propto}", "∅": r"\ensuremath{\emptyset}", "∧": r"\ensuremath{\land}",
    "∨": r"\ensuremath{\lor}", "¬": r"\ensuremath{\neg}", "⊕": r"\ensuremath{\oplus}",
    "⊗": r"\ensuremath{\otimes}",
    "✓": r"\ensuremath{\checkmark}", "✔": r"\ensuremath{\checkmark}",
    "✗": r"\ensuremath{\times}", "✘": r"\ensuremath{\times}",
    # Числовые множества
    "ℝ": r"\ensuremath{\mathbb{R}}", "ℂ": r"\ensuremath{\mathbb{C}}",
    "ℕ": r"\ensuremath{\mathbb{N}}", "ℤ": r"\ensuremath{\mathbb{Z}}",
    "ℚ": r"\ensuremath{\mathbb{Q}}",
    # Юникод индексы и степени
    "₀": r"\ensuremath{_0}", "₁": r"\ensuremath{_1}", "₂": r"\ensuremath{_2}",
    "₃": r"\ensuremath{_3}", "₄": r"\ensuremath{_4}", "₅": r"\ensuremath{_5}",
    "₆": r"\ensuremath{_6}", "₇": r"\ensuremath{_7}", "₈": r"\ensuremath{_8}",
    "₉": r"\ensuremath{_9}", "₊": r"\ensuremath{_+}", "₋": r"\ensuremath{_-}",
    "ᵢ": r"\ensuremath{_i}", "ⱼ": r"\ensuremath{_j}", "ₖ": r"\ensuremath{_k}",
    "ₙ": r"\ensuremath{_n}", "ₓ": r"\ensuremath{_x}",
    "⁰": r"\ensuremath{^0}", "¹": r"\ensuremath{^1}", "²": r"\ensuremath{^2}",
    "³": r"\ensuremath{^3}", "⁴": r"\ensuremath{^4}", "⁵": r"\ensuremath{^5}",
    "⁶": r"\ensuremath{^6}", "⁷": r"\ensuremath{^7}", "⁸": r"\ensuremath{^8}",
    "⁹": r"\ensuremath{^9}", "⁺": r"\ensuremath{^+}", "⁻": r"\ensuremath{^-}",
    "ⁿ": r"\ensuremath{^n}",
    # Типографика
    "—": "---", "–": "--", "«": "<<", "»": ">>", "“": "``", "”": "''",
    "„": ",,", "…": r"\ldots{}", "•": r"\textbullet{}", "\u00a0": "~",
    "−": "-", "№": r"No.~",
}


def clean_latex_prose(text: str) -> str:
    """Заменяет спецсимволы, греческие буквы и типографику на LaTeX, вырезает эмодзи."""
    for k, v in LATEX_UNICODE_MAP.items():
        text = text.replace(k, v)
    text = re.sub(r"[\U00010000-\U0010ffff]", "", text)
    text = re.sub(r"[\u200d\u200c\u200e\u200f\ufe0f\ufe0e]", "", text)
    # Оставляем ASCII, кириллицу (0x0400-0x052F) и Latin-1 (0x00A0-0x00FF), остальное отсекаем во избежание сбоев pdflatex
    res = []
    for ch in text:
        code = ord(ch)
        if code < 128 or (0x0400 <= code <= 0x052F) or (0x00A0 <= code <= 0x00FF):
            res.append(ch)
    return "".join(res)


def build_latex(title: str, blocks: list) -> bytes:
    """Генерирует чистый, компилируемый LaTeX-документ (.tex) с поддержкой кириллицы и математики."""
    def tex_escape(s: str) -> str:
        chars = {
            "\\": r"\textbackslash{}",
            "&": r"\&",
            "%": r"\%",
            "$": r"\$",
            "#": r"\#",
            "_": r"\_",
            "{": r"\{",
            "}": r"\}",
            "~": r"\textasciitilde{}",
            "^": r"\textasciicircum{}",
        }
        parts = re.split(r"(\$\$.*?\$\$|\$.*?\$|\\\(.*?\\\)|\\\[.*?\\\])", str(s or ""), flags=re.DOTALL)
        res = []
        for p in parts:
            if not p:
                continue
            if (p.startswith("$") and p.endswith("$")) or (p.startswith(r"\(") and p.endswith(r"\)")) or (p.startswith(r"\[") and p.endswith(r"\]")):
                res.append(clean_latex_prose(p))
            else:
                p = "".join(chars.get(ch, ch) for ch in p)
                res.append(clean_latex_prose(p))
        return "".join(res)

    def format_inline_tex(text: str) -> str:
        t = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", text)
        t = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\\textit{\1}", t)
        t = re.sub(r"`([^`]+)`", r"\\texttt{\1}", t)
        return t

    out = [
        r"\documentclass[11pt,a4paper]{article}",
        r"\usepackage[utf8]{inputenc}",
        r"\usepackage[T2A]{fontenc}",
        r"\usepackage[english,russian]{babel}",
        r"\usepackage{amsmath,amssymb,amsfonts}",
        r"\usepackage{geometry}",
        r"\geometry{left=25mm,right=25mm,top=25mm,bottom=25mm}",
        r"\usepackage{hyperref}",
        r"\hypersetup{colorlinks=true, linkcolor=blue, urlcolor=blue}",
        r"\usepackage{booktabs}",
        r"\usepackage{parskip}",
        "",
        f"\\title{{\\textbf{{{tex_escape(title)}}}}}",
        r"\author{Gemini Bot \and Артём}",
        f"\\date{{{now_local():%d.%m.%Y %H:%M}}}",
        "",
        r"\begin{document}",
        r"\maketitle",
        "",
    ]

    in_list = None
    for b in blocks:
        bt = b.get("t")
        if bt != "li" and in_list:
            out.append(f"\\end{{{in_list}}}")
            in_list = None

        if bt == "h":
            lvl = b.get("lvl", 1)
            t = format_inline_tex(tex_escape(b.get("text", "")))
            if lvl == 1:
                out.append(f"\\section*{{{t}}}")
            elif lvl == 2:
                out.append(f"\\subsection*{{{t}}}")
            else:
                out.append(f"\\subsubsection*{{{t}}}")
        elif bt == "p":
            t = format_inline_tex(tex_escape(b.get("text", "")))
            out.append(t + "\n")
        elif bt == "quote":
            t = format_inline_tex(tex_escape(b.get("text", "")))
            out.append(r"\begin{quote}" + "\n" + t + "\n" + r"\end{quote}")
        elif bt == "code":
            code_text = clean_latex_prose(b.get("text", "")).strip("\n")
            out.append(r"\begin{verbatim}" + "\n" + code_text + "\n" + r"\end{verbatim}")
        elif bt == "math":
            m_text = clean_latex_prose(b.get("text", "")).strip()
            out.append(r"\begin{equation*}" + "\n" + m_text + "\n" + r"\end{equation*}")
        elif bt == "li":
            is_ord = b.get("ordered", False)
            target_list = "enumerate" if is_ord else "itemize"
            if in_list != target_list:
                if in_list:
                    out.append(f"\\end{{{in_list}}}")
                out.append(f"\\begin{{{target_list}}}")
                in_list = target_list
            t = format_inline_tex(tex_escape(b.get("text", "")))
            out.append(f"  \\item {t}")
        elif bt == "table":
            rows = b.get("rows", [])
            if rows:
                cols = len(rows[0])
                col_spec = "l" * cols
                out.append(r"\begin{center}")
                out.append(f"\\begin{{tabular}}{{{col_spec}}}")
                out.append(r"\toprule")
                for r_idx, row in enumerate(rows):
                    escaped_cells = [format_inline_tex(tex_escape(str(c))) for c in row]
                    out.append(" & ".join(escaped_cells) + r" \\")
                    if r_idx == 0:
                        out.append(r"\midrule")
                out.append(r"\bottomrule")
                out.append(r"\end{tabular}")
                out.append(r"\end{center}")
        elif bt == "hr":
            out.append(r"\hrulefill")

    if in_list:
        out.append(f"\\end{{{in_list}}}")

    out.append(r"\end{document}")
    return "\n".join(out).encode("utf-8")


def build_docx(title: str, blocks: list) -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    def rgb(h: str) -> RGBColor:
        return RGBColor(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

    doc = Document()
    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Cm(2.2)
    sec.top_margin = sec.bottom_margin = Cm(2)
    normal = doc.styles["Normal"]
    normal.font.name, normal.font.size = "Calibri", Pt(11)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "Calibri")
    normal.paragraph_format.space_after = Pt(6)
    for name, size, color in (("Heading 1", 18, C_NAVY), ("Heading 2", 14, "2457A6"), ("Heading 3", 12, C_NAVY)):
        st = doc.styles[name]
        st.font.name, st.font.size, st.font.bold = "Calibri", Pt(size), True
        st.font.color.rgb = rgb(color)
        st.element.rPr.rFonts.set(qn("w:eastAsia"), "Calibri")

    def border(par, side: str, color: str, sz: int = 12, space: int = 6) -> None:
        pPr = par._p.get_or_add_pPr()
        pbdr = pPr.find(qn("w:pBdr"))
        if pbdr is None:
            pbdr = OxmlElement("w:pBdr")
            pPr.append(pbdr)
        el = OxmlElement(f"w:{side}")
        for k, v in (("val", "single"), ("sz", str(sz)), ("space", str(space)), ("color", color)):
            el.set(qn(f"w:{k}"), v)
        pbdr.append(el)

    def shade(el_pr, fill: str) -> None:
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), fill)
        el_pr.append(shd)

    def add_runs(par, text: str, color: str = "", bold: bool = False, size: float = 0) -> None:
        for kind, t, url in inline_tokens(text):
            if kind == "m":
                for mt, mode in tex_runs(t):
                    mrun = par.add_run(mt)
                    mrun.italic = True
                    mrun.font.color.rgb = rgb(color or C_NAVY)
                    if mode == "sup":
                        mrun.font.superscript = True
                    elif mode == "sub":
                        mrun.font.subscript = True
                    if size:
                        mrun.font.size = Pt(size)
                continue
            if kind == "a":
                t = f"{t} ({url})"
            run = par.add_run(t)
            run.bold = bold or kind == "b"
            run.italic = kind == "i"
            if kind == "c":
                run.font.name, run.font.size = "Consolas", Pt(10)
                run.font.color.rgb = rgb("B3261E")
            elif kind == "a":
                run.font.color.rgb = rgb("2457A6")
                run.underline = True
            elif color:
                run.font.color.rgb = rgb(color)
            if size and kind != "c":
                run.font.size = Pt(size)

    # титульный блок
    tp = doc.add_paragraph()
    border(tp, "bottom", C_BLUE, sz=18, space=8)
    r = tp.add_run(plain_text(title))
    r.bold, r.font.size = True, Pt(26)
    r.font.color.rgb = rgb(C_NAVY)
    tp.paragraph_format.space_after = Pt(2)
    sp = doc.add_paragraph()
    sr = sp.add_run(f"Подготовлено: {now_local():%d.%m.%Y %H:%M}")
    sr.font.size, sr.font.color.rgb = Pt(9), rgb("7A8699")
    sp.paragraph_format.space_after = Pt(10)

    skip_title = True
    for b in blocks:
        t = b["t"]
        if t == "h":
            if skip_title and b["lvl"] == 1 and plain_text(b["text"]).strip().lower() == plain_text(title).strip().lower():
                skip_title = False
                continue
            skip_title = False
            doc.add_heading(plain_text(b["text"]), level=b["lvl"])
        elif t == "p":
            add_runs(doc.add_paragraph(), b["text"])
        elif t == "li":
            if b["ordered"]:
                par = doc.add_paragraph()
                par.paragraph_format.left_indent = Cm(0.9 + 0.8 * b["lvl"])
                par.paragraph_format.first_line_indent = Cm(-0.6)
                par.paragraph_format.space_after = Pt(3)
                add_runs(par, f"{b['num']}. " + b["text"])
            else:
                style = "List Bullet" if b["lvl"] == 0 else f"List Bullet {b['lvl'] + 1}"
                par = doc.add_paragraph(style=style)
                par.paragraph_format.space_after = Pt(2)
                add_runs(par, b["text"])
        elif t == "code":
            par = doc.add_paragraph()
            border(par, "left", C_BLUE, sz=24, space=8)
            shade(par._p.get_or_add_pPr(), C_CODE_BG)
            par.paragraph_format.left_indent = Cm(0.4)
            par.paragraph_format.space_before = Pt(4)
            par.paragraph_format.space_after = Pt(10)
            lines = wrap_code(b["text"], 90).split("\n")
            for idx, line in enumerate(lines):
                run = par.add_run(line)
                run.font.name, run.font.size = "Consolas", Pt(9)
                run._element.rPr.rFonts.set(qn("w:eastAsia"), "Consolas")
                if idx < len(lines) - 1:
                    run.add_break()
        elif t == "math":
            par = doc.add_paragraph()
            par.alignment = WD_ALIGN_PARAGRAPH.CENTER
            shade(par._p.get_or_add_pPr(), C_LIGHT)
            par.paragraph_format.space_before = Pt(4)
            par.paragraph_format.space_after = Pt(10)
            add_runs(par, "$" + b["text"].replace("\n", " ") + "$", size=13)
        elif t == "quote":
            par = doc.add_paragraph()
            border(par, "left", "9DB7E0", sz=18, space=8)
            shade(par._p.get_or_add_pPr(), C_LIGHT)
            par.paragraph_format.left_indent = Cm(0.6)
            add_runs(par, b["text"], color="3B4A63")
            for run in par.runs:
                run.italic = True
        elif t == "hr":
            par = doc.add_paragraph()
            border(par, "bottom", "C9D8F0", sz=8, space=1)
        elif t == "table":
            rows = b["rows"]
            tbl = doc.add_table(rows=len(rows), cols=len(rows[0]))
            tbl.style = "Table Grid"
            for ri, row in enumerate(rows):
                for ci, text in enumerate(row):
                    cell = tbl.cell(ri, ci)
                    par = cell.paragraphs[0]
                    par.paragraph_format.space_after = Pt(2)
                    if ri == 0:
                        shade(cell._tc.get_or_add_tcPr(), C_NAVY)
                        add_runs(par, text, color="FFFFFF", bold=True, size=10)
                    else:
                        if ri % 2 == 0:
                            shade(cell._tc.get_or_add_tcPr(), "F4F7FC")
                        add_runs(par, text, size=10)
            doc.add_paragraph().paragraph_format.space_after = Pt(4)

    # номер страницы в колонтитуле
    fp = sec.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = fp.add_run("стр. ")
    run.font.size = Pt(8)
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), "PAGE")
    fr = OxmlElement("w:r")
    ft = OxmlElement("w:t")
    ft.text = "1"
    fr.append(ft)
    fld.append(fr)
    fp._p.append(fld)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def build_xlsx(title: str, blocks: list) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)
    thin = Side(style="thin", color="C9D3E3")
    border_all = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_fill = PatternFill("solid", fgColor=C_NAVY)
    zebra = PatternFill("solid", fgColor="F4F7FC")
    used: set = set()

    def sheet_name(raw: str) -> str:
        name = re.sub(r"[\[\]:*?/\\]", " ", plain_text(raw)).strip()[:28] or "Лист"
        base, n = name, 2
        while name.lower() in used:
            name = f"{base[:25]} {n}"
            n += 1
        used.add(name.lower())
        return name

    def value(cell: str):
        s = plain_text(cell).strip()
        if s.startswith("="):
            return s, None
        norm = s.replace("\u00a0", "").replace(" ", "")
        if re.fullmatch(r"-?\d+", norm):
            return int(norm), None
        if re.fullmatch(r"-?\d+[.,]\d+", norm):
            return float(norm.replace(",", ".")), None
        if re.fullmatch(r"-?\d+([.,]\d+)?%", norm):
            return float(norm[:-1].replace(",", ".")) / 100, "0.##%"
        return s, None

    tables, notes, last_head = [], [], title
    for b in blocks:
        if b["t"] == "h":
            last_head = b["text"]
            notes.append(("h", b["text"]))
        elif b["t"] == "table":
            tables.append((last_head, b["rows"]))
        elif b["t"] == "math":
            notes.append(("p", "$" + b["text"] + "$"))
        elif b["t"] in ("p", "li", "quote"):
            prefix = "• " if b["t"] == "li" else ""
            notes.append(("p", prefix + b["text"]))
        elif b["t"] == "code":
            for line in b["text"].split("\n"):
                notes.append(("c", line))

    if notes and (not tables or any(k == "p" for k, _ in notes)):
        ws = wb.create_sheet(sheet_name("Описание" if tables else "Данные"))
        ws.column_dimensions["A"].width = 110
        ws.append([plain_text(title)])
        ws["A1"].font = Font(bold=True, size=16, color=C_NAVY)
        ws.append([])
        for kind, text in notes:
            ws.append([plain_text(text)])
            c = ws.cell(row=ws.max_row, column=1)
            c.alignment = Alignment(wrap_text=True, vertical="top")
            if kind == "h":
                c.font = Font(bold=True, size=13, color=C_NAVY)
            elif kind == "c":
                c.font = Font(name="Consolas", size=10)
                c.fill = PatternFill("solid", fgColor=C_CODE_BG)

    for head, rows in tables:
        ws = wb.create_sheet(sheet_name(head))
        for r, row in enumerate(rows, start=1):
            for c, cell in enumerate(row, start=1):
                val, fmt = value(cell)
                xc = ws.cell(row=r, column=c, value=val)
                xc.border = border_all
                xc.alignment = Alignment(wrap_text=True, vertical="top")
                if fmt:
                    xc.number_format = fmt
                if r == 1:
                    xc.font = Font(bold=True, color="FFFFFF")
                    xc.fill = head_fill
                    xc.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
                elif r % 2 == 0:
                    xc.fill = zebra
        for c in range(1, len(rows[0]) + 1):
            longest = max(len(str(plain_text(r[c - 1]))) for r in rows)
            ws.column_dimensions[get_column_letter(c)].width = min(max(longest + 4, 14), 60)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        ws.sheet_view.showGridLines = False

    if not wb.sheetnames:
        wb.create_sheet("Данные").append([plain_text(title)])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_pptx(title: str, blocks: list) -> bytes:
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.oxml.ns import qn
    from pptx.util import Inches, Pt

    def rgb(h: str) -> RGBColor:
        return RGBColor(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    blank = prs.slide_layouts[6]
    W, H = prs.slide_width, prs.slide_height

    def rect(slide, x, y, w, h, color):
        shp = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, y, w, h)
        shp.fill.solid()
        shp.fill.fore_color.rgb = rgb(color)
        shp.line.fill.background()
        shp.shadow.inherit = False
        return shp

    def textbox(slide, x, y, w, h, anchor=MSO_ANCHOR.TOP):
        tb = slide.shapes.add_textbox(x, y, w, h)
        tf = tb.text_frame
        tf.word_wrap = True
        tf.vertical_anchor = anchor
        return tf

    def style_runs(par, text, size, color, bold=False, font="Calibri"):
        for kind, t, url in inline_tokens(text):
            if kind == "m":
                for mt, mode in tex_runs(t):
                    mrun = par.add_run()
                    mrun.text = mt
                    mrun.font.size = Pt(size)
                    mrun.font.italic = True
                    mrun.font.name = font
                    mrun.font.color.rgb = rgb(color)
                    if mode != "n":
                        mrun._r.get_or_add_rPr().set("baseline", "30000" if mode == "sup" else "-25000")
                continue
            run = par.add_run()
            run.text = f"{t} ({url})" if kind == "a" else t
            run.font.size = Pt(size)
            run.font.bold = bold or kind == "b"
            run.font.italic = kind == "i"
            run.font.name = "Consolas" if kind == "c" else font
            run.font.color.rgb = rgb("B3261E" if kind == "c" else color)

    def bullet(par, char: str, level: int):
        pPr = par._p.get_or_add_pPr()
        pPr.set("marL", str(int(Inches(0.4 + 0.45 * level))))
        pPr.set("indent", str(int(-Inches(0.35))))
        for tag in ("a:buNone", "a:buChar"):
            for el in pPr.findall(qn(tag)):
                pPr.remove(el)
        bu = pPr.makeelement(qn("a:buChar"), {"char": char})
        pPr.append(bu)

    def base_slide(slide_title: str, number: int):
        s = prs.slides.add_slide(blank)
        rect(s, 0, 0, W, Inches(1.15), C_NAVY)
        rect(s, 0, Inches(1.15), W, Inches(0.06), C_BLUE)
        tf = textbox(s, Inches(0.6), Inches(0.1), W - Inches(1.2), Inches(0.95), MSO_ANCHOR.MIDDLE)
        style_runs(tf.paragraphs[0], plain_text(slide_title), 30, "FFFFFF", bold=True)
        nb = textbox(s, W - Inches(1.4), H - Inches(0.5), Inches(1.0), Inches(0.35))
        nb.paragraphs[0].alignment = PP_ALIGN.RIGHT
        style_runs(nb.paragraphs[0], str(number), 12, "7A8699")
        return s

    # --- титульный слайд
    deck_title, subtitle = title, ""
    slides: list = []
    cur = None
    first_h1 = False
    for b in blocks:
        if b["t"] == "h" and b["lvl"] == 1 and not first_h1:
            deck_title, first_h1 = b["text"], True
            continue
        if b["t"] == "h" and b["lvl"] <= 2:
            cur = {"title": b["text"], "items": []}
            slides.append(cur)
            continue
        if cur is None:
            if b["t"] == "p" and not subtitle:
                subtitle = plain_text(b["text"])
                continue
            cur = {"title": "Обзор", "items": []}
            slides.append(cur)
        if b["t"] == "h":
            cur["items"].append(("h3", b["text"]))
        elif b["t"] == "li":
            cur["items"].append(("li", b))
        elif b["t"] in ("p", "quote"):
            cur["items"].append(("p", b["text"]))
        elif b["t"] == "math":
            cur["items"].append(("math", b["text"]))
        elif b["t"] == "code":
            cur["items"].append(("code", b["text"]))
        elif b["t"] == "table":
            cur["items"].append(("table", b["rows"]))

    s0 = prs.slides.add_slide(blank)
    rect(s0, 0, 0, W, H, C_NAVY)
    rect(s0, Inches(0.9), Inches(3.55), Inches(1.6), Inches(0.08), C_BLUE)
    tf = textbox(s0, Inches(0.9), Inches(1.6), W - Inches(1.8), Inches(1.9), MSO_ANCHOR.BOTTOM)
    style_runs(tf.paragraphs[0], plain_text(deck_title), 44, "FFFFFF", bold=True)
    tf2 = textbox(s0, Inches(0.9), Inches(3.8), W - Inches(1.8), Inches(1.6))
    style_runs(tf2.paragraphs[0], subtitle or "", 22, "C9D8F0")
    tf3 = textbox(s0, Inches(0.9), H - Inches(0.9), W - Inches(1.8), Inches(0.4))
    style_runs(tf3.paragraphs[0], f"{now_local():%d.%m.%Y}", 14, "9DB7E0")

    # --- разбиение слайдов на страницы, чтобы текст не вылезал
    def weight(kind, payload) -> float:
        if kind == "h3":
            return 1.4
        text = payload["text"] if kind == "li" else payload
        return 1 + len(plain_text(text)) // 80

    pages: list = []
    for sp in slides:
        chunk, w = [], 0.0
        first = True

        def emit(kind, data):
            nonlocal first
            pages.append({"title": sp["title"] + ("" if first else " (продолжение)"), "kind": kind, "data": data})
            first = False

        for kind, payload in sp["items"]:
            if kind == "code":
                if chunk:
                    emit("text", chunk)
                    chunk, w = [], 0.0
                lines = wrap_code(payload, 88).split("\n")
                for i in range(0, len(lines), 13):
                    emit("code", "\n".join(lines[i:i + 13]))
            elif kind == "table":
                if chunk:
                    emit("text", chunk)
                    chunk, w = [], 0.0
                head, body = payload[0], payload[1:]
                for i in range(0, max(len(body), 1), 7):
                    emit("table", [head] + body[i:i + 7])
            else:
                wt = weight(kind, payload)
                if chunk and w + wt > 9:
                    emit("text", chunk)
                    chunk, w = [], 0.0
                chunk.append((kind, payload))
                w += wt
        if chunk or first:
            emit("text", chunk)

    for number, pg in enumerate(pages, start=2):
        s = base_slide(pg["title"], number)
        x, y, w, h = Inches(0.7), Inches(1.5), W - Inches(1.4), H - Inches(2.2)
        if pg["kind"] == "text":
            tf = textbox(s, x, y, w, h)
            tf.paragraphs[0].text = ""
            first_par = True
            for kind, payload in pg["data"]:
                par = tf.paragraphs[0] if first_par else tf.add_paragraph()
                first_par = False
                par.space_after = Pt(10)
                if kind == "h3":
                    style_runs(par, plain_text(payload), 26, C_BLUE, bold=True)
                elif kind == "li":
                    lvl = payload["lvl"]
                    size = 26 - 3 * lvl
                    if payload["ordered"]:
                        style_runs(par, f"{payload['num']}. " + payload["text"], size, C_TEXT)
                        par._p.get_or_add_pPr().set("marL", str(int(Inches(0.4 + 0.45 * lvl))))
                    else:
                        bullet(par, ("•", "–", "·")[lvl], lvl)
                        style_runs(par, payload["text"], size, C_TEXT)
                elif kind == "math":
                    par.alignment = PP_ALIGN.CENTER
                    style_runs(par, "$" + payload.replace("\n", " ") + "$", 30, C_BLUE)
                else:
                    style_runs(par, payload, 24, C_TEXT)
        elif pg["kind"] == "code":
            rect(s, x, y, w, h, C_CODE_BG)
            tf = textbox(s, x + Inches(0.2), y + Inches(0.15), w - Inches(0.4), h - Inches(0.3))
            tf.paragraphs[0].text = ""
            for i, line in enumerate(pg["data"].split("\n")):
                par = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                run = par.add_run()
                run.text = line or " "
                run.font.name, run.font.size = "Consolas", Pt(16)
                run.font.color.rgb = rgb("1B2330")
        else:
            rows = pg["data"]
            ncol = len(rows[0])
            shape = s.shapes.add_table(len(rows), ncol, x, y, w, Inches(0.6) * len(rows))
            tbl = shape.table
            weights = [min(max(max(len(plain_text(r[c])) for r in rows), 6), 40) for c in range(ncol)]
            for c in range(ncol):
                tbl.columns[c].width = int(w * weights[c] / sum(weights))
            for ri, row in enumerate(rows):
                for ci, text in enumerate(row):
                    cell = tbl.cell(ri, ci)
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = rgb(C_NAVY if ri == 0 else ("F4F7FC" if ri % 2 == 0 else "FFFFFF"))
                    par = cell.text_frame.paragraphs[0]
                    style_runs(par, text, 18, "FFFFFF" if ri == 0 else C_TEXT, bold=ri == 0)

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def build_document(fmt: str, title: str, content: str) -> tuple:
    """Возвращает (байты, имя файла, mime). Выполняется в потоке — тяжёлые библиотеки грузятся лениво."""
    if fmt == "pdf" and len(content) > 80000:
        raise ValueError("Документ слишком большой для одного файла (80000 символов). Раздели его на части.")
    blocks = parse_markdown(content[:80000])
    base = re.sub(r"\s+", "_", clean_filename(plain_text(title)))[:60].strip("._") or "document"
    if fmt == "texpdf":
        from tex_export import compile_tex
        return compile_tex(build_latex(title, blocks)), f"{base}_LaTeX.pdf", "application/pdf"
    if fmt in ("tex", "latex"):
        return build_latex(title, blocks), f"{base}.tex", FORMAT_MIME["tex"]
    builders = {"pdf": build_pdf, "docx": build_docx, "xlsx": build_xlsx, "pptx": build_pptx}
    data = builders[fmt](title, blocks)
    return data, f"{base}.{fmt}", FORMAT_MIME[fmt]


async def handle_create_file(message: types.Message, scope: list, is_private: bool, args: dict, origin: tuple) -> str:
    fmt = FORMAT_ALIASES.get(str(args.get("format", "pdf") or "pdf").lower().strip())
    title = str(args.get("title", "") or "").strip() or "Ответ"
    content = str(args.get("content", "") or "").strip()
    caption = str(args.get("caption", "") or "").strip()
    where = str(args.get("where", "") or "")
    if not fmt:
        await message.reply("Такой формат не поддерживается. Доступны: PDF, LaTeX (.tex), Word (docx), Excel (xlsx), PowerPoint (pptx).")
        return "[файл не создан: неизвестный формат]"
    if not content:
        await message.reply("Не получилось подготовить содержимое файла. Повтори просьбу.")
        return "[файл не создан: пустое содержимое]"
    dest = resolve_destination(message, scope, str(args.get("group_name", "") or ""), where, origin)
    if dest != origin and not is_private and not await sender_can_manage(message):
        await message.reply("⛔ Отправлять файлы в другие темы могут только администраторы.")
        return "[отказ: недостаточно прав]"
    try:
        log.info("Telegram file build start; format=%s; chars=%d",fmt,len(content))
        async with chat_action(message.chat.id, ChatAction.UPLOAD_DOCUMENT, message.message_thread_id):
            data, filename, _ = await asyncio.to_thread(build_document, fmt, title, content)
        log.info("Telegram file built; format=%s; bytes=%d",fmt,len(data))
        if fmt == "pdf":
            from study_polish import pdf_notice, add_notice
            caption = add_notice(caption, pdf_notice(data, content))
        doc = BufferedInputFile(data, filename=filename)
        cap_html = md_to_html(caption[:900]) if caption else None

        async def _send(parse_mode):
            kw = {"caption": cap_html if parse_mode else (strip_md(caption)[:900] or None), "parse_mode": parse_mode}
            if dest == origin:
                return await message.reply_document(doc, **kw)
            return await bot.send_document(dest[0], doc, message_thread_id=dest[1], **kw)

        log.info("Telegram file upload start; bytes=%d",len(data))
        try:
            sent = await _send(ParseMode.HTML if caption else None)
        except TelegramBadRequest:
            sent = await _send(None)
        log.info("Telegram file upload done")
        remember_file(mem_key(message), "document", sent.document.file_id, filename)
        if dest != origin:
            await message.reply(f"✅ Файл «{filename}» отправлен: {dest_label(dest)}")
        return f"[создан файл {filename}]: {caption[:200]}"
    except ImportError as e:
        log.warning("Нет библиотеки для %s: %s", fmt, e)
        await message.reply(f"⚠️ Формат {fmt.upper()} сейчас недоступен (не установлена библиотека). "
                            "Пришлю ответ текстом.")
    except Exception as e:
        log.exception("create_file error")
        await message.reply(f"⚠️ Не удалось создать файл ({e}). Пришлю ответ текстом.")
    await reply_md(message, content)
    return "[файл не создан, ответ отправлен текстом]"


# =====================================================================
# 10. BOT, DISPATCHER, КЛАВИАТУРЫ
# =====================================================================
_proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("TELEGRAM_PROXY")
bot = Bot(token=TELEGRAM_BOT_TOKEN, session=AiohttpSession(proxy=_proxy_url, timeout=120.0) if _proxy_url else AiohttpSession(timeout=120.0))
dp = Dispatcher()


class SettingsStates(StatesGroup):
    waiting_for_profile = State()
    waiting_for_instructions = State()
    waiting_for_rename_new = State()
    waiting_for_media = State()


def webapp_url() -> str:
    """Адрес мини-приложения: WEBAPP_URL или RENDER_EXTERNAL_URL (Render подставляет сам)."""
    base = (os.environ.get("WEBAPP_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if base.endswith("/app"):
        base = base[:-4]
    return f"{base}/app" if base.startswith("https://") else ""


def get_reply_main_menu() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📅 Завтра"), KeyboardButton(text="🗓 Неделя")],
        [KeyboardButton(text="🧠 Карточки"), KeyboardButton(text="🎯 Квиз"), KeyboardButton(text="📋 Задачи")],
        [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="🏷 Темы группы")],
        [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="❓ Помощь")],
        [KeyboardButton(text="🧹 Очистить память"), KeyboardButton(text="❌ Скрыть меню")],
    ]
    url = webapp_url()
    if url:
        rows.insert(0, [KeyboardButton(text="📱 Приложение", web_app=WebAppInfo(url=url))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def mode_title() -> str:
    return "Подробный разбор с теорией" if get_current_account_settings().get("answer_mode") != "short" else "Краткий ответ (шпаргалка)"


def settings_text() -> str:
    return f"⚙️ <b>Панель управления ассистентом</b>\nРежим ответов: <b>{mode_title()}</b>"


def get_settings_keyboard() -> InlineKeyboardMarkup:
    mode_btn = "📚 Режим: подробный разбор" if get_current_account_settings().get("answer_mode") != "short" else "⚡ Режим: краткий ответ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Обо мне (профиль)", callback_data="set_profile_btn"),
         InlineKeyboardButton(text="📜 Инструкции боту", callback_data="set_prompt_btn")],
        [InlineKeyboardButton(text="🏷 Темы группы", callback_data="manage_topics_btn"),
         InlineKeyboardButton(text="👁 Показать всё", callback_data="view_all_settings_btn")],
        [InlineKeyboardButton(text=mode_btn, callback_data="toggle_mode_btn")],
        [InlineKeyboardButton(text="🎭 Стикеры и GIF", callback_data="media_menu_btn")],
        [InlineKeyboardButton(text="📋 Задачи", callback_data="open_tasks_btn"),
         InlineKeyboardButton(text="❌ Закрыть", callback_data="close_settings_btn")],
    ])


async def setup_bot_commands() -> None:
    private = [
        BotCommand(command="menu", description="🎛 Открыть главное меню"),
        BotCommand(command="settings", description="⚙️ Панель настроек"),
        BotCommand(command="profile", description="👤 Мой профиль и инструкции"),
        BotCommand(command="topics", description="🏷 Темы группы"),
        BotCommand(command="tasks", description="📋 Активные задачи и дедлайны"),
        BotCommand(command="clear", description="🧹 Очистить память ветки"),
        BotCommand(command="today", description="📅 Расписание на сегодня"),
        BotCommand(command="tomorrow", description="📅 Расписание на завтра"),
        BotCommand(command="week", description="🗓 Расписание на неделю"),
        BotCommand(command="cards", description="🧠 Карточки для повторения"),
        BotCommand(command="quiz", description="🎯 Квиз на 5 вопросов"),
        BotCommand(command="app", description="📱 Открыть мини-приложение"),
        BotCommand(command="stats", description="📊 Состояние ключей Gemini"),
        BotCommand(command="help", description="❓ Справка по работе"),
    ]
    group = [
        BotCommand(command="tasks", description="📋 Активные задачи и дедлайны"),
        BotCommand(command="clear", description="🧹 Очистить память ветки"),
        BotCommand(command="chatid", description="🆔 Показать ID чата"),
        BotCommand(command="addgroup", description="➕ Подключить группу (админ)"),
        BotCommand(command="alias", description="🏷 Короткие названия группы (админ)"),
        BotCommand(command="removegroup", description="➖ Отключить группу (админ)"),
    ]
    await bot.set_my_commands(private, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(group, scope=BotCommandScopeAllGroupChats())


# Если задан ADMIN_IDS — любые кнопки от не-админов отклоняем (регистрируется ПЕРВЫМ)
PUBLIC_CB_PREFIXES = ("dn_", "qz_")  # кнопки, доступные всем участникам группы
# Кнопки меню настроек/задач, доступные гостю со своим ключом (опасные mg_/del_/ren_/td_ сюда не входят)
GUEST_CB_PREFIXES = ("close_settings_btn", "view_all_settings_btn", "toggle_mode_btn", "open_tasks_btn",
                     "task_refresh", "tk_g", "tk_t_", "tk_l_", "set_profile_btn", "set_prompt_btn",
                     "manage_topics_btn", "back_to_main_settings", "media_")


def _guest_cb_ok(c) -> bool:
    if getattr(getattr(c.message, "chat", None), "type", None) == "private" and (c.data or "").startswith(("tt_d_", "tt_w_")):
        return True
    return bool(GUEST_CHECK and GUEST_CHECK(c.from_user.id)
                and (c.data or "").startswith(GUEST_CB_PREFIXES))

if ADMIN_IDS:
    @dp.callback_query(lambda c: c.from_user.id not in ADMIN_IDS
                       and not (c.data or "").startswith(PUBLIC_CB_PREFIXES)
                       and not _guest_cb_ok(c))
    async def cb_denied(call: types.CallbackQuery):
        await call.answer("⛔ Недостаточно прав", show_alert=True)


# =====================================================================
# 11. КОМАНДЫ И НАСТРОЙКИ
# =====================================================================
@dp.message(CommandStart(), F.chat.type == "private")
async def cmd_start_private(message: types.Message):
    parts = (message.text or "").split(maxsplit=1)
    invite = parts[1] if len(parts) > 1 else ""
    url = webapp_url()
    if re.fullmatch(r"tt_[A-Za-z0-9_-]{24}", invite) and url:
        button = InlineKeyboardButton(text="Посмотреть приглашение", web_app=WebAppInfo(url=f"{url}?invite={invite[3:]}"))
        await message.answer("Вам прислали расписание группы. Подписку можно подтвердить в ежедневнике.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]))
        return
    await message.answer("Привет! Я тут, чтобы тебе было проще с учёбой. Загляни в ежедневник: там ближайшая пара, домашка и карточки. А если хочется просто поболтать или что-то разобрать — пиши мне прямо сюда.",
                         reply_markup=get_reply_main_menu())


@dp.message(F.text == "❌ Скрыть меню", F.chat.type == "private")
async def hide_menu(message: types.Message):
    await message.answer("Клавиатура скрыта. Чтобы вернуть кнопки, напиши /menu.", reply_markup=ReplyKeyboardRemove())


@dp.message(Command("menu"), F.chat.type == "private")
async def show_menu(message: types.Message):
    await message.answer("🎛 Главное меню:", reply_markup=get_reply_main_menu())


@dp.message(F.text == "⚙️ Настройки", F.chat.type == "private")
@dp.message(Command("settings"), F.chat.type == "private")
async def cmd_settings(message: types.Message):
    if is_allowed(message):
        await message.answer(settings_text(), reply_markup=get_settings_keyboard(), parse_mode=ParseMode.HTML)


@dp.message(F.text == "👤 Мой профиль", F.chat.type == "private")
@dp.message(Command("profile"), F.chat.type == "private")
async def cmd_view_profile(message: types.Message):
    if not is_allowed(message):
        return
    settings = get_current_account_settings()
    acc_profile = settings.get("user_profile") or "Не задано"
    acc_instructions = settings.get("custom_instructions") or "Не задано"

    await message.answer(
        "📋 <b>Текущие настройки ассистента:</b>\n\n"
        f"👤 <b>О себе:</b>\n{esc(acc_profile)}\n\n"
        f"📜 <b>Инструкции:</b>\n{esc(acc_instructions)}\n\n"
        f"🎚 <b>Режим:</b> {mode_title()}",
        reply_markup=get_settings_keyboard(), parse_mode=ParseMode.HTML,
    )


@dp.message(F.text == "🏷 Темы группы", F.chat.type == "private")
@dp.message(Command("topics"), F.chat.type == "private")
async def cmd_topics_list(message: types.Message):
    if not is_allowed(message):
        return
    uid = message.from_user.id if message.from_user else None
    await message.reply("🏷 <b>Темы по группам:</b>\n\n" + topics_overview_html(uid), parse_mode=ParseMode.HTML)


@dp.message(F.text == "📋 Задачи")
@dp.message(Command("tasks"))
async def cmd_tasks(message: types.Message):
    if not is_allowed(message):
        return
    uid = message.from_user.id if message.from_user else None
    text, kb = render_tasks_root(tasks_scope(message.chat), uid)
    await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.message(Command("chatid"))
async def cmd_chatid(message: types.Message):
    """Показывает ID чата и темы; работает в любом чате."""
    thread = f"\nID темы: <code>{message.message_thread_id}</code>" if message.message_thread_id else ""
    if message.chat.type == "private":
        role = "личный чат"
    elif is_group_enabled(message.chat.id):
        role = "группа подключена: темы, задачи и публикации доступны"
    else:
        role = "группа НЕ подключена — администратор может написать /addgroup"
    await message.reply(f"ID этого чата: <code>{message.chat.id}</code>{thread}\nСтатус: {role}",
                        parse_mode=ParseMode.HTML)


@dp.message(Command("addgroup"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_addgroup(message: types.Message):
    if not await sender_can_manage(message):
        await message.reply("⛔ Подключать группу могут только администраторы.")
        return
    g = ensure_group(message.chat)
    g["enabled"] = True
    save_json(GROUPS_FILE, GROUPS)
    await message.reply(
        f"✅ Группа «{esc(g.get('title') or message.chat.id)}» подключена.\n"
        "Чтобы привязать тему, напиши внутри неё: <code>тема название</code>. "
        "Темы, задачи и публикации у каждой группы свои.", parse_mode=ParseMode.HTML)


@dp.message(Command("removegroup"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_removegroup(message: types.Message):
    if not await sender_can_manage(message):
        await message.reply("⛔ Отключать группу могут только администраторы.")
        return
    if message.chat.id == TARGET_CHAT_ID or message.chat.id in ALLOWED_CHAT_IDS:
        await message.reply("Эта группа задана в переменных окружения Render — убери её ID оттуда.")
        return
    g = ensure_group(message.chat)
    g["enabled"] = False
    save_json(GROUPS_FILE, GROUPS)
    await message.reply("Группа отключена. Темы сохранены — при повторном /addgroup вернутся.")


@dp.message(F.text == "🧹 Очистить память")
@dp.message(Command("clear"))
async def clear_memory(message: types.Message):
    if not is_allowed(message):
        return
    if mem_clear(mem_key(message)):
        await message.reply("🧹 Память этой ветки очищена!")
    else:
        await message.reply("Память этой ветки пуста.")


@dp.message(Command("app"), F.chat.type == "private")
async def cmd_app(message: types.Message):
    url = webapp_url()
    if not url:
        await message.answer("Мини-приложение пока не настроено: нужен публичный https-адрес сервиса "
                             "(на Render он подставляется сам, иначе задай переменную WEBAPP_URL).")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📱 Открыть приложение", web_app=WebAppInfo(url=url))]])
    await message.answer("Расписание, задачи, карточки, история и чат — в одном месте 👇", reply_markup=kb)


@dp.message(Command("stats"), F.chat.type == "private")
async def cmd_stats(message: types.Message):
    if not is_allowed(message):
        return
    await message.answer("📊 <b>Ключи Gemini</b> (✅ ответов · среднее время · ошибки)\n<pre>"
                         + esc(pool.report()) + "</pre>", parse_mode=ParseMode.HTML)


@dp.message(F.text == "❓ Помощь", F.chat.type == "private")
@dp.message(Command("help"), F.chat.type == "private")
async def cmd_help(message: types.Message):
    if not is_allowed(message):
        return
    await message.answer(
        "📖 <b>Как пользоваться ботом:</b>\n\n"
        "• В личке — просто пиши задачу, настройки в /settings.\n"
        "• В группе — <code>Гемини, реши…</code> или упоминание бота. "
        "Чтобы привязать ветку, напиши в ней: <code>тема Название</code>.\n"
        "• Фото, PDF, DOCX, текстовые файлы — с подписью (задача сразу решается) или без "
        "(попадают в контекст ветки).\n"
        "• Голосовые: в личке — любое, в группе — ответом на сообщение бота.\n"
        "• /tasks — активные напоминания и дедлайны; /clear — очистить память ветки.\n"
        "• Попроси: «Гемини, напомни в теме английский до 2026-10-20 сдать эссе» — опубликую и запомню.\n"
        "• 📅 Расписание: пришли фото или текст со своими парами, я разберу и сохраню; кнопки «Сегодня / Завтра / Неделя»; "
        "менять можно словами («перенеси физику на среду»).\n"
        "• 🧠 Карточки (слова для заучивания) и 🎯 квиз на 5 вопросов по любой теме или по присланному файлу.\n"
        "• 📱 Мини-приложение — расписание, задачи, карточки, история и чат в красивом интерфейсе.",
        reply_markup=get_reply_main_menu(), parse_mode=ParseMode.HTML,
    )


@dp.message(Command("key", "setkey"))
async def handle_key_command(message: types.Message, command: CommandObject):
    if message.chat.type != "private":
        await message.reply("🔑 Управление API-ключом доступно только в личных сообщениях с ботом.")
        return
    arg = (command.args or "").strip()
    uid = message.from_user.id
    uname = message.from_user.first_name or "Пользователь"

    acc = None
    if "ext_webapp" in sys.modules:
        w = sys.modules["ext_webapp"]
        acc = w.get_or_create_account({"id": uid, "first_name": uname})

    if not arg:
        current_k = acc.get("custom_api_key", "") if acc else ""
        masked = (current_k[:6] + "••••••••" + current_k[-4:]) if len(current_k) > 12 else ("задан" if current_k else "не подключён")
        status_line = f"Твой текущий ключ: <code>{masked}</code>" if current_k else "У тебя пока не подключён личный ключ."
        if is_primary_user(uid) and not current_k:
            status_line += "\n<i>(Как администратор, ты сейчас используешь общий пул сервера)</i>"

        await message.answer(
            "🔑 <b>Управление API-ключом Gemini (BYOK)</b>\n\n"
            f"{status_line}\n\n"
            "<b>Как привязать свой ключ:</b>\n"
            "1. Зайди на <a href=\"https://aistudio.google.com/apikey\">Google AI Studio</a> и создай бесплатный ключ.\n"
            "2. Отправь боту команду:\n"
            "<code>/key AIzaSy...твой_ключ</code>\n\n"
            "<b>Команды:</b>\n"
            "• <code>/key AIzaSy...</code> — проверить и сохранить ключ\n"
            "• <code>/key remove</code> — удалить ключ\n"
            "• <code>/models</code> — выбрать модель ИИ",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    if arg.lower() in ("remove", "delete", "clear", "сброс", "удалить"):
        if acc:
            acc["custom_api_key"] = ""
            save_json(SETTINGS_FILE, USER_SETTINGS)
            if "ext_webapp" in sys.modules:
                sys.modules["ext_webapp"].USER_POOLS.pop(acc["id"], None)
        await message.answer("🗑 Личный API-ключ удалён.")
        return

    async with chat_action(message.chat.id, ChatAction.TYPING):
        test_key = arg
        if len(test_key) < 20:
            await message.answer("❌ Ключ слишком короткий. Проверь правильность копирования.")
            return

        try:
            client = genai.Client(api_key=test_key, http_options=genai_types.HttpOptions(timeout=10000))
            test_models = ["gemini-4-hydrogen", "gemini-3.5-flash-lite", "gemini-2.5-flash"]
            last_err = None
            verified = False
            for m in test_models:
                try:
                    await asyncio.wait_for(client.aio.models.generate_content(model=m, contents="ping"), timeout=10)
                    verified = True
                    break
                except Exception as ex:
                    last_err = ex
                    up = str(ex).upper()
                    if any(w in up for w in ("429", "RESOURCE_EXHAUSTED", "QUOTA")):
                        verified = True
                        break
                    if any(w in up for w in ("API_KEY_INVALID", "INVALID", "UNAUTHENTICATED", "401", "403")):
                        break
                    continue

            if not verified:
                raise last_err or RuntimeError("Не удалось проверить ключ")

            if acc:
                acc["custom_api_key"] = test_key
                save_json(SETTINGS_FILE, USER_SETTINGS)
                if "ext_webapp" in sys.modules:
                    sys.modules["ext_webapp"].USER_POOLS.pop(acc["id"], None)

            masked = test_key[:6] + "••••••••" + test_key[-4:]
            await message.answer(
                f"✅ <b>Ключ успешно проверен и сохранён!</b>\n\n"
                f"Ключ: <code>{masked}</code>\n"
                f"Теперь бот использует твой персональный ключ и твои квоты Google AI Studio.\n"
                f"Выбрать предпочтительную модель: /models",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            await message.answer(f"❌ Ошибка проверки ключа: {e}\nУбедись, что скопировал ключ целиком из Google AI Studio.")


@dp.message(Command("models", "model"))
async def handle_models_command(message: types.Message):
    uid = message.from_user.id if message.from_user else None
    acc = None
    if "ext_webapp" in sys.modules and uid:
        acc = sys.modules["ext_webapp"].get_or_create_account({"id": uid, "first_name": message.from_user.first_name or ""})
    current_m = (acc.get("preferred_model") if acc else None) or GEMINI_MODEL

    rows = []
    for m in AVAILABLE_MODELS:
        prefix = "✅ " if m["id"] == current_m else ""
        rows.append([InlineKeyboardButton(text=f"{prefix}{m['name']}", callback_data=f"set_model:{m['id']}")])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer(
        "🤖 <b>Выбор модели Gemini</b>\n\n"
        f"Текущая модель: <code>{esc(current_m)}</code>\n\n"
        "Выбери модель для своих запросов:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


# =====================================================================
# 12. CALLBACKS
# =====================================================================
@dp.callback_query(F.data.startswith("set_model:"))
async def cb_set_model(call: types.CallbackQuery):
    chosen = call.data.split(":", 1)[1]
    uid = call.from_user.id if call.from_user else None
    if "ext_webapp" in sys.modules and uid:
        acc = sys.modules["ext_webapp"].get_or_create_account({"id": uid, "first_name": call.from_user.first_name or ""})
        acc["preferred_model"] = chosen
        save_json(SETTINGS_FILE, USER_SETTINGS)
    await call.answer(f"Модель переключена на {chosen}!")

    rows = []
    for m in AVAILABLE_MODELS:
        prefix = "✅ " if m["id"] == chosen else ""
        rows.append([InlineKeyboardButton(text=f"{prefix}{m['name']}", callback_data=f"set_model:{m['id']}")])
    await safe_edit(
        call.message,
        f"🤖 <b>Выбор модели Gemini</b>\n\nТекущая модель: <code>{esc(chosen)}</code>\n\nВыбери модель для своих запросов:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(F.data.startswith("doc_pdf:") | F.data.startswith("doc_docx:") | F.data.startswith("doc_tex:") | F.data.startswith("doc_texpdf:"))
async def cb_generate_doc(call: types.CallbackQuery):
    action = call.data.partition(":")[0]
    ans_data = RECENT_ANSWERS.get((call.message.chat.id, call.message.message_id))
    content = ans_data.get("text") if ans_data else (call.message.text or call.message.caption or "")
    if not content:
        await call.answer("Ответ устарел или не найден в памяти", show_alert=True)
        return

    fmt_map = {
        "doc_pdf": ("pdf", "📄 PDF"),
        "doc_docx": ("docx", "📝 Word"),
        "doc_texpdf": ("texpdf", "PDF из LaTeX"),
        "doc_tex": ("tex", "📐 LaTeX (.tex)"),
    }
    fmt, label = fmt_map.get(action, ("pdf", "📄 PDF"))
    await call.answer(f"Генерирую {label}...")
    title = "Ответ ассистента"
    try:
        async with chat_action(call.message.chat.id, ChatAction.UPLOAD_DOCUMENT):
            data, filename, _ = await asyncio.to_thread(build_document, fmt, title, content)
            doc = BufferedInputFile(data, filename=filename)
            caption = f"Твой ответ · {label}"
            if fmt == "pdf":
                from study_polish import pdf_notice, add_notice
                caption = add_notice(caption, pdf_notice(data, content))
            await call.message.reply_document(doc, caption=caption)
    except Exception as e:
        log.exception("cb_generate_doc error")
        await call.message.reply(f"⚠️ Не удалось создать {label}: {e}")
@dp.callback_query(F.data == "close_settings_btn")
async def cb_close_settings(call: types.CallbackQuery):
    with suppress(Exception):
        await call.message.delete()
    await call.answer()


@dp.callback_query(F.data == "view_all_settings_btn")
async def cb_view_all(call: types.CallbackQuery):
    uid = call.from_user.id if call.from_user else None
    topics_list = topics_overview_html(uid)
    active = sum(1 for t in TASKS["items"] if not t.get("done"))

    # Фильтруем группы для отображения в полной конфигурации
    is_main = not ADMIN_IDS or (uid in ADMIN_IDS)
    groups_count = len([(cid, g) for cid, g in enabled_groups() if can_see_chat(uid, cid)])

    await safe_edit(
        call.message,
        "📋 <b>Полная конфигурация:</b>\n\n"
        f"👤 <b>О себе:</b>\n{esc(get_current_account_settings().get('user_profile', 'Не задано'))}\n\n"
        f"📜 <b>Инструкции:</b>\n{esc(get_current_account_settings().get('custom_instructions', 'Не задано'))}\n\n"
        f"🎚 <b>Режим:</b> {mode_title()}\n\n"
        f"🏷 <b>Группы и темы:</b>\n{topics_list}\n\n"
        f"🔑 <b>Ключей Gemini:</b> {len(pool.keys)} · 🤖 <b>Модель:</b> <code>{esc(GEMINI_MODEL)}</code>\n"
        f"📋 <b>Активных задач:</b> {active}",
        get_settings_keyboard(),
    )
    await call.answer()


@dp.callback_query(F.data == "toggle_mode_btn")
async def cb_toggle_mode(call: types.CallbackQuery):
    settings = get_current_account_settings()
    settings["answer_mode"] = "short" if settings.get("answer_mode") != "short" else "detailed"
    save_json(SETTINGS_FILE, USER_SETTINGS)
    await safe_edit(call.message, settings_text(), get_settings_keyboard())
    await call.answer(f"Режим: {mode_title()}")


@dp.callback_query(F.data == "open_tasks_btn")
async def cb_open_tasks(call: types.CallbackQuery):
    text, kb = render_tasks(tasks_scope(call.message.chat))
    await call.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await call.answer()


@dp.callback_query(F.data == "task_refresh")
async def cb_task_refresh(call: types.CallbackQuery):
    text, kb = render_tasks(tasks_scope(call.message.chat))
    await safe_edit(call.message, text, kb)
    await call.answer("Обновлено")


def _is_private_call(call: types.CallbackQuery) -> bool:
    return call.message.chat.type == "private"


def _cid_ok(call: types.CallbackQuery, cid: int) -> bool:
    """Из группы можно открывать только её собственные задачи."""
    return _is_private_call(call) or cid == call.message.chat.id


@dp.callback_query(F.data.startswith("dn_"))
async def cb_task_mark(call: types.CallbackQuery):
    """Участник группы нажимает «Я сделал(а)» под напоминанием."""
    try:
        tid = int(call.data[3:])
    except ValueError:
        await call.answer()
        return
    task = next((t for t in TASKS["items"] if t["id"] == tid), None)
    if not task or task.get("done"):
        await call.answer("Это задание уже закрыто ✅", show_alert=True)
        with suppress(Exception):
            await call.message.edit_reply_markup(reply_markup=None)
        return
    if call.message.chat.id != task.get("chat_id", TARGET_CHAT_ID):
        await call.answer("Недоступно", show_alert=True)
        return
    done_by = task.setdefault("done_by", {})
    uid = str(call.from_user.id)
    if uid in done_by:
        del done_by[uid]
        msg = "Отметка снята"
    else:
        done_by[uid] = call.from_user.full_name or uid
        msg = "Отлично! Записал ✅"
    save_json(TASKS_FILE, TASKS)
    with suppress(TelegramBadRequest):
        await call.message.edit_reply_markup(reply_markup=done_kb(tid, len(done_by)))
    await call.answer(msg)


@dp.callback_query(F.data == "tk_g")
async def cb_tk_groups(call: types.CallbackQuery):
    uid = call.from_user.id if call.from_user else None
    text, kb = render_tasks_root(tasks_scope(call.message.chat), uid)
    await safe_edit(call.message, text, kb)
    await call.answer()


@dp.callback_query(F.data.startswith("tk_t_"))
async def cb_tk_topics(call: types.CallbackQuery):
    try:
        cid = int(call.data[5:])
    except ValueError:
        await call.answer()
        return
    if not _cid_ok(call, cid):
        await call.answer("⛔ Недоступно", show_alert=True)
        return
    text, kb = render_tasks_topics(cid, _is_private_call(call))
    await safe_edit(call.message, text, kb)
    await call.answer()


@dp.callback_query(F.data.startswith("tk_l_"))
async def cb_tk_list(call: types.CallbackQuery):
    try:
        _, _, cid_s, th_s = call.data.split("_", 3)
        cid, thread = int(cid_s), int(th_s)
    except ValueError:
        await call.answer()
        return
    if not _cid_ok(call, cid):
        await call.answer("⛔ Недоступно", show_alert=True)
        return
    text, kb = render_tasks_list(cid, thread, _is_private_call(call))
    await safe_edit(call.message, text, kb)
    await call.answer()


@dp.callback_query(F.data.startswith("td_"))
async def cb_task_done(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Недостаточно прав", show_alert=True)
        return
    try:
        _, tid_s, cid_s, th_s = call.data.split("_", 3)
        tid, cid, thread = int(tid_s), int(cid_s), int(th_s)
    except ValueError:
        await call.answer()
        return
    if not _cid_ok(call, cid):
        await call.answer("⛔ Недоступно", show_alert=True)
        return
    for t in TASKS["items"]:
        if t["id"] == tid and not t.get("done"):
            t["done"], t["done_at"] = True, time.time()
            save_json(TASKS_FILE, TASKS)
            cancel_notifications(tid)
            break
    text, kb = render_tasks_list(cid, thread, _is_private_call(call))
    await safe_edit(call.message, text, kb)
    await call.answer("Отмечено выполненным ✅")


@dp.callback_query(F.data == "set_profile_btn")
async def cb_set_profile(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(SettingsStates.waiting_for_profile)
    await call.message.answer("✏️ <b>Напиши информацию о себе следующим сообщением:</b>", parse_mode=ParseMode.HTML)
    await call.answer()


@dp.message(SettingsStates.waiting_for_profile, F.chat.type == "private")
async def handle_profile_input(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Пришли, пожалуйста, текстом.")
        return
    get_current_account_settings()["user_profile"] = message.text.strip()
    save_json(SETTINGS_FILE, USER_SETTINGS)
    await state.clear()
    await message.answer("✅ <b>Информация сохранена!</b>", reply_markup=get_reply_main_menu(), parse_mode=ParseMode.HTML)


@dp.callback_query(F.data == "set_prompt_btn")
async def cb_set_prompt(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(SettingsStates.waiting_for_instructions)
    await call.message.answer("✏️ <b>Напиши инструкции для ответов бота:</b>", parse_mode=ParseMode.HTML)
    await call.answer()


@dp.message(SettingsStates.waiting_for_instructions, F.chat.type == "private")
async def handle_instructions_input(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Пришли, пожалуйста, текстом.")
        return
    get_current_account_settings()["custom_instructions"] = message.text.strip()
    save_json(SETTINGS_FILE, USER_SETTINGS)
    await state.clear()
    await message.answer("✅ <b>Инструкции сохранены!</b>", reply_markup=get_reply_main_menu(), parse_mode=ParseMode.HTML)


# Имена тем в callback_data не помещаются (лимит 64 байта) — используем короткий хэш
def _th(name: str) -> str:
    return hashlib.md5(name.encode("utf-8")).hexdigest()[:10]


def _topic_by_hash(cid: int, h: str) -> Optional[str]:
    topics = GROUPS.get(str(cid), {}).get("topics", {})
    return next((n for n in topics if _th(n) == h), None)


@dp.callback_query(F.data == "manage_topics_btn")
async def cb_manage_topics(call: types.CallbackQuery):
    uid = call.from_user.id if call.from_user else None
    is_main = not ADMIN_IDS or (uid in ADMIN_IDS)

    # Обычным пользователям не показываем чужие учебные группы (с отрицательным ID)
    groups = [(cid, g) for cid, g in enabled_groups() if can_see_chat(uid, cid)]

    rows = [[InlineKeyboardButton(text=f"📂 {(g.get('title') or str(cid))[:40]}", callback_data=f"mg_{cid}")]
            for cid, g in groups]
    rows.append([InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_to_main_settings")])
    text = ("🏷 <b>Управление темами.</b> Выбери группу:" if groups
            else "Подключённых групп пока нет.")
    await safe_edit(call.message, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


async def show_group_topics(call: types.CallbackQuery, cid: int) -> None:
    g = GROUPS.get(str(cid))
    if not g:
        await call.answer("Группа не найдена", show_alert=True)
        return
    topics = g.get("topics", {})
    rows = [[InlineKeyboardButton(text=f"✏️ {name[:24]}", callback_data=f"ren_{cid}_{_th(name)}"),
             InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del_{cid}_{_th(name)}")] for name in topics]
    rows.append([InlineKeyboardButton(text="⬅️ К списку групп", callback_data="manage_topics_btn")])
    text = (f"🏷 <b>Темы группы «{esc(g.get('title') or cid)}»:</b>" + ("" if topics else "\nпока пусто")
            + "\n\nЧтобы добавить тему, напиши внутри неё в группе: <code>тема название</code>.")
    await safe_edit(call.message, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@dp.callback_query(F.data.startswith("mg_"))
async def cb_group_topics(call: types.CallbackQuery):
    try:
        cid = int(call.data[3:])
    except ValueError:
        await call.answer()
        return
    await show_group_topics(call, cid)


@dp.callback_query(F.data.startswith("del_"))
async def cb_delete_topic(call: types.CallbackQuery):
    try:
        _, cid_s, h = call.data.split("_", 2)
        cid = int(cid_s)
    except ValueError:
        await call.answer()
        return
    name = _topic_by_hash(cid, h)
    if name:
        del GROUPS[str(cid)]["topics"][name]
        save_json(GROUPS_FILE, GROUPS)
    await show_group_topics(call, cid)


@dp.callback_query(F.data.startswith("ren_"))
async def cb_start_rename(call: types.CallbackQuery, state: FSMContext):
    try:
        _, cid_s, h = call.data.split("_", 2)
        cid = int(cid_s)
    except ValueError:
        await call.answer()
        return
    name = _topic_by_hash(cid, h)
    if not name:
        await call.answer("Тема не найдена", show_alert=True)
        return
    await state.update_data(old_topic_name=name, cid=cid)
    await state.set_state(SettingsStates.waiting_for_rename_new)
    await call.message.answer(
        f"Напиши новое название для темы <b>{esc(name)}</b> (группа «{esc(group_title(cid))}»):",
        parse_mode=ParseMode.HTML)
    await call.answer()


@dp.message(SettingsStates.waiting_for_rename_new, F.chat.type == "private")
async def cb_finish_rename(message: types.Message, state: FSMContext):
    data = await state.get_data()
    old_name = data.get("old_topic_name")
    cid = data.get("cid")
    new_name = (message.text or "").strip().lower()
    if not new_name:
        await message.answer("Пришли новое название текстом.")
        return
    topics = GROUPS.get(str(cid), {}).get("topics", {})
    if old_name and old_name in topics:
        topics[new_name] = topics.pop(old_name)
        save_json(GROUPS_FILE, GROUPS)
        await message.answer(f"✅ Тема <b>{esc(old_name)}</b> переименована в <b>{esc(new_name)}</b>!",
                             reply_markup=get_settings_keyboard(), parse_mode=ParseMode.HTML)
    await state.clear()


@dp.callback_query(F.data == "back_to_main_settings")
async def cb_back_settings(call: types.CallbackQuery):
    await safe_edit(call.message, settings_text(), get_settings_keyboard())
    await call.answer()

def get_current_account_settings() -> dict:
    """Return the settings for the current event, or the owner's background settings."""
    account = CURRENT_ACCOUNT.get()
    if account is not None:
        return account
    accounts = USER_SETTINGS.get("accounts", {})
    return next(iter(accounts.values()), USER_SETTINGS)


def media_menu_text() -> str:
    settings = get_current_account_settings()
    n = len(settings.get("reminder_media") or [])
    on = settings.get("media_enabled", True)
    return ("🎭 <b>Стикеры и GIF для напоминаний</b>\n"
            f"В коллекции: <b>{n}</b> · отправка: <b>{'включена' if on else 'выключена'}</b>\n\n"
            "К каждому напоминанию бот добавляет случайный стикер или GIF из коллекции.")


def media_menu_kb() -> InlineKeyboardMarkup:
    settings = get_current_account_settings()
    on = settings.get("media_enabled", True)
    rows = [[InlineKeyboardButton(text="➕ Добавить", callback_data="media_add_btn"),
             InlineKeyboardButton(text="🗑 Очистить", callback_data="media_clear_btn")],
            [InlineKeyboardButton(text="🔔 Выключить" if on else "🔕 Включить", callback_data="media_toggle_btn")],
            [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_to_main_settings")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "media_menu_btn")
async def cb_media_menu(call: types.CallbackQuery):
    await safe_edit(call.message, media_menu_text(), media_menu_kb())
    await call.answer()


@dp.callback_query(F.data == "media_add_btn")
async def cb_media_add(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(SettingsStates.waiting_for_media)
    done_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Готово", callback_data="media_done_btn")]])
    await call.message.answer("Пришли стикеры или GIF (можно несколько), затем нажми «Готово».", reply_markup=done_kb)
    await call.answer()


@dp.callback_query(F.data == "media_done_btn")
async def cb_media_done(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer(media_menu_text(), reply_markup=media_menu_kb(), parse_mode=ParseMode.HTML)
    await call.answer()


@dp.callback_query(F.data == "media_clear_btn")
async def cb_media_clear(call: types.CallbackQuery):
    settings = get_current_account_settings()
    settings["reminder_media"] = []
    save_json(SETTINGS_FILE, USER_SETTINGS)
    await safe_edit(call.message, media_menu_text(), media_menu_kb())
    await call.answer("Коллекция очищена")


@dp.callback_query(F.data == "media_toggle_btn")
async def cb_media_toggle(call: types.CallbackQuery):
    settings = get_current_account_settings()
    settings["media_enabled"] = not settings.get("media_enabled", True)
    save_json(SETTINGS_FILE, USER_SETTINGS)
    await safe_edit(call.message, media_menu_text(), media_menu_kb())
    await call.answer()


@dp.message(SettingsStates.waiting_for_media, F.chat.type == "private", F.sticker | F.animation)
async def media_received(message: types.Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    if message.sticker:
        item = {"kind": "sticker", "file_id": message.sticker.file_id, "uid": message.sticker.file_unique_id}
    else:
        item = {"kind": "animation", "file_id": message.animation.file_id, "uid": message.animation.file_unique_id}

    settings = get_current_account_settings()
    media = settings.setdefault("reminder_media", [])

    if any(m.get("uid") == item["uid"] for m in media):
        await message.reply("Это уже в коллекции.")
        return
    if len(media) >= 30:
        await message.reply("В коллекции уже 30 штук — это максимум. Очисти её в меню.")
        return
    media.append(item)
    save_json(SETTINGS_FILE, USER_SETTINGS)
    await message.reply(f"✅ Добавлено (всего {len(media)}). Пришли ещё или нажми «Готово» выше.")


@dp.message(SettingsStates.waiting_for_media, F.chat.type == "private")
async def media_wrong_input(message: types.Message):
    await message.reply("Пришли стикер или GIF, либо нажми «Готово».")


@dp.message(Command("alias"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_alias(message: types.Message, command: CommandObject):
    """/alias унн, unn — короткие названия группы для просьб из лички."""
    if not is_allowed(message):
        return
    g = ensure_group(message.chat)
    if not command.args:
        cur = ", ".join(g.get("aliases") or []) or "нет"
        await message.reply(f"Псевдонимы этой группы: {esc(cur)}\nЗадать: <code>/alias унн, unn</code>",
                            parse_mode=ParseMode.HTML)
        return
    if not await sender_can_manage(message):
        await message.reply("⛔ Менять псевдонимы могут только администраторы.")
        return
    raw = command.args
    parts = re.split(r"[,;\n]+", raw) if re.search(r"[,;\n]", raw) else raw.split()
    g["aliases"] = [x.strip().lower() for x in parts if x.strip()][:10]
    save_json(GROUPS_FILE, GROUPS)
    await message.reply(f"✅ Псевдонимы группы «{esc(g.get('title') or message.chat.id)}»: "
                        f"{esc(', '.join(g['aliases']))}", parse_mode=ParseMode.HTML)


# =====================================================================
# 13. МЕДИА: фото, документы, голосовые
# =====================================================================
async def _after_media(message: types.Message, status: types.Message, saved_text: str, done_text: str) -> None:
    """Если есть подпись — сразу решаем задачу, иначе просто сохраняем в контекст."""
    if message.caption:
        with suppress(Exception):
            await status.edit_text(done_text, parse_mode=None)
        await ask_gemini(message, caption_query(message.caption))
    else:
        with suppress(Exception):
            await status.edit_text(saved_text, parse_mode=None)


@dp.message(F.photo)
async def handle_photo(message: types.Message):
    if not is_allowed(message):
        return
    remember_file(mem_key(message), "photo", message.photo[-1].file_id, "Фотография")
    status = await message.reply("⏳ Загружаю изображение страницы...")
    try:
        async with chat_action(message.chat.id, ChatAction.UPLOAD_DOCUMENT, message.message_thread_id):
            raw = await download_bytes(message.photo[-1].file_id)
            data = await asyncio.to_thread(shrink_image, raw)  # ресайз вне event loop
        mem_add(mem_key(message), {
            "type": "part", "name": "Фотография страницы", "size": len(data),
            "part": genai_types.Part.from_bytes(data=data, mime_type="image/jpeg"),
        })
        await _after_media(message, status, "📷 Изображение сохранено в контекст темы!",
                           "📖 Фотография изучена! Решаю...")
    except Exception as e:
        log.exception("photo error")
        with suppress(Exception):
            await status.edit_text(f"Не удалось обработать фото: {e}", parse_mode=None)


async def build_document_item(name: str, data: bytes) -> dict:
    ext = os.path.splitext(name.lower())[1]
    if ext in IMAGE_EXT:
        small = await asyncio.to_thread(shrink_image, data)
        return {"type": "part", "name": name, "size": len(small),
                "part": genai_types.Part.from_bytes(data=small, mime_type="image/jpeg")}
    if ext == ".pdf":
        if len(data) <= INLINE_PDF_LIMIT:
            return {"type": "part", "name": name, "size": len(data),
                    "part": genai_types.Part.from_bytes(data=data, mime_type="application/pdf")}
        # большие PDF — через File API (привязан к текущему ключу)
        idx = pool.pick()
        if idx is None:
            raise RuntimeError("Все ключи Gemini на паузе, попробуй позже.")
        uploaded = await pool.client(idx).aio.files.upload(
            file=io.BytesIO(data),
            config=dict(mime_type="application/pdf", display_name=clean_filename(name)[:40]),
        )
        return {"type": "file", "name": name, "size": 1024, "file_obj": uploaded, "key_idx": idx}
    if ext == ".docx":
        text = await asyncio.to_thread(docx_to_text, data)
        if not text:
            raise ValueError("В DOCX не найден текст.")
        return {"type": "doc", "name": name, "size": len(text) * 2,
                "text": f"Документ «{name}»:\n{text[:60000]}"}
    if ext in TEXT_EXT:
        text = data.decode("utf-8", errors="replace")
        return {"type": "doc", "name": name, "size": len(text) * 2,
                "text": f"Документ «{name}»:\n{text[:60000]}"}
    raise ValueError(f"Формат «{ext or 'без расширения'}» не поддерживается. "
                     "Подойдут: PDF, DOCX, TXT/MD/CSV/код, изображения.")


@dp.message(F.document, ~F.animation)
async def handle_document(message: types.Message):
    if not is_allowed(message):
        return
    doc = message.document
    name = doc.file_name or "file"
    if doc.file_size and doc.file_size > TG_DOWNLOAD_LIMIT:
        await message.reply("⚠️ Файл больше 20 МБ — Telegram не даёт боту его скачать. Сожмите файл.")
        return
    remember_file(mem_key(message), "document", doc.file_id, name)
    status = await message.reply(f"⏳ Читаю документ «{name}»...", parse_mode=None)
    try:
        async with chat_action(message.chat.id, ChatAction.UPLOAD_DOCUMENT, message.message_thread_id):
            data = await download_bytes(doc.file_id)
            item = await build_document_item(name, data)
        mem_add(mem_key(message), item)
        await _after_media(message, status, f"📄 Документ «{name}» сохранён в память темы!",
                           "📖 Документ изучен! Обрабатываю задачу...")
    except Exception as e:
        log.exception("document error")
        with suppress(Exception):
            await status.edit_text(f"Не удалось обработать документ: {e}", parse_mode=None)


@dp.message(F.voice | F.audio)
async def handle_voice(message: types.Message):
    if not is_allowed(message):
        return
    # В группе реагируем только на голосовые-ответы боту, чтобы не слушать всех подряд
    if message.chat.type != "private":
        rep = message.reply_to_message
        if not (rep and rep.from_user and rep.from_user.id == BOT_ID):
            return
    media = message.voice or message.audio
    if media.file_size and media.file_size > 15 * 1024 * 1024:
        await message.reply("⚠️ Аудио слишком большое (макс. 15 МБ).")
        return
    if getattr(media, "duration", 0) and media.duration > 600:
        await message.reply("⚠️ Аудио длиннее 10 минут — пришли покороче.")
        return
    mime = "audio/ogg" if message.voice else (message.audio.mime_type or "audio/mpeg")
    try:
        async with chat_action(message.chat.id, ChatAction.TYPING, message.message_thread_id):
            raw = await download_bytes(media.file_id)
        part = genai_types.Part.from_bytes(data=raw, mime_type=mime)
        prompt = ("Это голосовое сообщение. 1) Дословно расшифруй речь. 2) Затем выполни просьбу или ответь по сути. "
                  "Формат: первая строка «🎤 Расшифровка: …», затем пустая строка и ответ. "
                  "Публикуй в тему (функция) только если об этом явно просят.")
        if message.caption:
            prompt += f"\nПодпись к сообщению: {message.caption}"
        await ask_gemini(message, prompt, extra_parts=[part], force_tools=True,
                         remember_as="[голосовое сообщение]")
    except Exception as e:
        log.exception("voice error")
        await message.reply("Не разобрал голосовое 🎧 Запиши ещё раз или напиши текстом.", parse_mode=None)


def load_plugins() -> None:
    """Подключает ext_webapp / ext_timetable / ext_study / ext_growth."""
    core = sys.modules[__name__]
    for name in ("ext_webapp", "ext_timetable", "ext_study", "ext_growth"):
        try:
            importlib.import_module(name).setup(core)
            log.info("🧩 Модуль %s подключён", name)
        except ModuleNotFoundError as e:
            if e.name == name:
                log.warning("Модуль %s не найден — функции отключены", name)
            else:
                log.exception("Модуль %s не загрузился (не хватает зависимости %s)", name, e.name)
        except Exception:
            log.exception("Модуль %s не загрузился", name)


load_plugins()


# =====================================================================
# 14. ТЕКСТОВЫЕ СООБЩЕНИЯ (регистрируется ПОСЛЕДНИМ)
# =====================================================================
@dp.message(F.text)
async def handle_text_messages(message: types.Message):
    if not is_allowed(message):
        return
    text = message.text.strip()
    if text.startswith("/"):
        return  # неизвестные команды игнорируем
    thread_id = message.message_thread_id
    is_group = message.chat.type != "private"
    key = mem_key(message)

    # Привязка топика: «тема Название» или «Гемини, тема Название» (только в основной группе)
    query = extract_query(text, is_group)
    cand = query if query is not None else text
    if is_group and thread_id and cand.lower().startswith(("тема ", "топик ")):
        if not await sender_can_manage(message):
            await message.reply("⛔ Привязывать темы могут только админы.")
            return
        raw_name = cand.split(maxsplit=1)[1].strip().lower()
        g = ensure_group(message.chat)
        g["topics"][raw_name] = thread_id
        save_json(GROUPS_FILE, GROUPS)
        await message.reply(
            f"✅ Запомнил! Тема <b>{esc(raw_name)}</b> привязана к ID <code>{thread_id}</code> "
            f"(группа «{esc(g.get('title') or message.chat.id)}»)", parse_mode=ParseMode.HTML)
        return

    if query is None:
        # не обращение к боту — просто копим контекст ветки
        who = (message.from_user.first_name if message.from_user else None) or "Участник"
        mem_add(key, {"type": "text", "text": f"{who}: {text}"[:3000]})
        return
    if not query:
        await message.reply("Слушаю! Какую задачу поставить?")
        return
    await ask_gemini(message, query)


# =====================================================================
# 15. ВЕБ-СЕРВЕР ДЛЯ RENDER + ЗАПУСК
# =====================================================================
async def health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "model": GEMINI_MODEL, "keys": len(pool.keys)})


async def start_web_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    for setup_web in WEB_SETUP:
        try:
            setup_web(app)
        except Exception:
            log.exception("web setup")
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        log.info("🌐 Health-check сервер на порту %d", PORT)
    except OSError as err:
        if err.errno in (10048, 98):
            alt_port = PORT + 1
            log.warning("Порт %d занят другим процессом. Поднимаю сервер на порту %d...", PORT, alt_port)
            await web.TCPSite(runner, "0.0.0.0", alt_port).start()
            log.info("🌐 Health-check сервер на порту %d", alt_port)
        else:
            raise
    return runner


async def keep_alive() -> None:
    """Самопинг раз в 10 минут (на Render URL подставляется автоматически).
    Не заменяет внешний пингер (UptimeRobot) — см. README."""
    url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("KEEPALIVE_URL")
    if not url:
        return
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        while True:
            await asyncio.sleep(600)
            with suppress(Exception):
                await s.get(url.rstrip("/") + "/health")


async def run_bot() -> None:
    global BOT_USERNAME, BOT_ID
    await bot.delete_webhook(drop_pending_updates=False)
    me = await bot.me()
    BOT_USERNAME, BOT_ID = (me.username or "").lower(), me.id
    await setup_bot_commands()
    if webapp_url():
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="📱 Приложение", web_app=WebAppInfo(url=webapp_url())))
        except Exception as e:
            log.warning("Не удалось назначить кнопку мини-приложения: %s", e)
    log.info("🚀 Бот @%s запущен. Модель: %s, ключей: %d", BOT_USERNAME, GEMINI_MODEL, len(pool.keys))
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


async def main() -> None:
    await gist_pull()
    load_state()
    runner = await start_web_server()
    ka = asyncio.create_task(keep_alive())
    sched = asyncio.create_task(scheduler_loop())
    try:
        while True:
            try:
                await run_bot()
                break  # штатная остановка polling
            except TelegramConflictError:
                log.error("409 Conflict: бот уже запущен в другом месте. Жду 30 c...")
                await asyncio.sleep(30)
            except Exception as e:
                log.error("⚠️ Сбой соединения: %s. Переподключение через 5 секунд...", e)
                await asyncio.sleep(5)
    finally:
        ka.cancel()
        sched.cancel()
        await asyncio.gather(ka, sched, return_exceptions=True)
        await flush_gist()
        await bot.session.close()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Бот выключен.")
