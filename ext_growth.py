# -*- coding: utf-8 -*-
"""Рост: серия, XP, квест, цели, дневник, ИИ (фото/голос), план, дуэли, группа, пуши.
Подключать ПОСЛЕ ext_webapp. Env: BOT_USERNAME, PUBLIC_URL. ИИ идёт через ключи аккаунта из ext_webapp (свои у гостей, общий пул у админа)."""
import asyncio, functools, json, os, re, secrets, sys, time
from datetime import date, timedelta
from urllib.parse import parse_qsl

import aiohttp
from aiohttp import web

core = None
HERE = os.path.dirname(os.path.abspath(__file__))
FILE = "growth.json"  # хранится там же, где остальное состояние бота (STATE_FILES)
XP = {"cards": 2, "task": 10, "ask": 3, "goal": 5, "journal": 8, "duel": 20}
STEPS = [0, 1, 2, 4, 8, 16]
DB = {"u": {}, "duels": {}, "groups": {}}


def load():
    DB.update(core.load_json(FILE, {}) or {})


def save():
    core.save_json(FILE, DB)


def day():
    return core.now_local().date()


def award(u, kind):
    u["xp"] += XP[kind]
    if kind not in u["flags"]:
        u["flags"].append(kind)
    t = day()
    if len(u["flags"]) >= 3 and u["last"] != str(t):
        if u["last"] == str(t - timedelta(days=1)):
            u["streak"] += 1
        elif u["last"] == str(t - timedelta(days=2)) and u["freeze"] > 0:
            u["freeze"] -= 1
            u["streak"] += 1
        else:
            u["streak"] = 1
        u["last"], u["xp"] = str(t), u["xp"] + 30
        if u["streak"] % 7 == 0:
            u["freeze"] = min(2, u["freeze"] + 1)


def gid(request):
    return dict(parse_qsl(request.headers.get("X-Init-Data", ""))).get("chat_instance", "")


# ---------------------------------------------------------------- ИИ и данные
def W():
    return sys.modules["ext_webapp"]


def U(request, user):
    acc = W().get_request_account(request, user)
    u = DB["u"].setdefault(acc["id"], {"xp": 0, "streak": 0, "freeze": 1, "last": "", "qd": "", "flags": [], "goals": [],
                                        "journal": [], "plan": [], "notify": True, "gids": []})
    u.update(id=acc["id"], tg=user["id"], name=acc.get("name") or user.get("first_name", ""))
    if u["qd"] != str(day()):
        u["qd"], u["flags"] = str(day()), []
    return u


async def ai(request, user, prompt, files=None):
    w = W()
    acc = w.get_request_account(request, user)
    parts = (await w._decode_uploaded_files(files))[0] if files else []
    resp, _, _ = await w._generate_with_account_pool(acc, user, history=[], user_text=prompt, extra_parts=parts, use_tools=False,
                                                     current_topic=None, last_files_names=[], scope=core.enabled_groups(), is_private=True)
    return "".join(p.text for p in (resp.candidates[0].content.parts or []) if getattr(p, "text", None)).strip()


def jl(text):
    return json.loads(re.search(r"\{.*\}", text, re.S).group(0))


# ---------------------------------------------------------------- API
async def a_state(request, user):
    u, g = U(request, user), gid(request)
    if g:
        DB["groups"].setdefault(g, {"owner": u["id"], "dl": []})
        if g not in u["gids"]:
            u["gids"].append(g)
        save()
    t = str(day())
    return {"name": u["name"], "xp": u["xp"], "level": u["xp"] // 100 + 1, "streak": u["streak"], "freeze": u["freeze"],
            "done": u["last"] == t, "flags": u["flags"], "risk": bool(u["streak"] and u["last"] != t), "today": t,
            "goals": [{**x, "days": x["days"][-14:]} for x in u["goals"]], "journal": u["journal"][-1:], "plan": u["plan"],
            "notify": u["notify"], "bot": os.environ.get("BOT_USERNAME", "")}


async def a_snap(request, user):
    b, u = await request.json(), U(request, user)
    hint = {"photo": "На фото задача или конспект: разбери по шагам и объясни суть.",
            "voice": "Это аудио лекции: сделай краткий конспект и выдели термины."}.get(b.get("kind"), "Ответь кратко и по шагам.")
    q = str(b.get("text") or "Разбери.")[:3000]
    r = jl(await ai(request, user, f'{hint} По-русски, без LaTeX. Ответ СТРОГО JSON: {{"answer":"...","cards":[{{"front":"","back":""}}]}} — до 8 карточек по ключевым понятиям.\n\n{q}', b.get("files")))
    award(u, "ask")
    save()
    return {"answer": str(r.get("answer", "")), "cards": [c for c in r.get("cards", []) if c.get("front") and c.get("back")][:8]}


async def a_goal_add(request, user):
    name = str((await request.json())["name"]).strip()[:60]
    if name:
        U(request, user)["goals"].append({"id": secrets.token_hex(3), "name": name, "days": []})
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
    text, u = str((await request.json())["text"]).strip()[:2000], U(request, user)
    if not text:
        raise ValueError("Пусто")
    reply = await ai(request, user, "Ты внимательный коуч. По-русски, 2–3 предложения: отметь главное и задай один вопрос для размышления.\n\n" + text)
    first = not (u["journal"] and u["journal"][-1]["d"] == str(day()))
    u["journal"].append({"d": str(day()), "t": text, "ai": reply})
    del u["journal"][:-30]
    if first:
        award(u, "journal")
    save()
    return {"ai": reply}


async def a_plan(request, user):
    b, u = await request.json(), U(request, user)
    n = (date.fromisoformat(b["date"]) - day()).days
    if not 1 <= n <= 60:
        raise ValueError("Дата должна быть в пределах 60 дней")
    r = jl(await ai(request, user, f'Составь план подготовки к экзамену по предмету «{str(b["subject"])[:100]}»: дней до экзамена {n}, первый день {day()}, '
                    'по одному пункту в день, последние 1–2 дня — повторение. Ответ СТРОГО JSON {"days":[{"d":"YYYY-MM-DD","t":"что делать"}]}'))
    u["plan"] = [x for x in r["days"] if x.get("d") and x.get("t")][:60]
    save()
    return {"plan": u["plan"]}


async def a_duel_new(request, user):
    topic = str((await request.json())["topic"]).strip()[:100]
    r = jl(await ai(request, user, f'Сделай 5 тестовых вопросов по-русски на тему «{topic}». Ответ СТРОГО JSON {{"qs":[{{"q":"","a":["","","",""],"c":0}}]}}, c — индекс верного ответа.'))
    i = secrets.token_urlsafe(5)
    DB["duels"][i] = {"topic": topic, "qs": r["qs"][:5], "res": {}}
    save()
    return {"id": i}


async def a_duel_get(request, user):
    d, u = DB["duels"][request.match_info["id"]], U(request, user)
    return {"topic": d["topic"], "qs": [{"q": q["q"], "a": q["a"]} for q in d["qs"]], "res": list(d["res"].values()), "played": u["id"] in d["res"]}


async def a_duel_ans(request, user):
    d, u = DB["duels"][request.match_info["id"]], U(request, user)
    if u["id"] not in d["res"]:
        a = (await request.json())["a"]
        d["res"][u["id"]] = {"name": u["name"], "score": sum(1 for q, x in zip(d["qs"], a) if q["c"] == x)}
        award(u, "duel")
        save()
    return {"score": d["res"][u["id"]]["score"], "res": list(d["res"].values())}


async def a_board(request, user):
    u = U(request, user)
    g = gid(request) or (u["gids"][-1] if u["gids"] else "")
    gr = DB["groups"].get(g)
    rows = sorted(({"name": v["name"], "xp": v["xp"], "streak": v["streak"], "me": v["id"] == u["id"]}
                   for v in DB["u"].values() if g and g in v.get("gids", [])), key=lambda r: -r["xp"])[:20]
    return {"rows": rows, "has": bool(gr), "owner": bool(gr and gr["owner"] == u["id"]),
            "dl": [x for x in (gr or {}).get("dl", []) if x["d"] >= str(day())]}


async def a_dl_add(request, user):
    b, u = await request.json(), U(request, user)
    gr = DB["groups"].get(gid(request) or (u["gids"] or [""])[-1])
    if not gr or gr["owner"] != u["id"]:
        raise ValueError("Только ведущий группы")
    gr["dl"] = sorted(gr["dl"] + [{"t": str(b["t"])[:120], "d": str(b["d"])}], key=lambda x: x["d"])[:30]
    save()
    return {"ok": True}


async def a_notify(request, user):
    u = U(request, user)
    u["notify"] = not u["notify"]
    save()
    return {"notify": u["notify"]}


# ---------------------------------------------------------------- пуши
async def tg(uid, text):
    base = os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")
    async with aiohttp.ClientSession() as s:
        await s.post(f"https://api.telegram.org/bot{core.TELEGRAM_BOT_TOKEN}/sendMessage",
                     json={"chat_id": int(uid), "text": text, "reply_markup": {"inline_keyboard": [[{"text": "Открыть", "web_app": {"url": base + "/app"}}]]}})


async def tick():
    if core is not None:
        try:
            n = core.now_local()
            hm, t = n.strftime("%H:%M"), str(n.date())
            for uid, u in list(DB["u"].items()):
                if not u.get("notify") or u.get("sent") == t + hm:
                    continue
                if hm == "08:00":
                    item = next((p["t"] for p in u["plan"] if p["d"] == t), "")
                    msg = f"Доброе утро. Серия: {u['streak']}." + (f"\nПо плану: {item}" if item else "")
                elif hm == "20:30" and u["streak"] and u["last"] != t:
                    msg = f"Серия {u['streak']} дн. сгорит сегодня. Три минуты — и она спасена."
                else:
                    continue
                u["sent"] = t + hm
                await tg(u["tg"], msg)
        except Exception:
            core.log.exception("pusher")


# ---------------------------------------------------------------- подключение
def setup(c) -> None:
    """Подключать ПОСЛЕ ext_webapp, ext_study и ext_timetable."""
    global core
    core = c
    core.STATE_FILES = tuple(core.STATE_FILES) + (FILE,)
    core.PLUGIN_LOADERS.append(load)  # состояние читается после восстановления из Gist
    core.PLUGIN_TICKS.append(tick)  # пуши: планировщик бота вызывает раз в ~30 с
    routes = [("GET", "/api/g/state", a_state), ("POST", "/api/g/snap", a_snap), ("POST", "/api/g/goals", a_goal_add),
              ("DELETE", "/api/g/goals/{id}", a_goal_del), ("POST", "/api/g/goals/{id}/check", a_goal_check),
              ("POST", "/api/g/journal", a_journal), ("POST", "/api/g/plan", a_plan), ("POST", "/api/g/duel", a_duel_new),
              ("GET", "/api/g/duel/{id}", a_duel_get), ("POST", "/api/g/duel/{id}/answer", a_duel_ans),
              ("GET", "/api/g/board", a_board), ("POST", "/api/g/group/dl", a_dl_add), ("POST", "/api/g/notify", a_notify)]
    hooks = {"/api/tasks/{id}/done": "task", "/api/cards/{id}/review": "cards", "/api/ask": "ask"}
    for i, (m, p, old) in enumerate(core.API_ROUTES):  # XP за реальные действия в уже существующих разделах
        if p in hooks and m == "POST":
            def make(old, kind):
                @functools.wraps(old)  # сохраняет __module__ — per_account для карточек продолжает работать
                async def hooked(request, user):
                    r = await old(request, user)
                    if isinstance(r, dict) and not r.get("error") and r.get("ok", True):
                        award(U(request, user), kind)
                        save()
                    return r
                return hooked
            core.API_ROUTES[i] = (m, p, make(old, hooks[p]))
    core.API_ROUTES.extend(routes)
