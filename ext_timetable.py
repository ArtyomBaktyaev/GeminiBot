# -*- coding: utf-8 -*-
"""
Расписание пар (модуль для bot.py).

* Разбор расписания из фото / скриншота / PDF / текста через Gemini (функция save_schedule) с подтверждением.
* Кнопки «Сегодня / Завтра / Неделя», выбор дня, листание недель.
* Правки словами: добавить / изменить / удалить пару, разовые изменения на дату, чётные/нечётные недели.
* Утренняя сводка и напоминание за N минут до пары (используется планировщик бота).
* Экспорт в календарь (.ics) и API для мини-приложения.

Модуль не импортирует bot.py: всё нужное приходит через setup(core).
"""
import base64
import re
import secrets
import time as _time
from contextlib import suppress
from datetime import date, datetime, time, timedelta
from typing import Optional

from aiogram import F
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

core = None  # объект bot.py (модуль), подставляется в setup()
FILE = "timetable.json"

DAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
DAYS_FULL = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря"]
PARITY_NAME = {"odd": "нечётная неделя", "even": "чётная неделя"}

def default_state() -> dict:
    return {"lessons": [], "exceptions": [], "next_id": 1, "next_exc": 1, "anchor": "", "digest_on": False,
            "digest_hour": 8, "remind_min": 0, "owner": 0, "sent": {}}


ST: dict = default_state()
PENDING: dict = {}  # черновики расписания по аккаунтам


def load() -> None:
    # Загружаем настройки из файла через core
    data = core.load_json(FILE, {})
    ST.clear()
    state = default_state()
    if isinstance(data, dict):
        state.update(data)
    ST.update(state)

def save() -> None:
    core.save_plugin_state(FILE, ST)


def get_current_st() -> dict:
    return core.profile_plugin_state("timetable", ST, default_state)


def get_pending() -> dict:
    account = core.CURRENT_ACCOUNT.get()
    return PENDING.setdefault(account["id"] if account else None, {})


def subscription():
    ref = get_current_st().get("following") or {}
    owner = core.USER_SETTINGS.get("accounts", {}).get(ref.get("id"))
    if owner and ref.get("code") and owner.get("timetable_invite") == ref["code"]:
        return owner
    return None


def grid():
    owner = subscription()
    if owner is None:
        return get_current_st()
    # Read the publisher's own grid, never subscriptions, notes or settings.
    with core.timetable_account_context({"id": owner["tg_id"]}, owner):
        return get_current_st()


def grid_id():
    account = subscription() or core.CURRENT_ACCOUNT.get() or {}
    return account.get("id", "")


def public_lesson(lesson):
    fields = ("id", "weekday", "start", "end", "subject", "room", "teacher", "kind", "parity")
    return {k: lesson[k] for k in fields if k in lesson}


def share_info():
    owner = subscription()
    return {"following": bool(owner), "source": grid_id(),
            "publisher": owner.get("name", "") if owner else "",
            "notify_changes": bool((get_current_st().get("following") or {}).get("notify")),
            "published": bool((core.CURRENT_ACCOUNT.get() or {}).get("timetable_invite"))}


async def api_share(request, user):
    body = await request.json()
    action = body.get("action")
    account = core.CURRENT_ACCOUNT.get()
    state = get_current_st()
    if action == "publish":
        if subscription():
            raise ValueError("Можно поделиться только собственным расписанием")
        if not core.BOT_USERNAME or not core.webapp_url():
            raise ValueError("Ссылка появится после подключения бота и адреса мини-приложения")
        if not state["lessons"] and not state["exceptions"]:
            raise ValueError("Сначала добавьте пару в расписание")
        if not account.get("timetable_invite"):
            account["timetable_invite"] = secrets.token_urlsafe(18)
            core.save_json(core.SETTINGS_FILE, core.USER_SETTINGS)
        return {"url": f"https://t.me/{core.BOT_USERNAME}?start=tt_{account['timetable_invite']}"}
    if action == "revoke":
        account.pop("timetable_invite", None)
        core.save_json(core.SETTINGS_FILE, core.USER_SETTINGS)
    elif action == "leave":
        state.pop("following", None)
        save()
    elif action in ("preview", "follow"):
        token = body.get("code")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24}", token):
            raise ValueError("Ссылка недействительна")
        owner = next((a for a in core.USER_SETTINGS["accounts"].values()
                      if a.get("timetable_invite") == token), None)
        if not owner:
            raise ValueError("Ссылка отозвана или устарела")
        if owner["id"] == account["id"]:
            raise ValueError("Это ваше расписание")
        if action == "preview":
            return {"publisher": owner.get("name") or "Одногруппник"}
        with core.timetable_account_context({"id": owner["tg_id"]}, owner):
            seq = get_current_st().get("change_seq", 0)
        state["following"] = {"id": owner["id"], "code": token, "seen": seq, "notify": body.get("notify") is True}
        save()
    else:
        raise ValueError("Неизвестное действие")
    return share_info()


def material_key(source, day, lesson):
    occurrence = f"x{lesson['exc_id']}" if lesson.get("exc_id") else str(lesson["id"])
    return f"{source}/{day.isoformat()}/{occurrence}"


async def api_materials(request, user):
    body = request.query if request.method == "GET" else await request.json()
    day = parse_date(body.get("date"))
    source = grid_id()
    if not day or body.get("source") != source:
        raise ValueError("Расписание изменилось. Откройте пару заново")
    pair = next((l for l in lessons_on(day) if l.get("id") == int(body.get("id") or 0)
                 and int(l.get("exc_id") or 0) == int(body.get("exc_id") or 0)), None)
    if pair is None:
        raise ValueError("В этот день такой пары уже нет")
    records = get_current_st().setdefault("materials", {})
    key = material_key(source, day, pair)
    current = records.get(key, {})
    if request.method == "GET":
        return current
    if body.get("revision", 0) != current.get("revision", 0):
        raise ValueError("Материалы уже изменены. Откройте пару заново")
    value = {k: str(body.get(k) or "").strip() for k in ("description", "homework", "notes")}
    if any(len(t) > 16000 for t in value.values()):
        raise ValueError("До 16 000 символов в одном поле")
    files = body.get("files", [])
    if not isinstance(files, list) or len(files) > 8:
        raise ValueError("До восьми файлов на занятие")
    value["files"] = []
    total = 0
    for f in files:
        data = f.get("data", "") if isinstance(f, dict) else ""
        if not isinstance(data, str):
            raise ValueError("Некорректный файл")
        header, _, encoded = data.partition(",")
        if not re.fullmatch(r"data:[\w.+/-]*;base64", header):
            raise ValueError("Некорректный файл")
        total += len(encoded)
        if total > 4 * 1024 * 1024:
            raise ValueError("Общий размер файлов на занятие — до 3 МБ")
        size = len(base64.b64decode(encoded, validate=True))
        value["files"].append({"name": str(f.get("name") or "Файл")[:180], "type": header[5:-7],
                               "size": size, "data": data})
    value["revision"] = current.get("revision", 0) + 1
    records[key] = value
    save()
    return value


# ======================================================================
# Нормализация данных
# ======================================================================
EN_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def weekday_index(v) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):  # уже нормализованный индекс 0..6 (Пн=0)
        return int(v) if 0 <= int(v) <= 6 else None
    s = str(v).strip().lower()
    if s.isdigit():  # строка «1»..«7» — номер дня, как пишут люди (Пн=1)
        n = int(s)
        return n - 1 if 1 <= n <= 7 else None
    for i in range(7):
        if s.startswith(DAYS_SHORT[i].lower()) or s.startswith(DAYS_FULL[i].lower()[:3]) or s.startswith(EN_DAYS[i]):
            return i
    return None


def norm_time(v) -> str:
    m = re.search(r"(\d{1,2})\s*[:.\-]\s*(\d{2})", str(v or ""))
    if not m:
        m2 = re.fullmatch(r"\s*(\d{1,2})\s*", str(v or ""))
        if not m2:
            return ""
        h, mi = int(m2.group(1)), 0
    else:
        h, mi = int(m.group(1)), int(m.group(2))
    return f"{h:02d}:{mi:02d}" if 0 <= h < 24 and 0 <= mi < 60 else ""


def norm_parity(v) -> str:
    s = str(v or "all").strip().lower()
    if s in ("odd", "нечёт", "нечет") or s.startswith(("нечёт", "нечет", "числит", "верх")):
        return "odd"
    if s in ("even", "чёт", "чет") or s.startswith(("чёт", "чет", "знаменат", "низ")):
        return "even"
    return "all"


def add_minutes(t: str, minutes: int) -> str:
    h, m = map(int, t.split(":"))
    total = min(h * 60 + m + minutes, 23 * 60 + 59)
    return f"{total // 60:02d}:{total % 60:02d}"


def clean_lesson(d: dict) -> Optional[dict]:
    """Приводит пару к стандартному виду; None — если не хватает обязательных полей."""
    if not isinstance(d, dict):
        return None
    wd = weekday_index(d.get("weekday"))
    start = norm_time(d.get("start"))
    subject = str(d.get("subject") or "").strip()
    if wd is None or not start or not subject:
        return None
    end = norm_time(d.get("end")) or add_minutes(start, 90)
    return {"weekday": wd, "start": start, "end": end, "subject": subject[:80],
            "room": str(d.get("room") or "").strip()[:40], "teacher": str(d.get("teacher") or "").strip()[:60],
            "kind": str(d.get("kind") or "").strip()[:30], "parity": norm_parity(d.get("parity"))}


def parse_date(v) -> Optional[date]:
    s = str(v or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        with suppress(ValueError):
            return datetime.strptime(s, fmt).date()
    return None


def today() -> date:
    return core.now_local().date()


# ======================================================================
# Недели и расписание на дату
# ======================================================================
def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def parity_of(d: date) -> Optional[str]:
    s = grid()
    if not s.get("anchor"):
        return None
    a = parse_date(s["anchor"])
    if not a:
        return None
    weeks = (monday(d) - monday(a)).days // 7
    return "odd" if weeks % 2 == 0 else "even"


def set_current_parity(parity: str) -> None:
    s = get_current_st()
    mon = monday(today())
    s["anchor"] = (mon if parity == "odd" else mon - timedelta(days=7)).isoformat()


def lessons_on(d: date) -> list:
    s = grid()
    par = parity_of(d)
    iso = d.isoformat()
    exceptions = s.get("exceptions") or []
    lessons = s.get("lessons") or []
    cancelled = {e.get("lesson_id") for e in exceptions if isinstance(e, dict) and e.get("date") == iso and e.get("kind") == "cancel"}
    out = []
    for l in lessons:
        if not isinstance(l, dict):
            continue
        if l.get("weekday") != d.weekday() or l.get("id") in cancelled:
            continue
        if l.get("parity") != "all" and par is not None and l.get("parity") != par:
            continue
        x = public_lesson(l)
        x["unsure_parity"] = l.get("parity") != "all" and par is None
        out.append(x)
    for e in exceptions:
        if isinstance(e, dict) and e.get("date") == iso and e.get("kind") == "add" and isinstance(e.get("lesson"), dict):
            x = public_lesson(e["lesson"])
            x.update({"extra": True, "id": 0, "exc_id": e.get("id", 0)})
            out.append(x)
    out.sort(key=lambda l: l.get("start", ""))
    return out


def cancelled_on(d: date) -> list:
    s = grid()
    iso = d.isoformat()
    exceptions = s.get("exceptions") or []
    lessons = s.get("lessons") or []
    ids = {e.get("lesson_id") for e in exceptions if isinstance(e, dict) and e.get("date") == iso and e.get("kind") == "cancel"}
    return [public_lesson(l) for l in lessons if isinstance(l, dict) and l.get("id") in ids]


def day_title(d: date) -> str:
    return f"{DAYS_FULL[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"


# ======================================================================
# Форматирование
# ======================================================================
def fmt_lesson(l: dict) -> str:
    esc = core.esc
    parts = [f"🕘 {l['start']}–{l['end']} · <b>{esc(l['subject'])}</b>"]
    if l.get("room"):
        parts.append(f"ауд. {esc(l['room'])}")
    if l.get("teacher"):
        parts.append(esc(l["teacher"]))
    if l.get("kind"):
        parts.append(esc(l["kind"]))
    line = " · ".join(parts)
    if l.get("extra"):
        line += " ✨ разово"
    if l.get("unsure_parity"):
        line += f" ({'нечёт.' if l['parity'] == 'odd' else 'чёт.'} неделя)"
    return line


def format_day(d: date, with_next: bool = True) -> str:
    esc = core.esc
    par = parity_of(d)
    head = f"📅 <b>{day_title(d)}</b>" + (f" · {PARITY_NAME[par]}" if par else "")
    lessons = lessons_on(d)
    if not lessons:
        body = "🎉 Пар нет — свободный день!"
    else:
        body = "\n".join(fmt_lesson(l) for l in lessons)
    out = f"{head}\n\n{body}"
    canc = cancelled_on(d)
    if canc:
        out += "\n\n❌ <b>Отменено:</b> " + esc(", ".join(f"{l['subject']} ({l['start']})" for l in canc))
    if with_next and d == today() and lessons:
        now_t = core.now_local().strftime("%H:%M")
        nxt = next((l for l in lessons if l["start"] > now_t), None)
        if nxt:
            out += f"\n\n⏭ Следующая: <b>{esc(nxt['subject'])}</b> в {nxt['start']}"
        else:
            out += "\n\n✅ На сегодня всё."
    return out


def format_week(d: date) -> str:
    esc = core.esc
    mon = monday(d)
    par = parity_of(mon)
    head = f"🗓 <b>Неделя {mon:%d.%m}–{mon + timedelta(days=6):%d.%m}</b>" + (f" · {PARITY_NAME[par]}" if par else "")
    blocks = []
    for i in range(7):
        day = mon + timedelta(days=i)
        ls = lessons_on(day)
        mark = " 👈" if day == today() else ""
        if not ls:
            if i >= 5:
                continue
            blocks.append(f"<b>{DAYS_SHORT[i]} {day:%d.%m}</b>{mark} — пар нет")
            continue
        lines = [f"<b>{DAYS_SHORT[i]} {day:%d.%m}</b>{mark}"]
        for l in ls:
            room = f" ({esc(l['room'])})" if l.get("room") else ""
            lines.append(f"  {l['start']} {esc(l['subject'])}{room}" + (" ✨" if l.get("extra") else ""))
        blocks.append("\n".join(lines))
    return head + "\n\n" + "\n".join(blocks)


def day_kb(d: date) -> InlineKeyboardMarkup:
    mon = monday(d)
    row = []
    for i in range(7):
        day = mon + timedelta(days=i)
        label = DAYS_SHORT[i] if day != d else f"•{DAYS_SHORT[i]}"
        row.append(InlineKeyboardButton(text=label, callback_data=f"tt_d_{day.isoformat()}"))
    nav = [InlineKeyboardButton(text="⬅️", callback_data=f"tt_d_{(d - timedelta(days=7)).isoformat()}"),
           InlineKeyboardButton(text="Сегодня", callback_data=f"tt_d_{today().isoformat()}"),
           InlineKeyboardButton(text="🗓 Неделя", callback_data=f"tt_w_{d.isoformat()}"),
           InlineKeyboardButton(text="➡️", callback_data=f"tt_d_{(d + timedelta(days=7)).isoformat()}")]
    return InlineKeyboardMarkup(inline_keyboard=[row, nav])


def week_kb(d: date) -> InlineKeyboardMarkup:
    mon = monday(d)
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️", callback_data=f"tt_w_{(mon - timedelta(days=7)).isoformat()}"),
        InlineKeyboardButton(text="Сегодня", callback_data=f"tt_d_{today().isoformat()}"),
        InlineKeyboardButton(text="➡️", callback_data=f"tt_w_{(mon + timedelta(days=7)).isoformat()}")]])


def lessons_overview(lessons: list, with_ids: bool = False) -> str:
    """Список пар по дням недели (для подтверждения разбора и /schedule)."""
    esc = core.esc
    if not lessons:
        return "пока пусто"
    by_day: dict = {}
    for l in lessons:
        by_day.setdefault(l["weekday"], []).append(l)
    blocks = []
    for wd in sorted(by_day):
        lines = [f"<b>{DAYS_FULL[wd]}</b>"]
        for l in sorted(by_day[wd], key=lambda x: x["start"]):
            extra = [x for x in (l.get("room") and f"ауд. {l['room']}", l.get("teacher"), l.get("kind")) if x]
            par = {"odd": " [нечёт.]", "even": " [чёт.]"}.get(l["parity"], "")
            idp = f"#{l['id']} " if with_ids and l.get("id") else ""
            lines.append(f"  {idp}{l['start']}–{l['end']} {esc(l['subject'])}" + (f" — {esc(', '.join(extra))}" if extra else "") + par)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# ======================================================================
# Операции над данными (используются и ботом, и мини-приложением)
# ======================================================================
def record_change(state, text):
    seq = state.get("change_seq", 0) + 1
    state["change_seq"] = seq
    entries = state.setdefault("changes", [])
    entries.append({"seq": seq, "text": text})
    del entries[:-20]


def add_lessons(items: list, merge_dupes: bool = True) -> list:
    s = get_current_st()
    added = []
    for raw in items:
        l = clean_lesson(raw)
        if not l:
            continue
        if merge_dupes and any(x["weekday"] == l["weekday"] and x["start"] == l["start"]
                               and x["subject"].lower() == l["subject"].lower() and x["parity"] == l["parity"]
                               for x in s["lessons"]):
            continue
        l["id"] = s["next_id"]
        s["next_id"] += 1
        s["lessons"].append(l)
        added.append(l)
        record_change(s, f"Добавлена пара: {l['subject']} · {DAYS_SHORT[l['weekday']]} {l['start']}")
    return added


def update_lesson(lid: int, fields: dict) -> Optional[dict]:
    s = get_current_st()
    for l in s["lessons"]:
        if l["id"] == lid:
            merged = dict(l)
            for k, v in fields.items():
                if v is not None and (v != "" or k in ("room", "teacher", "kind")):
                    merged[k] = v
            new = clean_lesson(merged)
            if not new:
                return None
            new["id"] = lid
            if new != l:
                record_change(s, f"Изменена пара: {new['subject']} · {DAYS_SHORT[new['weekday']]} {new['start']}" +
                              (f" · ауд. {new['room']}" if new["room"] else ""))
            l.clear()
            l.update(new)
            return l
    return None


def delete_lesson(lid: int) -> bool:
    s = get_current_st()
    before = len(s["lessons"])
    for l in s["lessons"]:
        if l["id"] == lid:
            record_change(s, f"Удалена пара: {l['subject']} · {DAYS_SHORT[l['weekday']]} {l['start']}")
    s["lessons"] = [l for l in s["lessons"] if l["id"] != lid]
    s["exceptions"] = [e for e in s["exceptions"] if e.get("lesson_id") != lid]
    return len(s["lessons"]) != before


def add_exception(d: date, kind: str, lesson_id: int = 0, lesson: Optional[dict] = None) -> Optional[dict]:
    s = get_current_st()
    exc = {"id": s["next_exc"], "date": d.isoformat(), "kind": kind}
    if kind == "cancel":
        if not any(l["id"] == lesson_id for l in s["lessons"]):
            return None
        if any(e["date"] == exc["date"] and e["kind"] == "cancel" and e.get("lesson_id") == lesson_id
               for e in s["exceptions"]):
            return None
        exc["lesson_id"] = lesson_id
    else:
        item = dict(lesson or {})
        item["weekday"] = d.weekday()
        l = clean_lesson(item)
        if not l:
            return None
        exc["lesson"] = l
    s["next_exc"] += 1
    s["exceptions"].append(exc)
    changed = exc.get("lesson") or next(l for l in s["lessons"] if l["id"] == lesson_id)
    label = "Отменена пара" if kind == "cancel" else "Добавлена пара"
    record_change(s, f"{label}: {changed['subject']} · {d:%d.%m} {changed['start']}")
    return exc


def delete_exception(exc_id: int) -> bool:
    s = get_current_st()
    before = len(s["exceptions"])
    for exc in s["exceptions"]:
        if exc["id"] == exc_id:
            record_change(s, f"Отменено разовое изменение на {exc['date']}")
    s["exceptions"] = [e for e in s["exceptions"] if e["id"] != exc_id]
    return len(s["exceptions"]) != before


def resolve_when(when: str) -> tuple:
    """-> ('day', date) | ('week', date)."""
    w = (when or "").strip().lower()
    t = today()
    if w in ("", "today", "сегодня"):
        return "day", t
    if w in ("tomorrow", "завтра"):
        return "day", t + timedelta(days=1)
    if w in ("yesterday", "вчера"):
        return "day", t - timedelta(days=1)
    if w in ("week", "неделя", "эта неделя", "на неделю", "this_week"):
        return "week", t
    if w in ("next_week", "следующая неделя", "на следующую неделю"):
        return "week", t + timedelta(days=7)
    d = parse_date(w)
    if d:
        return "day", d
    wd = weekday_index(w)
    if wd is not None:
        delta = (wd - t.weekday()) % 7
        return "day", t + timedelta(days=delta)
    return "day", t


# ======================================================================
# Календарь .ics
# ======================================================================
def _ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def build_ics() -> bytes:
    state = grid()
    stamp = core.now_local().strftime("%Y%m%dT%H%M%S")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//GeminiBot//Timetable//RU", "CALSCALE:GREGORIAN",
             "X-WR-CALNAME:Расписание пар"]
    base = monday(today())
    for l in state["lessons"]:
        d = base + timedelta(days=l["weekday"])
        interval, count = 1, 26
        if l["parity"] != "all" and state.get("anchor"):
            interval, count = 2, 13
            if parity_of(d) != l["parity"]:
                d += timedelta(days=7)
        h1, m1 = map(int, l["start"].split(":"))
        h2, m2 = map(int, l["end"].split(":"))
        start = datetime.combine(d, time(h1, m1))
        end = datetime.combine(d, time(h2, m2))
        desc = ", ".join(x for x in (l.get("teacher"), l.get("kind")) if x)
        lines += ["BEGIN:VEVENT", f"UID:lesson-{l['id']}-{start:%Y%m%d}@geminibot", f"DTSTAMP:{stamp}",
                  f"DTSTART:{start:%Y%m%dT%H%M%S}", f"DTEND:{end:%Y%m%dT%H%M%S}",
                  f"RRULE:FREQ=WEEKLY;INTERVAL={interval};COUNT={count}", f"SUMMARY:{_ics_escape(l['subject'])}"]
        if l.get("room"):
            lines.append(f"LOCATION:{_ics_escape('ауд. ' + l['room'])}")
        if desc:
            lines.append(f"DESCRIPTION:{_ics_escape(desc)}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


# ======================================================================
# Telegram: команды и кнопки
# ======================================================================
async def _private_ok(message) -> bool:
    uid = message.from_user.id if message.from_user else None
    return message.chat.type == "private" and bool(uid) and core.CURRENT_ACCOUNT.get() is not None


async def show_day(message, d: date) -> None:
    await message.answer(format_day(d), reply_markup=day_kb(d), parse_mode=ParseMode.HTML)


async def cmd_today(message):
    if await _private_ok(message):
        await show_day(message, today())


async def cmd_tomorrow(message):
    if await _private_ok(message):
        await show_day(message, today() + timedelta(days=1))


async def cmd_week(message):
    if await _private_ok(message):
        await message.answer(format_week(today()), reply_markup=week_kb(today()), parse_mode=ParseMode.HTML)


async def cmd_schedule(message):
    state = grid()
    if not await _private_ok(message):
        return
    if not state["lessons"]:
        await message.answer("Расписание пока пустое. Пришли фото, скриншот, PDF или текст со своими парами — "
                             "я разберу и сохраню.")
        return
    par = parity_of(today())
    await message.answer(f"📚 <b>Моё расписание</b>" + (f" · сейчас {PARITY_NAME[par]}" if par else "")
                         + "\n\n" + lessons_overview(state["lessons"], with_ids=True)
                         + "\n\nИзменить можно словами: «перенеси физику на среду на 11:30», «завтра первой пары нет».",
                         reply_markup=day_kb(today()), parse_mode=ParseMode.HTML)


async def cmd_ics(message):
    state = grid()
    if not await _private_ok(message):
        return
    if not state["lessons"]:
        await message.answer("Расписание пустое — экспортировать нечего.")
        return
    await message.answer_document(BufferedInputFile(build_ics(), filename="raspisanie.ics"),
                                  caption="📆 Расписание для календаря (Google, Apple, Outlook). "
                                          "Чётные/нечётные недели учтены, разовые изменения в файл не входят.")


async def _edit(call, text: str, kb) -> None:
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def cb_day(call):
    d = parse_date(call.data[5:])
    if not d:
        await call.answer()
        return
    await _edit(call, format_day(d), day_kb(d))
    await call.answer()


async def cb_week(call):
    d = parse_date(call.data[5:])
    if not d:
        await call.answer()
        return
    await _edit(call, format_week(d), week_kb(d))
    await call.answer()


def pending_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Сохранить", callback_data="tt_ok"),
        InlineKeyboardButton(text="✏️ Исправить", callback_data="tt_fix"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="tt_no")]])


async def cb_pending(call):
    pending = get_pending()
    state = get_current_st()
    action = call.data
    if action == "tt_fix":
        await call.message.answer("Напиши, что поправить, например: «в пятницу вместо физики информатика» или "
                                  "«все семинары в аудитории 305». Я обновлю черновик, потом нажми «Сохранить».")
        await call.answer()
        return
    if action == "tt_no":
        pending.clear()
        with suppress(Exception):
            await call.message.edit_reply_markup(reply_markup=None)
        await call.message.answer("Хорошо, расписание не менял.")
        await call.answer()
        return
    if not pending.get("lessons"):
        await call.answer("Черновика уже нет — пришли расписание ещё раз", show_alert=True)
        return
    if pending.get("mode") == "replace":
        state["lessons"] = []
        state["exceptions"] = []
    added = add_lessons(pending["lessons"], merge_dupes=pending.get("mode") != "replace")
    if pending.get("parity") in ("odd", "even"):
        set_current_parity(pending["parity"])
    if not core.ADMIN_IDS:
        state["owner"] = call.message.chat.id
    pending.clear()
    save()
    with suppress(Exception):
        await call.message.edit_reply_markup(reply_markup=None)
    note = "" if state.get("anchor") or not any(l["parity"] != "all" for l in state["lessons"]) else \
        "\n\n⚠️ В расписании есть чётные/нечётные недели. Скажи, какая неделя сейчас («сейчас нечётная»)."
    await call.message.answer(f"✅ Сохранено пар: {len(added)}.{note}\n\n" + format_day(today()),
                              reply_markup=day_kb(today()), parse_mode=ParseMode.HTML)
    await call.answer("Сохранено")


# ======================================================================
# Инструменты для Gemini
# ======================================================================
LESSON_PROPS = {
    "weekday": {"type": "STRING", "enum": DAYS_SHORT, "description": "День недели"},
    "start": {"type": "STRING", "description": "Начало пары HH:MM"},
    "end": {"type": "STRING", "description": "Конец пары HH:MM (если не указан — оставь пустым)"},
    "subject": {"type": "STRING", "description": "Название предмета"},
    "room": {"type": "STRING", "description": "Аудитория"},
    "teacher": {"type": "STRING", "description": "Преподаватель"},
    "kind": {"type": "STRING", "description": "Тип: лекция, семинар, лабораторная…"},
    "parity": {"type": "STRING", "enum": ["all", "odd", "even"],
               "description": "all — каждую неделю; odd — нечётные недели (верхняя/числитель); even — чётные (нижняя/знаменатель)"},
}

TOOLS = [{
    "name": "save_schedule",
    "description": "Сохраняет расписание пар, разобранное из фото, скриншота, PDF или текста пользователя (с подтверждением).",
    "parameters": {"type": "OBJECT", "properties": {
        "lessons": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": LESSON_PROPS,
                                                "required": ["weekday", "start", "subject"]},
                    "description": "ВСЕ пары из расписания (или полный исправленный список для черновика)."},
        "mode": {"type": "STRING", "enum": ["replace", "merge"],
                 "description": "replace — полное расписание заменяет старое; merge — добавить к существующему."},
        "current_week_parity": {"type": "STRING", "enum": ["odd", "even"],
                                "description": "Если известно, какая неделя сейчас (нечётная/чётная)."},
    }, "required": ["lessons"]},
}, {
    "name": "edit_schedule",
    "description": "Добавляет, изменяет или удаляет пары в сохранённом расписании (постоянные изменения).",
    "parameters": {"type": "OBJECT", "properties": {
        "operations": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "op": {"type": "STRING", "enum": ["add", "update", "delete"]},
            "id": {"type": "INTEGER", "description": "Номер пары из списка (для update/delete)"},
            **LESSON_PROPS}, "required": ["op"]}}},
        "required": ["operations"]},
}, {
    "name": "schedule_exception",
    "description": "Разовое изменение на конкретную дату: отменить пару, перенести её (другое время/аудитория) или добавить пару.",
    "parameters": {"type": "OBJECT", "properties": {
        "date": {"type": "STRING", "description": "Дата YYYY-MM-DD"},
        "action": {"type": "STRING", "enum": ["cancel", "move", "add"]},
        "lesson_id": {"type": "INTEGER", "description": "Номер пары (для cancel/move)"},
        "cancel_all": {"type": "BOOLEAN", "description": "true — отменить все пары этого дня"},
        **{k: v for k, v in LESSON_PROPS.items() if k != "weekday"}},
        "required": ["date", "action"]},
}, {
    "name": "show_schedule",
    "description": "Показывает расписание в чате: на сегодня, завтра, день недели, дату или неделю.",
    "parameters": {"type": "OBJECT", "properties": {
        "when": {"type": "STRING", "description": "today, tomorrow, week, next_week, название дня недели или дата YYYY-MM-DD"}},
        "required": ["when"]},
}, {
    "name": "set_week_parity",
    "description": "Запоминает, какая сейчас неделя: нечётная или чётная.",
    "parameters": {"type": "OBJECT", "properties": {
        "current_week": {"type": "STRING", "enum": ["odd", "even"]}}, "required": ["current_week"]},
}, {
    "name": "timetable_settings",
    "description": "Настройки расписания: утренняя сводка и напоминания перед парой.",
    "parameters": {"type": "OBJECT", "properties": {
        "digest_on": {"type": "BOOLEAN", "description": "Присылать утреннюю сводку"},
        "digest_hour": {"type": "INTEGER", "description": "Час сводки (0–23)"},
        "remind_minutes": {"type": "INTEGER", "description": "За сколько минут напоминать о паре; 0 — выключить"}}},
}]


def _items(val) -> list:
    return val if isinstance(val, list) else []


async def t_save_schedule(message, scope, is_private, args, origin) -> str:
    pending = get_pending()
    if not is_private:
        await message.reply("Расписание сохраняю только в личных сообщениях.")
        return "[расписание: только в личке]"
    cleaned = [l for l in (clean_lesson(x) for x in _items(args.get("lessons"))) if l]
    if not cleaned:
        await message.reply("Не нашёл в этом ни одной пары с днём, временем и названием. Пришли более чёткое "
                            "фото или текстом.")
        return "[расписание не распознано]"
    mode = "merge" if args.get("mode") == "merge" else "replace"
    pending.clear()
    pending.update({"lessons": cleaned, "mode": mode, "chat": message.chat.id,
                    "parity": args.get("current_week_parity") if args.get("current_week_parity") in ("odd", "even") else ""})
    text = (f"🔎 Вот что я разобрал ({len(cleaned)} пар, режим: {'замена' if mode == 'replace' else 'добавление'}):\n\n"
            + lessons_overview(cleaned) + "\n\nВсё верно?")
    if len(text) > 3900:
        text = text[:3900] + "…"
    await message.answer(text, reply_markup=pending_kb(), parse_mode=ParseMode.HTML)
    return f"[черновик расписания: {len(cleaned)} пар, ждёт подтверждения]"


async def t_edit_schedule(message, scope, is_private, args, origin) -> str:
    report = []
    for op in _items(args.get("operations")):
        if not isinstance(op, dict):
            continue
        kind = op.get("op")
        try:
            lid = int(float(op.get("id") or 0))
        except (TypeError, ValueError):
            lid = 0
        if kind == "add":
            added = add_lessons([op], merge_dupes=False)
            report.append(f"➕ {added[0]['subject']} ({DAYS_SHORT[added[0]['weekday']]} {added[0]['start']})" if added
                          else "⚠️ Не хватило данных, чтобы добавить пару (нужны день, время, название).")
        elif kind == "update":
            fields = {k: op.get(k) for k in LESSON_PROPS if op.get(k) not in (None, "")}
            if "weekday" in fields:
                fields["weekday"] = weekday_index(fields["weekday"])
            l = update_lesson(lid, fields)
            report.append(f"✏️ #{lid}: {l['subject']} → {DAYS_SHORT[l['weekday']]} {l['start']}–{l['end']}"
                          + (f", ауд. {l['room']}" if l.get("room") else "") if l else f"⚠️ Пара #{lid} не найдена")
        elif kind == "delete":
            report.append(f"🗑 Пара #{lid} удалена" if delete_lesson(lid) else f"⚠️ Пара #{lid} не найдена")
    if not report:
        await message.reply("Не понял, что именно изменить в расписании.")
        return "[изменений нет]"
    save()
    msg = "📝 <b>Расписание обновлено:</b>\n" + "\n".join(core.esc(x) for x in report)
    await message.answer(msg, reply_markup=day_kb(today()), parse_mode=ParseMode.HTML)
    return "[расписание изменено: " + "; ".join(report)[:250] + "]"


async def t_schedule_exception(message, scope, is_private, args, origin) -> str:
    state = get_current_st()
    d = parse_date(args.get("date"))
    action = args.get("action")
    if not d or action not in ("cancel", "move", "add"):
        await message.reply("Не понял дату или действие. Напиши, например: «завтра первой пары нет».")
        return "[исключение не создано]"
    done = []
    try:
        lid = int(float(args.get("lesson_id") or 0))
    except (TypeError, ValueError):
        lid = 0
    if action == "cancel":
        targets = [l["id"] for l in lessons_on(d) if l["id"]] if args.get("cancel_all") else [lid]
        for t in targets:
            exc = add_exception(d, "cancel", lesson_id=t)
            if exc:
                lesson = next(l for l in state["lessons"] if l["id"] == t)
                done.append(f"❌ отменена: {lesson['subject']} ({lesson['start']})")
    elif action == "move":
        src = next((l for l in state["lessons"] if l["id"] == lid), None)
        if src:
            new = dict(src)
            for k in ("start", "end", "room", "teacher", "kind", "subject"):
                if args.get(k):
                    new[k] = args[k]
            if args.get("start") and not args.get("end"):
                new["end"] = add_minutes(norm_time(args["start"]) or src["start"],
                                         max(1, _minutes(src["end"]) - _minutes(src["start"])))
            add_exception(d, "cancel", lesson_id=lid)
            exc = add_exception(d, "add", lesson=new)
            if exc:
                done.append(f"🔁 перенесена: {new['subject']} → {exc['lesson']['start']}"
                            + (f", ауд. {exc['lesson']['room']}" if exc['lesson'].get("room") else ""))
    else:
        exc = add_exception(d, "add", lesson={k: args.get(k) for k in LESSON_PROPS if k != "weekday"})
        if exc:
            done.append(f"✨ добавлена: {exc['lesson']['subject']} в {exc['lesson']['start']}")
    if not done:
        await message.reply("Не получилось: не нашёл подходящую пару или данных не хватает.")
        return "[исключение не создано]"
    save()
    await message.answer(f"📌 <b>{day_title(d)}</b>: " + core.esc("; ".join(done)) + "\n\n" + format_day(d, with_next=False),
                         reply_markup=day_kb(d), parse_mode=ParseMode.HTML)
    return "[разовое изменение: " + "; ".join(done)[:250] + "]"


def _minutes(t: str) -> int:
    h, m = map(int, t.split(":"))
    return h * 60 + m


async def t_show_schedule(message, scope, is_private, args, origin) -> str:
    kind, d = resolve_when(str(args.get("when", "")))
    if kind == "week":
        await message.answer(format_week(d), reply_markup=week_kb(d), parse_mode=ParseMode.HTML)
    else:
        await message.answer(format_day(d), reply_markup=day_kb(d), parse_mode=ParseMode.HTML)
    return f"[показано расписание: {kind} {d.isoformat()}]"


async def t_set_week_parity(message, scope, is_private, args, origin) -> str:
    parity = args.get("current_week")
    if parity not in ("odd", "even"):
        return "[неделя не изменена]"
    set_current_parity(parity)
    save()
    await message.reply(f"✅ Запомнил: сейчас {PARITY_NAME[parity]}.")
    return f"[сейчас {PARITY_NAME[parity]}]"


async def t_settings(message, scope, is_private, args, origin) -> str:
    state = get_current_st()
    changes = []
    if "digest_on" in args and args["digest_on"] is not None:
        state["digest_on"] = bool(args["digest_on"])
        changes.append("утренняя сводка " + ("включена" if state["digest_on"] else "выключена"))
    if args.get("digest_hour") is not None:
        with suppress(TypeError, ValueError):
            h = int(float(args["digest_hour"]))
            if 0 <= h <= 23:
                state["digest_hour"] = h
                state["digest_on"] = True
                changes.append(f"сводка в {h:02d}:00")
    if args.get("remind_minutes") is not None:
        with suppress(TypeError, ValueError):
            m = int(float(args["remind_minutes"]))
            if 0 <= m <= 180:
                state["remind_min"] = m
                changes.append("напоминания перед парой выключены" if m == 0 else f"напоминание за {m} мин до пары")
    if not state["owner"] and not core.ADMIN_IDS:
        state["owner"] = message.chat.id
    if not changes:
        await message.reply("Не понял, что настроить. Например: «присылай сводку в 7 утра» или «напоминай за 15 минут».")
        return "[настройки не изменены]"
    save()
    await message.reply("⚙️ Готово: " + "; ".join(changes) + ".")
    return "[настройки расписания: " + "; ".join(changes) + "]"


def prompt_hook(scope, is_private) -> str:
    pending = get_pending()
    state = get_current_st()
    if not is_private:
        return ""
    lines = []
    for l in sorted(state["lessons"], key=lambda x: (x["weekday"], x["start"]))[:90]:
        par = {"odd": " [нечёт.]", "even": " [чёт.]"}.get(l["parity"], "")
        lines.append(f"#{l['id']} {DAYS_SHORT[l['weekday']]} {l['start']}–{l['end']} {l['subject']}"
                     + (f", ауд. {l['room']}" if l["room"] else "") + (f", {l['teacher']}" if l["teacher"] else "")
                     + (f", {l['kind']}" if l["kind"] else "") + par)
    par_now = parity_of(today())
    parts = [
        "РАСПИСАНИЕ ПАР (личное, только в личных сообщениях):\n"
        "- Если пользователь присылает расписание (фото, скриншот, PDF, текст) или просит его запомнить — вызови "
        "save_schedule (mode=replace, если расписание полное), аккуратно перенеся ВСЕ пары: день, время HH:MM, предмет, "
        "аудиторию, преподавателя, тип и чётность недели (если есть отдельные нечётная/чётная недели или числитель/"
        "знаменатель). Ничего не выдумывай. Если на фото не расписание — не вызывай.\n"
        "- Постоянные правки («перенеси физику на среду», «кабинет 305», «убери пару») — edit_schedule с номерами из "
        "списка ниже. Разовые на дату («завтра первой пары нет», «в пятницу пара в другой аудитории») — "
        "schedule_exception. «Что завтра / какие пары в среду / расписание на неделю» — show_schedule. «Сейчас "
        "нечётная/чётная неделя» — set_week_parity. «Присылай сводку в 7 утра», «напоминай за 15 минут» — "
        "timetable_settings.",
        f"Неделя сейчас: {PARITY_NAME[par_now] if par_now else 'неизвестно (спроси, если в расписании есть чёт/нечёт)'}.",
        "ТЕКУЩЕЕ РАСПИСАНИЕ:\n" + ("\n".join(lines) if lines else "пока не сохранено"),
    ]
    if pending.get("lessons"):
        draft = [f"{DAYS_SHORT[l['weekday']]} {l['start']}–{l['end']} {l['subject']}"
                 + (f", ауд. {l['room']}" if l["room"] else "") + (f", {l['teacher']}" if l["teacher"] else "")
                 + (f" [{l['parity']}]" if l["parity"] != "all" else "") for l in pending["lessons"][:90]]
        parts.append("ЧЕРНОВИК, ожидающий подтверждения (правки к нему вноси повторным save_schedule с ПОЛНЫМ "
                     "исправленным списком):\n" + "\n".join(draft))
    return "\n".join(parts)


# ======================================================================
# Сводка и напоминания (вызывается планировщиком бота)
# ======================================================================
def build_digest() -> str:
    esc = core.esc
    d = today()
    lessons = lessons_on(d)
    if lessons:
        first = lessons[0]
        room = f" · ауд. {esc(first['room'])}" if first.get("room") else ""
        intro = f"☀️ Сегодня первая пара в <b>{first['start']}</b> — <b>{esc(first['subject'])}</b>{room}."
    else:
        intro = "☀️ Сегодня пар нет."
    out = [intro, "", format_day(d, with_next=False)]
    soon = []
    now = core.now_local()
    for t in core.active_tasks():
        if not core.account_owns_task(core.CURRENT_ACCOUNT.get(), t):
            continue
        dl = core.parse_deadline(t.get("deadline"))
        if dl and dl <= now + timedelta(days=2):
            when = "🔴 просрочено" if dl < now else f"до {dl:%d.%m %H:%M}"
            soon.append((dl, f"• {esc(core.strip_md(t['text']).strip().splitlines()[0][:70])} — {when}"))
    if soon:
        soon.sort(key=lambda x: x[0])
        out += ["", "📋 <b>Скоро дедлайны:</b>"] + [x[1] for x in soon[:5]]
    due_fn = getattr(core, "study_due_count", None)
    if due_fn:
        n = due_fn()
        if n:
            out += ["", f"🧠 К повторению сегодня: <b>{n}</b> карточек — /cards"]
    return "\n".join(out)


async def tick() -> None:
    for account in list(core.USER_SETTINGS.get("accounts", {}).values()):
        uid = account.get("tg_id")
        if not uid:
            continue
        with core.timetable_account_context({"id": uid}, account):
            state = get_current_st()
            if not (state.get("lessons") or state.get("exceptions") or subscription() or state.get("owner") or core.is_primary_user(uid)):
                continue
            try:
                await tick_account(uid, state)
            except Exception:
                core.log.exception("Не удалось отправить уведомление расписания")


async def tick_account(uid, state):
    now = core.now_local()
    iso = now.date().isoformat()
    following = state.get("following", {})
    if following.get("notify") and subscription():
        pending = [c for c in grid().get("changes", []) if c["seq"] > following.get("seen", 0)]
        if pending:
            lines = [c["text"] for c in pending[-8:]]
            await core.bot.send_message(uid, "Изменения в расписании группы\n\n" + "\n".join(lines))
            following["seen"] = pending[-1]["seq"]
            save()
    if "sent" not in state or not isinstance(state["sent"], dict):
        state["sent"] = {}
    sent = state["sent"]
    stale = [k for k in sent if not k.startswith(iso)]
    for k in stale:
        del sent[k]
    changed = bool(stale)
    if state.get("digest_on") and state.get("digest_hour", 8) <= now.hour < state.get("digest_hour", 8) + 3 and f"{iso}:digest" not in sent:
        await core.bot.send_message(uid, build_digest(), reply_markup=day_kb(now.date()), parse_mode="HTML")
        sent[f"{iso}:digest"] = 1
        changed = True
    mins = int(state.get("remind_min") or 0)
    if mins > 0:
        for l in lessons_on(now.date()):
            key = f"{iso}:{l.get('id') or 'x' + str(l.get('exc_id'))}:{l.get('start', '')}"
            if key in sent:
                continue
            h, m = map(int, l["start"].split(":"))
            left = (datetime.combine(now.date(), time(h, m), tzinfo=now.tzinfo) - now).total_seconds() / 60
            if 0 < left <= mins:
                room = f" · ауд. {core.esc(l['room'])}" if l.get("room") else ""
                await core.bot.send_message(uid, f"⏰ Через {int(left + 0.5)} мин: <b>{core.esc(l['subject'])}</b> в {l['start']}{room}", parse_mode="HTML")
                sent[key] = 1
                changed = True
    if changed:
        save()


# ======================================================================
# API для мини-приложения
# ======================================================================
def api_snapshot() -> dict:
    s = get_current_st()
    t = today()
    timetable = grid()
    exceptions = []
    for e in timetable["exceptions"]:
        if e["date"] >= (t - timedelta(days=7)).isoformat():
            public = {k: e[k] for k in ("id", "date", "kind", "lesson_id") if k in e}
            if e.get("lesson"):
                public["lesson"] = public_lesson(e["lesson"])
            exceptions.append(public)
    return {"lessons": [public_lesson(l) for l in timetable["lessons"]], "exceptions": exceptions,
            "sharing": share_info(), "anchor": timetable.get("anchor", ""), "parity_now": parity_of(t), "today": t.isoformat(),
            "settings": {"digest_on": s["digest_on"], "digest_hour": s["digest_hour"], "remind_min": s["remind_min"]}}


def day_payload(d: date) -> dict:
    lessons = lessons_on(d)
    source = grid_id()
    records = get_current_st().get("materials", {})
    for l in lessons:
        meta = records.get(material_key(source, d, l), {})
        l.update(source=source, date=d.isoformat(), homework=meta.get("homework", ""),
                 description=meta.get("description", ""), has_notes=bool(meta.get("notes")),
                 file_count=len(meta.get("files", [])), shared=bool(subscription()))
    return {"date": d.isoformat(), "title": day_title(d), "weekday": d.weekday(), "parity": parity_of(d),
            "lessons": lessons, "cancelled": cancelled_on(d)}


async def api_timetable(request, user):
    return api_snapshot()


async def api_day(request, user):
    d = parse_date(request.query.get("date")) or today()
    return day_payload(d)


async def api_week(request, user):
    d = parse_date(request.query.get("date")) or today()
    mon = monday(d)
    return {"monday": mon.isoformat(), "sharing": share_info(), "parity": parity_of(mon),
            "days": [day_payload(mon + timedelta(days=i)) for i in range(7)]}


async def api_lesson_save(request, user):
    if subscription():
        raise ValueError("Сетку пар может менять только автор расписания")
    body = await request.json()
    lid = int(body.get("id") or 0)
    if lid:
        fields = {k: body.get(k) for k in LESSON_PROPS if k in body}
        if "weekday" in fields:
            fields["weekday"] = weekday_index(fields["weekday"])
        l = update_lesson(lid, fields)
        if not l:
            return {"error": "Пара не найдена или данные некорректны"}
    else:
        added = add_lessons([dict(body)], merge_dupes=False)
        if not added:
            return {"error": "Нужны день, время начала и название"}
        l = added[0]
    save()
    return {"ok": True, "lesson": l}


async def api_lesson_delete(request, user):
    if subscription():
        raise ValueError("Сетку пар может менять только автор расписания")
    ok = delete_lesson(int(request.match_info["id"]))
    save()
    return {"ok": ok}


async def api_exception_save(request, user):
    if subscription():
        raise ValueError("Сетку пар может менять только автор расписания")
    body = await request.json()
    d = parse_date(body.get("date"))
    kind = body.get("kind")
    if not d or kind not in ("cancel", "add"):
        return {"error": "Нужны дата и тип (cancel/add)"}
    exc = add_exception(d, kind, lesson_id=int(body.get("lesson_id") or 0), lesson=body.get("lesson"))
    if not exc:
        return {"error": "Не удалось создать изменение"}
    save()
    return {"ok": True, "exception": exc}


async def api_exception_delete(request, user):
    if subscription():
        raise ValueError("Сетку пар может менять только автор расписания")
    ok = delete_exception(int(request.match_info["id"]))
    save()
    return {"ok": ok}


async def api_settings(request, user):
    body = await request.json()
    state = get_current_st()
    if subscription() and "notify_changes" in body:
        following = state["following"]
        if body["notify_changes"] and not following.get("notify"):
            following["seen"] = grid().get("change_seq", 0)
        following["notify"] = bool(body["notify_changes"])
    if "digest_on" in body:
        state["digest_on"] = bool(body["digest_on"])
    if "digest_hour" in body:
        h = int(body["digest_hour"])
        if 0 <= h <= 23:
            state["digest_hour"] = h
    if "remind_min" in body:
        m = int(body["remind_min"])
        if 0 <= m <= 180:
            state["remind_min"] = m
    if not subscription() and body.get("parity") in ("odd", "even"):
        set_current_parity(body["parity"])
    save()
    return {"ok": True, **api_snapshot()}


# ======================================================================
# Подключение к боту
# ======================================================================
def setup(c) -> None:
    global core
    core = c
    core.STATE_FILES = tuple(core.STATE_FILES) + (FILE,)
    core.PLUGIN_LOADERS.append(load)
    core.PLUGIN_TOOLS.extend(TOOLS)
    core.PLUGIN_HANDLERS.update({
        "save_schedule": t_save_schedule, "edit_schedule": t_edit_schedule,
        "schedule_exception": t_schedule_exception, "show_schedule": t_show_schedule,
        "set_week_parity": t_set_week_parity, "timetable_settings": t_settings})
    core.PLUGIN_PROMPT.append(prompt_hook)
    core.PLUGIN_TICKS.append(tick)
    core.API_ROUTES.extend([
        ("POST", "/api/timetable/share", api_share),
        ("GET", "/api/timetable/materials", api_materials),
        ("POST", "/api/timetable/materials", api_materials),
        ("GET", "/api/timetable", api_timetable), ("GET", "/api/timetable/day", api_day),
        ("GET", "/api/timetable/week", api_week), ("POST", "/api/timetable/lesson", api_lesson_save),
        ("DELETE", "/api/timetable/lesson/{id}", api_lesson_delete),
        ("POST", "/api/timetable/exception", api_exception_save),
        ("DELETE", "/api/timetable/exception/{id}", api_exception_delete),
        ("POST", "/api/timetable/settings", api_settings)])
    dp = core.dp
    priv = F.chat.type == "private"
    dp.message(F.text == "📅 Сегодня", priv)(cmd_today)
    dp.message(Command("today"), priv)(cmd_today)
    dp.message(F.text == "📅 Завтра", priv)(cmd_tomorrow)
    dp.message(Command("tomorrow"), priv)(cmd_tomorrow)
    dp.message(F.text == "🗓 Неделя", priv)(cmd_week)
    dp.message(Command("week"), priv)(cmd_week)
    dp.message(Command("schedule"), priv)(cmd_schedule)
    dp.message(Command("ics"), priv)(cmd_ics)
    dp.callback_query(F.data.startswith("tt_d_"))(cb_day)
    dp.callback_query(F.data.startswith("tt_w_"))(cb_week)
    dp.callback_query(F.data.in_({"tt_ok", "tt_no", "tt_fix"}))(cb_pending)
