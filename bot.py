import asyncio
import os
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
OWNER_ID = 8838634478               # आपका user id (सिर्फ़ आप bot चला सकें)
SOURCE_CHANNEL_ID = -1003298610413   # बड़ा channel (t.me/c/3298610413 -> -100 लगाकर)

GROUP_ID = -1004385109465            # यहाँ Download/Watch link जाएगा
CHANNEL_ID = -1004435774367          # यहाँ image/video जाएगा, और इसी पर नज़र रहेगी
STICKER_ID = "CAACAgUAAxkBAAICH2qksuIcm_ZAZV8pXDtSbcWP2QuoAAK6IQAC90IBVB6iwsOwbpqjPQQ"

QUIET_SECONDS = 3   # आख़िरी video के इतने सेकंड बाद तक कुछ नया न आए तो batch पूरा
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


async def send_media(msg, caption):
    """media को CHANNEL_ID में भेजो (पहले file_id से, न चले तो download करके)"""

    async def _send(source):
        if msg.photo:
            return await userbot.send_photo(CHANNEL_ID, source, caption=caption)
        if msg.video:
            return await userbot.send_video(CHANNEL_ID, source, caption=caption)
        if msg.animation:
            return await userbot.send_animation(CHANNEL_ID, source, caption=caption)
        if msg.document:
            return await userbot.send_document(CHANNEL_ID, source, caption=caption)
        return await userbot.send_message(CHANNEL_ID, caption or ".")

    if not msg.media:
        m = await userbot.send_message(CHANNEL_ID, caption or ".")
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

            # 2) Download/Watch link -> group
            if link:
                await userbot.send_message(GROUP_ID, link)
            else:
                await notify(f"Post {pid} में button link नहीं मिला")
                continue

            await notify(f"Post {pid} भेजा। अब videos channel में forward करो…")

            # 3) videos आने तक रुको
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
