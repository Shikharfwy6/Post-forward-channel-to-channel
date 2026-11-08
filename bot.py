import asyncio
import os
import html as htmllib
import re
import time

from pyrogram import Client, filters, idle, enums

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
BYPASS_TIMEOUT = 120         # group के bypass bot के जवाब का इंतज़ार (सेकंड)
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
    """message के text, hyperlink और buttons से सारे URLs (क्रम में)"""
    raw = ""
    if m.text:
        raw = m.text.html
    elif m.caption:
        raw = m.caption.html
    urls = [u.rstrip(".,;)") for u in URL_RE.findall(htmllib.unescape(raw))]
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


async def wait_bypass_link(sent_id):
    """group में हमारे भेजे link के बाद आए bypass bot के reply से t.me ?start= link निकालो"""
    t0 = time.time()
    while time.time() - t0 < BYPASS_TIMEOUT:
        await asyncio.sleep(3)
        if running["stop"]:
            return None
        replies, others = [], []
        async for m in userbot.get_chat_history(GROUP_ID, limit=15):
            if m.id <= sent_id:
                break
            if m.outgoing:
                continue
            link = next((u for u in message_urls(m) if parse_deep(u)), None)
            if link:
                (replies if m.reply_to_message_id == sent_id else others).append(link)
        if replies:
            return replies[0]
        if others:
            return others[0]
    return None


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
            # बाहरी (shortener) link: group के bypass bot से bypass कराओ
            sent = await userbot.send_message(GROUP_ID, current)
            await notify(f"Post {pid}: link group में भेजा, bypass का इंतज़ार…")
            bypassed = await wait_bypass_link(sent.id)
            if not bypassed:
                await notify(f"Post {pid}: bypass bot से link नहीं मिला")
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

    # सारे chats/channels की जानकारी cache करो, वरना "Peer id invalid" आ सकता है
    async for _ in userbot.get_dialogs():
        pass

    print("Userbot + Bot चालू हैं। Bot को start id aur end id भेजो।")
    await idle()
    await bot.stop()
    await userbot.stop()


if __name__ == "__main__":
    userbot.run(main())
