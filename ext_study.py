# -*- coding: utf-8 -*-
"""
Учёба (модуль для bot.py): квизы на 5 вопросов и карточки «выучил / не выучил».

* Квиз: Gemini составляет вопросы (функция create_quiz), бот отправляет обычные квиз-опросы Telegram,
  считает результат каждого участника, показывает ошибки и предлагает «Повторить ошибки».
* Карточки: слова и термины с переводом, транскрипцией и примером (функция add_flashcards);
  повторение по системе Лейтнера (чем лучше знаешь, тем реже карточка возвращается).
* API для мини-приложения.
"""
import random
import time as _time
from contextlib import suppress
from datetime import timedelta
from typing import Optional

from aiogram import F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

core = None
FILE = "study.json"
INTERVALS = {1: 0, 2: 1, 3: 3, 4: 7, 5: 16}  # через сколько дней вернётся карточка, попав в коробку N
SESSION_SIZE = 10
ST: dict = {}
SESS: dict = {}  # chat_id -> {"queue": [id...], "seen": 0, "ok": 0, "again": set()}


def default_state() -> dict:
    return {"cards": [], "next_card": 1, "quiz_log": [], "quizzes": {}, "polls": {}, "next_quiz": 1}


def load() -> None:
    data = core.load_json(FILE, default_state())
    ST.clear()
    ST.update(default_state())
    ST.update(data)


def save() -> None:
    # ограничиваем рост файла
    log = ST["quiz_log"]
    del log[:-300]
    qs = ST["quizzes"]
    for k in sorted(qs, key=int)[:-30]:
        for q in qs[k]["questions"]:
            ST["polls"].pop(str(q.get("poll_id")), None)
        del qs[k]
    core.save_json(FILE, ST)


def today_iso() -> str:
    return core.now_local().date().isoformat()


# ======================================================================
# Карточки
# ======================================================================
def due_cards(deck: Optional[str] = None) -> list:
    t = today_iso()
    out = [c for c in ST["cards"] if c["due"] <= t and (not deck or c["deck"] == deck)]
    out.sort(key=lambda c: (c["due"], c["box"]))
    return out


def card_stats() -> dict:
    cards = ST["cards"]
    return {"total": len(cards), "due": len(due_cards()), "learned": sum(1 for c in cards if c["box"] >= 4),
            "decks": sorted({c["deck"] for c in cards})}


def due_count() -> int:
    return len(due_cards())


def add_cards(items: list, deck: str = "english") -> list:
    added = []
    have = {(c["deck"], c["front"].lower()) for c in ST["cards"]}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        front, back = str(it.get("front") or "").strip(), str(it.get("back") or "").strip()
        if not front or not back or (deck, front.lower()) in have:
            continue
        card = {"id": ST["next_card"], "deck": deck, "front": front[:120], "back": back[:300],
                "ph": str(it.get("transcription") or it.get("ph") or "").strip()[:60],
                "ex": str(it.get("example") or it.get("ex") or "").strip()[:250],
                "box": 1, "due": today_iso(), "seen": 0, "ok": 0, "created": _time.time()}
        ST["next_card"] += 1
        ST["cards"].append(card)
        have.add((deck, front.lower()))
        added.append(card)
    return added


def review(card_id: int, known: bool) -> Optional[dict]:
    for c in ST["cards"]:
        if c["id"] == card_id:
            c["seen"] += 1
            if known:
                c["ok"] += 1
                c["box"] = min(5, c["box"] + 1)
                c["due"] = (core.now_local().date() + timedelta(days=INTERVALS[c["box"]])).isoformat()
            else:
                c["box"] = 1
                c["due"] = today_iso()
            return c
    return None


def delete_card(card_id: int) -> bool:
    before = len(ST["cards"])
    ST["cards"] = [c for c in ST["cards"] if c["id"] != card_id]
    return len(ST["cards"]) != before


def get_card(card_id: int) -> Optional[dict]:
    return next((c for c in ST["cards"] if c["id"] == card_id), None)


def front_text(c: dict, left: int) -> str:
    esc = core.esc
    ph = f"  <i>{esc(c['ph'])}</i>" if c.get("ph") else ""
    return f"🃏 <b>{esc(c['front'])}</b>{ph}\n\nОсталось в сессии: {left}\nВспомни перевод, потом нажми «Показать»."


def back_text(c: dict) -> str:
    esc = core.esc
    ph = f"  <i>{esc(c['ph'])}</i>" if c.get("ph") else ""
    ex = f"\n\n💬 {esc(c['ex'])}" if c.get("ex") else ""
    return f"🃏 <b>{esc(c['front'])}</b>{ph}\n\n✅ <b>{esc(c['back'])}</b>{ex}"


def cards_menu_text() -> str:
    s = card_stats()
    if not s["total"]:
        return ("🧠 <b>Карточки</b>\n\nПока пусто. Напиши, например: «Гемини, добавь карточки: ubiquitous, mitigate, "
                "scrutiny» или «сделай карточки из этого текста» — я добавлю перевод, транскрипцию и пример.")
    return (f"🧠 <b>Карточки</b>\n\nВсего: <b>{s['total']}</b> · к повторению сегодня: <b>{s['due']}</b> · "
            f"выучено: <b>{s['learned']}</b>")


def cards_menu_kb() -> InlineKeyboardMarkup:
    n = due_count()
    rows = []
    if ST["cards"]:
        rows.append([InlineKeyboardButton(text=f"▶️ Учить ({min(n, SESSION_SIZE)})" if n else "🔁 Потренироваться ещё",
                                          callback_data="fc_s" if n else "fc_r")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else InlineKeyboardMarkup(inline_keyboard=[])


async def cmd_cards(message):
    if message.chat.type != "private" or not core.is_allowed(message) or not core.is_primary_user(message.from_user.id):
        return
    await message.answer(cards_menu_text(), reply_markup=cards_menu_kb() if ST["cards"] else None,
                         parse_mode=ParseMode.HTML)


async def _next_card(call, chat_id: int) -> None:
    sess = SESS.get(chat_id)
    if not sess or not sess["queue"]:
        seen, ok = (sess["seen"], sess["ok"]) if sess else (0, 0)
        SESS.pop(chat_id, None)
        text = (f"🏁 Сессия завершена: знал {ok} из {seen}.\n\n" if seen else "") + cards_menu_text()
        await _edit(call, text, cards_menu_kb() if ST["cards"] else None)
        return
    card = get_card(sess["queue"][0])
    if not card:
        sess["queue"].pop(0)
        await _next_card(call, chat_id)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👀 Показать", callback_data=f"fc_v_{card['id']}")]])
    await _edit(call, front_text(card, len(sess["queue"])), kb)


async def _edit(call, text, kb) -> None:
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception as e:
        if "not modified" not in str(e).lower():
            raise


async def cb_cards(call):
    chat_id = call.message.chat.id
    data = call.data
    if data in ("fc_s", "fc_r"):
        pool = due_cards() if data == "fc_s" else random.sample(ST["cards"], min(SESSION_SIZE, len(ST["cards"])))
        queue = [c["id"] for c in pool[:SESSION_SIZE]]
        if not queue:
            await call.answer("Карточек для повторения нет", show_alert=True)
            return
        SESS[chat_id] = {"queue": queue, "seen": 0, "ok": 0, "again": set()}
        await _next_card(call, chat_id)
    elif data.startswith("fc_v_"):
        card = get_card(int(data[5:]))
        if not card:
            await call.answer("Карточка удалена")
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Не знал", callback_data=f"fc_no_{card['id']}"),
            InlineKeyboardButton(text="✅ Знал", callback_data=f"fc_ok_{card['id']}")]])
        await _edit(call, back_text(card), kb)
    elif data.startswith(("fc_ok_", "fc_no_")):
        known = data.startswith("fc_ok_")
        cid = int(data.split("_")[2])
        sess = SESS.get(chat_id)
        if sess and sess["queue"] and sess["queue"][0] == cid:
            sess["queue"].pop(0)
            sess["seen"] += 1
            sess["ok"] += 1 if known else 0
            if not known and cid not in sess["again"]:  # неизвестную вернём в конец сессии один раз
                sess["again"].add(cid)
                sess["queue"].append(cid)
        review(cid, known)
        save()
        await _next_card(call, chat_id)
    await call.answer()


# ======================================================================
# Квизы
# ======================================================================
def quiz_stats() -> dict:
    by: dict = {}
    for r in ST["quiz_log"]:
        t = by.setdefault(r["topic"], {"topic": r["topic"], "total": 0, "correct": 0, "runs": 0})
        t["total"] += r["total"]
        t["correct"] += r["correct"]
        t["runs"] += 1
    topics = sorted(({**t, "pct": round(100 * t["correct"] / t["total"]) if t["total"] else 0} for t in by.values()),
                    key=lambda x: x["pct"])
    return {"recent": ST["quiz_log"][-10:][::-1], "topics": topics, "runs": len(ST["quiz_log"])}


def clean_questions(items) -> list:
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        opts = [str(o).strip()[:100] for o in (it.get("options") or []) if str(o).strip()][:4]
        try:
            ci = int(float(it.get("correct_index")))
        except (TypeError, ValueError):
            continue
        q = str(it.get("question") or "").strip()[:290]
        if q and 2 <= len(opts) and 0 <= ci < len(opts):
            out.append({"q": q, "options": opts, "correct": ci, "expl": str(it.get("explanation") or "").strip()[:190]})
    return out[:10]


async def send_quiz(chat_id: int, thread: Optional[int], topic: str, questions: list) -> int:
    qid = ST["next_quiz"]
    ST["next_quiz"] += 1
    quiz = {"id": qid, "topic": topic[:60], "chat": chat_id, "thread": thread, "questions": [],
            "answers": {}, "done": []}
    ST["quizzes"][str(qid)] = quiz
    await core.bot.send_message(chat_id, f"🎯 <b>Квиз: {core.esc(topic)}</b> — {len(questions)} вопросов.\n"
                                         "Отвечай на опросы ниже; в конце покажу результат.",
                                message_thread_id=thread, parse_mode=ParseMode.HTML)
    for i, q in enumerate(questions):
        msg = await core.bot.send_poll(chat_id, f"{i + 1}/{len(questions)}. {q['q']}"[:300], q["options"], type="quiz",
                                       correct_option_id=q["correct"], explanation=q["expl"] or None,
                                       is_anonymous=False, message_thread_id=thread)
        quiz["questions"].append({**q, "poll_id": msg.poll.id})
        ST["polls"][str(msg.poll.id)] = {"qid": qid, "i": i}
    save()
    return qid


async def on_poll_answer(ans) -> None:
    ref = ST["polls"].get(str(ans.poll_id))
    quiz = ST["quizzes"].get(str(ref["qid"])) if ref else None
    if not quiz:
        return
    uid = str(ans.user.id)
    rec = quiz["answers"].setdefault(uid, {"name": ans.user.full_name, "res": {}})
    q = quiz["questions"][ref["i"]]
    rec["res"][str(ref["i"])] = list(ans.option_ids) == [q["correct"]]
    total = len(quiz["questions"])
    if len(rec["res"]) < total or uid in quiz["done"]:
        save()
        return
    quiz["done"].append(uid)
    correct = sum(1 for v in rec["res"].values() if v)
    ST["quiz_log"].append({"ts": _time.time(), "topic": quiz["topic"], "total": total, "correct": correct,
                           "who": rec["name"]})
    wrong = [quiz["questions"][int(i)] for i, ok in sorted(rec["res"].items(), key=lambda x: int(x[0])) if not ok]
    esc = core.esc
    emoji = "🏆" if correct == total else "👍" if correct >= total * 0.6 else "📚"
    lines = [f"{emoji} <b>{esc(rec['name'])}</b>: {correct}/{total}"]
    for w in wrong:
        lines.append(f"• {esc(w['q'].split('. ', 1)[-1])}\n  ✅ <b>{esc(w['options'][w['correct']])}</b>")
    kb = None
    if wrong:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="🔁 Повторить ошибки", callback_data=f"qz_r_{quiz['id']}_{uid}")]])
    save()
    await core.bot.send_message(quiz["chat"], "\n".join(lines), message_thread_id=quiz["thread"],
                                parse_mode=ParseMode.HTML, reply_markup=kb)


async def cb_retry(call):
    try:
        _, _, qid, uid = call.data.split("_")
    except ValueError:
        await call.answer()
        return
    if str(call.from_user.id) != uid:
        await call.answer("Это кнопка другого участника", show_alert=True)
        return
    quiz = ST["quizzes"].get(qid)
    rec = quiz["answers"].get(uid) if quiz else None
    if not rec:
        await call.answer("Квиз уже недоступен", show_alert=True)
        return
    wrong = [{"q": q["q"].split(". ", 1)[-1], "options": q["options"], "correct": q["correct"], "expl": q["expl"]}
             for i, q in enumerate(quiz["questions"]) if not rec["res"].get(str(i))]
    with suppress(Exception):
        await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Запускаю повтор")
    if wrong:
        random.shuffle(wrong)
        await send_quiz(quiz["chat"], quiz["thread"], quiz["topic"] + " — ошибки", wrong)


async def cmd_quiz(message):
    if not core.is_allowed(message):
        return
    if message.chat.type == "private" and not core.is_primary_user(message.from_user.id if message.from_user else None):
        return
    st = quiz_stats()
    text = ("🎯 <b>Квиз на 5 вопросов</b>\n\nНапиши: «Гемини, сделай квиз по теме …» — или прикрепи конспект/PDF и "
            "скажи «квиз по этому файлу». Можно указать число вопросов и сложность.")
    if st["topics"]:
        weak = st["topics"][:3]
        text += "\n\n📊 <b>Результаты по темам:</b>\n" + "\n".join(
            f"• {core.esc(t['topic'])}: {t['pct']}% ({t['correct']}/{t['total']})" for t in weak)
    await message.answer(text, parse_mode=ParseMode.HTML)


# ======================================================================
# Инструменты для Gemini
# ======================================================================
TOOLS = [{
    "name": "create_quiz",
    "description": "Создаёт квиз (по умолчанию 5 вопросов) и отправляет его в чат обычными квиз-опросами Telegram.",
    "parameters": {"type": "OBJECT", "properties": {
        "topic": {"type": "STRING", "description": "Тема квиза"},
        "questions": {"type": "ARRAY", "description": "Вопросы с вариантами ответа (2–4) и ровно одним верным.",
                      "items": {"type": "OBJECT", "properties": {
                          "question": {"type": "STRING"},
                          "options": {"type": "ARRAY", "items": {"type": "STRING"}},
                          "correct_index": {"type": "INTEGER", "description": "Номер верного варианта, начиная с 0"},
                          "explanation": {"type": "STRING", "description": "Краткое пояснение (до 150 символов)"}},
                          "required": ["question", "options", "correct_index"]}}},
        "required": ["topic", "questions"]},
}, {
    "name": "add_flashcards",
    "description": "Добавляет карточки для заучивания (слова, термины, определения).",
    "parameters": {"type": "OBJECT", "properties": {
        "deck": {"type": "STRING", "description": "Колода, по умолчанию english"},
        "cards": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "front": {"type": "STRING", "description": "Слово или термин"},
            "back": {"type": "STRING", "description": "Перевод или определение на русском"},
            "transcription": {"type": "STRING", "description": "Транскрипция (для английских слов)"},
            "example": {"type": "STRING", "description": "Короткий пример употребления"}},
            "required": ["front", "back"]}}},
        "required": ["cards"]},
}]


async def t_create_quiz(message, scope, is_private, args, origin) -> str:
    qs = clean_questions(args.get("questions"))
    if not qs:
        await message.reply("Не получилось составить вопросы. Попробуй уточнить тему.")
        return "[квиз не создан]"
    chat_id, thread = origin
    topic = str(args.get("topic") or "Квиз").strip() or "Квиз"
    await send_quiz(chat_id, thread, topic, qs)
    return f"[квиз «{topic}»: {len(qs)} вопросов отправлено]"


async def t_add_flashcards(message, scope, is_private, args, origin) -> str:
    if not is_private:
        await message.reply("Карточки хранятся в личных сообщениях — напиши мне в личку.")
        return "[карточки: только в личке]"
    deck = str(args.get("deck") or "english").strip().lower()[:30] or "english"
    added = add_cards(args.get("cards"), deck)
    if not added:
        await message.reply("Новых карточек нет (возможно, такие слова уже добавлены).")
        return "[карточки не добавлены]"
    save()
    preview = "\n".join(f"• {core.esc(c['front'])} — {core.esc(c['back'])}" for c in added[:12])
    more = f"\n…и ещё {len(added) - 12}" if len(added) > 12 else ""
    await message.answer(f"✅ Добавлено карточек: <b>{len(added)}</b> (колода «{core.esc(deck)}»)\n{preview}{more}\n\n"
                         "Учить: /cards", parse_mode=ParseMode.HTML)
    return f"[добавлено карточек: {len(added)}]"


def prompt_hook(scope, is_private) -> str:
    s = card_stats()
    return ("УЧЁБА:\n"
            "- «Сделай квиз / тест по теме …» или «квиз по этому файлу» — вызови create_quiz: по умолчанию 5 вопросов "
            "(если просят другое число — столько, максимум 10), у каждого 3–4 варианта и ровно один верный, "
            "correct_index с нуля, пояснение до 150 символов. Вопросы только по реальным фактам; для файла — по его "
            "содержимому.\n"
            "- «Добавь карточки / слова …», «сделай карточки из текста» — вызови add_flashcards: для английских слов "
            "укажи перевод, транскрипцию и короткий пример, для терминов — определение.\n"
            f"Сейчас карточек: {s['total']}, к повторению сегодня: {s['due']}.")


# ======================================================================
# API для мини-приложения
# ======================================================================
async def api_study(request, user):
    return {"cards": card_stats(), "quiz": quiz_stats()}


async def api_cards(request, user):
    deck = request.query.get("deck") or None
    items = due_cards(deck) if request.query.get("due") else sorted(ST["cards"], key=lambda c: c["id"], reverse=True)
    return {"cards": items[:300], "stats": card_stats()}


async def api_card_add(request, user):
    body = await request.json()
    added = add_cards([body], str(body.get("deck") or "english").strip().lower()[:30] or "english")
    if not added:
        return {"error": "Нужны слово и перевод (или такая карточка уже есть)"}
    save()
    return {"ok": True, "card": added[0]}


async def api_card_delete(request, user):
    ok = delete_card(int(request.match_info["id"]))
    save()
    return {"ok": ok}


async def api_card_review(request, user):
    body = await request.json()
    c = review(int(request.match_info["id"]), bool(body.get("known")))
    save()
    return {"ok": bool(c), "card": c, "stats": card_stats()}


# ======================================================================
def setup(c) -> None:
    global core
    core = c
    core.STATE_FILES = tuple(core.STATE_FILES) + (FILE,)
    core.PLUGIN_LOADERS.append(load)
    core.PLUGIN_TOOLS.extend(TOOLS)
    core.PLUGIN_HANDLERS.update({"create_quiz": t_create_quiz, "add_flashcards": t_add_flashcards})
    core.PLUGIN_PROMPT.append(prompt_hook)
    core.study_due_count = due_count
    core.API_ROUTES.extend([
        ("GET", "/api/study", api_study), ("GET", "/api/cards", api_cards), ("POST", "/api/cards", api_card_add),
        ("DELETE", "/api/cards/{id}", api_card_delete), ("POST", "/api/cards/{id}/review", api_card_review)])
    dp = core.dp
    dp.message(F.text == "🧠 Карточки", F.chat.type == "private")(cmd_cards)
    dp.message(Command("cards"), F.chat.type == "private")(cmd_cards)
    dp.message(F.text == "🎯 Квиз")(cmd_quiz)
    dp.message(Command("quiz"))(cmd_quiz)
    dp.callback_query(F.data.startswith("fc_"))(cb_cards)
    dp.callback_query(F.data.startswith("qz_r_"))(cb_retry)
    dp.poll_answer()(on_poll_answer)
