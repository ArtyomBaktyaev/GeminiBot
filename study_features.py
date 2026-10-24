# Дополнительные учебные функции. Подключаются к существующему aiogram Dispatcher.
import asyncio, json, os, re, html, uuid
from datetime import datetime, timedelta
from urllib.parse import quote
from aiogram import F, types
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile

DATA_FILE = os.getenv('STUDY_DATA_FILE', 'study_data.json')
_DATA = {'lessons': [], 'cards': {}, 'quizzes': {}, 'pending': {}}

def load_data():
    global _DATA
    try:
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            saved=json.load(f)
        if isinstance(saved, dict):
            _DATA.update(saved)
    except FileNotFoundError:
        save_data()
    except Exception:
        pass

def save_data():
    tmp=DATA_FILE+'.tmp'
    with open(tmp,'w',encoding='utf-8') as f: json.dump(_DATA,f,ensure_ascii=False,indent=2)
    os.replace(tmp,DATA_FILE)

def esc(s): return html.escape(str(s), quote=False)

DAY_MAP={'пн':0,'понедельник':0,'вт':1,'вторник':1,'ср':2,'среда':2,'чт':3,'четверг':3,'пт':4,'пятница':4,'сб':5,'суббота':5,'вс':6,'воскресенье':6}
DAY_NAMES=['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье']

def lesson_text(l):
    parity={'any':'любая','even':'чётная','odd':'нечётная'}.get(l.get('parity','any'),'любая')
    return f"• <b>{esc(l['time'])}</b> — {esc(l['subject'])}" + (f" · {esc(l['room'])}" if l.get('room') else '') + (f" · {esc(l['teacher'])}" if l.get('teacher') else '') + (f" <i>({parity} неделя)</i>" if parity!='любая' else '')

def register_study_handlers(dp, bot, is_allowed, timezone_obj):
    load_data()
    @dp.message(Command('study'))
    async def study_help(m: types.Message):
        if not is_allowed(m): return
        await m.answer('🎓 <b>Учебный раздел</b>\n\n/schedule — расписание\n/lesson Пн 09:00 Математический анализ | 302 | Иванов | любая — добавить пару\n/ics — экспорт расписания в календарь\n/quiz тема — создать учебный квиз (пока режим подготовки)\n/cards — английские карточки\n/card слово | перевод | пример — добавить карточку',parse_mode=ParseMode.HTML)

    @dp.message(Command('lesson'))
    async def add_lesson(m: types.Message):
        if not is_allowed(m): return
        raw=(m.text or '').partition(' ')[2].strip()
        match=re.match(r'^(\S+)\s+(\d{1,2}:\d{2})\s+([^|]+?)(?:\s*\|\s*([^|]*))?(?:\s*\|\s*([^|]*))?(?:\s*\|\s*(любая|чётная|четная|нечётная|нечетная))?$',raw,re.I)
        if not match or match.group(1).lower() not in DAY_MAP:
            await m.answer('Формат: <code>/lesson Пн 09:00 Математический анализ | 302 | Иванов | чётная</code>\nПоследние поля необязательны.',parse_mode=ParseMode.HTML); return
        day,time,subject,room,teacher,parity=match.groups()
        parity=(parity or 'любая').lower()
        parity='even' if parity in ('чётная','четная') else 'odd' if parity in ('нечётная','нечетная') else 'any'
        _DATA['lessons'].append({'id':uuid.uuid4().hex[:8],'day':DAY_MAP[day.lower()],'time':time,'subject':subject.strip(),'room':(room or '').strip(),'teacher':(teacher or '').strip(),'parity':parity,'owner':m.from_user.id if m.from_user else 0})
        save_data(); await m.answer('✅ Пара добавлена в основное расписание.\n'+lesson_text(_DATA['lessons'][-1]),parse_mode=ParseMode.HTML)

    @dp.message(Command('schedule'))
    async def show_schedule(m: types.Message):
        if not is_allowed(m): return
        args=(m.text or '').partition(' ')[2].strip().lower()
        today=datetime.now(timezone_obj).date()
        if args in ('сегодня','today'): days=[today.weekday()]
        elif args in ('завтра','tomorrow'): days=[(today+timedelta(days=1)).weekday()]
        elif args in DAY_MAP: days=[DAY_MAP[args]]
        else: days=list(range(7))
        out=[]
        for d in days:
            items=sorted([x for x in _DATA['lessons'] if x['day']==d],key=lambda x:x['time'])
            if items: out.append('<b>'+DAY_NAMES[d]+'</b>\n'+'\n'.join(lesson_text(x) for x in items))
        await m.answer(('📚 <b>Расписание</b>\n\n'+'\n\n'.join(out)) if out else 'Расписание пока пустое. Добавь пару командой /lesson или открой /study для формата.',parse_mode=ParseMode.HTML)

    @dp.message(Command('ics'))
    async def export_ics(m: types.Message):
        if not is_allowed(m): return
        if not _DATA['lessons']:
            await m.answer('Расписание пустое. Сначала добавь пары командой /lesson.'); return
        now=datetime.now(timezone_obj).date(); monday=now-timedelta(days=now.weekday())
        lines=['BEGIN:VCALENDAR','VERSION:2.0','PRODID:-//Academic Bot//Schedule//RU','CALSCALE:GREGORIAN']
        for l in _DATA['lessons']:
            hh,mm=map(int,l['time'].split(':')); first=monday+timedelta(days=l['day'])
            start=datetime(first.year,first.month,first.day,hh,mm)
            end=start+timedelta(minutes=90)
            def dt(d): return d.strftime('%Y%m%dT%H%M%S')
            summary=(l['subject'] or '').replace('\\','\\\\').replace(',','\\,').replace(';','\\;').replace('\n','\\n')
            desc='; '.join(x for x in [l.get('teacher',''),l.get('room',''),'Неделя: '+l.get('parity','any')] if x)
            lines += ['BEGIN:VEVENT','UID:'+l['id']+'@academic-bot','DTSTAMP:'+dt(datetime.utcnow()),'DTSTART;TZID='+getattr(timezone_obj,'key','Europe/Warsaw')+':'+dt(start),'DTEND;TZID='+getattr(timezone_obj,'key','Europe/Warsaw')+':'+dt(end),'SUMMARY:'+summary,'DESCRIPTION:'+desc,'RRULE:FREQ=WEEKLY;COUNT=16','END:VEVENT']
        lines.append('END:VCALENDAR')
        await m.answer_document(BufferedInputFile(('\r\n'.join(lines)+'\r\n').encode(),filename='schedule.ics'),caption='📅 Расписание для импорта в календарь')

    @dp.message(Command('card'))
    async def add_card(m: types.Message):
        if not is_allowed(m): return
        raw=(m.text or '').partition(' ')[2]
        parts=[p.strip() for p in raw.split('|')]
        if len(parts)<2 or not parts[0] or not parts[1]:
            await m.answer('Формат: <code>/card word | перевод | пример предложения</code>',parse_mode=ParseMode.HTML); return
        uid=str(m.from_user.id if m.from_user else m.chat.id); deck=_DATA['cards'].setdefault(uid,[])
        deck.append({'word':parts[0],'translation':parts[1],'example':parts[2] if len(parts)>2 else '', 'due':0,'interval':0,'known':0,'unknown':0})
        save_data(); await m.answer(f'✅ Карточка добавлена: <b>{esc(parts[0])}</b> — {esc(parts[1])}',parse_mode=ParseMode.HTML)

    @dp.message(Command('cards'))
    async def review_card(m: types.Message):
        if not is_allowed(m): return
        uid=str(m.from_user.id if m.from_user else m.chat.id); deck=_DATA['cards'].setdefault(uid,[])
        if not deck: await m.answer('Колода пуста. Добавь слово: /card word | перевод | пример'); return
        due=[c for c in deck if c.get('due',0)<=datetime.now().timestamp()]
        c=due[0] if due else min(deck,key=lambda x:x.get('due',0))
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='👀 Показать перевод',callback_data='study_reveal:'+uid+':'+str(deck.index(c))),InlineKeyboardButton(text='Следующее слово',callback_data='study_next:'+uid+':'+str(deck.index(c))) ]])
        await m.answer(f"🇬🇧 <b>{esc(c['word'])}</b>\n\nПопробуй вспомнить перевод.",parse_mode=ParseMode.HTML,reply_markup=kb)

    @dp.callback_query(F.data.startswith('study_reveal:'))
    async def reveal_card(q: types.CallbackQuery):
        _,uid,idx=q.data.split(':');
        if uid!=str(q.from_user.id): await q.answer('Это не твоя колода',show_alert=True); return
        try: c=_DATA['cards'][uid][int(idx)]
        except Exception: await q.answer('Карточка не найдена'); return
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='✅ Выучил',callback_data=f'study_rate:{uid}:{idx}:1'),InlineKeyboardButton(text='❌ Не знаю',callback_data=f'study_rate:{uid}:{idx}:0')]])
        await q.message.edit_text(f"🇬🇧 <b>{esc(c['word'])}</b>\n🇷🇺 {esc(c['translation'])}"+(f"\n💬 {esc(c['example'])}" if c.get('example') else ''),parse_mode=ParseMode.HTML,reply_markup=kb); await q.answer()

    @dp.callback_query(F.data.startswith('study_next:'))
    async def next_card(q: types.CallbackQuery):
        _,uid,idx=q.data.split(':')
        if uid!=str(q.from_user.id): await q.answer('Это не твоя колода',show_alert=True); return
        deck=_DATA['cards'].get(uid,[])
        if not deck: await q.answer('Колода пуста'); return
        c=deck[(int(idx)+1)%len(deck)]; i=(int(idx)+1)%len(deck)
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='👀 Показать перевод',callback_data=f'study_reveal:{uid}:{i}'),InlineKeyboardButton(text='Следующее слово',callback_data=f'study_next:{uid}:{i}')]])
        await q.message.edit_text(f"🇬🇧 <b>{esc(c['word'])}</b>\n\nПопробуй вспомнить перевод.",parse_mode=ParseMode.HTML,reply_markup=kb); await q.answer()

    @dp.callback_query(F.data.startswith('study_rate:'))
    async def rate_card(q: types.CallbackQuery):
        _,uid,idx,known=q.data.split(':')
        if uid!=str(q.from_user.id): await q.answer('Это не твоя колода',show_alert=True); return
        try: c=_DATA['cards'][uid][int(idx)]
        except Exception: await q.answer('Карточка не найдена'); return
        now=datetime.now().timestamp()
        if known=='1': c['known']=c.get('known',0)+1; c['interval']=max(1,c.get('interval',0)*2 or 1)
        else: c['unknown']=c.get('unknown',0)+1; c['interval']=0
        c['due']=now+max(600,c.get('interval',0)*86400) if known=='1' else now+600
        save_data(); await q.answer('Отлично!' if known=='1' else 'Вернёмся к слову через 10 минут')
        await q.message.edit_text(f"{ '✅ Записал: выучено' if known=='1' else '🔁 Записал: повторить позже' }\n{esc(c['word'])} — {esc(c['translation'])}",parse_mode=ParseMode.HTML)

    @dp.message(Command('quiz'))
    async def quiz_start(m: types.Message):
        if not is_allowed(m): return
        topic=(m.text or '').partition(' ')[2].strip()
        if not topic:
            await m.answer('Укажи тему квиза, например: <code>/quiz пределы последовательностей</code>. Пришли конспект/PDF в чат и попроси Gemini составить 5 вопросов по нему.',parse_mode=ParseMode.HTML); return
        # Режим подготовки совместим с существующим Gemini: не выдаём фиктивный интерактивный квиз.
        await m.answer(f'🧠 Тема квиза: <b>{esc(topic)}</b>\n\nЧтобы не показывать выдуманные вопросы, напиши: <code>Гемини, составь квиз из 5 вопросов по теме «{esc(topic)}», по одному вопросу за раз, с вариантами ответа и объяснением правильного ответа.</code> Я обработаю запрос существующим Gemini.',parse_mode=ParseMode.HTML)

