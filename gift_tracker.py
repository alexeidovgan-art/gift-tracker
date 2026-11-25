"""
Gift Market Tracker v3 — как у "tracker" с топиками и занятием лога.

Как это работает:
1. Бот следит за маркетом подарков и постит каждый новый лот в группу,
   в нужную тему (дешёвые/средние/дорогие) — по цене ОТНОСИТЕЛЬНО ФЛОРА,
   а не по абсолютной цене.
2. В посте всё, кроме подарка и владельца, видно сразу. Есть кнопка
   «Занять лог».
3. Первый, кто нажал «Занять лог» (и до этого писал боту /start),
   получает в личку от бота полную карточку: подарок, владелец,
   кнопка «Написать». В группе кнопка превращается в «🔒 Занято (@ник)»
   и для всех остальных недоступна.

Установка (свой компьютер/VPS):
    pip install -U telethon
    pip install -U TelegramGifts   # необязательно: внешний флор (TGMrkt/Fragment/GetGems)
    python gift_tracker.py
(первый запуск спросит API_ID, API_HASH, токен бота и код входа — один раз)

Запуск на бесплатном хостинге без карты (Render.com и т.п.):
    У бесплатного тарифа Render нет ни диска, ни консоли с вводом,
    поэтому сначала запусти ОДИН РАЗ get_session.py где угодно, где
    можно печатать в консоль — проще всего в Google Colab (бесплатно,
    без карты). Он спросит код входа и выдаст строку. Эту строку и
    API_ID/API_HASH/BOT_TOKEN добавь в Render как Environment
    Variables: API_ID, API_HASH, BOT_TOKEN, TG_SESSION. Дальше при
    каждом запуске/перезапуске скрипт логинится этой строкой сам,
    без единого вопроса.

Настройка (всё делается прямо в Telegram):
    1. Создай группу, включи в её настройках "Темы" (Topics/Forum),
       добавь туда своего бота админом (управление темами + сообщениями).
    2. Создай темы: дешёвые / средние / дорогие.
    3. В группе (не важно в какой теме) напиши:  /here
    4. Зайди в тему "дешёвые" и напиши:   /settopic cheap
       В тему "средние":                  /settopic mid
       В тему "дорогие":                  /settopic expensive
    5. Напиши боту в личку:  /start   — без этого занимать логи нельзя.
    6. /auto — слежка за топ-коллекциями, либо /watch <название> на конкретные.
"""
import asyncio
import json
import logging
import os
import threading
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from itertools import count

from telethon import TelegramClient, errors, events, functions
from telethon.sessions import StringSession
from telethon.tl.custom import Button

CFG_FILE = "config.json"
POLL_EVERY = 1.5     # пауза между опросами одной коллекции, сек
LIMIT = 15           # сколько свежих лотов смотреть за опрос
TOP_N = 10           # авто-режим: сколько самых ликвидных коллекций брать
FLOOR_REFRESH = 25   # как часто обновлять флор по каждой коллекции, сек

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("tracker")

TIER_LABEL = {"cheap": "ДЕШЁВЫЕ", "mid": "СРЕДНИЕ", "expensive": "ДОРОГИЕ"}

cfg, tasks, gifts, lots, floor_cache = {}, {}, {}, {}, {}
lot_counter = count(1)
claim_lock = asyncio.Lock()
user = bot = None

# необязательная внешняя библиотека с флор-ценами (TGMrkt/Fragment/GetGems)
try:
    from TelegramGifts import TelegramGifts
    TG = TelegramGifts()
except Exception:
    TG = None
    log.info("TelegramGifts не установлен — флор беру только с маркета Telegram.")


# ───────────── конфиг ─────────────
def save_cfg():
    with open(CFG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def load_cfg():
    # На бесплатном хостинге (Render и т.п.) секреты берём из переменных
    # окружения — там нет интерактивного ввода и нет надёжного диска.
    if os.environ.get("API_ID"):
        cfg.update(
            api_id=int(os.environ["API_ID"]),
            api_hash=os.environ["API_HASH"],
            bot_token=os.environ["BOT_TOKEN"],
        )
    elif os.path.exists(CFG_FILE):
        with open(CFG_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f))
    else:
        print("Первая настройка (один раз).")
        print("API_ID/API_HASH: https://my.telegram.org -> API development tools")
        print("Токен бота: @BotFather -> /newbot")
        cfg.update(
            api_id=int(input("API_ID: ").strip()),
            api_hash=input("API_HASH: ").strip(),
            bot_token=input("BOT_TOKEN: ").strip(),
        )
    cfg.setdefault("group_id", None)
    cfg.setdefault("topics", {})       # {"cheap": topic_id, "mid": ..., "expensive": ...}
    cfg.setdefault("started", [])      # user_id, кто писал /start боту
    cfg.setdefault("watch", [])        # id коллекций вручную, пусто = авто топ-N
    cfg.setdefault("tier_cheap", 1.15)  # ≤ +15% к флору = дешёвые
    cfg.setdefault("tier_mid", 1.5)     # ≤ +50% к флору = средние, выше = дорогие
    cfg.setdefault("stars_per_ton", 63.4)  # грубый курс для сравнения TON-флора со звёздами
    save_cfg()


# ───────────── утилиты по подаркам ─────────────
def price_of(g):
    """(текст цены, цена в звёздах или None)."""
    parts, stars = [], None
    for a in getattr(g, "resell_amount", None) or []:
        if type(a).__name__ == "StarsTonAmount":
            parts.append(f"{a.amount / 1e9:g} TON")
        else:
            stars = a.amount
            parts.append(f"{a.amount} ⭐")
    if not parts and getattr(g, "resell_stars", None):
        stars = g.resell_stars
        parts.append(f"{stars} ⭐")
    return " / ".join(parts) or "?", stars


def username_of(u):
    if not u:
        return None
    if getattr(u, "username", None):
        return u.username
    for x in getattr(u, "usernames", None) or []:
        if x.active:
            return x.username
    return None


def model_name_of(g):
    for a in getattr(g, "attributes", None) or []:
        if type(a).__name__ == "StarGiftAttributeModel":
            return getattr(a, "name", None)
    return None


def message_cost_of(u):
    stars = getattr(u, "send_paid_messages_stars", None)
    return f"{stars} ⭐" if stars else "Бесплатно"


async def load_gifts():
    res = await user(functions.payments.GetStarGiftsRequest(hash=0))
    items = [g for g in getattr(res, "gifts", []) if getattr(g, "title", None)]
    gifts.clear()
    gifts.update({g.id: g for g in items})


def chosen_ids():
    if cfg["watch"]:
        return [i for i in cfg["watch"] if i in gifts]
    top = sorted(gifts.values(),
                 key=lambda g: getattr(g, "availability_resale", 0) or 0,
                 reverse=True)
    return [g.id for g in top[:TOP_N]]


def sync_tasks():
    want = set(chosen_ids())
    for gid in list(tasks):
        if gid not in want:
            tasks.pop(gid).cancel()
    for gid in want:
        if gid not in tasks:
            tasks[gid] = asyncio.create_task(watch(gifts[gid]))


# ───────────── флор ─────────────
async def floor_refresher():
    while True:
        for gid in list(tasks.keys()):
            try:
                r = await user(functions.payments.GetResaleStarGiftsRequest(
                    gift_id=gid, sort_by_price=True, offset="", limit=1))
                if r.gifts:
                    _, stars = price_of(r.gifts[0])
                    if stars:
                        floor_cache[gid] = stars
            except errors.FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
            except Exception:
                pass
            await asyncio.sleep(1)
        await asyncio.sleep(FLOOR_REFRESH)


def classify(gid, gift_title, model_name, stars):
    if stars is None:
        return "mid"
    floor = None
    if TG and model_name:
        try:
            info = TG.get_model_details(gift_title, model_name)
            if info and info.get("price_ton"):
                floor = round(info["price_ton"] * cfg["stars_per_ton"])
        except Exception:
            pass
    if floor is None:
        floor = floor_cache.get(gid)
    if not floor:
        return "mid"
    ratio = stars / floor
    if ratio <= cfg["tier_cheap"]:
        return "cheap"
    if ratio <= cfg["tier_mid"]:
        return "mid"
    return "expensive"


# ───────────── публикация лота ─────────────
def topic_id_of(message):
    r = message.reply_to
    if not r:
        return None
    return getattr(r, "reply_to_top_id", None) or getattr(r, "reply_to_msg_id", None)


async def send_to_topic(tier, text, buttons):
    gid = cfg.get("group_id")
    if not gid:
        log.warning("Группа не настроена — напиши /here в группе.")
        return None
    topic_id = (cfg.get("topics") or {}).get(tier)
    try:
        return await bot.send_message(gid, text, reply_to=topic_id, buttons=buttons,
                                       parse_mode="html", link_preview=False)
    except Exception:
        log.exception("Не удалось отправить в тему %s", tier)
        return None


async def post_lot(g, users_map, tier, price_text):
    owner = getattr(g, "owner_id", None)
    uid = getattr(owner, "user_id", None)
    u = users_map.get(uid)
    if uid and not u:
        try:
            u = await user.get_entity(uid)
        except Exception:
            u = None
    model = model_name_of(g) or "—"
    premium = "Premium" if getattr(u, "premium", False) else "Без премиума"
    msg_cost = message_cost_of(u) if u else "—"

    lot_id = next(lot_counter)
    lots[lot_id] = dict(claimed=False, claimer=None, uid=uid,
                         uname=username_of(u), title=g.title, num=g.num,
                         slug=g.slug, price=price_text, model=model)

    text = (
        f"🔥 НОВЫЙ ЛИСТИНГ [{TIER_LABEL[tier]}]\n\n"
        f"🎁 Подарок: Скрыто\n"
        f"💰 Цена: {escape(price_text)}\n"
        f"👤 Владелец: Скрыто\n"
        f"♟ Модель: {escape(model)}\n"
        f"🎚 Уровень: —\n"
        f"🎁 NFT у продавца: —\n"
        f"💬 Сообщение: {escape(msg_cost)}\n"
        f"⚡ Статус: {premium}"
    )
    btn = [[Button.inline("📣 Занять лог", data=f"occ_{lot_id}")]]
    msg = await send_to_topic(tier, text, btn)
    if not msg:
        lots.pop(lot_id, None)


async def watch(gift):
    seen, first = {}, True
    while True:
        try:
            r = await user(functions.payments.GetResaleStarGiftsRequest(
                gift_id=gift.id, offset="", limit=LIMIT))
            users_map = {u.id: u for u in getattr(r, "users", None) or []}
            for g in r.gifts:
                price_text, stars = price_of(g)
                old = seen.get(g.id)
                if old == price_text:
                    continue
                seen[g.id] = price_text
                if first:
                    continue  # первый проход — просто запоминаем текущие лоты
                tier = classify(gift.id, gift.title, model_name_of(g), stars)
                asyncio.create_task(post_lot(g, users_map, tier, price_text))
            first = False
        except asyncio.CancelledError:
            raise
        except errors.FloodWaitError as e:
            log.warning("[%s] FloodWait %ss", gift.title, e.seconds)
            await asyncio.sleep(e.seconds + 1)
        except Exception:
            log.exception("[%s] ошибка опроса", gift.title)
            await asyncio.sleep(3)
        await asyncio.sleep(POLL_EVERY)


# ───────────── занятие лога ─────────────
async def on_callback(e):
    data = e.data.decode()
    if data == "noop":
        await e.answer("Этот лог уже занят.", alert=False)
        return
    if not data.startswith("occ_"):
        return
    lot_id = int(data[4:])
    lot = lots.get(lot_id)
    if not lot:
        await e.answer("Лог устарел.", alert=True)
        return

    async with claim_lock:
        if lot["claimed"]:
            await e.answer(f"Уже занято (@{lot['claimer']})" if lot["claimer"] else "Уже занято", alert=True)
            return
        if e.sender_id not in cfg["started"]:
            await e.answer("Сначала напиши /start этому боту в личку.", alert=True)
            return
        sender = await e.get_sender()
        cname = username_of(sender) or (getattr(sender, "first_name", None) or str(e.sender_id))
        lot["claimed"], lot["claimer"] = True, cname

    try:
        await e.edit(buttons=[[Button.inline(f"🔒 Занято (@{cname})", data="noop")]])
    except Exception:
        log.exception("Не удалось отредактировать сообщение в группе")

    owner_line = f"@{lot['uname']}" if lot["uname"] else (f"id {lot['uid']}" if lot["uid"] else "скрыт")
    detail = (
        f"✅ Лог закреплён за тобой\n\n"
        f"🎁 <b>{escape(lot['title'])}</b> #{lot['num']}\n"
        f"💰 {escape(lot['price'])}\n"
        f"♟ Модель: {escape(lot['model'])}\n"
        f"👤 Владелец: {escape(owner_line)}"
    )
    buttons = []
    if lot["uname"]:
        buttons.append([Button.url("✉️ Написать владельцу", f"https://t.me/{lot['uname']}")])
    elif lot["uid"]:
        buttons.append([Button.url("✉️ Написать владельцу", f"tg://user?id={lot['uid']}")])
    buttons.append([Button.url("🎁 Открыть подарок", f"https://t.me/nft/{lot['slug']}")])

    try:
        await bot.send_message(e.sender_id, detail, buttons=buttons, parse_mode="html", link_preview=False)
        await e.answer("Готово! Детали — в личке у бота.", alert=False)
    except Exception:
        log.exception("Не смог написать claimer-у в личку")
        await e.answer("Не получилось написать тебе в личку. Проверь /start.", alert=True)


# ───────────── команды ─────────────
async def on_msg(e):
    text = (e.raw_text or "").strip()
    if not text.startswith("/"):
        return
    cmd, _, arg = text.partition(" ")
    cmd, arg = cmd.split("@")[0].lower(), arg.strip()

    if cmd == "/start" and e.is_private:
        if e.sender_id not in cfg["started"]:
            cfg["started"].append(e.sender_id)
            save_cfg()
        await e.respond("✅ Готово. Теперь можешь занимать логи — детали придут сюда же.", parse_mode=None)
        return

    if not (e.is_group or e.is_channel):
        return  # остальные команды только внутри группы

    if cmd == "/here":
        cfg["group_id"] = e.chat_id
        save_cfg()
        await e.respond("✅ Эта группа теперь получает логи. Теперь зайди в каждую тему и настрой /settopic.", parse_mode=None)
        return

    if e.chat_id != cfg.get("group_id"):
        return  # чужая группа — молчим

    if cmd == "/settopic":
        tier = arg.lower()
        if tier not in TIER_LABEL and tier not in ("cheap", "mid", "expensive"):
            await e.respond("Пример (запусти ВНУТРИ нужной темы): /settopic cheap", parse_mode=None)
            return
        cfg["topics"][tier] = topic_id_of(e.message)
        save_cfg()
        await e.respond(f"✅ Тема привязана к «{tier}».", parse_mode=None)

    elif cmd in ("/watch", "/unwatch"):
        if not arg:
            await e.respond(f"Пример: {cmd} plush", parse_mode=None)
            return
        await load_gifts()
        found = [g for g in gifts.values() if arg.lower() in g.title.lower()]
        if not found:
            await e.respond("Такой коллекции не нашёл.", parse_mode=None)
            return
        ids = {g.id for g in found}
        if cmd == "/watch":
            cfg["watch"] = list(dict.fromkeys(cfg["watch"] + [g.id for g in found]))
        else:
            cur = cfg["watch"] or chosen_ids()
            cfg["watch"] = [i for i in cur if i not in ids]
        save_cfg()
        sync_tasks()
        verb = "Добавил" if cmd == "/watch" else "Убрал"
        await e.respond(f"{verb}: {', '.join(g.title for g in found)}", parse_mode=None)

    elif cmd == "/auto":
        cfg["watch"] = []
        save_cfg()
        sync_tasks()
        await e.respond(f"Авто-режим: топ-{TOP_N} коллекций по числу лотов в ресейле.", parse_mode=None)

    elif cmd == "/tier":
        parts = arg.split()
        if len(parts) != 2:
            await e.respond("Пример: /tier 15 50  (дешёвые ≤ +15% к флору, средние ≤ +50%)", parse_mode=None)
            return
        try:
            c, m = float(parts[0]) / 100 + 1, float(parts[1]) / 100 + 1
        except ValueError:
            await e.respond("Числа в процентах, например: /tier 15 50", parse_mode=None)
            return
        cfg["tier_cheap"], cfg["tier_mid"] = c, m
        save_cfg()
        await e.respond(f"✅ Дешёвые ≤ +{parts[0]}%, средние ≤ +{parts[1]}% к флору.", parse_mode=None)

    elif cmd == "/status":
        names = ", ".join(gifts[i].title for i in chosen_ids() if i in gifts) or "—"
        mode = "ручной список" if cfg["watch"] else f"авто, топ-{TOP_N}"
        topics = cfg.get("topics") or {}
        await e.respond(
            f"Слежу ({mode}): {names}\n"
            f"Темы: дёшево={topics.get('cheap','—')} средне={topics.get('mid','—')} дорого={topics.get('expensive','—')}\n"
            f"Пороги: дёшево ≤{(cfg['tier_cheap']-1)*100:.0f}%, средне ≤{(cfg['tier_mid']-1)*100:.0f}%",
            parse_mode=None,
        )


def start_ping_server():
    """Крошечный веб-сервер только для того, чтобы Render (и UptimeRobot)
    видели 'живой' веб-сервис и не усыпляли контейнер. На обычном
    VPS/компьютере он не нужен, но и не мешает."""
    class Ping(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass  # не засорять лог пингами

    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), Ping).serve_forever()


async def main():
    global user, bot
    load_cfg()
    threading.Thread(target=start_ping_server, daemon=True).start()

    session_str = os.environ.get("TG_SESSION")
    user_session = StringSession(session_str) if session_str else "user"
    user = TelegramClient(user_session, cfg["api_id"], cfg["api_hash"])
    bot = TelegramClient("bot", cfg["api_id"], cfg["api_hash"])
    await user.start()   # локально/на VPS без TG_SESSION спросит номер и код
    await bot.start(bot_token=cfg["bot_token"])
    await load_gifts()
    bot.add_event_handler(on_msg, events.NewMessage(incoming=True))
    bot.add_event_handler(on_callback, events.CallbackQuery)
    sync_tasks()
    asyncio.create_task(floor_refresher())
    me = await bot.get_me()
    log.info("Готово! Добавь @%s в группу и напиши там /here.", me.username)
    await asyncio.gather(bot.run_until_disconnected(), user.run_until_disconnected())


if __name__ == "__main__":
    asyncio.run(main())
