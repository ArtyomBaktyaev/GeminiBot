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
            "goals": [
                {"id": "g_sport", "name": "Спорт / Разминка", "cat": "body", "days": []},
                {"id": "g_eng", "name": "Английский 15 мин", "cat": "mind", "days": []},
                {"id": "g_code", "name": "Код / Пет-проект", "cat": "work", "days": []},
            ],
            "journal": [],
            "plan": [],
            "plan_subject": "",
            "plan_date": "",
            "notify": True,
            "gids": [],
            "lessons_checked": "",
        },
    )
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
    t_str = str(day())
    if u.get("qd") != t_str:
        u["qd"], u["flags"] = t_str, []
    return u


def award(u: dict, kind: str) -> bool:
    u["xp"] = max(0, int(u.get("xp", 0)) + XP.get(kind, 5))
    quest_alias = "task" if kind in ("task", "goal", "lesson") else ("ask" if kind in ("ask", "journal", "duel") else kind)
    if kind not in u["flags"]:
        u["flags"].append(kind)
    if quest_alias not in u["flags"]:
        u["flags"].append(quest_alias)

    t = day()
    t_str = str(t)
    quest_completed = sum(1 for k in QUEST_KEYS if k in u["flags"]) >= 3 or len(set(u["flags"])) >= 3
    if quest_completed and u.get("last") != t_str:
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
    """Прямой вызов Gemini без примешивания академического досье README (для живой рефлексии)."""
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


def _companion_stage(streak: int, level: int) -> dict:
    score = streak * 2 + level
    stages = [
        (0, "Семя Импульса", "Новое начало. Выполни 3-минутный квест, чтобы пробудить росток."),
        (4, "Неоновый Росток", "Первые корни пущены. Остров начинает накапливать энергию."),
        (10, "Древо Синтеза", "Крона окрепла, вокруг появляются светлячки фокуса."),
        (20, "Кристальная Роща", "Мощный ритм! Биолюминесценция освещает весь остров."),
        (35, "Парящий Эдем", "Легендарная дисциплина. Дерево резонирует на пике формы."),
        (60, "Древо Сингулярности", "Абсолютный мастер привычек и знаний."),
    ]
    cur = (1, stages[0][1], stages[0][2])
    for idx, st in enumerate(stages):
        if score >= st[0]:
            cur = (idx + 1, st[1], st[2])
    return {"stage": cur[0], "title": cur[1], "desc": cur[2]}


# =====================================================================
# 4. API ОБРАБОТЧИКИ
# =====================================================================
async def a_state(request, user):
    u, g = U(request, user), gid(request)
    if g:
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
        {"id": "task", "title": "Закрыть задачу или привычку", "done": any(x in u["flags"] for x in ("task", "goal", "lesson")), "xp": XP["task"], "tab": "tasks"},
        {"id": "ask", "title": "Вопрос ИИ / Разбор / Дневник", "done": any(x in u["flags"] for x in ("ask", "journal", "duel")), "xp": XP["ask"], "tab": "chat"},
    ]

    return {
        "name": u["name"],
        "avatar": u.get("avatar", "🧑‍💻"),
        "xp": xp,
        "xp_in_level": xp % 100,
        "level": level,
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
        "plan": u["plan"],
        "plan_subject": u.get("plan_subject", ""),
        "plan_date": u.get("plan_date", ""),
        "notify": bool(u.get("notify", True)),
        "bot": getattr(core, "BOT_USERNAME", "") or os.environ.get("BOT_USERNAME", ""),
    }


async def a_checkin_lessons(request, user):
    """Переключатель (можно отметить и отменить отметку посещения пар за сегодня)."""
    u, t = U(request, user), str(day())
    if u.get("lessons_checked") == t:
        u["lessons_checked"] = ""
        if "lesson" in u["flags"]:
            u["flags"].remove("lesson")
        u["xp"] = max(0, int(u.get("xp", 0)) - XP["lesson"])
        save()
        return {"ok": True, "undone": True, "xp": u["xp"]}
    u["lessons_checked"] = t
    award(u, "lesson")
    save()
    return {"ok": True, "undone": False, "xp": u["xp"]}


async def a_snap(request, user):
    """Разбор фото конспекта/задачи или голосового -> чистые формулы + карточки + тест."""
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
        '  "title": "Краткое название темы",\n'
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

    # Безопасное сохранение карточек через штатный обработчик ext_study
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
    """Генерирует красивый PDF для разбора конспекта или отчёта прогресса без съезжания текста."""
    b, u = await request.json(), U(request, user)
    kind = str(b.get("kind") or "report")
    if kind == "snap":
        title = clean_math(str(b.get("title") or "Конспект разбора").strip())
        content = clean_math(str(b.get("content") or "").strip())
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
        else:
            lines.append("Привычки ещё не добавлены.")

        if u.get("plan"):
            lines.extend(["", f"## План подготовки: {clean_math(u.get('plan_subject') or 'Экзамен')}"])
            lines.append("| Дата | План на день |")
            lines.append("| --- | --- |")
            for p in u["plan"][:30]:
                lines.append(f"| {p['d']} | {clean_math(p['t'])} |")

        if u.get("journal"):
            lines.extend(["", "## Последние записи дневника рефлексии"])
            for j in reversed(u["journal"][-10:]):
                lines.append(f"### {j['d']}")
                lines.append(f"**Запись:** {clean_math(j['t'])}")
                lines.append(f"> **Отклик ментора:** {clean_math(j['ai'])}")
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
                toggled_on = False
            else:
                g["days"].append(t)
                award(u, "goal")
                toggled_on = True
    save()
    return {"ok": True, "checked": toggled_on, "xp": u["xp"]}


async def a_journal(request, user):
    text, u = str((await request.json()).get("text") or "").strip()[:2500], U(request, user)
    if not text:
        raise ValueError("Напишите хотя бы пару слов о сегодняшнем дне")

    # Проверяем, не написал ли пользователь одно-два тестовых/случайных слова вроде «шала», «лала», «тест»
    words = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", text)
    is_short_or_random = len(text) < 8 or (len(words) <= 2 and len(text) < 18)

    sys_prompt = (
        "Ты живой, умный и искренний собеседник для вечернего дневника. Язык: русский.\n"
        "ЖЕСТКИЕ ПРАВИЛА:\n"
        "1. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать шаблонные фразы: «сегодня ты проделал огромную/отличную работу», "
        "«главный инсайт дня», «системность и чёткая структура побеждают хаос», «бесценный опыт». Никакого инфоцыганского пафоса!\n"
        "2. Отвечай СТРОГО по сути того, что написал человек, не придумывай за него про математику или задачи, если он об этом не писал.\n"
        "3. Если человек написал короткую бессмыслицу, проверочное слово («шала», «лала», «тест», «абырвалг») или одно слово без контекста — "
        "ответь с лёгкой доброй иронией в 1–2 предложениях (например: обыграй это слово и спроси, устал ли он так сильно, что сил хватило только на него, или как реально прошёл день)."
    )
    user_msg = (
        f"Пользователь написал очень короткую или тестовую фразу: «{text}». Ответь живо и с юмором без шаблонов!"
        if is_short_or_random
        else f"Запись пользователя в вечернем дневнике ({u['name']}):\n{text}\n\nДай тёплый, конкретный отклик по сути его текста (2–3 предложения) и один живой вопрос."
    )

    reply = clean_math(await ai_direct(request, user, sys_prompt, user_msg, temp=0.75))
    first = not any(j.get("d") == str(day()) for j in u["journal"])
    entry = {"id": secrets.token_hex(4), "d": str(day()), "t": text, "ai": reply}
    u["journal"].append(entry)
    del u["journal"][:-50]
    if first and not is_short_or_random:
        award(u, "journal")
    save()
    return {"ai": reply, "entry": entry}


async def a_journal_del(request, user):
    """Удаление конкретной записи рефлексии по ID."""
    u = U(request, user)
    jid = request.match_info["id"]
    u["journal"] = [j for j in u.get("journal", []) if j.get("id") != jid]
    save()
    return {"ok": True}


async def a_plan(request, user):
    b, u = await request.json(), U(request, user)
    subj = str(b.get("subject") or "").strip()[:100]
    target_date = str(b.get("date") or "").strip()
    slot_time = str(b.get("time") or "19:00").strip()
    push_tt = bool(b.get("push_to_tt", True))

    if not subj or not target_date:
        raise ValueError("Укажите предмет и дату экзамена")
    n = (date.fromisoformat(target_date) - day()).days
    if not 1 <= n <= 60:
        raise ValueError("Дата экзамена должна быть от 1 до 60 дней вперёд")

    r = jl(
        await ai(
            request,
            user,
            f'Составь чёткий план подготовки к экзамену по предмету «{subj}»: '
            f'дней до сдачи: {n}, старт: {day()}, по одному конкретному блоку тем в день, '
            'без LaTeX. Ответ СТРОГО JSON: {"days":[{"d":"YYYY-MM-DD","t":"что делать"}]}',
        )
    )
    plan_days = [
        {"d": str(x["d"]), "t": clean_math(str(x["t"]))}
        for x in (r.get("days") or [])
        if x.get("d") and x.get("t")
    ][:60]
    u["plan"] = plan_days
    u["plan_subject"] = subj
    u["plan_date"] = target_date

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
    return {"plan": u["plan"], "added_to_tt": added_to_tt}


async def a_duel_new(request, user):
    topic = str((await request.json()).get("topic") or "").strip()[:100]
    if not topic:
        raise ValueError("Введите тему дуэли")
    r = jl(
        await ai(
            request,
            user,
            f'Составьте 5 тестовых вопросов по-русски на тему «{topic}» (без сырого LaTeX, используй Unicode-символы). '
            'Ответ СТРОГО JSON: {"qs":[{"q":"Текст вопроса","a":["Вариант 1","Вариант 2","Вариант 3","Вариант 4"],"c":0}]}',
        )
    )
    i = secrets.token_urlsafe(4).replace("-", "").replace("_", "")[:6]
    qs = [
        {
            "q": clean_math(str(q["q"])),
            "a": [clean_math(str(opt)) for opt in (q.get("a") or [])[:4]],
            "c": int(q.get("c", 0)),
        }
        for q in (r.get("qs") or [])[:5]
    ]
    DB["duels"][i] = {"id": i, "topic": topic, "qs": qs, "res": {}, "created": time.time()}
    save()
    return {"id": i, "topic": topic}


async def a_duel_get(request, user):
    did = request.match_info["id"].strip()
    if did not in DB["duels"]:
        raise ValueError("Дуэль не найдена или устарела")
    d, u = DB["duels"][did], U(request, user)
    return {
        "id": did,
        "topic": d["topic"],
        "qs": [{"q": q["q"], "a": q["a"]} for q in d["qs"]],
        "res": list(d["res"].values()),
        "played": u["id"] in d["res"],
    }


async def a_duel_ans(request, user):
    did = request.match_info["id"].strip()
    d, u = DB["duels"][did], U(request, user)
    if u["id"] not in d["res"]:
        a = (await request.json()).get("a") or []
        score = sum(1 for q, x in zip(d["qs"], a) if int(q.get("c", 0)) == int(x))
        d["res"][u["id"]] = {"name": u["name"], "avatar": u.get("avatar", "🧑‍💻"), "score": score, "total": len(d["qs"])}
        award(u, "duel")
        save()
    return {"score": d["res"][u["id"]]["score"], "res": list(d["res"].values())}


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
        ("POST", "/api/g/checkin", a_checkin_lessons),
        ("POST", "/api/g/snap", a_snap),
        ("POST", "/api/g/pdf", a_pdf_export),
        ("POST", "/api/g/goals", a_goal_add),
        ("DELETE", "/api/g/goals/{id}", a_goal_del),
        ("POST", "/api/g/goals/{id}/check", a_goal_check),
        ("POST", "/api/g/journal", a_journal),
        ("DELETE", "/api/g/journal/{id}", a_journal_del),
        ("POST", "/api/g/plan", a_plan),
        ("POST", "/api/g/duel", a_duel_new),
        ("GET", "/api/g/duel/{id}", a_duel_get),
        ("POST", "/api/g/duel/{id}/answer", a_duel_ans),
        ("GET", "/api/g/board", a_board),
        ("POST", "/api/g/group/join", a_group_join),
        ("POST", "/api/g/group/dl", a_dl_add),
        ("POST", "/api/g/notify", a_notify),
        ("POST", "/api/g/briefing", a_briefing_now),
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

    core.API_ROUTES.extend(routes)
