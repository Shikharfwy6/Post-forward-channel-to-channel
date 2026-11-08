import asyncio
import os
import html as htmllib
import json
import urllib.parse
import urllib.request
import re
import time

from pyrogram import Client, filters, idle, enums, raw

# ================= SETTINGS =================
# सिर्फ़ ये चार ENV से आते हैं
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
STRING_SESSION = os.environ["STRING_SESSION"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

# बाकी सब code में
OWNER_ID = 8838634478                # आपका user id (सिर्फ़ आप bot चला सकें)
SOURCE_CHANNEL_ID = -1003298610413   # बड़ा channel (t.me/c/3298610413 -> -100 लगाकर)

GROUP_ID = -1004385109465            # यहाँ Download/Watch link जाएगा
CHANNEL_ID = -1004435774367          # यहाँ image/video जाएगा, और इसी पर नज़र रहेगी
STICKER_ID = "CAACAgUAAxkBAAICH2qksuIcm_ZAZV8pXDtSbcWP2QuoAAK6IQAC90IBVB6iwsOwbpqjPQQ"

QUIET_SECONDS = 15   # आख़िरी video के इतने सेकंड बाद तक कुछ नया न आए तो batch पूरा

# --- Fubuki_xRobot वाला auto मोड ---
AUTO_FETCH = True            # True: link पर क्लिक की जगह userbot खुद /start भेजकर bot का reply उठाएगा
BOT_QUIET_SECONDS = 8        # bot का आख़िरी message आने के इतने सेकंड बाद reply पूरा माना जाएगा
FIRST_TIMEOUT = 60           # bot से इतने सेकंड तक कुछ न आए तो यह post छोड़ दो
BYPASS_TIMEOUT = 90         # group के bypass bot के जवाब का इंतज़ार (सेकंड)
BYPASS_BOT = "Nick_Bypass_Bot"   # bypass bot का username (बिना @)
BYPASS_VIA_DM = True         # True: पहले bot को सीधे DM में link भेजो, न चले तो group में
MAX_HOPS = 6                 # एक post पर link-चेन की ज़्यादा से ज़्यादा कड़ियाँ
# ============================================

userbot = Client(
    "userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=STRING_SESSION,
    in_memory=True,
    parse_mode=enums.ParseMode.HTML,
)

bot = Client(
    "controlbot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
)

running = {"active": False, "stop": False}
own_sent_ids = set()   # script के अपने भेजे messages (video गिनती में न आएँ)
state = {"got_video": False, "last_video": 0.0}


# ---------- helpers ----------
def clean_caption(html):
    if not html:
        return ""
    # t.me / tg:// वाले links (anchor सहित) हटाओ
    html = re.sub(r'<a\s+href="[^"]*(?:t\.me|tg://)[^"]*"[^>]*>.*?</a>', "", html, flags=re.S | re.I)
    # खुले t.me links हटाओ
    html = re.sub(r"(?:https?://)?t\.me/\S+", "", html)
    # @username हटाओ
    html = re.sub(r"@\w+", "", html)
    # ख़ाली बचे tags हटाओ
    html = re.sub(r"<(b|i|u|s|code|strong|em)>\s*</\1>", "", html)
    html = re.sub(r"[ \t]+\n", "\n", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


def get_first_button_link(msg):
    """नीचे के buttons में पहला URL (Download/Watch)"""
    kb = msg.reply_markup
    if not kb or not hasattr(kb, "inline_keyboard"):
        return None
    for row in kb.inline_keyboard:
        for btn in row:
            if getattr(btn, "url", None):
                return btn.url
    return None


async def send_media(msg, caption, dest=None):
    """media को dest (default CHANNEL_ID) में भेजो (पहले file_id से, न चले तो download करके)"""
    dest = dest or CHANNEL_ID

    async def _send(source):
        if msg.photo:
            return await userbot.send_photo(dest, source, caption=caption)
        if msg.video:
            return await userbot.send_video(dest, source, caption=caption)
        if msg.animation:
            return await userbot.send_animation(dest, source, caption=caption)
        if msg.document:
            return await userbot.send_document(dest, source, caption=caption)
        return await userbot.send_message(dest, caption or ".")

    if not msg.media:
        m = await userbot.send_message(dest, caption or ".")
        own_sent_ids.add(m.id)
        return

    media_obj = msg.photo or msg.video or msg.animation or msg.document
    try:
        m = await _send(media_obj.file_id)
    except Exception as e:
        print("file_id से भेजना fail, download करके भेज रहा हूँ:", e)
        path = await userbot.download_media(msg)
        try:
            m = await _send(path)
        finally:
            if path and os.path.exists(path):
                os.remove(path)
    own_sent_ids.add(m.id)


async def send_sticker():
    try:
        m = await userbot.send_sticker(CHANNEL_ID, STICKER_ID)
        own_sent_ids.add(m.id)
    except Exception as e:
        # Bot API वाला file_id userbot पर न चले तो Saved Messages वाला sticker लो
        print("Sticker file_id fail:", e)
        async for sm in userbot.get_chat_history("me", limit=50):
            if sm.sticker:
                m = await userbot.send_sticker(CHANNEL_ID, sm.sticker.file_id)
                own_sent_ids.add(m.id)
                return
        print("Saved Messages में sticker नहीं मिला। पहले एक sticker वहाँ भेज दो।")


async def wait_for_videos():
    """videos आने का इंतज़ार; आख़िरी video के बाद QUIET_SECONDS शांति हो तो आगे बढ़ो"""
    state["got_video"] = False
    state["last_video"] = 0.0
    while True:
        await asyncio.sleep(2)
        if running["stop"]:
            return
        if state["got_video"] and time.time() - state["last_video"] >= QUIET_SECONDS:
            return


# ---------- Fubuki_xRobot से videos उठाना ----------
collect = {"bot_id": None, "msgs": [], "last": 0.0}


@userbot.on_message(filters.private & filters.incoming)
async def from_file_bot(_, m):
    if collect["bot_id"] and m.from_user and m.from_user.id == collect["bot_id"]:
        collect["msgs"].append(m)
        collect["last"] = time.time()


URL_RE = re.compile(r"https?://[^\s\"<>']+")
DEEP_RE = re.compile(r"https?://t\.me/([A-Za-z0-9_]+)\?start=([\w-]+)")


def parse_deep(url):
    m = DEEP_RE.match(url or "")
    return (m.group(1), m.group(2)) if m else None


def message_urls(m):
    """message के text, hyperlink (entities) और buttons से सारे URLs (क्रम में)"""
    urls = []
    txt = m.text or m.caption or ""
    ents = (m.entities if m.text else m.caption_entities) or []
    urls += [u.rstrip(".,;)") for u in URL_RE.findall(str(txt))]
    for e in ents:
        if getattr(e, "url", None):
            urls.append(e.url)
        elif getattr(e.type, "name", str(e.type)) == "URL":
            urls.append(txt[e.offset:e.offset + e.length])
    kb = m.reply_markup
    if kb and hasattr(kb, "inline_keyboard"):
        for row in kb.inline_keyboard:
            for btn in row:
                if getattr(btn, "url", None):
                    urls.append(btn.url)
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def pick_next_link(urls):
    """bot-link (?start=) या बाहरी link चुनो; t.me के channel/join links छोड़ दो"""
    for u in urls:
        if parse_deep(u):
            return u
        if "t.me/" not in u and "telegram.me/" not in u:
            return u
    return None


async def refresh(m):
    """bot ने message edit किया हो तो ताज़ा रूप लो"""
    try:
        fresh = await userbot.get_messages(m.chat.id, m.id)
        if fresh and not fresh.empty:
            return fresh
    except Exception:
        pass
    return m


group_inbox = {}   # group में आए नए (दूसरों के) messages


@userbot.on_message(filters.chat(GROUP_ID) & filters.incoming)
@userbot.on_edited_message(filters.chat(GROUP_ID) & filters.incoming)
async def group_watch(_, m):
    group_inbox[m.id] = m
    if len(group_inbox) > 100:
        for k in sorted(group_inbox)[:-100]:
            group_inbox.pop(k, None)


# ---------- Bot API से पढ़ना (जब userbot को message "unsupported" दिखे) ----------
# Nick Bypass Bot का reply नए तरह का message है जिसे पुराना Pyrogram पढ़ नहीं पाता
# (MessageMediaUnsupported). Bot API नया है, वह पूरा पढ़ लेता है। इसलिए हम उस message
# को अपने control bot को forward करते हैं और Bot API (getUpdates) से उसका पूरा रूप पढ़ते हैं।
botinfo = {"username": None}
botapi_updates = []   # [(समय, update dict)]
ANY_DEEP_RE = re.compile(r"https?://t\.me/([A-Za-z0-9_]+)\?start=([\w-]+)")


def _botapi_call(method, params, timeout=35):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


async def botapi_poller():
    offset = None
    while True:
        try:
            params = {"timeout": 20, "allowed_updates": json.dumps(["message"])}
            if offset:
                params["offset"] = offset
            res = await asyncio.to_thread(_botapi_call, "getUpdates", params)
            for u in res.get("result", []):
                offset = u["update_id"] + 1
                botapi_updates.append((time.time(), u))
            del botapi_updates[:-50]
        except Exception as e:
            print("[botapi] poll error:", type(e).__name__)
            await asyncio.sleep(5)


def find_deep_in_json(obj):
    """पूरे JSON में (text, entities, buttons, कहीं भी) पहला t.me ?start= link"""
    blob = json.dumps(obj, ensure_ascii=False)
    for m in ANY_DEEP_RE.finditer(blob):
        if botinfo["username"] and m.group(1).lower() == botinfo["username"].lower():
            continue
        return m.group(0)
    return None


async def link_via_botapi(chat, mid, errors):
    """message को control bot को forward करो, Bot API से पढ़कर link निकालो"""
    uname = botinfo["username"]
    if not uname:
        errors.append("control bot का username नहीं मिला")
        return None
    t0 = time.time()
    try:
        await userbot.forward_messages(uname, chat, mid)
    except Exception as e:
        errors.append("forward: " + repr(e)[:100])
        return None

    info = None
    deadline = time.time() + 25
    while time.time() < deadline:
        await asyncio.sleep(2)
        for ts, u in list(botapi_updates):
            if ts < t0:
                continue
            link = find_deep_in_json(u)
            if link:
                return link
            msg = u.get("message", {})
            info = "BotAPI keys=" + ",".join(list(msg.keys())[:10])
            txt = msg.get("text") or msg.get("caption") or ""
            if txt:
                info += f" text='{txt[:40]}'"
    errors.append(info or "Bot API से कोई update नहीं आया")
    return None


async def raw_group_messages(sent_id, chat_id=None):
    """Pyrogram के parse से बचकर: group के नए messages सीधे Telegram से (raw), ताज़ा रूप में"""
    peer = await userbot.resolve_peer(chat_id or GROUP_ID)
    res = await userbot.invoke(
        raw.functions.messages.GetHistory(
            peer=peer, offset_id=0, offset_date=0, add_offset=0,
            limit=30, max_id=0, min_id=sent_id, hash=0,
        )
    )
    out = []
    for m in res.messages:
        if not hasattr(m, "message"):          # service / empty message
            continue
        if getattr(m, "out", False):           # हमारे अपने messages
            continue
        text = m.message or ""
        urls = [u.rstrip(".,;)") for u in URL_RE.findall(text)]
        for e in (getattr(m, "entities", None) or []):
            u = getattr(e, "url", None)
            if u:
                urls.append(u)
        mk = getattr(m, "reply_markup", None)
        for row in (getattr(mk, "rows", None) or []):
            for btn in row.buttons:
                u = getattr(btn, "url", None)
                if u:
                    urls.append(u)
        rt = getattr(m, "reply_to", None)
        media = type(m.media).__name__ if getattr(m, "media", None) else "-"
        out.append({
            "id": m.id,
            "reply_to": getattr(rt, "reply_to_msg_id", None),
            "urls": urls,
            "unsupported": media == "MessageMediaUnsupported",
            "summary": f"#{m.id} media={media} len={len(text)} '{text[:40]}'".replace("\n", " "),
        })
    return out


async def wait_bypass_link(sent_id, chat_id=None):
    """
    group में हमारे भेजे link के बाद आए bypass bot के reply से t.me ?start= link निकालो।
    लौटाता है: (link या None, diagnostic list)
    """
    chat = chat_id or GROUP_ID
    errors = []
    tried_fw = set()
    t0 = time.time()
    cands = {}
    last_raw = []
    while time.time() - t0 < BYPASS_TIMEOUT:
        await asyncio.sleep(3)
        if running["stop"]:
            return None, []

        # --- रास्ता 1: raw (सबसे भरोसेमंद) ---
        try:
            last_raw = await raw_group_messages(sent_id, chat)
        except Exception as e:
            print("[bypass] raw error:", repr(e))
            errors.append("raw: " + repr(e)[:100])
            last_raw = []
        r_replies, r_others = [], []
        for r in last_raw:
            link = next((u for u in r["urls"] if parse_deep(u)), None)
            if link:
                (r_replies if r["reply_to"] == sent_id else r_others).append(link)
        if r_replies:
            return r_replies[0], []
        if r_others:
            return r_others[0], []

        # --- रास्ता 1b: message "unsupported" है -> control bot को forward करके Bot API से पढ़ो ---
        for r in last_raw:
            if r["unsupported"] and r["id"] not in tried_fw:
                tried_fw.add(r["id"])
                link = await link_via_botapi(chat, r["id"], errors)
                if link:
                    return link, []

        # --- रास्ता 2: Pyrogram से (live handler + history + ताज़ा fetch) ---
        ids = {i for i in group_inbox if i > sent_id} if chat == GROUP_ID else set()
        try:
            async for m in userbot.get_chat_history(chat, limit=30):
                if m.id <= sent_id:
                    break
                ids.add(m.id)
        except Exception as e:
            print("[bypass] history error:", repr(e))
            errors.append("history: " + repr(e)[:100])

        cands = {i: group_inbox[i] for i in ids if i in group_inbox} if chat == GROUP_ID else {}
        if ids:
            try:
                fresh = await userbot.get_messages(chat, sorted(ids))
                if not isinstance(fresh, list):
                    fresh = [fresh]
                for fm in fresh:
                    if fm and not fm.empty and not fm.outgoing:
                        cands[fm.id] = fm
            except Exception as e:
                print("[bypass] get_messages error:", repr(e))
                errors.append("get_messages: " + repr(e)[:100])

        replies, others = [], []
        for i in sorted(cands, reverse=True):
            m = cands[i]
            try:
                link = next((u for u in message_urls(m) if parse_deep(u)), None)
            except Exception as e:
                print("[bypass] parse error:", repr(e))
                continue
            if link:
                (replies if m.reply_to_message_id == sent_id else others).append(link)
        if replies:
            return replies[0], []
        if others:
            return others[0], []

    # --- नहीं मिला: क्या दिखा, वह बताओ ---
    diag = [r["summary"] for r in last_raw[:4]]
    if not diag:
        for i in sorted(cands)[:3]:
            m = cands[i]
            who = (m.from_user.first_name if m.from_user else "?")
            snippet = str(m.text or m.caption or f"[media:{m.media}]")[:60].replace("\n", " ")
            diag.append(f"{who}: {snippet}")
    diag += [f"ERR {e}" for e in errors[-2:]]
    print(f"[bypass] timeout, raw={len(last_raw)} pyro={len(cands)}")
    return None, diag


async def resolve_chain(start_link, notify, pid):
    """
    link -> (bot को /start) -> reply में नया link -> (बाहरी link हो तो group का bypass bot)
    -> फिर bot... जब तक दूसरे bot का link न मिले। वही आख़िरी link है।
    """
    current = start_link
    last_bot = None

    for hop in range(MAX_HOPS):
        if running["stop"]:
            return None
        deep = parse_deep(current)

        if deep:
            bot_user, payload = deep
            if last_bot and bot_user.lower() != last_bot.lower():
                return current                      # दूसरे bot का link = आख़िरी link
            await notify(f"Post {pid}: @{bot_user} को /start भेज रहा हूँ…")
            msgs = await fetch_from_bot(bot_user, payload)
            if not msgs:
                await notify(f"Post {pid}: @{bot_user} का कोई reply नहीं आया")
                return None
            last_bot = bot_user
            msgs = [await refresh(x) for x in msgs]
            urls = [u for x in msgs for u in message_urls(x)]
            nxt = pick_next_link(urls)
            if not nxt:
                for x in msgs:
                    await forward_to_group(x)
                await notify(f"Post {pid}: reply में link नहीं मिला, message group में forward किया")
                return None
            current = nxt
        else:
            # बाहरी (shortener) link: bypass bot से bypass कराओ (पहले DM, फिर group)
            bypassed, diag = None, []
            routes = ["dm", "group"] if BYPASS_VIA_DM else ["group"]
            for route in routes:
                if route == "dm":
                    bp_user = await userbot.get_users(BYPASS_BOT)
                    sent = await userbot.send_message(bp_user.id, current)
                    await notify(f"Post {pid}: link @{BYPASS_BOT} को DM में भेजा, इंतज़ार…")
                    bypassed, diag = await wait_bypass_link(sent.id, bp_user.id)
                else:
                    sent = await userbot.send_message(GROUP_ID, current)
                    await notify(f"Post {pid}: link group में भेजा, bypass का इंतज़ार…")
                    bypassed, diag = await wait_bypass_link(sent.id)
                if bypassed:
                    break
                extra = ("\nदेखा गया:\n" + "\n".join(diag)) if diag else "\n(कुछ नया नहीं दिखा)"
                await notify(f"Post {pid}: {route} रास्ते से link नहीं मिला{extra}")
            if not bypassed:
                return None
            current = bypassed

    await notify(f"Post {pid}: बहुत ज़्यादा कड़ियाँ हो गईं, रुक गया")
    return None


async def forward_to_group(m):
    """bot का message group में forward करो; न हो पाए तो text की copy भेजो"""
    try:
        await userbot.forward_messages(GROUP_ID, m.chat.id, m.id)
    except Exception as e:
        print("forward fail, text भेज रहा हूँ:", e)
        txt = m.text.html if m.text else (m.caption.html if m.caption else "")
        if txt:
            await userbot.send_message(GROUP_ID, txt, disable_web_page_preview=True)


async def fetch_from_bot(username, payload):
    """deep link पर क्लिक करने जैसा: bot को /start <payload> भेजो और उसके media जमा करो"""
    user = await userbot.get_users(username)
    collect["bot_id"] = user.id
    collect["msgs"] = []
    collect["last"] = 0.0

    await userbot.send_message(username, f"/start {payload}")

    t0 = time.time()
    while True:
        await asyncio.sleep(2)
        if running["stop"]:
            break
        if collect["msgs"]:
            if time.time() - collect["last"] >= BOT_QUIET_SECONDS:
                break
        elif time.time() - t0 > FIRST_TIMEOUT:
            break

    msgs = sorted(collect["msgs"], key=lambda x: x.id)
    collect["bot_id"] = None
    return msgs


# ---------- channel पर नज़र ----------
@userbot.on_message(filters.chat(CHANNEL_ID))
async def watch_channel(_, m):
    if m.id in own_sent_ids:
        return
    is_video = bool(m.video) or bool(
        m.document and m.document.mime_type and m.document.mime_type.startswith("video")
    )
    if is_video:
        state["got_video"] = True
        state["last_video"] = time.time()


# ---------- मुख्य काम ----------
async def process_range(start_id, end_id, notify):
    await notify(f"शुरू: post {start_id} से {end_id} तक")

    for pid in range(start_id, end_id + 1):
        if running["stop"]:
            await notify("रोक दिया गया ⛔")
            return
        try:
            msg = await userbot.get_messages(SOURCE_CHANNEL_ID, pid)
            if not msg or msg.empty or msg.service:
                continue

            link = get_first_button_link(msg)
            raw = msg.caption.html if msg.caption else (msg.text.html if msg.text else "")
            caption = clean_caption(raw)

            # 1) image/video (बिना username) -> channel
            await send_media(msg, caption)

            # 2) Download/Watch link
            if not link:
                await notify(f"Post {pid} में button link नहीं मिला")
                continue

            deep = re.search(r"t\.me/([A-Za-z0-9_]+)\?start=([\w-]+)", link)

            if AUTO_FETCH:
                # link-चेन चलाओ: bot -> link -> bypass -> bot -> ... -> आख़िरी link
                final_link = await resolve_chain(link, notify, pid)
                if not final_link:
                    continue
                await userbot.send_message(GROUP_ID, final_link)
                await notify(f"Post {pid}: आख़िरी link group में भेजा। अब videos channel में forward करो…")
                await wait_for_videos()
            else:
                # पुराना तरीका: link group में भेजो, videos आप forward करोगे
                await userbot.send_message(GROUP_ID, link)
                await notify(f"Post {pid} भेजा। अब videos channel में forward करो…")
                await wait_for_videos()

            # 4) sticker भेजो, फिर अगला post
            await send_sticker()
        except Exception as e:
            print(f"Post {pid} में error:", e)
            await notify(f"Post {pid} error: {e}")

    await notify("सारे posts पूरे हो गए ✅")


# ---------- control bot ----------
@bot.on_message(filters.private & filters.user(OWNER_ID) & filters.command("start"))
async def bot_start(_, message):
    await message.reply_text(
        "start id aur end id bhejo.\nExample: 9 16\nया दो links भेजो।\n/stop से रोको।"
    )


@bot.on_message(filters.private & filters.user(OWNER_ID) & filters.command("stop"))
async def bot_stop(_, message):
    running["stop"] = True
    await message.reply_text("रुक रहा हूँ…")


@bot.on_message(
    filters.private & filters.user(OWNER_ID) & filters.text & ~filters.command(["start", "stop"])
)
async def bot_range(_, message):
    text = message.text.strip()

    # links हों तो उनके आख़िरी नंबर लो, वरना सीधे दो नंबर
    links = re.findall(r"t\.me/c/\d+/(\d+)", text)
    nums = [int(x) for x in links] if len(links) >= 2 else [int(x) for x in re.findall(r"\d+", text)]
    if len(nums) < 2:
        await message.reply_text("दो नंबर भेजो: start id और end id. Example: 9 16")
        return
    if running["active"]:
        await message.reply_text("पहले वाला काम चल रहा है। /stop से रोको।")
        return

    start_id, end_id = sorted(nums[:2])
    running["active"] = True
    running["stop"] = False

    async def notify(t):
        await message.reply_text(t)

    async def job():
        try:
            await process_range(start_id, end_id, notify)
        finally:
            running["active"] = False

    asyncio.create_task(job())


# ---------- चालू करो ----------
async def main():
    await userbot.start()
    await bot.start()
    botinfo["username"] = (await bot.get_me()).username
    asyncio.create_task(botapi_poller())

    # सारे chats/channels की जानकारी cache करो, वरना "Peer id invalid" आ सकता है
    async for _ in userbot.get_dialogs():
        pass

    print("Userbot + Bot चालू हैं। Bot को start id aur end id भेजो।")
    await idle()
    await bot.stop()
    await userbot.stop()


if __name__ == "__main__":
    userbot.run(main())
