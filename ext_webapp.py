# -*- coding: utf-8 -*-
"""
Мини-приложение Telegram (модуль для bot.py).

Возможности:
* Изолированные аккаунты с аватарками (фото или эмодзи), именем и личным README.md.
* Bring Your Own Key (BYOK): поддержка личных API-ключей Gemini (с проверкой и каскадом моделей) + экран регистрации для внешних пользователей.
* Сохранение и просмотр прикреплённых файлов сверху каждой записи в Истории.
* Компактная клавиатура Telegram-бота (2 ряда вместо 6).
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from typing import Optional
from urllib.parse import parse_qsl, quote

from aiohttp import web
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    KeyboardButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from google import genai
from google.genai import types as genai_types

core = None
HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "webapp", "index.html")
APP_CHATS: dict = {}        # account_id -> list of chat messages
GENERATED_FILES: dict = {}  # file_id -> {"data": bytes, "name": str, "mime": str, "ts": float}
USER_POOLS: dict = {}       # account_id -> (keys_tuple, KeyPool)
LATEX_RE = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]|\\\((.+?)\\\)|\$(?!\s)([^$\n]+?)(?<!\s)\$", re.S)


def validate_init_data(init_data: str, token: str, max_age: int = 86400 * 7) -> Optional[dict]:
    """Проверка подписи Telegram WebApp. Возвращает dict пользователя или None."""
    try:
        if not init_data:
            return None
        fields = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        parsed = dict(fields)
        if len(parsed) != len(fields):
            return None
        got = parsed.pop("hash", "")
        if not got:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        auth_date = int(parsed.get("auth_date", "0"))
        if auth_date <= 0 or abs(time.time() - auth_date) > max_age:
            return None
        user = json.loads(parsed.get("user", "null"))
        if not isinstance(user, dict) or type(user.get("id")) is not int or user["id"] <= 0:
            return None
        return user
    except (ValueError, TypeError):
        return None


def _parse_keys(raw: str) -> list:
    return [x.strip() for x in re.split(r"[,\s;]+", raw or "") if x.strip()]


def _mask_key(raw: str) -> str:
    keys = _parse_keys(raw)
    if not keys:
        return ""
    k = keys[0]
    masked = (k[:6] + "••••••••" + k[-4:]) if len(k) > 12 else "••••••••"
    return masked + (f" (+ещё {len(keys) - 1})" if len(keys) > 1 else "")


def is_user_admin(uid: int) -> bool:
    if not uid:
        return False
    if getattr(core, "ADMIN_IDS", None):
        raw = core.ADMIN_IDS
        if isinstance(raw, (str, int)):
            raw = [raw]
        admin_ids = set()
        for x in raw:
            for part in str(x).replace(",", " ").split():
                clean = part.strip().lstrip("-")
                if clean.isdigit():
                    admin_ids.add(int(part.strip()))
        if admin_ids:
            return int(uid) in admin_ids
    # Если ADMIN_IDS в .env пуст, владельцем считается первый аккаунт в settings.json
    accs = core.USER_SETTINGS.get("accounts", {})
    if accs:
        first_acc = next(iter(accs.values()))
        if first_acc.get("tg_id") and int(first_acc["tg_id"]) == int(uid):
            return True
    return False


# =====================================================================
# МУЛЬТИ-АККАУНТЫ, АВАТАРКИ И BYOK (СВОИ API-КЛЮЧИ)
# =====================================================================
def ensure_accounts_store() -> dict:
    s = core.USER_SETTINGS
    accs = s.setdefault("accounts", {})
    first_admin = 0
    if getattr(core, "ADMIN_IDS", None):
        for part in core.ADMIN_IDS:
            if str(part).strip().lstrip("-").isdigit():
                first_admin = int(str(part).strip())
                break
    if not accs:
        aid = f"tg_{first_admin}" if first_admin else "acc_main"
        accs[aid] = {
            "id": aid,
            "name": "Артём",
            "avatar": "🧑‍💻",
            "tg_id": first_admin,
            "custom_api_key": "",
            "user_profile": s.get("user_profile", ""),
            "custom_instructions": s.get("custom_instructions", ""),
            "user_readme": s.get("user_readme", ""),
            "answer_mode": s.get("answer_mode", "detailed"),
            "preferred_model": s.get("preferred_model") or core.GEMINI_MODEL,
            "model_fallback": bool(s.get("model_fallback", True)),
            "media_enabled": bool(s.get("media_enabled", True)),
        }
        core.save_json(core.SETTINGS_FILE, s)
    # Авто-исправление старого дефолтного имени «Основной аккаунт»
    for a in accs.values():
        if a.get("name") == "Основной аккаунт":
            a["name"] = "Артём"
        a.setdefault("avatar", "🧑‍💻")
        a.setdefault("custom_api_key", "")
    return accs


def get_or_create_account(user: dict, requested_acc_id: str = "") -> dict:
    accs = ensure_accounts_store()
    uid = int(user.get("id") or 0)
    uname = (user.get("first_name") or "").strip() or "Пользователь"
    admin_ok = is_user_admin(uid)

    req_id = (requested_acc_id or "").strip()
    if req_id and req_id in accs:
        target = accs[req_id]
        # Безопасность: пользователь может открывать ТОЛЬКО свои профили!
        if int(target.get("tg_id") or 0) == uid:
            if target.get("name") == "Основной аккаунт":
                target["name"] = uname
            return target
        # Чужой аккаунт переключать запрещено

    if uid:
        for a in accs.values():
            if int(a.get("tg_id") or 0) == uid:
                if a.get("name") == "Основной аккаунт":
                    a["name"] = uname
                    core.save_json(core.SETTINGS_FILE, core.USER_SETTINGS)
                return a

        if admin_ok and "acc_main" in accs and not accs["acc_main"].get("tg_id"):
            accs["acc_main"]["tg_id"] = uid
            accs["acc_main"]["name"] = uname
            core.save_json(core.SETTINGS_FILE, core.USER_SETTINGS)
            return accs["acc_main"]

        new_id = f"tg_{uid}"
        accs[new_id] = {
            "id": new_id,
            "name": uname,
            "avatar": "🧑‍💻",
            "tg_id": uid,
            "custom_api_key": "",
            "user_profile": "Студент университета.",
            "custom_instructions": "Объясняй понятно, по делу, при решении задач давай пошаговый ход мысли.",
            "user_readme": "",
            "answer_mode": "detailed",
            "preferred_model": core.GEMINI_MODEL,
            "model_fallback": True,
            "media_enabled": True,
        }
        core.save_json(core.SETTINGS_FILE, core.USER_SETTINGS)
        return accs[new_id]

    return next(iter(accs.values()))


def get_account_pool(acc: dict):
    """Возвращает персональный KeyPool, если у аккаунта задан свой ключ, иначе общий пул (для админов)."""
    raw_key = (acc.get("custom_api_key") or "").strip()
    keys = _parse_keys(raw_key)
    if keys:
        aid = acc["id"]
        cached = USER_POOLS.get(aid)
        if not cached or cached[0] != tuple(keys):
            USER_POOLS[aid] = (tuple(keys), core.KeyPool(keys))
        return USER_POOLS[aid][1], True
    if is_user_admin(int(acc.get("tg_id") or 0)):
        return core.pool, False
    return None, True


def account_needs_key(acc: dict, user: dict) -> bool:
    """True, если пользователь не админ и ещё не ввёл свой собственный API-ключ Gemini."""
    if is_user_admin(int(user.get("id") or 0)):
        return False
    return not bool(_parse_keys(acc.get("custom_api_key", "")))


def _guest_has_own_key(uid: int) -> bool:
    """Гость допускается к чату с ИИ только со своим ключом."""
    for a in ensure_accounts_store().values():
        if int(a.get("tg_id") or 0) == int(uid):
            return bool(_parse_keys(a.get("custom_api_key", "")))
    return False


def guest_error(user: dict):
    """None, если можно работать; иначе ответ-ошибка (гость без своего ключа не должен тратить ключи владельца)."""
    uid = int(user.get("id") or 0)
    if is_user_admin(uid):
        return None
    if not _guest_has_own_key(uid):
        return web.json_response({"error": "Подключите свой ключ Gemini в настройках приложения"}, status=403)
    return None


@contextmanager
def account_context(user: dict, account: dict, *, private: bool = True):
    uid = user["id"]
    guest_uid = uid if private and not is_user_admin(uid) else None
    account_token = core.CURRENT_ACCOUNT.set(account)
    guest_token = core.CURRENT_GUEST.set(guest_uid)
    try:
        yield
    finally:
        core.CURRENT_GUEST.reset(guest_token)
        core.CURRENT_ACCOUNT.reset(account_token)


def authenticate(request: web.Request):
    """Require signed Telegram Mini App credentials."""
    raw_init = request.headers.get("X-Init-Data", "") or request.query.get("initData", "")
    user = validate_init_data(raw_init, core.TELEGRAM_BOT_TOKEN)
    if user is not None:
        return user
    return web.json_response(
        {"error": "Пожалуйста, откройте мини-приложение через Telegram-бота."}, status=401
    )


def get_request_account(request: web.Request, user: dict) -> dict:
    req_acc = request.headers.get("X-Account-Id", "") or request.query.get("acc", "")
    return get_or_create_account(user, req_acc)


def wrap(handler):
    async def inner(request: web.Request):
        user = authenticate(request)
        if isinstance(user, web.Response):
            return user
        try:
            account = get_request_account(request, user)
            with account_context(user, account):
                data = await handler(request, user)
            if isinstance(data, web.StreamResponse):
                return data
        except asyncio.CancelledError:
            raise
        except web.HTTPException:
            raise
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
            return web.json_response({"error": f"Некорректный запрос: {e}"}, status=400)
        except Exception as e:
            core.log.exception("api error %s", request.path)
            return web.json_response({"error": str(e)[:300] or "Ошибка сервера"}, status=500)
        return web.json_response(data, dumps=lambda o: json.dumps(o, ensure_ascii=False))
    return inner


async def index(request: web.Request) -> web.Response:
    try:
        with open(INDEX, "r", encoding="utf-8") as f:
            html = f.read()
    except OSError:
        return web.Response(text="webapp/index.html не найден в репозитории", status=404)
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


def de_latex(text: str) -> str:
    return LATEX_RE.sub(lambda m: core.tex_plain(next(g for g in m.groups() if g is not None)), text or "")


def mod(name: str):
    return sys.modules.get(name)


def prompt_hook(scope, is_private) -> str:
    readme = (core.get_current_account_settings().get("user_readme") or "").strip()
    if not readme:
        return ""
    return (
        "ДОСЬЕ И ПОЛНАЯ ИНСТРУКЦИЯ О ПОЛЬЗОВАТЕЛЕ (README.md АКТИВНОГО АККАУНТА):\n"
        "Внимательно изучи этот документ. Всегда обращайся к пользователю по имени, указанному ниже, "
        "учитывай его курс, вуз, стек, проекты, повадки и предпочтения с самого первого сообщения. "
        "НИКОГДА не спрашивай, как зовут пользователя, если это уже написано в досье:\n"
        f"{readme}"
    )


def _store_generated_file(data: bytes, filename: str, mime: str) -> str:
    now = time.time()
    for k in [k for k, v in GENERATED_FILES.items() if now - v["ts"] > 7200]:
        GENERATED_FILES.pop(k, None)
    while len(GENERATED_FILES) >= 60:
        oldest = min(GENERATED_FILES, key=lambda k: GENERATED_FILES[k]["ts"])
        GENERATED_FILES.pop(oldest, None)
    fid = uuid.uuid4().hex
    GENERATED_FILES[fid] = {"owner": core.CURRENT_ACCOUNT.get()["id"], "data": data, "name": filename, "mime": mime, "ts": now}
    return fid


async def _decode_uploaded_files(raw_files: list) -> tuple:
    """Превращает прикреплённые в чате файлы в части для Gemini + сохраняет их для просмотра в Истории."""
    extra_parts = []
    file_names = []
    stored_meta = []
    for f in (raw_files or [])[:5]:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name") or "file").strip()[:80]
        mime = str(f.get("type") or "application/octet-stream").strip()
        data_url = str(f.get("data") or "")
        if "," in data_url:
            data_url = data_url.split(",", 1)[1]
        if not data_url:
            continue
        raw = base64.b64decode(data_url)
        fid = _store_generated_file(raw, name, mime)
        stored_meta.append({"id": fid, "name": name, "size": len(raw), "mime": mime})
        item = await core.build_document_item(name, raw)
        file_names.append(name)
        if item["type"] == "part" and item.get("part") is not None:
            extra_parts.append(item["part"])
        elif item["type"] == "doc" and item.get("text"):
            extra_parts.append(item["text"])
        elif item["type"] == "file" and item.get("file_obj") is not None:
            extra_parts.append(item["file_obj"])
    return extra_parts, file_names, stored_meta


def accounts_summary_list(user: dict) -> list:
    accs = ensure_accounts_store()
    uid = int(user.get("id") or 0)
    out = []
    for a in accs.values():
        # Изоляция аккаунтов: пользователь видит ТОЛЬКО свои собственные профили!
        if int(a.get("tg_id") or 0) != uid:
            continue
        has_own_key = bool(_parse_keys(a.get("custom_api_key", "")))
        out.append({
            "id": a["id"],
            "name": a.get("name") or a["id"],
            "avatar": a.get("avatar") or "🧑‍💻",
            "tg_id": a.get("tg_id") or 0,
            "model": a.get("preferred_model") or core.GEMINI_MODEL,
            "has_readme": bool((a.get("user_readme") or "").strip()),
            "has_custom_key": has_own_key,
            "masked_key": _mask_key(a.get("custom_api_key", "")),
        })
    return out


# ---------------------------------------------------------------- API
async def api_me(request, user):
    acc = get_request_account(request, user)
    admin_ok = is_user_admin(int(user.get("id") or 0))
    has_own_key = bool(_parse_keys(acc.get("custom_api_key", "")))
    return {
        "user": {"id": user["id"], "name": acc.get("name") or user.get("first_name", ""), "is_admin": admin_ok},
        "account": {
            "id": acc["id"],
            "name": acc.get("name") or "Артём",
            "avatar": acc.get("avatar") or "🧑‍💻",
            "tg_id": acc.get("tg_id") or 0,
            "has_custom_key": has_own_key,
            "masked_key": _mask_key(acc.get("custom_api_key", "")),
        },
        "needs_onboarding": False,
        "has_ai": has_own_key or (admin_ok and bool(core.pool.keys)),
        "accounts": accounts_summary_list(user),
        "now": core.now_local().isoformat(),
        "tz": getattr(core.TZ, "key", "UTC"),
        "model": acc.get("preferred_model") or core.GEMINI_MODEL,
        "model_fallback": bool(acc.get("model_fallback", True)),
        "features": {"timetable": bool(mod("ext_timetable")), "study": bool(mod("ext_study"))},
    }


async def api_verify_key(request, user):
    """Живая проверка пользовательского API-ключа Google AI Studio."""
    body = await request.json()
    keys = _parse_keys(str(body.get("api_key") or ""))
    if not keys:
        return {"error": "Введите хотя бы один API-ключ"}
    test_key = keys[0]

    if len(test_key) < 20:
        return {"error": "Слишком короткий ключ. Проверьте правильность копирования."}

    try:
        client = genai.Client(api_key=test_key, http_options=genai_types.HttpOptions(timeout=10000))
        cascade = getattr(core, "MODEL_CASCADE", [])
        test_models = [getattr(core, "GEMINI_MODEL", "gemini-3.8-flash")] + list(cascade) + [
            "gemini-3.8-flash",
            "gemini-3.6-flash",
            "gemini-3.5-flash-lite",
            "gemini-2.5-flash",
        ]
        test_models = list(dict.fromkeys(test_models))

        last_err = None
        for m in test_models:
            if not m:
                continue
            try:
                await asyncio.wait_for(
                    client.aio.models.generate_content(model=m, contents="ping"),
                    timeout=10,
                )
                return {"ok": True, "count": len(keys), "masked": _mask_key(body.get("api_key", ""))}
            except Exception as ex:
                last_err = ex
                up_sub = str(ex).upper()
                if any(w in up_sub for w in ("429", "RESOURCE_EXHAUSTED", "QUOTA")):
                    return {"ok": True, "count": len(keys), "masked": _mask_key(body.get("api_key", ""))}
                if any(w in up_sub for w in ("API_KEY_INVALID", "INVALID", "UNAUTHENTICATED", "401", "403", "PERMISSION_DENIED")):
                    return {"error": "Ключ недействителен. Проверьте правильность копирования из Google AI Studio."}
                if "404" in up_sub or "NOT_FOUND" in up_sub:
                    continue
                continue

        if last_err:
            up_last = str(last_err).upper()
            if any(w in up_last for w in ("API_KEY_INVALID", "INVALID", "UNAUTHENTICATED", "401", "403")):
                return {"error": "Ключ недействителен. Проверьте правильность копирования из Google AI Studio."}

        return {"ok": True, "count": len(keys), "masked": _mask_key(body.get("api_key", ""))}
    except Exception as e:
        up = str(e).upper()
        if any(w in up for w in ("API_KEY_INVALID", "INVALID", "UNAUTHENTICATED", "401", "403")):
            return {"error": "Ключ недействителен. Проверьте правильность копирования из Google AI Studio."}
        if "429" in up or "RESOURCE_EXHAUSTED" in up:
            return {"ok": True, "count": len(keys), "masked": _mask_key(body.get("api_key", ""))}
        return {"error": f"Не удалось проверить ключ: {str(e)[:120]}"}


async def api_account_save(request, user):
    """Создание, редактирование профиля (имя, аватарка, свой API-ключ) или удаление аккаунта."""
    body = await request.json()
    accs = ensure_accounts_store()
    action = body.get("action", "create")
    uid = int(user.get("id") or 0)

    if action == "delete":
        aid = str(body.get("id") or "")
        user_accs = [a for a in accs.values() if int(a.get("tg_id") or 0) == uid]
        if len(user_accs) <= 1:
            return {"error": "Нельзя удалить единственный профиль"}
        if aid in accs and int(accs[aid].get("tg_id") or 0) == uid:
            del accs[aid]
            APP_CHATS.pop(aid, None)
            USER_POOLS.pop(aid, None)
            core.save_json(core.SETTINGS_FILE, core.USER_SETTINGS)
        remaining = [a for a in accs.values() if int(a.get("tg_id") or 0) == uid]
        next_acc = remaining[0] if remaining else next(iter(accs.values()))
        return {"ok": True, "active_id": next_acc["id"], "accounts": accounts_summary_list(user)}

    name = str(body.get("name") or "").strip()[:40]
    if not name:
        return {"error": "Укажите имя профиля"}
    avatar = str(body.get("avatar") or "").strip() or "🧑‍💻"

    aid = str(body.get("id") or "").strip()
    if aid and aid in accs:
        acc = accs[aid]
        if int(acc.get("tg_id") or 0) != uid:
            return {"error": "Нет доступа к этому профилю"}
        acc["name"] = name
        acc["avatar"] = avatar
        if "custom_api_key" in body:
            acc["custom_api_key"] = str(body.get("custom_api_key") or "").strip()
            USER_POOLS.pop(aid, None)
    else:
        aid = "acc_" + uuid.uuid4().hex[:6]
        acc = {
            "id": aid,
            "name": name,
            "avatar": avatar,
            "tg_id": uid,
            "custom_api_key": str(body.get("custom_api_key") or "").strip(),
            "user_profile": f"Меня зовут {name}.",
            "custom_instructions": "Объясняй понятно, по делу, при решении задач давай пошаговый ход мысли.",
            "user_readme": "",
            "answer_mode": "detailed",
            "preferred_model": core.GEMINI_MODEL,
            "model_fallback": True,
            "media_enabled": True,
        }
        accs[aid] = acc

    core.save_json(core.SETTINGS_FILE, core.USER_SETTINGS)
    return {
        "ok": True,
        "active_id": acc["id"],
        "account": {
            "id": acc["id"],
            "name": acc["name"],
            "avatar": acc["avatar"],
            "tg_id": acc.get("tg_id") or 0,
            "has_custom_key": bool(_parse_keys(acc.get("custom_api_key", ""))),
            "masked_key": _mask_key(acc.get("custom_api_key", "")),
        },
        "accounts": accounts_summary_list(user),
    }


async def api_home(request, user):
    acc = get_request_account(request, user)
    out = {"today": None, "tasks": [], "cards_due": 0, "cards_total": 0, "parity": None}
    tt, st = mod("ext_timetable"), mod("ext_study")
    if tt:
        try:
            out["today"] = tt.day_payload(tt.today())
            out["has_schedule"] = bool(tt.grid().get("lessons") or tt.grid().get("exceptions"))
            out["sharing"] = tt.share_info()
            out["now"] = core.now_local().isoformat()
            out["parity"] = tt.parity_of(tt.today())
        except Exception:
            core.log.exception("home timetable error")
    if st:
        try:
            s = st.card_stats()
            out["cards_due"], out["cards_total"] = s["due"], s["total"]
        except Exception:
            core.log.exception("home study stats error")
    out["tasks"] = task_list(acc["id"], only_deadline=True)[:8]
    return out


def is_primary_account(acc_id: str) -> bool:
    """Проверяет, является ли аккаунт главным (первым) профилем владельца."""
    accs = ensure_accounts_store()
    first_id = next(iter(accs.keys()), "")
    return acc_id == first_id


def task_list(account_id: str, only_deadline: bool = False) -> list:
    now = core.now_local()
    items = []
    primary = is_primary_account(account_id)
    for t in core.active_tasks():
        t_acc = t.get("acc", "")
        # Главный аккаунт видит свои и общие задачи групп; новые аккаунты — строго только свои
        if primary:
            if t_acc and t_acc != account_id:
                continue
        else:
            if t_acc != account_id:
                continue

        dl = core.parse_deadline(t.get("deadline"))
        if only_deadline and not dl:
            continue
        items.append({
            "id": t["id"], "chat_id": t.get("chat_id", core.TARGET_CHAT_ID), "thread": t.get("thread_id") or 0,
            "topic": t.get("topic", ""), "text": core.strip_md(t["text"]).strip(), "deadline": t.get("deadline", ""),
            "deadline_iso": dl.isoformat() if dl else "", "overdue": bool(dl and dl < now),
            "soon": bool(dl and now <= dl <= now + core.timedelta(days=2)), "file": t.get("file", ""),
            "kind": t.get("kind", "task"), "notif": core.has_notifications(t["id"]),
            "done_by": list((t.get("done_by") or {}).values())})
    items.sort(key=lambda x: (not x["deadline_iso"], x["deadline_iso"], x["id"]))
    return items


async def api_tasks(request, user):
    acc = get_request_account(request, user)
    primary = is_primary_account(acc["id"])
    groups: dict = {}
    for t in task_list(acc["id"]):
        g = groups.setdefault(t["chat_id"], {"chat_id": t["chat_id"], "title": core.chat_label(t["chat_id"]), "topics": {}})
        tp = g["topics"].setdefault(t["thread"], {"thread": t["thread"], "name": "", "tasks": []})
        tp["tasks"].append(t)
    out = []
    for g in groups.values():
        topics = list(g["topics"].values())
        for tp in topics:
            tp["name"] = core.topic_label(g["chat_id"], tp["thread"], [{"topic": tp["tasks"][0]["topic"]}])
        out.append({**g, "topics": topics})

    # Список недавно выполненных задач (для возможности отменить выполнение)
    done_items = []
    for t in reversed(core.TASKS.get("items", [])):
        if not t.get("done"):
            continue
        t_acc = t.get("acc", "")
        if primary:
            if t_acc and t_acc != acc["id"]:
                continue
        elif t_acc != acc["id"]:
            continue
        done_items.append({
            "id": t["id"],
            "topic": t.get("topic") or "общая",
            "text": core.strip_md(t.get("text", "")).strip()[:160],
            "deadline": t.get("deadline", ""),
            "done_at": t.get("done_at", 0),
        })
        if len(done_items) >= 20:
            break

    avail_groups = [
        {"chat_id": user["id"], "title": "Личные напоминания", "topics": [{"name": "личное", "thread": 0}]}
    ]
    for ag in acc.get("groups", []):
        avail_groups.append(ag)
    if primary:
        for cid, g in core.enabled_groups():
            t_list = [{"name": k, "thread": v} for k, v in (g.get("topics") or {}).items()] or [{"name": "общая", "thread": 0}]
            avail_groups.append({
                "chat_id": cid,
                "title": g.get("title") or f"Группа {cid}",
                "topics": t_list,
            })

    return {
        "groups": out,
        "done_tasks": done_items,
        "total": sum(len(tp["tasks"]) for g in out for tp in g["topics"]),
        "available_groups": avail_groups,
    }

async def api_task_create(request, user):
    acc = get_request_account(request, user)
    body = await request.json()
    text = str(body.get("text") or "").strip()[:2000]
    if not text:
        return {"error": "Введите текст задачи"}

    chat_id = int(body.get("chat_id") or user["id"])
    if not is_primary_account(acc["id"]):
        chat_id = int(user["id"])  # гость создаёт задачи только в своём личном чате
    topic_name = str(body.get("topic") or "").strip() or ("личное" if chat_id > 0 else "общая")
    thread_id = int(body.get("thread") or 0) or None
    deadline = str(body.get("deadline") or "").strip()
    kind = str(body.get("kind") or "task").strip()
    if kind not in ("task", "reminder", "timer"):
        kind = "task"
    remind_daily = bool(body.get("remind_daily"))
    publish = bool(body.get("publish")) and chat_id < 0 and is_primary_account(acc["id"])

    if chat_id < 0 and not thread_id:
        g_topics = (core.GROUPS.get(str(chat_id)) or {}).get("topics") or {}
        if topic_name.lower() in g_topics:
            thread_id = g_topics[topic_name.lower()]

    if publish and chat_id < 0 and thread_id:
        scope = [(cid, g) for cid, g in core.enabled_groups() if cid == chat_id]
        if scope:
            await core.execute_send(
                scope, "", topic_name, text, [], with_media=True,
                task_id=core.TASKS["next_id"] if (deadline or remind_daily) else None,
            )

    db_kind = "timer" if kind == "timer" else "task"
    task = core.add_task(chat_id, topic_name, thread_id, text, deadline, "", kind=db_kind)
    task["acc"] = acc["id"]
    core.save_json(core.TASKS_FILE, core.TASKS)

    dest = (chat_id, thread_id)
    dl_obj = core.parse_deadline(deadline)
    if kind == "timer" and dl_obj and dl_obj > core.now_local():
        core.plan_timer(task, dest, dl_obj, [30])
    elif (kind == "reminder" or remind_daily) and dl_obj:
        core.plan_task_notifications(task, dest, daily=True, before=[60])

    return {"ok": True, "task": task}

async def api_task_done(request, user):
    tid = int(request.match_info["id"])
    acc = get_request_account(request, user)
    for t in core.TASKS["items"]:
        if t["id"] == tid and not t.get("done"):
            if not core.account_owns_task(acc, t):
                return {"error": "Задача недоступна"}
            t["done"], t["done_at"] = True, time.time()
            t["paused_notifications"] = [dict(n) for n in core.SCHEDULE["items"] if n["task_id"] == tid]
            core.cancel_notifications(tid)
            core.save_json(core.TASKS_FILE, core.TASKS)
            return {"ok": True}
    return {"ok": False}

async def api_task_undone(request, user):
    tid = int(request.match_info["id"])
    acc = get_request_account(request, user)
    for t in core.TASKS["items"]:
        if t["id"] == tid and t.get("done"):
            if not core.account_owns_task(acc, t):
                return {"error": "Задача недоступна"}
            pending_ids = {n["id"] for n in core.SCHEDULE["items"]}
            restored = [n for n in t.pop("paused_notifications", []) if n["ts"] > time.time() and n["id"] not in pending_ids]
            if restored:
                core.SCHEDULE["items"].extend(restored)
                core.save_json(core.SCHEDULE_FILE, core.SCHEDULE)
            t["done"] = False
            t.pop("done_at", None)
            core.save_json(core.TASKS_FILE, core.TASKS)
            return {"ok": True}
    return {"ok": False}

async def api_history(request, user):
    acc = get_request_account(request, user)
    primary = is_primary_account(acc["id"])
    items = []
    for idx, it in enumerate(reversed(core.HISTORY["items"][-150:])):
        acc_tag = it.get("acc")
        # Если у записи нет тега аккаунта — показываем её только на главном (твоем) аккаунте
        if not acc_tag:
            if not primary:
                continue
        elif acc_tag != acc["id"]:
            continue
        items.append({**it, "idx": len(core.HISTORY["items"]) - 1 - idx})
    return {"items": items[:120]}


async def api_history_clear(request, user):
    acc = get_request_account(request, user)
    primary = is_primary_account(acc["id"])
    if primary:
        # Главный аккаунт очищает свои и старые бесхозные записи
        core.HISTORY["items"] = [
            it for it in core.HISTORY["items"]
            if it.get("acc") and it.get("acc") != acc["id"]
        ]
    else:
        # Новый аккаунт чистит строго только свои записи
        core.HISTORY["items"] = [
            it for it in core.HISTORY["items"]
            if it.get("acc") != acc["id"]
        ]
    core.save_json(core.HISTORY_FILE, core.HISTORY)
    return {"ok": True}


async def api_chat_delete(request, user):
    acc = get_request_account(request, user)
    body = await request.json()
    msg_id = str(body.get("id") or "")
    hist_ts = body.get("ts")
    chat_list = APP_CHATS.setdefault(acc["id"], [])
    if msg_id:
        chat_list[:] = [m for m in chat_list if m.get("id") != msg_id]
    if hist_ts is not None:
        try:
            target_ts = float(hist_ts)
            core.HISTORY["items"] = [
                it for it in core.HISTORY["items"]
                if not ((it.get("acc") == acc["id"] or (not it.get("acc") and is_primary_account(acc["id"])))
                        and (msg_id in it.get("message_ids", []) if "message_ids" in it
                             else float(it.get("ts") or 0) == target_ts))
            ]
            core.save_json(core.HISTORY_FILE, core.HISTORY)
        except (ValueError, TypeError):
            pass
    return {"ok": True}


async def api_chat_clear(request, user):
    acc = get_request_account(request, user)
    APP_CHATS.pop(acc["id"], None)
    return {"ok": True}


async def api_file_download(request, user):
    fid = request.match_info.get("id", "")
    f = GENERATED_FILES.get(fid)
    if not f or f.get("owner") != get_request_account(request, user)["id"] or time.time() - f["ts"] > 7200:
        return web.json_response({"error": "Файл устарел или не найден"}, status=404)
    inline = request.query.get("inline") == "1"
    disp_type = "inline" if inline else "attachment"
    disp = f"{disp_type}; filename*=UTF-8''{quote(f['name'])}"
    return web.Response(
        body=f["data"],
        content_type=f["mime"],
        headers={"Content-Disposition": disp, "Cache-Control": "no-store"},
    )


async def api_file_send_tg(request, user):
    fid = request.match_info.get("id", "")
    f = GENERATED_FILES.get(fid)
    if not f or f.get("owner") != get_request_account(request, user)["id"] or time.time() - f["ts"] > 7200:
        return {"error": "Файл устарел или не найден"}
    acc = get_request_account(request, user)
    target_uid = int(acc.get("tg_id") or user["id"])
    doc = core.BufferedInputFile(f["data"], filename=f["name"])
    await core.bot.send_document(target_uid, doc, caption=f"📄 {f['name']}")
    return {"ok": True}


async def api_export(request, user):
    body = await request.json()
    content = str(body.get("content") or "").strip()
    fmt = str(body.get("format") or "pdf").lower().strip()
    title = str(body.get("title") or "Ответ").strip()
    if not content:
        return {"error": "Текст для экспорта пуст"}
    if fmt not in ("pdf", "docx", "tex", "texpdf"):
        return {"error": f"Формат {fmt} не поддерживается (доступны: pdf, docx, tex, texpdf)"}

    data, fname, mime = await asyncio.to_thread(core.build_document, fmt, title, content)
    fid = _store_generated_file(data, fname, mime)
    from study_polish import pdf_notice
    warning = pdf_notice(data, content) if fmt == "pdf" else ""
    return {"ok": True, "id": fid, "name": fname, "size": len(data), "warning": warning}


async def _generate_with_account_pool(acc: dict, user: dict, **kwargs):
    """Вызывает core.generate_reply с личным пулом ключей и настройками аккаунта."""
    pool_to_use, is_custom = get_account_pool(acc)
    if pool_to_use is None:
        raise RuntimeError("Для работы укажите свой бесплатный API-ключ Gemini в настройках профиля (aistudio.google.com/apikey).")
    user_name = acc.get("name") or user.get("first_name") or "Пользователь"
    resp = await core.generate_reply(pool=pool_to_use, account=acc, user_name=user_name, **kwargs)
    return resp, pool_to_use, is_custom


async def api_ask(request, user):
    acc = get_request_account(request, user)
    chat_list = APP_CHATS.setdefault(acc["id"], [])

    body = await request.json()
    text = str(body.get("text", "")).strip()[:4000]
    raw_files = body.get("files") or []
    user_msg_id = str(body.get("user_msg_id") or uuid.uuid4().hex[:10])
    bot_msg_id = str(body.get("bot_msg_id") or uuid.uuid4().hex[:10])

    if not text and not raw_files:
        return {"error": "Пустой запрос"}
    if not text and raw_files:
        text = "Изучи прикреплённый файл и кратко опиши, что в нём, либо реши задания из него."

    if get_account_pool(acc)[0] is None:
        return {"error": "ИИ недоступен для этого профиля"}
    extra_parts, uploaded_names, uploaded_meta = await _decode_uploaded_files(raw_files)

    history = []
    recent_file_names = []
    for entry in chat_list[-12:]:
        for pt in entry.get("parts") or []:
            history.append({"type": "part", "part": pt})
        for fn in entry.get("file_names") or []:
            recent_file_names.append(fn)
        history.append({"type": "text", "text": entry["text"]})
    recent_file_names.extend(uploaded_names)

    can_publish = is_user_admin(user["id"])
    scope = core.enabled_groups() if can_publish else []
    resp, active_pool, _ = await _generate_with_account_pool(
        acc,
        user,
        history=history,
        user_text=text,
        extra_parts=extra_parts,
        use_tools=True,
        current_topic=None,
        last_files_names=recent_file_names[-5:],
        scope=scope,
        is_private=True,
    )

    texts = []
    calls = []
    try:
        parts = resp.candidates[0].content.parts or []
    except Exception:
        parts = []
    for p in parts:
        if getattr(p, "function_call", None):
            calls.append(p.function_call)
        elif getattr(p, "text", None):
            texts.append(p.text)

    generated_files = []
    tool_reports = []

    for call in calls:
        args = dict(call.args or {})
        if call.name == "create_file":
            fmt = core.FORMAT_ALIASES.get(str(args.get("format", "pdf") or "pdf").lower().strip(), "pdf")
            title = str(args.get("title", "") or "").strip() or "Документ"
            content = str(args.get("content", "") or "").strip()
            caption = str(args.get("caption", "") or "").strip()
            if content:
                data, filename, mime = await asyncio.to_thread(core.build_document, fmt, title, content)
                if fmt == "pdf":
                    from study_polish import pdf_notice, add_notice
                    caption = add_notice(caption, pdf_notice(data, content))
                fid = _store_generated_file(data, filename, mime)
                generated_files.append({
                    "id": fid,
                    "name": filename,
                    "size": len(data),
                    "mime": mime,
                    "caption": caption,
                })
                if caption and not texts:
                    tool_reports.append(caption)
                elif not texts:
                    tool_reports.append(f"📄 Документ **{filename}** готов!")
        elif call.name == "send_group_topic_message":
            if not can_publish:
                tool_reports.append("⛔ Публиковать в группы могут только администраторы.")
                continue
            topic = str(args.get("topic_name", ""))
            post = str(args.get("message_text", ""))
            deadline = str(args.get("deadline", "") or "")
            group_name = str(args.get("group_name", "") or "")
            want_btn = bool(deadline.strip()) or bool(args.get("remind_daily"))
            ok, status, thread_id, cid, gtitle = await core.execute_send(
                scope, group_name, topic, post, [], with_media=True,
                task_id=core.TASKS["next_id"] if want_btn else None,
            )
            rep = f"{'✅' if ok else '❌'} {status} (группа: **{gtitle or '—'}**, тема: **{topic}**)"
            if ok:
                task = core.add_task(cid, topic, thread_id, post, deadline)
                rep += f"\nЗадача **#{task['id']}** сохранена в /tasks."
            tool_reports.append(rep)
        elif call.name == "mark_tasks_done":
            ids = set()
            for value in args.get("task_ids") or []:
                if type(value) is int and value > 0:
                    ids.add(value)
            done_cnt = 0
            for t in core.TASKS["items"]:
                if not core.account_owns_task(acc, t):
                    continue
                if t["id"] in ids and not t.get("done"):
                    t["done"], t["done_at"] = True, time.time()
                    core.cancel_notifications(t["id"])
                    done_cnt += 1
            if done_cnt:
                core.save_json(core.TASKS_FILE, core.TASKS)
                tool_reports.append(f"✅ Отмечено выполненным задач: **{done_cnt}**")

    raw_answer = "\n\n".join(texts + tool_reports).strip()
    answer = de_latex(raw_answer)
    if not answer and not generated_files:
        answer = "Модель вернула пустой ответ — попробуйте переформулировать."

    used_model = getattr(active_pool, "last_used_model", None) or acc.get("preferred_model") or core.GEMINI_MODEL
    user_name = acc.get("name") or user.get("first_name") or "Артём"
    files_tag = f" [Файлы: {', '.join(uploaded_names)}]" if uploaded_names else ""

    chat_list.append({
        "id": user_msg_id,
        "role": "user",
        "text": f"{user_name}{files_tag}: {text}",
        "parts": [p for p in extra_parts if not isinstance(p, str)],
        "file_names": uploaded_names,
    })
    chat_list.append({
        "id": bot_msg_id,
        "role": "bot",
        "text": f"Ассистент: {answer[:1800]}",
        "parts": [],
        "file_names": [],
    })
    del chat_list[:-24]

    # Сохраняем запись в Историю вместе с метаданными прикреплённых и созданных файлов!
    ts = time.time()
    core.HISTORY["items"].append({
        "ts": ts,
        "src": f"приложение ({user_name})",
        "message_ids": [user_msg_id, bot_msg_id],
        "acc": acc["id"],
        "who": user_name[:40],
        "q": text[:600],
        "attached_files": uploaded_meta,
        "bot_files": generated_files,
        "a": (answer or "")[:1800],
    })
    del core.HISTORY["items"][:-core.HISTORY_LIMIT]
    core.save_json(core.HISTORY_FILE, core.HISTORY)

    return {
        "answer": answer,
        "export_text": raw_answer,
        "model": used_model,
        "files": generated_files,
        "attached_files": uploaded_meta,
        "user_msg_id": user_msg_id,
        "bot_msg_id": bot_msg_id,
        "ts": ts,
    }


async def api_settings_get(request, user):
    acc = get_request_account(request, user)
    pref_model = acc.get("preferred_model") or core.GEMINI_MODEL
    active_pool, is_custom = get_account_pool(acc)

    models_meta = getattr(core, "AVAILABLE_MODELS", [])
    return {
        "account": {
            "id": acc["id"],
            "name": acc.get("name") or "Артём",
            "avatar": acc.get("avatar") or "🧑‍💻",
            "tg_id": acc.get("tg_id") or 0,
            "has_custom_key": bool(_parse_keys(acc.get("custom_api_key", ""))),
            "masked_key": _mask_key(acc.get("custom_api_key", "")),
        },
        "is_admin": is_user_admin(int(user.get("id") or 0)),
        "using_custom_pool": is_custom,
        "accounts": accounts_summary_list(user),
        "answer_mode": acc.get("answer_mode", "detailed"),
        "media_enabled": bool(acc.get("media_enabled", True)),
        "user_profile": acc.get("user_profile", ""),
        "custom_instructions": acc.get("custom_instructions", ""),
        "user_readme": acc.get("user_readme", ""),
        "model": pref_model,
        "model_fallback": bool(acc.get("model_fallback", True)),
        "last_used_model": getattr(active_pool, "last_used_model", pref_model),
        "available_models": models_meta,
        "keys": active_pool.report().split("\n") if active_pool else ["Свой ключ ещё не подключён"],
    }


async def api_settings_set(request, user):
    acc = get_request_account(request, user)
    body = await request.json()
    if isinstance(body.get("account_name"), str) and body["account_name"].strip():
        acc["name"] = body["account_name"].strip()[:40]
    if isinstance(body.get("avatar"), str) and body["avatar"].strip():
        acc["avatar"] = body["avatar"].strip()
    if "custom_api_key" in body:
        acc["custom_api_key"] = str(body.get("custom_api_key") or "").strip()
        USER_POOLS.pop(acc["id"], None)
    if body.get("answer_mode") in ("detailed", "short"):
        acc["answer_mode"] = body["answer_mode"]
    if "media_enabled" in body:
        acc["media_enabled"] = bool(body["media_enabled"])
    if "model_fallback" in body:
        acc["model_fallback"] = bool(body["model_fallback"])
    if isinstance(body.get("model"), str) and body["model"].strip():
        chosen = body["model"].strip()
        allowed_ids = {m["id"] for m in getattr(core, "AVAILABLE_MODELS", [])}
        if not allowed_ids or chosen in allowed_ids:
            acc["preferred_model"] = chosen
    for k in ("user_profile", "custom_instructions"):
        if isinstance(body.get(k), str):
            acc[k] = body[k].strip()[:4000]
    if isinstance(body.get("user_readme"), str):
        acc["user_readme"] = body["user_readme"].strip()[:40000]

    core.save_json(core.SETTINGS_FILE, core.USER_SETTINGS)
    return await api_settings_get(request, user)


# =====================================================================
# КОМПАКТНОЕ МЕНЮ КОМАНД И КНОПОК В TELEGRAM-ЧАТЕ (ПУНКТ 2)
# =====================================================================
def compact_reply_main_menu() -> ReplyKeyboardMarkup:
    """Аккуратная клавиатура в 2 ряда вместо 6 громоздких рядов."""
    rows = [
        [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📋 Задачи"), KeyboardButton(text="⚙️ Настройки")],
    ]
    url = core.webapp_url()
    if url:
        rows.insert(0, [KeyboardButton(text="📱 Приложение", web_app=WebAppInfo(url=url))])
    else:
        rows.insert(0, [KeyboardButton(text="🧠 Карточки"), KeyboardButton(text="🎯 Квиз")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


async def compact_setup_bot_commands() -> None:
    """Оставляем в меню '/' только самые нужные команды без мусора."""
    private = [
        BotCommand(command="app", description="📱 Открыть мини-приложение"),
        BotCommand(command="today", description="📅 Расписание на сегодня"),
        BotCommand(command="tasks", description="📋 Активные задачи и дедлайны"),
        BotCommand(command="cards", description="🧠 Карточки для повторения"),
        BotCommand(command="clear", description="🧹 Очистить память диалога"),
        BotCommand(command="menu", description="🎛 Обновить кнопки меню"),
    ]
    await core.bot.set_my_commands(private, scope=BotCommandScopeAllPrivateChats())


def mount(app: web.Application) -> None:
    app._client_max_size = 25 * 1024 * 1024
    app.router.add_get("/app", index)
    for method, path, handler in core.API_ROUTES:
        app.router.add_route(method, path, wrap(handler))
    core.log.info("📱 Мини-приложение: /app (%d маршрутов API)", len(core.API_ROUTES))

from aiogram import Router

webapp_router = Router(name="webapp_router")

@webapp_router.message(Command("start"))
async def _handle_start_cmd(message):
    print(f"--> [TG] Получен /start от ID={message.from_user.id} ({message.from_user.first_name})", flush=True)
    menu = compact_reply_main_menu()
    await message.answer(
        "👋 **Привет!** Я твой помощник по учёбе.\n\n"
        "Расписание, задачи и карточки — по кнопке **«📱 Приложение»** внизу. "
        "А если нужно что-то разобрать или просто спросить — пиши мне сюда как обычному человеку.",
        reply_markup=menu,
        parse_mode="Markdown"
    )

@webapp_router.message(Command("menu"))
async def _handle_menu_cmd(message):
    print(f"--> [TG] Получен /menu от ID={message.from_user.id}", flush=True)
    menu = compact_reply_main_menu()
    await message.answer("Готово, кнопки на месте 👌", reply_markup=menu)

def setup(c) -> None:
    global core
    core = c
    core.PLUGIN_PROMPT.append(prompt_hook)
    core.timetable_account_context = account_context
    core.GUEST_CHECK = _guest_has_own_key

    @core.dp.callback_query.outer_middleware()
    async def _tg_account_cb_middleware(handler, event, data):
        u = getattr(event, "from_user", None)
        if not u or not u.id:
            return await handler(event, data)
        user = {"id": u.id, "first_name": u.first_name or ""}
        account = get_or_create_account(user)
        chat = getattr(getattr(event, "message", None), "chat", None)
        with account_context(user, account, private=getattr(chat, "type", "") == "private"):
            return await handler(event, data)

    core.get_reply_main_menu = compact_reply_main_menu
    orig_setup_cmds = core.setup_bot_commands

    async def _patched_setup_cmds():
        await orig_setup_cmds()
        await compact_setup_bot_commands()

    core.setup_bot_commands = _patched_setup_cmds

    @core.dp.message.outer_middleware()
    async def _tg_account_middleware(handler, event, data):
        u = getattr(event, "from_user", None)
        if u and getattr(u, "id", None):
            user = {"id": u.id, "first_name": u.first_name or ""}
            acc = get_or_create_account(user)
            data["account"] = acc
            user_pool, _ = get_account_pool(acc)
            data["pool"] = user_pool
            private = getattr(getattr(event, "chat", None), "type", "") == "private"

            # Если гость в личке без ключа пытается спросить ИИ (не команда)
            if user_pool is None and private:
                txt = (getattr(event, "text", None) or "").strip()
                entry = txt.split(maxsplit=1)[0].split("@")[0] if txt else ""
                tab = {"/cards": "study", "🧠 Карточки": "study", "/tasks": "tasks", "📋 Задачи": "tasks"}.get(txt) or {"/cards":"study","/tasks":"tasks"}.get(entry)
                if tab and core.webapp_url():
                    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть ежедневник",
                        web_app=WebAppInfo(url=f"{core.webapp_url()}?tab={tab}"))]])
                    await event.answer("Ваши карточки" if tab == "study" else "Ваши задачи", reply_markup=kb)
                    return
                if entry not in ("/start", "/help", "/menu", "/app", "/today", "/week", "/tomorrow", "/schedule", "/ics") and txt not in ("📅 Сегодня", "📅 Завтра", "🗓 Неделя"):
                    await event.answer("Расписание, задачи и карточки доступны в ежедневнике.", reply_markup=compact_reply_main_menu())
                    return

            with account_context(user, acc, private=private):
                return await handler(event, data)
        return await handler(event, data)

    # Ставим наш outer_middleware самым первым в очереди диспетчера
    mw_list = getattr(core.dp.message.outer_middleware, "_middlewares", None)
    if isinstance(mw_list, list) and len(mw_list) > 1:
        mw_list.insert(0, mw_list.pop())

    core.API_ROUTES.extend([
        ("GET", "/api/me", api_me),
        ("POST", "/api/accounts", api_account_save),
        ("POST", "/api/keys/verify", api_verify_key),
        ("GET", "/api/home", api_home),
        ("GET", "/api/tasks", api_tasks),
        ("POST", "/api/tasks", api_task_create),
        ("POST", "/api/tasks/{id}/done", api_task_done),
        ("POST", "/api/tasks/{id}/undone", api_task_undone),
        ("GET", "/api/history", api_history),
        ("DELETE", "/api/history", api_history_clear),
        ("POST", "/api/ask", api_ask),
        ("POST", "/api/chat/delete", api_chat_delete),
        ("POST", "/api/chat/clear", api_chat_clear),
        ("GET", "/api/files/{id}", api_file_download),
        ("POST", "/api/files/{id}/send", api_file_send_tg),
        ("POST", "/api/export", api_export),
        ("GET", "/api/settings", api_settings_get),
        ("POST", "/api/settings", api_settings_set),
    ])
    core.WEB_SETUP.append(mount)
