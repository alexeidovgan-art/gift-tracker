"""
Gift Market Tracker v6 (flood-safe)

- Только подарки на аккаунте Telegram (есть owner_id, нет owner_address).
- Только лоты, у владельца которых ЕСТЬ юзернейм.
- Мгновенное занятие лога: блокировка и правка сообщения в группе без сетевых запросов.
- После занятия в фоне перепроверяется владелец.

Локально / VPS:       pip install -U telethon ; python gift_tracker.py
Render (без карты):   переменные API_ID, API_HASH, BOT_TOKEN, TG_SESSION
                      Build: pip install -r requirements.txt
                      Start: python gift_tracker.py

В Telegram:
    /here              — в группе: привязать группу
    /settopic cheap    — внутри темы (cheap / mid / expensive), С ПРОБЕЛОМ
    /start             — боту в личку (обязательно для занятия логов)
    /auto или /watch X — что отслеживать
    /tier 15 50        — пороги: дёшево ≤ +15% к флору, средне ≤ +50%
    /status            — проверка
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
POLL_EVERY = 3.0       # пауза между опросами одной коллекции, сек (меньше = риск flood wait)
LIMIT = 15
TOP_N = 3              # авто-режим: сколько коллекций
FLOOR_REFRESH = 120    # обновление флора, сек

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("tracker")

TIER_LABEL = {"cheap": "ДЕШЁВЫЕ", "mid": "СРЕДНИЕ", "expensive": "ДОРОГИЕ"}

cfg, tasks, gifts, lots, floor_cache = {}, {}, {}, {}, {}
lot_counter = count(1)
user = bot = None

try:
    from TelegramGifts import TelegramGifts
    TG = TelegramGifts()
except Exception:
    TG = None


# ───────────── конфиг ─────────────
def save_cfg():
    try:
        with open(CFG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        log.exception("Не удалось сохранить config.json")


def load_cfg():
    if os.environ.get("API_ID"):
        cfg.update(api_id=int(os.environ["API_ID"]),
                   api_hash=os.environ["API_HASH"],
                   bot_token=os.environ["BOT_TOKEN"])
    elif os.path.exists(CFG_FILE):
        with open(CFG_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f))
    else:
        cfg.update(api_id=int(input("API_ID: ").strip()),
                   api_hash=input("API_HASH: ").strip(),
                   bot_token=input("BOT_TOKEN: ").strip())
    cfg.setdefault("group_id", None)
    cfg.setdefault("topics", {})
    cfg.setdefault("started", [])
    cfg.setdefault("watch", [])
    cfg.setdefault("tier_cheap", 1.15)
    cfg.setdefault("tier_mid", 1.5)
    cfg.setdefault("stars_per_ton", 63.4)
    save_cfg()


# ───────────── утилиты ─────────────
def price_of(g):
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


def is_account_gift(g):
    return bool(getattr(g, "owner_id", None)) and not getattr(g, "owner_address", None)


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


# ───────────── флор и тир ─────────────
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
            await asyncio.sleep(5)
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


# ───────────── публикация ─────────────
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


async def post_lot(g, owner_id, owner_uname, gift_id, gift_title, price_text, stars):
    tier = await asyncio.to_thread(classify, gift_id, gift_title, model_name_of(g), stars)
    model = model_name_of(g) or "—"
    lot_id = next(lot_counter)

    group_text = (
        f"🔥 НОВЫЙ ЛИСТИНГ [{TIER_LABEL[tier]}]\n\n"
        f"🎁 Подарок: Скрыто\n"
        f"💰 Цена: {escape(price_text)}\n"
        f"👤 Владелец: Скрыто\n"
        f"♟ Модель: {escape(model)}\n"
        f"🎚 Уровень: —\n"
        f"🎁 NFT у продавца: —\n"
        f"⚡ Статус: —"
    )
    lots[lot_id] = dict(
        claimed=False, claimer=None, group_text=group_text, msg=None,
        title=g.title, num=g.num, slug=g.slug, price=price_text, model=model,
        owner_id=owner_id, owner_uname=owner_uname,
    )
    btn = [[Button.inline("📣 Занять лог", data=f"occ_{lot_id}")]]
    msg = await send_to_topic(tier, group_text, btn)
    if msg:
        lots[lot_id]["msg"] = msg
    else:
        lots.pop(lot_id, None)


async def watch(gift):
    seen, first = {}, True
    while True:
        try:
            r = await user(functions.payments.GetResaleStarGiftsRequest(
                gift_id=gift.id, offset="", limit=LIMIT))
            users_map = {u.id: u for u in getattr(r, "users", None) or []}
            for g in r.gifts:
                if not is_account_gift(g):
                    continue
                price_text, stars = price_of(g)
                if seen.get(g.id) == price_text:
                    continue
                seen[g.id] = price_text
                if first:
                    continue
                owner_id = g.owner_id.user_id
                owner_uname = username_of(users_map.get(owner_id))
                if not owner_uname:
                    continue
                asyncio.create_task(post_lot(g, owner_id, owner_uname, gift.id,
                                             gift.title, price_text, stars))
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
def build_detail(lot):
    text = (
        f"✅ Лог закреплён за тобой\n\n"
        f"🎁 <b>{escape(lot['title'])}</b> #{lot['num']}\n"
        f"💰 {escape(lot['price'])}\n"
        f"♟ Модель: {escape(lot['model'])}\n"
        f"👤 Владелец: @{escape(lot['owner_uname'])}"
    )
    buttons = [
        [Button.url("👤 Посмотреть владельца", f"https://t.me/{lot['owner_uname']}")],
        [Button.url("🎁 Открыть ссылку на подарок", f"https://t.me/nft/{lot['slug']}")],
    ]
    return text, buttons


async def verify_owner(lot, claimer_id):
    await asyncio.sleep(3)
    try:
        res = await user(functions.payments.GetUniqueStarGiftRequest(slug=lot["slug"]))
        gift = getattr(res, "gift", None)
        if not gift or not is_account_gift(gift):
            warn = "⚠️ Подарок уже не на аккаунте (перевели в блокчейн или продали). Не пиши владельцу."
        elif gift.owner_id.user_id != lot["owner_id"]:
            warn = "⚠️ Владелец этого подарка уже сменился. Не пиши по старой ссылке."
        else:
            return
        await bot.send_message(claimer_id, warn, parse_mode=None)
    except Exception:
        log.exception("Не удалось перепроверить владельца")


async def on_callback(e):
    data = e.data.decode()
    if data == "noop":
        await e.answer("Этот лог уже занят.", alert=False)
        return
    if not data.startswith("occ_"):
        return
    lot = lots.get(int(data[4:]))
    if not lot:
        await e.answer("Лог устарел.", alert=True)
        return
    if e.sender_id not in cfg["started"]:
        await e.answer("Сначала напиши /start этому боту в личку.", alert=True)
        return
    if lot["claimed"]:
        who = lot["claimer"]
        await e.answer(f"Уже занято (@{who})" if who else "Уже занято", alert=True)
        return

    lot["claimed"] = True
    sender = e.sender
    cname = username_of(sender) or getattr(sender, "first_name", None) or str(e.sender_id)
    lot["claimer"] = cname

    try:
        await e.edit(lot["group_text"], parse_mode="html",
                     buttons=[[Button.inline(f"🔒 Занято (@{cname})", data="noop")]])
    except Exception:
        log.exception("Не удалось обновить сообщение в группе")

    await e.answer("Лог твой! Детали в личке у бота.")

    text, buttons = build_detail(lot)
    try:
        await bot.send_message(e.sender_id, text, buttons=buttons,
                               parse_mode="html", link_preview=False)
    except Exception:
        log.exception("Не смог написать в личку")
        await bot.send_message(
            e.sender_id,
            "⚠️ Не получилось отправить детали лога. Напиши боту /start и нажми «Занять» ещё раз.",
            parse_mode=None,
        )
        return

    asyncio.create_task(verify_owner(lot, e.sender_id))


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
        await e.respond("✅ Готово. Теперь можешь занимать логи — детали придут сюда.", parse_mode=None)
        return

    if not (e.is_group or e.is_channel):
        return

    if cmd == "/here":
        cfg["group_id"] = e.chat_id
        save_cfg()
        await e.respond("✅ Группа привязана. Теперь в каждой теме напиши /settopic cheap (или mid / expensive).", parse_mode=None)
        return

    if e.chat_id != cfg.get("group_id"):
        return

    if cmd == "/settopic":
        tier = arg.lower()
        if tier not in ("cheap", "mid", "expensive"):
            await e.respond("Пример (внутри нужной темы): /settopic cheap", parse_mode=None)
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
        await e.respond(f"Авто-режим: топ-{TOP_N} коллекций.", parse_mode=None)

    elif cmd == "/tier":
        parts = arg.split()
        if len(parts) != 2:
            await e.respond("Пример: /tier 15 50", parse_mode=None)
            return
        try:
            cfg["tier_cheap"] = float(parts[0]) / 100 + 1
            cfg["tier_mid"] = float(parts[1]) / 100 + 1
        except ValueError:
            await e.respond("Числа в процентах, например: /tier 15 50", parse_mode=None)
            return
        save_cfg()
        await e.respond(f"✅ Дешёвые ≤ +{parts[0]}%, средние ≤ +{parts[1]}%.", parse_mode=None)

    elif cmd == "/status":
        names = ", ".join(gifts[i].title for i in chosen_ids() if i in gifts) or "—"
        mode = "ручной список" if cfg["watch"] else f"авто, топ-{TOP_N}"
        t = cfg.get("topics") or {}
        await e.respond(
            f"Слежу ({mode}): {names}\n"
            f"Темы: дёшево={t.get('cheap', '—')} средне={t.get('mid', '—')} дорого={t.get('expensive', '—')}",
            parse_mode=None,
        )


# ───────────── запуск ─────────────
def start_ping_server():
    class Ping(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 10000))), Ping).serve_forever()


async def main():
    global user, bot
    load_cfg()
    threading.Thread(target=start_ping_server, daemon=True).start()

    session_str = os.environ.get("TG_SESSION")
    user = TelegramClient(StringSession(session_str) if session_str else "user",
                          cfg["api_id"], cfg["api_hash"])
    bot = TelegramClient("bot", cfg["api_id"], cfg["api_hash"])
    await user.start()
    await bot.start(bot_token=cfg["bot_token"])
    await load_gifts()
    bot.add_event_handler(on_msg, events.NewMessage(incoming=True))
    bot.add_event_handler(on_callback, events.CallbackQuery)
    sync_tasks()
    asyncio.create_task(floor_refresher())
    me = await bot.get_me()
    log.info("Готово! @%s, напиши в группе /here.", me.username)
    await asyncio.gather(bot.run_until_disconnected(), user.run_until_disconnected())


if __name__ == "__main__":
    asyncio.run(main())
