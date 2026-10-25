# -*- coding: utf-8 -*-
"""
Рост v2.0: SQLite + Gist-зеркало, петля возвращения, 3-минутный квест,
живой компаньон, умный разбор фото/лекций (с авто-карточками и тестом),
режим «Сдать за N дней» с записью в расписание, дуэли, комнаты групп и лидерборд в ТГ.
Подключать ПОСЛЕ ext_webapp, ext_timetable и ext_study.
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
from datetime import date, timedelta
from urllib.parse import parse_qsl

import aiohttp
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

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
    "lesson": 15,  # чекин посещения пар
}
QUEST_KEYS = ("cards", "task", "ask")
DB = {"u": {}, "duels": {}, "groups": {}}


# =====================================================================
# 1. ХРАНИЛИЩЕ: SQLite (основное по user_id) + JSON (для Gist backup)
# =====================================================================
def _init_sqlite() -> None:
    with sqlite3.connect(DB_FILE) as con:
        con.execute("CREATE TABLE IF NOT EXISTS kv_store (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        con.commit()


def load() -> None:
    _init_sqlite()
    loaded = {}
    with sqlite3.connect(DB_FILE) as con:
        rows = con.execute("SELECT k, v FROM kv_store").fetchall()
        for k, v in rows:
            try:
                loaded[k] = json.loads(v)
            except Exception:
                pass
    gist_data = core.load_json(FILE, {}) or {}
    # Если SQLite пуст (например, после редеплоя на Render), восстанавливаем из Gist JSON
    for section in ("u", "duels", "groups"):
        merged = dict(gist_data.get(section) or {})
        merged.update(loaded.get(section) or {})
        DB[section] = merged
    _save_sqlite()


def _save_sqlite() -> None:
    try:
        with sqlite3.connect(DB_FILE) as con:
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
# 2. ПРОФИЛЬ ИГРОКА, XP, СЕРИЯ И ЕЖЕДНЕВНЫЙ КВЕСТ
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
    t_str = str(day())
    if u.get("qd") != t_str:
        u["qd"], u["flags"] = t_str, []
    return u


def award(u: dict, kind: str) -> bool:
    """Начисляет XP и проверяет закрытие ежедневного квеста (3 разных действия за день)."""
    u["xp"] = int(u.get("xp", 0)) + XP.get(kind, 5)
    # Привычка или дневник тоже засчитываются как шаг «задача» для квеста, если дедлайнов нет
    quest_alias = "task" if kind in ("task", "goal", "lesson") else ("ask" if kind in ("ask", "journal", "duel") else kind)
    if kind not in u["flags"]:
        u["flags"].append(kind)
    if quest_alias not in u["flags"]:
        u["flags"].append(quest_alias)

    t = day()
    t_str = str(t)
    quest_completed = sum(1 for k in QUEST_KEYS if k in u["flags"]) >= 3 or len(set(u["flags"])) >= 3
    leveled_streak = False

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
        u["xp"] += 35  # бонус за закрытие дня
        if u["streak"] % 7 == 0:
            u["freeze"] = min(3, int(u.get("freeze", 0)) + 1)
        leveled_streak = True
    return leveled_streak


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


def jl(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("ИИ вернул некорректный формат, попробуйте ещё раз")
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
    cur = stages[0]
    for idx, st in enumerate(stages):
        if score >= st[0]:
            cur = (idx + 1, st[1], st[2])
    return {"stage": cur[0], "title": cur[1], "desc": cur[2]}


# =====================================================================
# 3. API ОБРАБОТЧИКИ
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
        "journal": u["journal"][-5:],
        "plan": u["plan"],
        "plan_subject": u.get("plan_subject", ""),
        "plan_date": u.get("plan_date", ""),
        "notify": bool(u.get("notify", True)),
        "bot": getattr(core, "BOT_USERNAME", "") or os.environ.get("BOT_USERNAME", ""),
    }


async def a_checkin_lessons(request, user):
    """Чекин посещения пар за сегодня (+15 XP)."""
    u, t = U(request, user), str(day())
    if u.get("lessons_checked") == t:
        return {"ok": True, "already": True, "xp": u["xp"]}
    u["lessons_checked"] = t
    award(u, "lesson")
    save()
    return {"ok": True, "xp": u["xp"]}


async def a_snap(request, user):
    """Умный разбор фото задачи/конспекта или аудио лекции -> разбор + авто-карточки в ext_study + мини-тест."""
    b, u = await request.json(), U(request, user)
    kind = b.get("kind", "photo")
    hint = {
        "photo": "На фото задача, слайд или конспект: подробно разбери по шагам и выдели суть.",
        "voice": "Это аудиозапись с лекции или голосовая заметка: сделай структурированный конспект и выдели ключевые термины.",
    }.get(kind, "Разбери тему подробно и по шагам.")
    q = str(b.get("text") or "Разбери материал.").strip()[:3000]

    prompt = (
        f"{hint}\nЯзык: русский, без сырого LaTeX (используй читаемые символы: ², √, π, →).\n"
        "Верни СТРОГО валидный JSON следующего формата:\n"
        '{\n'
        '  "title": "Краткое название темы",\n'
        '  "answer": "Подробный разбор в Markdown (с заголовками, списками и ключевыми выводами)",\n'
        '  "cards": [{"front": "Термин или вопрос", "back": "Краткое определение или формула"}],\n'
        '  "quiz": [{"q": "Вопрос на проверку понимания", "options": ["Вариант 1","Вариант 2","Вариант 3","Вариант 4"], "answer": 0, "explain": "Пояснение"}]\n'
        '}\n'
        "Сделай от 4 до 6 карточек и ровно 3 тестовых вопроса.\n\n"
        f"Запрос/контекст: {q}"
    )
    r = jl(await ai(request, user, prompt, b.get("files")))
    cards = [c for c in (r.get("cards") or []) if c.get("front") and c.get("back")][:8]
    quiz = [qz for qz in (r.get("quiz") or []) if qz.get("q") and isinstance(qz.get("options"), list)][:5]
    deck_title = str(r.get("title") or "Конспект ИИ").strip()[:40]

    # Автоматически сохраняем карточки в модуль Учёбы (ext_study), если он подключён
    saved_cards = 0
    st = sys.modules.get("ext_study")
    if st and cards and b.get("save_cards", True):
        try:
            async with W().plugin_ctx(user):
                for c in cards:
                    st.ST["cards"].append({
                        "id": st.ST["next_id"],
                        "front": str(c["front"]).strip()[:200],
                        "ph": "",
                        "back": str(c["back"]).strip()[:400],
                        "ex": "",
                        "deck": deck_title,
                        "box": 1,
                        "due": str(day()),
                    })
                    st.ST["next_id"] += 1
                    saved_cards += 1
                st.save()
        except Exception:
            core.log.exception("auto save cards")

    award(u, "ask")
    if saved_cards:
        award(u, "cards")
    save()
    return {
        "title": deck_title,
        "answer": str(r.get("answer", "")),
        "cards": cards,
        "saved_cards": saved_cards,
        "quiz": quiz,
    }


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
    for g in u["goals"]:
        if g["id"] == request.match_info["id"]:
            if t in g["days"]:
                g["days"].remove(t)
            else:
                g["days"].append(t)
                award(u, "goal")
    save()
    return {"ok": True}


async def a_journal(request, user):
    text, u = str((await request.json()).get("text") or "").strip()[:2500], U(request, user)
    if not text:
        raise ValueError("Напишите хотя бы пару строк о сегодняшнем дне")
    reply = await ai(
        request,
        user,
        "Ты мудрый, тёплый и структурный вечерний ментор. На русском языке (2–4 предложения): "
        "поддержи пользователя, подсвети главный инсайт из его записи и задай один глубокий вопрос на завтра.\n\n"
        + text,
    )
    first = not (u["journal"] and u["journal"][-1]["d"] == str(day()))
    u["journal"].append({"d": str(day()), "t": text, "ai": reply})
    del u["journal"][:-30]
    if first:
        award(u, "journal")
    save()
    return {"ai": reply}


async def a_plan(request, user):
    """Режим «Сдать за N дней»: строит план и подкидывает слоты подготовки в расписание пар и задачи."""
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
            f'Составь чёткий план подготовки к экзамену/зачёту по предмету «{subj}»: '
            f'дней до сдачи: {n}, старт: {day()}, по одному конкретному блоку тем/практики в день, '
            'последние 1–2 дня — генеральный прогон по билетам. '
            'Ответ СТРОГО JSON: {"days":[{"d":"YYYY-MM-DD","t":"конкретная тема и задача на день"}]}',
        )
    )
    plan_days = [x for x in (r.get("days") or []) if x.get("d") and x.get("t")][:60]
    u["plan"] = plan_days
    u["plan_subject"] = subj
    u["plan_date"] = target_date

    # Подкидываем слоты подготовки прямо в расписание (ext_timetable) как разовые занятия
    added_to_tt = 0
    tt = sys.modules.get("ext_timetable")
    if tt and push_tt:
        try:
            async with W().plugin_ctx(user):
                # Удаляем старые слоты подготовки к этому же предмету, чтобы не дублировать
                tt.ST["exceptions"] = [
                    ex for ex in tt.ST.get("exceptions", [])
                    if not (ex.get("kind") == "add" and str(ex.get("subject", "")).startswith(f"🎯 Подготовка: {subj}"))
                ]
                h_start, m_start = [int(x) for x in (slot_time.split(":") if ":" in slot_time else ("19", "00"))[:2]]
                end_total = min(23 * 60 + 50, h_start * 60 + m_start + 45)
                end_time = f"{end_total // 60:02d}:{end_total % 60:02d}"
                for item in plan_days[:21]:  # до 3 недель вперёд в календарь пар
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
            f'Составьте 5 интересных тестовых вопросов по-русски на тему «{topic}» (по 4 варианта ответа). '
            'Ответ СТРОГО JSON: {"qs":[{"q":"Текст вопроса","a":["Вариант 1","Вариант 2","Вариант 3","Вариант 4"],"c":0}]}, '
            'где c — индекс верного ответа (0..3).',
        )
    )
    i = secrets.token_urlsafe(4).replace("-", "").replace("_", "")[:6]
    DB["duels"][i] = {"id": i, "topic": topic, "qs": r["qs"][:5], "res": {}, "created": time.time()}
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
# 4. ГРУППОВЫЕ КОМНАТЫ, ЛИДЕРБОРД И ОБЩИЕ ДЕДЛАЙНЫ
# =====================================================================
def _active_group(request, u: dict) -> tuple:
    g = gid(request) or (u["gids"][-1] if u.get("gids") else "")
    gr = DB["groups"].get(g) if g else None
    return g, gr


async def a_board(request, user):
    u = U(request, user)
    g, gr = _active_group(request, u)
    if gr:
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
                for v in DB["u"].values()
                if g in v.get("gids", [])
            ),
            key=lambda r: (-r["xp"], -r["streak"]),
        )[:30]
    else:
        # Глобальный топ, если пользователь ещё не вступил в комнату группы
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
                for v in DB["u"].values()
            ),
            key=lambda r: (-r["xp"], -r["streak"]),
        )[:20]

    return {
        "rows": rows,
        "has": bool(gr),
        "code": gr.get("code", g[:8].upper()) if gr else "",
        "title": gr.get("title", "Комната группы") if gr else "Общий рейтинг",
        "owner": bool(gr and gr["owner"] == u["id"]),
        "dl": [x for x in (gr or {}).get("dl", []) if x["d"] >= str(day())],
    }


async def a_group_join(request, user):
    """Создание или вход в комнату группы по коду (например, FPM-2026)."""
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
    """Староста/ведущий добавляет общий дедлайн группы — он появляется у всех участников."""
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
# 5. УТРЕННИЙ БРИФИНГ И ВЕЧЕРНИЙ ПУШ СПАСЕНИЯ СЕРИИ
# =====================================================================
def build_briefing_text(u: dict) -> str:
    t_date = day()
    t_str = str(t_date)
    lines = [f"☀️ <b>Утренний брифинг · {u.get('name', 'Друг')}</b>"]
    lines.append(f"🔥 Серия: <b>{u.get('streak', 0)} дн.</b> · ⚡ Уровень <b>{int(u.get('xp', 0)) // 100 + 1}</b> ({u.get('xp', 0)} XP)")

    # 1. Пары на сегодня из ext_timetable
    tt = sys.modules.get("ext_timetable")
    if tt:
        try:
            dp = tt.day_payload(t_date)
            lessons = dp.get("lessons") or []
            if lessons:
                lines.append("\n📅 <b>Пары на сегодня:</b>")
                for l in lessons[:5]:
                    room = f" (ауд. {l['room']})" if l.get("room") else ""
                    lines.append(f"  • <code>{l['start']}–{l['end']}</code> <b>{core.esc(l['subject'])}</b>{core.esc(room)}")
            else:
                lines.append("\n🎉 <b>Сегодня пар нет</b> — отличное время для фокуса на своих целях.")
        except Exception:
            pass

    # 2. Горящие задачи / дедлайны
    active = [t for t in core.active_tasks() if t.get("deadline")]
    if active:
        lines.append("\n⏳ <b>Главные дедлайны:</b>")
        for task in active[:3]:
            snippet = core.esc(core.strip_md(task["text"]).strip()[:70])
            lines.append(f"  • до <b>{core.esc(task['deadline'])}</b> — {snippet}")

    # 3. Пункт из плана «Сдать за N дней»
    plan_item = next((p["t"] for p in u.get("plan", []) if p.get("d") == t_str), "")
    if plan_item:
        subj = core.esc(u.get("plan_subject") or "Экзамен")
        lines.append(f"\n🎯 <b>План «{subj}» на сегодня:</b>\n  • {core.esc(plan_item)}")

    lines.append("\n⚡ <i>Закрой 3-минутный квест в приложении, чтобы продлить серию!</i>")
    return "\n".join(lines)


async def send_tg_push(uid: int, html_text: str) -> None:
    url = core.webapp_url()
    kb = None
    if url:
        kb = {"inline_keyboard": [[{"text": "📱 Открыть приложение", "web_app": {"url": url}}]]}
    payload = {"chat_id": int(uid), "text": html_text, "parse_mode": "HTML"}
    if kb:
        payload["reply_markup"] = kb
    async with aiohttp.ClientSession() as s:
        await s.post(f"https://api.telegram.org/bot{core.TELEGRAM_BOT_TOKEN}/sendMessage", json=payload)


async def a_briefing_now(request, user):
    """Ручная отправка красивого утреннего брифинга в Telegram (для проверки и удобства)."""
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
# 6. ПОДКЛЮЧЕНИЕ МАРШРУТОВ И КОМАНДЫ /top В TELEGRAM
# =====================================================================
def setup(c) -> None:
    global core
    core = c
    if FILE not in core.STATE_FILES:
        core.STATE_FILES = tuple(core.STATE_FILES) + (FILE,)
    core.PLUGIN_LOADERS.append(load)
    core.PLUGIN_TICKS.append(tick)
    load()

    # Вирусная команда /top или /leaderboard прямо в Telegram-чате
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
        ("POST", "/api/g/goals", a_goal_add),
        ("DELETE", "/api/g/goals/{id}", a_goal_del),
        ("POST", "/api/g/goals/{id}/check", a_goal_check),
        ("POST", "/api/g/journal", a_journal),
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
