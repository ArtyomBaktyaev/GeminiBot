# -*- coding: utf-8 -*-
"""
Рост v2.1:
- Авто-починка карточек ext_study после разбора конспектов
- Чистая математика без сырого LaTeX ($\Pi_1$, \neq -> Π₁, ≠)
- Красивый утренний брифинг без обрезанных слов
- Живая ИИ-рефлексия без шаблонов + удаление + группировка по дням
- Экспорт разборов и отчёта прогресса в красивый PDF
- Отмена чекина пар и интеграция с расписанием
"""
import asyncio
import functools
import json
import os
import re
import secrets
import sqlite3
import sys
import time
from contextlib import closing, suppress
from datetime import date, timedelta
from urllib.parse import parse_qsl

import aiohttp
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from google.genai import types as genai_types

core = None
HERE = os.path.dirname(os.path.abspath(__file__))
FILE = "growth.json"
DB_FILE = os.path.join(HERE, "growth.db")

XP = {
    "cards": 5,
    "task": 15,
    "ask": 5,
    "goal": 10,
    "journal": 12,
    "duel": 25,
    "lesson": 15,
    "pomodoro": 15,
    "exam_day": 10,
}
XP_LABELS = {
    "cards": "🃏 Повторение карточек",
    "task": "✅ Выполнение задачи",
    "ask": "🤖 Разбор темы с ИИ",
    "goal": "🎯 Выполнение привычки",
    "journal": "💬 Запись в рефлексии",
    "duel": "⚔️ Дуэль знаний",
    "lesson": "🎓 Посещение пар",
    "pomodoro": "⏱ Фокус-сессия (25 мин)",
    "exam_day": "📚 Изучен день подготовки к экзамену",
    "streak_bonus": "🔥 Бонус за закрытие квеста дня",
}
QUEST_KEYS = ("cards", "task", "ask")
DB = {"u": {}, "duels": {}, "groups": {}}


# =====================================================================
# 1. КОНВЕРТЕР ФОРМУЛ (УБИРАЕТ СЫРОЙ LaTeX В КОНСПЕКТАХ И ТЕСТАХ)
# =====================================================================
_SUB_DIGITS = str.maketrans("0123456789+-=()nijk", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₙᵢⱼₖ")
_SUP_DIGITS = str.maketrans("0123456789+-=()n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿ")


def clean_math(text: str) -> str:
    """Превращает $\\Pi_1: A_1x$, \\neq, \\Delta и др. в чистый читаемый вид без $ и слэшей."""
    if not text:
        return ""
    s = str(text)
    # 1. Сначала прогоняем блочные и строчные $...$ через штатный tex_plain из bot.py
    if core and hasattr(core, "tex_plain"):
        s = re.sub(
            r"\$\$(.+?)\$\$|\\\[(.+?)\\\]|\\\((.+?)\\\)|\$(?!\s)([^$\n]+?)(?<!\s)\$",
            lambda m: core.tex_plain(next(g for g in m.groups() if g is not None)),
            s,
            flags=re.S,
        )
        # Если остались одиночные команды LaTeX вне знаков $
        for cmd, sym in getattr(core, "TEX_SYMBOLS", {}).items():
            s = re.sub(rf"\\{re.escape(cmd)}(?![A-Za-z])", sym, s)
    # 2. Чистим частые конструкции вроде A_1, \Pi_1, x^2, \frac{a}{b}
    s = re.sub(r"\\(?: dfrac|tfrac|frac)\s*\{([^{}]+)\}\s*\{([^{}]+)\}", r"(\1)/(\2)", s)
    s = re.sub(r"\\sqrt\s*\{([^{}]+)\}", r"√(\1)", s)
    s = re.sub(r"_\{([0-9+\-=()nijk]+)\}", lambda m: m.group(1).translate(_SUB_DIGITS), s)
    s = re.sub(r"_([0-9nijk])\b", lambda m: m.group(1).translate(_SUB_DIGITS), s)
    s = re.sub(r"\^\{([0-9+\-=()n]+)\}", lambda m: m.group(1).translate(_SUP_DIGITS), s)
    s = re.sub(r"\^([0-9n])\b", lambda m: m.group(1).translate(_SUP_DIGITS), s)
    s = s.replace("$", "")
    return s


# =====================================================================
# 2. ХРАНИЛИЩЕ SQLite + АВТО-ПОЧИНКА КАРТОЧЕК EXT_STUDY
# =====================================================================
def _init_sqlite() -> None:
    with closing(sqlite3.connect(DB_FILE, timeout=10)) as con:
        con.execute("CREATE TABLE IF NOT EXISTS kv_store (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        con.commit()


def _repair_study_cards() -> None:
    """Чинит карточки в ext_study и защищает /api/home от падений при проверке due."""
    st = sys.modules.get("ext_study")
    if not st or not hasattr(st, "ST") or not isinstance(st.ST, dict):
        return
    cards = st.ST.get("cards")
    if not isinstance(cards, list):
        return

    # Определяем эталонный формат поля due по функции card_stats
    fixed = []
    t_str = str(day())
    for idx, c in enumerate(cards, 1):
        if not isinstance(c, dict) or not c.get("front") or not c.get("back"):
            continue
        c.setdefault("id", idx)
        c.setdefault("ph", "")
        c.setdefault("ex", "")
        c.setdefault("deck", "общее")
        c["box"] = int(c.get("box") or 1)
        c["front"] = clean_math(str(c["front"]))
        c["back"] = clean_math(str(c["back"]))
        if not isinstance(c.get("due"), str) or len(str(c.get("due"))) != 10:
            c["due"] = t_str
        fixed.append(c)
    st.ST["cards"] = fixed
    if fixed:
        st.ST["next_id"] = max(int(x.get("id") or 0) for x in fixed) + 1
    with suppress(Exception):
        st.save()


def load() -> None:
    _init_sqlite()
    loaded = {}
    with closing(sqlite3.connect(DB_FILE, timeout=10)) as con:
        rows = con.execute("SELECT k, v FROM kv_store").fetchall()
        for k, v in rows:
            with suppress(Exception):
                loaded[k] = json.loads(v)
    gist_data = core.load_json(FILE, {}) or {}
    for section in ("u", "duels", "groups"):
        merged = dict(gist_data.get(section) or {})
        merged.update(loaded.get(section) or {})
        DB[section] = merged
    _save_sqlite()
    _repair_study_cards()


def _save_sqlite() -> None:
    try:
        with closing(sqlite3.connect(DB_FILE, timeout=10)) as con:
            for k in ("u", "duels", "groups"):
                con.execute(
                    "INSERT OR REPLACE INTO kv_store (k, v) VALUES (?, ?)",
                    (k, json.dumps(DB.get(k, {}), ensure_ascii=False)),
                )
            con.commit()
    except Exception:
        if core:
            core.log.exception("sqlite save error")


def save() -> None:
    _save_sqlite()
    core.save_json(FILE, DB)


def day() -> date:
    return core.now_local().date()


def W():
    return sys.modules["ext_webapp"]


def gid(request) -> str:
    return dict(parse_qsl(request.headers.get("X-Init-Data", ""))).get("chat_instance", "")


# =====================================================================
# 3. ПРОФИЛЬ ИГРОКА, XP, СЕРИЯ И ЕЖЕДНЕВНЫЙ КВЕСТ
# =====================================================================
def U(request, user) -> dict:
    acc = W().get_request_account(request, user)
    dirty = False
    if acc["id"] not in DB["u"]:
        dirty = True
    u = DB["u"].setdefault(
        acc["id"],
        {
            "xp": 0,
            "streak": 0,
            "best_streak": 0,
            "freeze": 1,
            "last": "",
            "qd": "",
            "flags": [],
            "xp_log": [],
            "goals": [
                {"id": "g_sport", "name": "Спорт / Разминка", "cat": "body", "days": []},
                {"id": "g_eng", "name": "Английский 15 мин", "cat": "mind", "days": []},
                {"id": "g_code", "name": "Код / Пет-проект", "cat": "work", "days": []},
            ],
            "journal": [],
            "journal_analysis": "",
            "plan": [],
            "plan_subject": "",
            "plan_date": "",
            "plan_analysis": "",
            "notify": True,
            "gids": [],
            "lessons_checked": "",
        },
    )
    u.setdefault("xp_log", [])
    u.setdefault("journal_analysis", "")
    u.setdefault("plan_analysis", "")
    u.update(
        id=acc["id"],
        tg=int(user["id"]),
        name=acc.get("name") or user.get("first_name") or "Игрок",
        avatar=acc.get("avatar") or "🧑‍💻",
    )
    for g in u.get("goals", []):
        g.setdefault("cat", "mind")
    for j in u.get("journal", []):
        if not j.get("id"):
            j["id"] = secrets.token_hex(4)
        if "msgs" not in j:
            j["msgs"] = [
                {"role": "user", "t": j.get("t", "")},
                {"role": "ai", "t": j.get("ai", "")},
            ]
    t_str = str(day())
    if u.get("qd") != t_str:
        u["qd"], u["flags"] = t_str, []
        dirty = True
    if dirty:
        save()
    return u


def _log_xp(u: dict, kind: str, delta: int, custom_label: str = "") -> None:
    log_list = u.setdefault("xp_log", [])
    log_list.append({
        "id": secrets.token_hex(3),
        "ts": time.time(),
        "d": str(day()),
        "kind": kind,
        "xp": delta,
        "lvl": max(1, int(u.get("xp", 0)) // 100 + 1),
        "label": custom_label or XP_LABELS.get(kind, kind),
    })
    del log_list[:-80]


def award(u: dict, kind: str, custom_label: str = "") -> bool:
    pts = XP.get(kind, 5)
    u["xp"] = max(0, int(u.get("xp", 0)) + pts)
    _log_xp(u, kind, pts, custom_label)

    quest_alias = "task" if kind in ("task", "goal", "lesson", "pomodoro") else ("ask" if kind in ("ask", "journal", "duel") else kind)
    if kind not in u["flags"]:
        u["flags"].append(kind)
    if quest_alias not in u["flags"]:
        u["flags"].append(quest_alias)

    t = day()
    t_str = str(t)
    quest_completed = sum(1 for k in QUEST_KEYS if k in u["flags"]) >= 3
    if quest_completed and u.get("last") != t_str:
        u["prev_last"] = u.get("last", "")
        if u.get("last") == str(t - timedelta(days=1)):
            u["streak"] = int(u.get("streak", 0)) + 1
        elif u.get("last") == str(t - timedelta(days=2)) and int(u.get("freeze", 0)) > 0:
            u["freeze"] -= 1
            u["streak"] = int(u.get("streak", 0)) + 1
        else:
            u["streak"] = 1
        u["best_streak"] = max(int(u.get("best_streak", 0)), u["streak"])
        u["last"] = t_str
        u["xp"] += 35
        _log_xp(u, "streak_bonus", 35)
        if u["streak"] % 7 == 0:
            u["freeze"] = min(3, int(u.get("freeze", 0)) + 1)
        return True
    return False


async def ai(request, user, prompt: str, files=None) -> str:
    w = W()
    acc = w.get_request_account(request, user)
    parts = (await w._decode_uploaded_files(files))[0] if files else []
    resp, _, _ = await w._generate_with_account_pool(
        acc,
        user,
        history=[],
        user_text=prompt,
        extra_parts=parts,
        use_tools=False,
        current_topic=None,
        last_files_names=[],
        scope=core.enabled_groups(),
        is_private=True,
    )
    return "".join(p.text for p in (resp.candidates[0].content.parts or []) if getattr(p, "text", None)).strip()


async def ai_direct(request, user, system_prompt: str, user_text: str, temp: float = 0.7) -> str:
    w = W()
    acc = w.get_request_account(request, user)
    pool_to_use, _ = w.get_account_pool(acc)
    if pool_to_use is None:
        raise RuntimeError("Подключите свой API-ключ Gemini в настройках профиля.")
    cfg = genai_types.GenerateContentConfig(system_instruction=system_prompt, temperature=temp)
    for model_name in pool_to_use.get_model_order():
        idx = pool_to_use.pick_for_model(model_name)
        if idx is None:
            continue
        try:
            resp = await asyncio.wait_for(
                pool_to_use.client(idx).aio.models.generate_content(
                    model=model_name, contents=[user_text], config=cfg
                ),
                timeout=14,
            )
            return "".join(p.text for p in (resp.candidates[0].content.parts or []) if getattr(p, "text", None)).strip()
        except Exception:
            continue
    return await ai(request, user, f"{system_prompt}\n\nЗапись пользователя: {user_text}")


def jl(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("ИИ вернул некорректный ответ, попробуйте ещё раз")
    return json.loads(m.group(0))


TREE_STAGES = [
    {"stage": 1, "min_score": 0, "min_lvl": 1, "title": "Семя Импульса", "desc": "Пробуждение био-ядра в кратере острова. Первые два ростка и энергетическое кольцо."},
    {"stage": 2, "min_score": 4, "min_lvl": 2, "title": "Неоновый Росток", "desc": "Молодое деревце с гибким стволом, первыми ветвями и парящими кристаллами фокуса."},
    {"stage": 3, "min_score": 10, "min_lvl": 4, "title": "Древо Синтеза", "desc": "Развилистая крона, глубокие корни в породе острова и биолюминесцентные соцветия."},
    {"stage": 4, "min_score": 20, "min_lvl": 7, "title": "Кристальная Роща", "desc": "Главное древо в окружении двух малых деревьев, водопад эфира и спутники-осколки."},
    {"stage": 5, "min_score": 35, "min_lvl": 12, "title": "Парящий Эдем", "desc": "Древнее древо с золотисто-изумрудной короной и двойными орбитальными кольцами."},
    {"stage": 6, "min_score": 60, "min_lvl": 20, "title": "Древо Сингулярности", "desc": "Мировое древо Иггдрасиль с галактическим порталом в кроне и квантовым лучом."},
]


def _companion_stage(streak: int, level: int) -> dict:
    score = streak * 2 + level
    cur = TREE_STAGES[0]
    for st in TREE_STAGES:
        if score >= st["min_score"] or level >= st["min_lvl"]:
            cur = st
    return {**cur, "score": score, "roadmap": TREE_STAGES}


# =====================================================================
# 4. API ОБРАБОТЧИКИ
# =====================================================================
async def a_state(request, user):
    u, g = U(request, user), gid(request)
    if g and (g not in DB["groups"] or g not in u["gids"]):
        DB["groups"].setdefault(g, {"id": g, "code": g[:8].upper(), "title": "Группа чата", "owner": u["id"], "dl": []})
        if g not in u["gids"]:
            u["gids"].append(g)
        save()
    t = str(day())
    xp = int(u.get("xp", 0))
    level = xp // 100 + 1
    streak = int(u.get("streak", 0))
    done_today = u.get("last") == t
    risk = bool(streak > 0 and not done_today)
    comp = _companion_stage(streak, level)

    quest_steps = [
        {"id": "cards", "title": "Повторить карточки", "done": "cards" in u["flags"], "xp": XP["cards"], "tab": "study"},
        {"id": "task", "title": "Закрыть задачу или привычку", "done": any(x in u["flags"] for x in ("task", "goal", "lesson", "pomodoro")), "xp": XP["task"], "tab": "tasks"},
        {"id": "ask", "title": "Вопрос ИИ / Разбор / Дневник", "done": any(x in u["flags"] for x in ("ask", "journal", "duel")), "xp": XP["ask"], "tab": "chat"},
    ]

    return {
        "name": u["name"],
        "avatar": u.get("avatar", "🧑‍💻"),
        "xp": xp,
        "xp_in_level": xp % 100,
        "level": level,
        "xp_log": list(reversed(u.get("xp_log", [])[-40:])),
        "streak": streak,
        "best_streak": int(u.get("best_streak", streak)),
        "freeze": int(u.get("freeze", 1)),
        "done": done_today,
        "flags": u["flags"],
        "quest": quest_steps,
        "risk": risk,
        "companion": comp,
        "today": t,
        "lessons_checked": u.get("lessons_checked") == t,
        "goals": [{**x, "days": x["days"][-14:]} for x in u["goals"]],
        "journal": u["journal"][-30:],
        "journal_analysis": u.get("journal_analysis", ""),
        "plan": u["plan"],
        "plan_subject": u.get("plan_subject", ""),
        "plan_date": u.get("plan_date", ""),
        "plan_analysis": u.get("plan_analysis", ""),
        "notify": bool(u.get("notify", True)),
        "bot": getattr(core, "BOT_USERNAME", "") or os.environ.get("BOT_USERNAME", ""),
    }


async def a_quest_undo(request, user):
    """Отмена любого шага ежедневного квеста (карточки, задача, вопрос ИИ)."""
    b, u = await request.json(), U(request, user)
    qid = str(b.get("id") or "")
    t_str = str(day())
    group_map = {
        "cards": ("cards",),
        "task": ("task", "goal", "lesson", "pomodoro"),
        "ask": ("ask", "journal", "duel"),
    }
    to_remove = group_map.get(qid, (qid,))
    removed_any = False
    for k in to_remove:
        if k in u["flags"]:
            u["flags"].remove(k)
            removed_any = True
    if removed_any:
        pts = XP.get(qid, 5)
        u["xp"] = max(0, int(u.get("xp", 0)) - pts)
        _log_xp(u, qid, -pts, f"↩️ Отмена: {XP_LABELS.get(qid, qid)}")
        # Если из-за отмены квест дня перестал быть закрытым — откатываем сегодняшний бонус
        if sum(1 for k in QUEST_KEYS if k in u["flags"]) < 3 and u.get("last") == t_str:
            u["last"] = u.get("prev_last", "")
            u["streak"] = max(0, int(u.get("streak", 1)) - 1)
            u["xp"] = max(0, int(u.get("xp", 0)) - 35)
            _log_xp(u, "streak_bonus", -35, "↩️ Отмена бонуса квеста")
        save()
    return {"ok": True, "xp": u["xp"]}


async def a_card_undo(request, user):
    """Откат только что отвеченной карточки во время сессии повторения."""
    b, u = await request.json(), U(request, user)
    cid = int(b.get("id") or 0)
    prev_box = int(b.get("box") or 1)
    prev_due = str(b.get("due") or day())
    st = sys.modules.get("ext_study")
    if st:
        async with W().plugin_ctx(user):
            for c in st.ST.get("cards", []):
                if int(c.get("id") or 0) == cid:
                    c["box"] = prev_box
                    c["due"] = prev_due
                    st.save()
                    break
    u["xp"] = max(0, int(u.get("xp", 0)) - XP["cards"])
    save()
    return {"ok": True}


async def a_pomodoro_done(request, user):
    """Начисление опыта за завершённый 25-минутный фокус-таймер на Главной."""
    u = U(request, user)
    award(u, "pomodoro")
    save()
    return {"ok": True, "xp": u["xp"]}


async def a_story_image(request, user):
    """Сохраняет PNG сторис-карточки на сервере и может сразу прислать её пользователю в чат бота."""
    import base64
    b, u = await request.json(), U(request, user)
    data_url = str(b.get("data_url") or "")
    if "," in data_url:
        data_url = data_url.split(",", 1)[1]
    if not data_url:
        raise ValueError("Пустое изображение")
    raw = base64.b64decode(data_url)
    fname = f"streak-{u.get('streak', 1)}-days.png"
    fid = W()._store_generated_file(raw, fname, "image/png")

    if b.get("send_tg") and u.get("tg"):
        photo = core.BufferedInputFile(raw, filename=fname)
        caption = (
            f"🔥 <b>Моя серия: {u.get('streak', 0)} дн.</b> (Уровень {int(u.get('xp', 0)) // 100 + 1} · {u.get('xp', 0)} XP)\n"
            "Сохрани картинку в галерею или выложи в Сторис в 1 клик!"
        )
        await core.bot.send_photo(u["tg"], photo, caption=caption, parse_mode="HTML")

    return {"ok": True, "id": fid, "name": fname, "size": len(raw)}


async def a_checkin_lessons(request, user):
    u, t = U(request, user), str(day())
    if u.get("lessons_checked") == t:
        u["lessons_checked"] = ""
        if "lesson" in u["flags"]:
            u["flags"].remove("lesson")
        u["xp"] = max(0, int(u.get("xp", 0)) - XP["lesson"])
        _log_xp(u, "lesson", -XP["lesson"], "↩️ Отмена чекина пар")
        save()
        return {"ok": True, "undone": True, "xp": u["xp"]}
    u["lessons_checked"] = t
    award(u, "lesson")
    save()
    return {"ok": True, "undone": False, "xp": u["xp"]}


async def a_snap(request, user):
    b, u = await request.json(), U(request, user)
    kind = b.get("kind", "photo")
    hint = {
        "photo": "На фото задача, слайд или конспект: подробно разбери по шагам и выдели суть.",
        "voice": "Это аудиозапись с лекции или голосовая заметка: сделай структурированный конспект и выдели ключевые термины.",
    }.get(kind, "Разбери тему подробно и по шагам.")
    q = str(b.get("text") or "Разбери материал.").strip()[:3000]

    prompt = (
        f"{hint}\nЯзык: русский. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать LaTeX ($...$, \\Pi, \\neq, \\Delta, _1). "
        "Все формулы, греческие буквы и индексы пиши сразу готовыми Unicode-символами (Π₁, A₁, x², ≠, ≤, ≥, Δ, α, √, →).\n"
        "Верни СТРОГО валидный JSON следующего формата:\n"
        '{\n'
        '  "title": "Краткое название предмета/темы (например: Аналитическая геометрия)",\n'
        '  "answer": "Подробный разбор в Markdown (заголовки ##, списки, жирный текст, формулы в Unicode)",\n'
        '  "cards": [{"front": "Термин или вопрос", "back": "Краткое определение или формула"}],\n'
        '  "quiz": [{"q": "Вопрос на проверку понимания", "options": ["Вариант 1","Вариант 2","Вариант 3","Вариант 4"], "answer": 0, "explain": "Пояснение"}]\n'
        '}\n'
        "Сделай от 4 до 6 карточек и ровно 3 тестовых вопроса.\n\n"
        f"Запрос/контекст: {q}"
    )
    r = jl(await ai(request, user, prompt, b.get("files")))
    deck_title = clean_math(str(r.get("title") or "Конспект ИИ").strip())[:40]
    clean_ans = clean_math(str(r.get("answer") or ""))

    cards = []
    for c in (r.get("cards") or [])[:8]:
        if isinstance(c, dict) and c.get("front") and c.get("back"):
            cards.append({
                "front": clean_math(str(c["front"]).strip())[:200],
                "back": clean_math(str(c["back"]).strip())[:400],
            })

    quiz = []
    for qz in (r.get("quiz") or [])[:5]:
        if isinstance(qz, dict) and qz.get("q") and isinstance(qz.get("options"), list) and len(qz["options"]) >= 2:
            ans_idx = 0
            with suppress(Exception):
                ans_idx = max(0, min(len(qz["options"]) - 1, int(qz.get("answer", 0))))
            quiz.append({
                "q": clean_math(str(qz["q"]).strip()),
                "options": [clean_math(str(o).strip()) for o in qz["options"][:4]],
                "answer": ans_idx,
                "explain": clean_math(str(qz.get("explain") or "").strip()),
            })

    saved_cards = 0
    st = sys.modules.get("ext_study")
    if st and cards and b.get("save_cards", True):
        try:
            async with W().plugin_ctx(user):
                if isinstance(st.ST, dict) and isinstance(st.ST.get("cards"), list):
                    next_id = max([int(x.get("id") or 0) for x in st.ST["cards"]] + [int(st.ST.get("next_id") or 1)])
                    for c in cards:
                        next_id += 1
                        st.ST["cards"].append({
                            "id": next_id,
                            "front": c["front"],
                            "ph": "",
                            "back": c["back"],
                            "ex": "",
                            "deck": deck_title,
                            "box": 1,
                            "due": str(day()),
                        })
                        saved_cards += 1
                    st.ST["next_id"] = next_id + 1
                    _repair_study_cards()
        except Exception:
            core.log.exception("auto save cards")

    award(u, "ask")
    if saved_cards:
        award(u, "cards")
    save()
    return {
        "title": deck_title,
        "answer": clean_ans,
        "cards": cards,
        "saved_cards": saved_cards,
        "quiz": quiz,
    }


async def a_pdf_export(request, user):
    b, u = await request.json(), U(request, user)
    kind = str(b.get("kind") or "report")
    if kind in ("snap", "custom"):
        title = clean_math(str(b.get("title") or "Документ").strip())
        content = clean_math(str(b.get("content") or "").strip())
    elif kind == "journal":
        title = f"Дневник рефлексии — {u['name']} ({day():%d.%m.%Y})"
        lines = [f"# Дневник рефлексии и разбор: {u['name']}", ""]
        if u.get("journal_analysis"):
            lines.extend(["## ИИ-анализ за последние 7 дней", clean_math(u["journal_analysis"]), ""])
        lines.append("## История диалогов рефлексии")
        for j in reversed(u.get("journal", [])[-20:]):
            lines.append(f"### 📅 {j.get('d', '')}")
            for m in j.get("msgs") or [{"role": "user", "t": j.get("t", "")}, {"role": "ai", "t": j.get("ai", "")}]:
                who = "👤 Вы" if m.get("role") == "user" else "💡 Ментор"
                lines.append(f"**{who}:** {clean_math(m.get('t', ''))}")
            lines.append("")
        content = "\n".join(lines)
    else:
        title = f"Отчёт прогресса — {u['name']} ({day():%d.%m.%Y})"
        comp = _companion_stage(int(u.get("streak", 0)), int(u.get("xp", 0)) // 100 + 1)
        lines = [
            f"# Профиль роста: {u['name']}",
            f"- **Серия без пропусков:** {u.get('streak', 0)} дн. (рекорд: {u.get('best_streak', u.get('streak', 0))} дн.)",
            f"- **Уровень и опыт:** Уровень {int(u.get('xp', 0)) // 100 + 1} ({u.get('xp', 0)} XP)",
            f"- **Стадия острова:** {comp['title']} (стадия {comp['stage']} из 6)",
            f"- **Запас заморозок:** {u.get('freeze', 0)} шт.",
            "",
            "## Активные цели и привычки (за 14 дней)",
        ]
        if u.get("goals"):
            lines.append("| Привычка | Выполнено дней (14 дн.) | Статус сегодня |")
            lines.append("| --- | --- | --- |")
            for g in u["goals"]:
                done_now = "Выполнено" if str(day()) in (g.get("days") or []) else "Ожидает"
                lines.append(f"| {clean_math(g['name'])} | {len(g.get('days') or [])} дн. | {done_now} |")
        if u.get("plan"):
            lines.extend(["", f"## План подготовки: {clean_math(u.get('plan_subject') or 'Экзамен')}"])
            if u.get("plan_analysis"):
                lines.append(clean_math(u["plan_analysis"]))
            lines.append("| Дата | Тема дня | Статус |")
            lines.append("| --- | --- | --- |")
            for p in u["plan"][:30]:
                st_done = "Изучено" if p.get("done") else "В плане"
                lines.append(f"| {p['d']} | {clean_math(p['t'])} | {st_done} |")
        content = "\n".join(lines)

    data, filename, mime = await asyncio.to_thread(core.build_document, "pdf", title, content)
    fid = W()._store_generated_file(data, filename, mime)
    return {"id": fid, "name": filename, "size": len(data), "mime": mime}


async def a_goal_add(request, user):
    b = await request.json()
    name = str(b.get("name") or "").strip()[:60]
    cat = str(b.get("cat") or "mind").strip()[:20]
    if name:
        U(request, user)["goals"].append({"id": secrets.token_hex(3), "name": name, "cat": cat, "days": []})
        save()
    return {"ok": True}


async def a_goal_del(request, user):
    u = U(request, user)
    u["goals"] = [g for g in u["goals"] if g["id"] != request.match_info["id"]]
    save()
    return {"ok": True}


async def a_goal_check(request, user):
    u, t = U(request, user), str(day())
    toggled_on = False
    for g in u["goals"]:
        if g["id"] == request.match_info["id"]:
            if t in g["days"]:
                g["days"].remove(t)
                u["xp"] = max(0, int(u.get("xp", 0)) - XP["goal"])
                _log_xp(u, "goal", -XP["goal"], f"↩️ Отмена: {g['name']}")
                toggled_on = False
            else:
                g["days"].append(t)
                award(u, "goal", f"🎯 Привычка: {g['name']}")
                toggled_on = True
    save()
    return {"ok": True, "checked": toggled_on, "xp": u["xp"]}


async def a_journal(request, user):
    text, u = str((await request.json()).get("text") or "").strip()[:2500], U(request, user)
    if not text:
        raise ValueError("Напишите хотя бы пару слов о сегодняшнем дне")

    words = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", text)
    is_short_or_random = len(text) < 8 or (len(words) <= 2 and len(text) < 18)

    sys_prompt = (
        "Ты живой, умный и искренний собеседник для вечернего дневника. Язык: русский.\n"
        "ЖЕСТКИЕ ПРАВИЛА:\n"
        "1. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать шаблонные фразы: «сегодня ты проделал огромную/отличную работу», "
        "«главный инсайт дня», «системность и чёткая структура побеждают хаос», «бесценный опыт».\n"
        "2. Отвечай СТРОГО по сути того, что написал человек.\n"
        "3. Если человек написал короткую тестовую фразу — ответь с лёгкой доброй иронией в 1–2 предложениях."
    )
    user_msg = (
        f"Пользователь написал короткую фразу: «{text}». Ответь живо и с юмором без шаблонов!"
        if is_short_or_random
        else f"Запись пользователя ({u['name']}):\n{text}\n\nДай тёплый отклик по сути (2–3 предложения) и задай один вопрос."
    )

    reply = clean_math(await ai_direct(request, user, sys_prompt, user_msg, temp=0.75))
    first = not any(j.get("d") == str(day()) for j in u["journal"])
    entry = {
        "id": secrets.token_hex(4),
        "d": str(day()),
        "t": text,
        "ai": reply,
        "msgs": [{"role": "user", "t": text}, {"role": "ai", "t": reply}],
    }
    u["journal"].append(entry)
    del u["journal"][:-50]
    if first and not is_short_or_random:
        award(u, "journal")
    save()
    return {"ai": reply, "entry": entry}


async def a_journal_reply(request, user):
    """Продолжение диалога внутри существующей записи рефлексии (без создания новой)."""
    b, u = await request.json(), U(request, user)
    jid = request.match_info["id"]
    reply_text = str(b.get("text") or "").strip()[:2000]
    if not reply_text:
        raise ValueError("Введите текст ответа")

    target = next((j for j in u.get("journal", []) if j.get("id") == jid), None)
    if not target:
        raise ValueError("Запись рефлексии не найдена")

    msgs = target.setdefault("msgs", [{"role": "user", "t": target.get("t", "")}, {"role": "ai", "t": target.get("ai", "")}])
    msgs.append({"role": "user", "t": reply_text})

    transcript = "\n".join(
        f"{'Пользователь' if m['role'] == 'user' else 'Ментор'}: {m['t']}"
        for m in msgs[-10:]
    )
    sys_prompt = (
        "Ты ведёшь тёплый вечерний диалог рефлексии с пользователем. "
        "Отвечай по сути его последнего сообщения в контексте вашей беседы (2–3 предложения, без пафоса и штампов). "
        "Если уместно, задай уточняющий вопрос или поддержи его вывод."
    )
    ai_ans = clean_math(await ai_direct(request, user, sys_prompt, transcript, temp=0.7))
    msgs.append({"role": "ai", "t": ai_ans})
    target["ai"] = ai_ans
    save()
    return {"ok": True, "entry": target, "ai": ai_ans}


async def a_session_toggle(request, user):
    """Включает или выключает режим «Сессионная неделя» для выбранного понедельника (скрывает обычные пары)."""
    b, u = await request.json(), U(request, user)
    mon = str(b.get("monday") or "").strip()[:10]
    if not mon:
        raise ValueError("Не указана дата недели")
    s_weeks = u.setdefault("session_weeks", [])
    if mon in s_weeks:
        s_weeks.remove(mon)
        active = False
    else:
        s_weeks.append(mon)
        active = True
    save()
    return {"ok": True, "is_session_week": active, "session_weeks": s_weeks}


async def a_session_exam_add(request, user):
    """Добавляет экзамен / коллоквиум / тест / лабу в выбранный день."""
    b, u = await request.json(), U(request, user)
    d_str = str(b.get("date") or "").strip()[:10]
    subj = str(b.get("subject") or "").strip()[:120]
    if not d_str or not subj:
        raise ValueError("Укажите предмет и дату")
    exam = {
        "id": str(b.get("id") or ("ex_" + secrets.token_hex(3))),
        "date": d_str,
        "start": str(b.get("start") or "10:00").strip()[:5],
        "end": str(b.get("end") or "12:00").strip()[:5],
        "subject": subj,
        "kind": str(b.get("kind") or "Экзамен").strip()[:40],
        "room": str(b.get("room") or "").strip()[:40],
        "teacher": str(b.get("teacher") or "").strip()[:60],
        "notes": str(b.get("notes") or "").strip()[:1000],
    }
    exams = u.setdefault("session_exams", [])
    exams[:] = [x for x in exams if x.get("id") != exam["id"]]
    exams.append(exam)
    save()
    return {"ok": True, "exam": exam}


async def a_session_exam_del(request, user):
    """Удаляет экзамен / коллоквиум / тест / лабу."""
    u = U(request, user)
    ex_id = request.match_info["id"]
    u["session_exams"] = [x for x in u.get("session_exams", []) if str(x.get("id")) != str(ex_id)]
    save()
    return {"ok": True}


async def a_journal_analyze7(request, user):
    """Глубокий ИИ-анализ рефлексий за последние 7 дней (что сделал круто, что исправить, план)."""
    u = U(request, user)
    cutoff = str(day() - timedelta(days=7))
    recent = [j for j in u.get("journal", []) if str(j.get("d", "")) >= cutoff]
    if not recent:
        recent = u.get("journal", [])[-7:]
    if not recent:
        raise ValueError("Пока нет записей в дневнике для анализа")

    dump = []
    for j in recent:
        msgs = j.get("msgs") or [{"role": "user", "t": j.get("t", "")}, {"role": "ai", "t": j.get("ai", "")}]
        dialog_str = " | ".join(f"{'Я' if m['role']=='user' else 'ИИ'}: {m['t']}" for m in msgs)
        dump.append(f"[{j.get('d')}]: {dialog_str}")

    sys_prompt = (
        "Ты аналитик личной эффективности и внимательный ментор. Проанализируй записи дневника пользователя за последние 7 дней.\n"
        "Структурируй ответ в Markdown строго по 4 блокам:\n"
        "## ✅ Что сделано правильно и сильные стороны\n"
        "## ⚠️ Где терялась энергия и что стоит скорректировать\n"
        "## 🔧 Что нужно доделать или закрыть\n"
        "## 🎯 Топ-3 конкретных фокуса на следующую неделю\n"
        "Пиши конкретно по фактам из его записей, без воды и общих фраз."
    )
    analysis = clean_math(await ai_direct(request, user, sys_prompt, "\n".join(dump), temp=0.5))
    u["journal_analysis"] = analysis
    save()
    return {"ok": True, "analysis": analysis}


async def a_journal_del(request, user):
    u = U(request, user)
    jid = request.match_info["id"]
    u["journal"] = [j for j in u.get("journal", []) if j.get("id") != jid]
    save()
    return {"ok": True}


async def a_plan(request, user):
    """Режим «Сдать за N дней» с поддержкой файлов билетов/тем, списка тем и подробным разбором каждого дня."""
    b, u = await request.json(), U(request, user)
    subj = str(b.get("subject") or "").strip()[:100]
    target_date = str(b.get("date") or "").strip()
    slot_time = str(b.get("time") or "19:00").strip()
    topics_raw = str(b.get("topics_text") or "").strip()[:6000]
    files = b.get("files") or []
    push_tt = bool(b.get("push_to_tt", True))

    if not subj or not target_date:
        raise ValueError("Укажите предмет и дату экзамена/коллоквиума")
    n = (date.fromisoformat(target_date) - day()).days
    if not 1 <= n <= 60:
        raise ValueError("Дата экзамена должна быть от 1 до 60 дней вперёд")

    extra_context = f"\nСписок тем / билетов от пользователя:\n{topics_raw}" if topics_raw else ""
    if files:
        extra_context += "\n(Также изучи прикреплённый файл с программой/вопросами к экзамену и распредели ВСЕ темы из него по дням)."

    prompt = (
        f'Составь подробный план подготовки к экзамену/коллоквиуму по предмету «{subj}».\n'
        f'Дней до сдачи: {n}, первый день подготовки: {day()}. '
        'Последние 1–2 дня оставь на генеральное повторение и прогон сложных билетов. '
        'Формулы пиши только Unicode-символами без LaTeX ($).\n'
        f'{extra_context}\n\n'
        'Верни СТРОГО JSON следующего формата:\n'
        '{\n'
        '  "analysis": "Краткая стратегия подготовки: как распределена нагрузка, на что сделать упор (2-4 предложения)",\n'
        '  "days": [\n'
        '    {\n'
        '      "d": "YYYY-MM-DD",\n'
        '      "t": "Название главного блока тем на этот день",\n'
        '      "sub": ["Подтема или билет 1", "Подтема или билет 2"],\n'
        '      "details": "Подробная инструкция на день: какие определения/теоремы выучить, какие типы задач прорешать и на какие ловушки обратить внимание"\n'
        '    }\n'
        '  ]\n'
        '}'
    )
    r = jl(await ai(request, user, prompt, files))
    plan_days = []
    for x in (r.get("days") or [])[:60]:
        if isinstance(x, dict) and x.get("d") and x.get("t"):
            plan_days.append({
                "d": str(x["d"]),
                "t": clean_math(str(x["t"])),
                "sub": [clean_math(str(s)) for s in (x.get("sub") or [])[:8]],
                "details": clean_math(str(x.get("details") or "")),
                "done": False,
                "deep_notes": "",
            })
    u["plan"] = plan_days
    u["plan_subject"] = subj
    u["plan_date"] = target_date
    u["plan_analysis"] = clean_math(str(r.get("analysis") or ""))

    added_to_tt = 0
    tt = sys.modules.get("ext_timetable")
    if tt and push_tt:
        try:
            async with W().plugin_ctx(user):
                tt.ST["exceptions"] = [
                    ex for ex in tt.ST.get("exceptions", [])
                    if not (ex.get("kind") == "add" and str(ex.get("subject", "")).startswith(f"🎯 Подготовка: {subj}"))
                ]
                h_start, m_start = [int(x) for x in (slot_time.split(":") if ":" in slot_time else ("19", "00"))[:2]]
                end_total = min(23 * 60 + 50, h_start * 60 + m_start + 45)
                end_time = f"{end_total // 60:02d}:{end_total % 60:02d}"
                for item in plan_days[:21]:
                    tt.ST["exceptions"].append({
                        "id": tt.ST["next_id"],
                        "date": str(item["d"]),
                        "kind": "add",
                        "start": f"{h_start:02d}:{m_start:02d}",
                        "end": end_time,
                        "subject": f"🎯 Подготовка: {subj}",
                        "room": "Самоподготовка",
                        "teacher": "",
                        "lesson_kind": "Подготовка к экзамену",
                        "homework": str(item["t"])[:200],
                    })
                    tt.ST["next_id"] += 1
                    added_to_tt += 1
                tt.save()
        except Exception:
            core.log.exception("push plan to timetable")

    save()
    return {"plan": u["plan"], "analysis": u["plan_analysis"], "added_to_tt": added_to_tt}


async def a_plan_check(request, user):
    """Отметка дня подготовки к экзамену изученным (+10 XP) или снятие отметки."""
    b, u = await request.json(), U(request, user)
    d_target = str(b.get("d") or "")
    for p in u.get("plan", []):
        if p.get("d") == d_target:
            p["done"] = not bool(p.get("done"))
            if p["done"]:
                award(u, "exam_day", f"📚 Изучен день: {p['t'][:40]}")
            else:
                u["xp"] = max(0, int(u.get("xp", 0)) - XP["exam_day"])
            save()
            return {"ok": True, "done": p["done"], "xp": u["xp"]}
    return {"ok": False}


async def a_plan_expand(request, user):
    """Генерация подробного конспекта-шпаргалки по темам конкретного дня подготовки."""
    b, u = await request.json(), U(request, user)
    d_target = str(b.get("d") or "")
    target = next((p for p in u.get("plan", []) if p.get("d") == d_target), None)
    if not target:
        raise ValueError("День не найден в плане")

    subj = u.get("plan_subject") or "Экзамен"
    subs = ", ".join(target.get("sub") or [])
    prompt = (
        f"Предмет: «{subj}». Тема дня: «{target['t']}» ({subs}).\n"
        "Напиши структурированный, глубокий конспект-шпаргалку для подготовки к экзамену/коллоквиуму:\n"
        "1. Главные определения и формулировки теорем.\n"
        "2. Ключевые формулы и свойства (используй чистые Unicode-символы без сырого LaTeX $).\n"
        "3. Алгоритм решения типовых задач по этой теме и частые вопросы преподавателей на защите."
    )
    notes = clean_math(await ai(request, user, prompt))
    target["deep_notes"] = notes
    save()
    return {"ok": True, "deep_notes": notes}

# =====================================================================
# 5. ГРУППОВЫЕ КОМНАТЫ И ЛИДЕРБОРД
# =====================================================================
def _active_group(request, u: dict) -> tuple:
    g = gid(request) or (u["gids"][-1] if u.get("gids") else "")
    gr = DB["groups"].get(g) if g else None
    return g, gr


async def a_board(request, user):
    u = U(request, user)
    g, gr = _active_group(request, u)
    pool_users = [v for v in DB["u"].values() if g in v.get("gids", [])] if gr else list(DB["u"].values())
    rows = sorted(
        (
            {
                "name": v["name"],
                "avatar": v.get("avatar", "🧑‍💻"),
                "xp": int(v.get("xp", 0)),
                "level": int(v.get("xp", 0)) // 100 + 1,
                "streak": int(v.get("streak", 0)),
                "me": v["id"] == u["id"],
            }
            for v in pool_users
        ),
        key=lambda r: (-r["xp"], -r["streak"]),
    )[:30]

    return {
        "rows": rows,
        "has": bool(gr),
        "code": gr.get("code", g[:8].upper()) if gr else "",
        "title": gr.get("title", "Комната группы") if gr else "Общий рейтинг",
        "owner": bool(gr and gr["owner"] == u["id"]),
        "dl": [x for x in (gr or {}).get("dl", []) if x["d"] >= str(day())],
    }


async def a_group_join(request, user):
    b, u = await request.json(), U(request, user)
    code = re.sub(r"[^A-Za-zА-Яа-я0-9_-]", "", str(b.get("code") or "")).upper()[:16]
    title = str(b.get("title") or "").strip()[:50]
    if not code:
        code = "GRP-" + secrets.token_hex(2).upper()

    found_key = next((k for k, v in DB["groups"].items() if v.get("code", "").upper() == code or k.upper() == code), None)
    if not found_key:
        found_key = code
        DB["groups"][found_key] = {
            "id": found_key,
            "code": code,
            "title": title or f"Поток {code}",
            "owner": u["id"],
            "dl": [],
        }
    if found_key in u["gids"]:
        u["gids"].remove(found_key)
    u["gids"].append(found_key)
    save()
    return {"ok": True, "code": code, "title": DB["groups"][found_key]["title"]}


async def a_dl_add(request, user):
    b, u = await request.json(), U(request, user)
    _, gr = _active_group(request, u)
    if not gr or gr["owner"] != u["id"]:
        raise ValueError("Добавлять общие дедлайны может только создатель/староста комнаты")
    t_text = str(b.get("t") or "").strip()[:140]
    d_str = str(b.get("d") or "").strip()
    if not t_text or not d_str:
        raise ValueError("Заполните дату и описание дедлайна")
    gr["dl"] = sorted(gr.get("dl", []) + [{"t": t_text, "d": d_str}], key=lambda x: x["d"])[:30]
    save()
    return {"ok": True}


async def a_notify(request, user):
    u = U(request, user)
    u["notify"] = not u.get("notify", True)
    save()
    return {"notify": u["notify"]}


# =====================================================================
# 6. АККУРАТНЫЙ УТРЕННИЙ БРИФИНГ (БЕЗ ОБРЫВКОВ СЛОВ И МУСОРА)
# =====================================================================
def _clean_task_summary(raw_text: str, max_len: int = 85) -> str:
    """Вычищает служебные строки («Ссылка на...», «Дедлайн:...») и обрезает строго по целым словам."""
    txt = clean_math(core.strip_md(raw_text or ""))
    cleaned_lines = []
    for line in txt.splitlines():
        s = line.strip(" \t•-*—")
        if not s:
            continue
        low = s.lower()
        if any(low.startswith(p) for p in ("дедлайн:", "📅 срок:", "срок:", "ссылка", "🔗", "✅ выполнил", "напоминание:")):
            # Если строка начинается с «Напоминание: сдать лабу», оставляем саму суть после двоеточия
            if low.startswith("напоминание:") and len(s) > 13:
                s = s[12:].strip()
            else:
                continue
        if re.fullmatch(r"https?://\S+", s):
            continue
        cleaned_lines.append(s)

    joined = re.sub(r"\s+", " ", " · ".join(cleaned_lines)).strip()
    if not joined:
        joined = re.sub(r"\s+", " ", txt).strip()
    if len(joined) <= max_len:
        return joined
    cut = joined.rfind(" ", 0, max_len)
    return (joined[:cut] if cut > max_len // 2 else joined[:max_len]).rstrip(" .,;:-·") + "…"


def _fmt_nice_deadline(raw_dl: str) -> str:
    d_obj = core.parse_deadline(raw_dl)
    if not d_obj:
        return raw_dl
    if len(str(raw_dl).strip()) <= 10 or (d_obj.hour == 23 and d_obj.minute == 59):
        return d_obj.strftime("%d.%m.%Y")
    return d_obj.strftime("%d.%m в %H:%M")


def build_briefing_text(u: dict) -> str:
    t_date = day()
    t_str = str(t_date)
    lvl = int(u.get("xp", 0)) // 100 + 1
    lines = [
        f"☀️ <b>Доброе утро, {core.esc(u.get('name', 'Друг'))}!</b>",
        f"🔥 Серия: <b>{u.get('streak', 0)} дн.</b> · ⚡ Уровень <b>{lvl}</b> ({u.get('xp', 0)} XP)",
    ]

    # 1. Пары на сегодня
    tt = sys.modules.get("ext_timetable")
    if tt:
        with suppress(Exception):
            dp = tt.day_payload(t_date)
            lessons = dp.get("lessons") or []
            if lessons:
                lines.append("\n📅 <b>Расписание на сегодня:</b>")
                for l in lessons[:5]:
                    room = f" · ауд. {l['room']}" if l.get("room") else ""
                    lines.append(f"  • <code>{l['start']}–{l['end']}</code> <b>{core.esc(l['subject'])}</b>{core.esc(room)}")
            else:
                lines.append("\n🎉 <b>Сегодня пар по расписанию нет!</b>")

    # 2. Ближайшие актуальные дедлайны (отсортированные по дате, без мусора и обрывков)
    now = core.now_local()
    uid = u.get("tg")
    is_main = core.is_primary_user(uid)
    candidates = []
    for task in core.active_tasks():
        if not is_main and task.get("acc") != u.get("id"):
            continue
        dl_raw = task.get("deadline", "")
        if not dl_raw:
            continue
        dl_dt = core.parse_deadline(dl_raw)
        if dl_dt and dl_dt < now - timedelta(days=1):
            continue  # старые просроченные не спамим в утреннюю сводку
        candidates.append((dl_dt or now + timedelta(days=365), task))

    candidates.sort(key=lambda x: x[0])
    if candidates:
        lines.append("\n⏳ <b>Ближайшие дедлайны:</b>")
        for _, task in candidates[:4]:
            nice_dl = core.esc(_fmt_nice_deadline(task["deadline"]))
            summary = core.esc(_clean_task_summary(task["text"], 82))
            topic = f" <i>({core.esc(task['topic'])})</i>" if task.get("topic") else ""
            lines.append(f"  • <b>до {nice_dl}</b>{topic} — {summary}")

    # 3. Пункт из плана подготовки к экзамену
    plan_item = next((p["t"] for p in u.get("plan", []) if p.get("d") == t_str), "")
    if plan_item:
        subj = core.esc(u.get("plan_subject") or "Экзамен")
        lines.append(f"\n🎯 <b>План подготовки ({subj}):</b>\n  • {core.esc(plan_item)}")

    lines.append("\n⚡ <i>Закрой 3-минутный квест в приложении, чтобы продлить серию!</i>")
    return "\n".join(lines)


async def send_tg_push(uid: int, html_text: str) -> None:
    url = core.webapp_url()
    kb = {"inline_keyboard": [[{"text": "📱 Открыть приложение", "web_app": {"url": url}}]]} if url else None
    payload = {"chat_id": int(uid), "text": html_text, "parse_mode": "HTML"}
    if kb:
        payload["reply_markup"] = kb
    async with aiohttp.ClientSession() as s:
        await s.post(f"https://api.telegram.org/bot{core.TELEGRAM_BOT_TOKEN}/sendMessage", json=payload)


async def a_briefing_now(request, user):
    u = U(request, user)
    if not u.get("tg"):
        raise ValueError("Не найден Telegram ID профиля")
    await send_tg_push(u["tg"], build_briefing_text(u))
    return {"ok": True}


async def tick() -> None:
    if core is None:
        return
    try:
        n = core.now_local()
        hm, t_str = n.strftime("%H:%M"), str(n.date())
        for uid, u in list(DB["u"].items()):
            if not u.get("notify", True) or not u.get("tg") or u.get("sent") == t_str + hm:
                continue
            if hm == "08:00":
                msg = build_briefing_text(u)
            elif hm == "22:00" and int(u.get("streak", 0)) > 0 and u.get("last") != t_str:
                frz = int(u.get("freeze", 0))
                frz_note = f" (в запасе ❄️ {frz} заморозка)" if frz > 0 else " (заморозок больше нет!)"
                msg = (
                    f"🔥 <b>Серия {u['streak']} дн. сгорит через 2 часа!</b>{frz_note}\n\n"
                    "Зайди на 3 минуты: повтори пару карточек, отметь привычку или задай вопрос ИИ — и день спасён."
                )
            else:
                continue
            u["sent"] = t_str + hm
            save()
            await send_tg_push(u["tg"], msg)
    except Exception:
        core.log.exception("growth pusher error")


# =====================================================================
# 7. ПОДКЛЮЧЕНИЕ МАРШРУТОВ И КОМАНДЫ /top
# =====================================================================
def setup(c) -> None:
    global core
    core = c
    if FILE not in core.STATE_FILES:
        core.STATE_FILES = tuple(core.STATE_FILES) + (FILE,)
    core.PLUGIN_LOADERS.append(load)
    core.PLUGIN_TICKS.append(tick)
    load()

    @core.dp.message(Command("top", "leaderboard"))
    async def cmd_top_leaderboard(message):
        rows = sorted(
            DB["u"].values(),
            key=lambda v: (-int(v.get("xp", 0)), -int(v.get("streak", 0))),
        )[:10]
        if not rows:
            await message.reply("🏆 Рейтинг пока пуст. Откройте мини-приложение и выполните первый квест!")
            return
        medals = ["🥇", "🥈", "🥉"]
        lines = ["🏆 <b>Лидерборд потока (XP и Серия):</b>\n"]
        for idx, r in enumerate(rows):
            m = medals[idx] if idx < 3 else f"<b>{idx + 1}.</b>"
            av = r.get("avatar", "")
            av_str = (av + " ") if av and not str(av).startswith(("data:", "http")) else ""
            lvl = int(r.get("xp", 0)) // 100 + 1
            lines.append(
                f"{m} {av_str}<b>{core.esc(r.get('name', 'Игрок'))}</b> — "
                f"<code>{r.get('xp', 0)} XP</code> (Ур. {lvl}) · 🔥 {r.get('streak', 0)} дн."
            )
        url = core.webapp_url()
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚡ Ворваться в топ", web_app=WebAppInfo(url=url))]]) if url else None
        await message.reply("\n".join(lines), parse_mode="HTML", reply_markup=kb)
        
    routes = [
        ("GET", "/api/g/state", a_state),
        ("POST", "/api/g/quest/undo", a_quest_undo),
        ("POST", "/api/g/card/undo", a_card_undo),
        ("POST", "/api/g/pomodoro", a_pomodoro_done),
        ("POST", "/api/g/story_image", a_story_image),
        ("POST", "/api/g/checkin", a_checkin_lessons),
        ("POST", "/api/g/snap", a_snap),
        ("POST", "/api/g/pdf", a_pdf_export),
        ("POST", "/api/g/goals", a_goal_add),
        ("DELETE", "/api/g/goals/{id}", a_goal_del),
        ("POST", "/api/g/goals/{id}/check", a_goal_check),
        ("POST", "/api/g/journal", a_journal),
        ("POST", "/api/g/journal/{id}/reply", a_journal_reply),
        ("POST", "/api/g/journal/analyze7", a_journal_analyze7),
        ("DELETE", "/api/g/journal/{id}", a_journal_del),
        ("POST", "/api/g/plan", a_plan),
        ("POST", "/api/g/plan/check", a_plan_check),
        ("POST", "/api/g/plan/expand", a_plan_expand),
        ("POST", "/api/g/duel", a_duel_new),
        ("GET", "/api/g/duel/{id}", a_duel_get),
        ("POST", "/api/g/duel/{id}/answer", a_duel_ans),
        ("GET", "/api/g/board", a_board),
        ("POST", "/api/g/group/join", a_group_join),
        ("POST", "/api/g/group/dl", a_dl_add),
        ("POST", "/api/g/notify", a_notify),
        ("POST", "/api/g/briefing", a_briefing_now),
        ("POST", "/api/g/session/toggle", a_session_toggle),
        ("POST", "/api/g/session/exam", a_session_exam_add),
        ("DELETE", "/api/g/session/exam/{id}", a_session_exam_del),
    ]

    hooks = {"/api/tasks/{id}/done": "task", "/api/cards/{id}/review": "cards", "/api/ask": "ask"}
    for i, (m, p, old) in enumerate(core.API_ROUTES):
        if p in hooks and m == "POST":
            def make(old_fn, kind_key):
                @functools.wraps(old_fn)
                async def hooked(request, user):
                    r = await old_fn(request, user)
                    if isinstance(r, dict) and not r.get("error") and r.get("ok", True):
                        award(U(request, user), kind_key)
                        save()
                    return r
                return hooked
            core.API_ROUTES[i] = (m, p, make(old, hooks[p]))
        elif m == "GET" and p in ("/api/timetable/week", "/api/home"):
            def make_tt_hook(old_fn, path_name):
                @functools.wraps(old_fn)
                async def hooked_tt(request, user):
                    r = await old_fn(request, user)
                    if not isinstance(r, dict) or r.get("error"):
                        return r
                    u = U(request, user)
                    s_weeks = set(u.get("session_weeks") or [])
                    s_exams = u.get("session_exams") or []

                    def _fmt_ex(ex):
                        return {
                            "id": ex["id"],
                            "session_exam": True,
                            "date": ex["date"],
                            "start": ex.get("start") or "10:00",
                            "end": ex.get("end") or "12:00",
                            "subject": ex.get("subject") or "Экзамен",
                            "kind": ex.get("kind") or "Экзамен",
                            "room": ex.get("room") or "",
                            "teacher": ex.get("teacher") or "",
                            "description": ex.get("notes") or "",
                            "homework": "",
                            "notes": ex.get("notes") or "",
                            "parity": "all",
                            "extra": True,
                        }

                    if path_name == "/api/timetable/week":
                        mon = str(r.get("monday") or "")
                        is_sess = mon in s_weeks
                        r["is_session_week"] = is_sess
                        r["session_weeks"] = list(s_weeks)
                        for d_obj in r.get("days") or []:
                            d_date = str(d_obj.get("date") or "")
                            day_exams = [_fmt_ex(ex) for ex in s_exams if ex.get("date") == d_date]
                            if is_sess:
                                d_obj["lessons"] = sorted(day_exams, key=lambda x: x["start"])
                                d_obj["cancelled"] = []
                            elif day_exams:
                                d_obj["lessons"] = sorted((d_obj.get("lessons") or []) + day_exams, key=lambda x: x["start"])
                    elif path_name == "/api/home" and isinstance(r.get("today"), dict):
                        t_date = str(r["today"].get("date") or day())[:10]
                        dt_obj = date.fromisoformat(t_date)
                        mon = str(dt_obj - timedelta(days=dt_obj.weekday()))
                        is_sess = mon in s_weeks
                        r["is_session_week"] = is_sess
                        day_exams = [_fmt_ex(ex) for ex in s_exams if ex.get("date") == t_date]
                        if is_sess:
                            r["today"]["lessons"] = sorted(day_exams, key=lambda x: x["start"])
                        elif day_exams:
                            r["today"]["lessons"] = sorted((r["today"].get("lessons") or []) + day_exams, key=lambda x: x["start"])
                    return r
                return hooked_tt
            core.API_ROUTES[i] = (m, p, make_tt_hook(old, p))

    core.API_ROUTES.extend(routes)
