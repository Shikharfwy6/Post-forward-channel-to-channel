import asyncio
import os
import html as htmllib
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
OWNER_ID = 8838634478                 # आपका user id (सिर्फ़ आप bot चला सकें)
SOURCE_CHANNEL_ID = -1003298610413   # बड़ा channel (t.me/c/3298610413 -> -100 लगाकर)

GROUP_ID = -1004385109465            # यहाँ Download/Watch link जाएगा
CHANNEL_ID = -1004435774367          # यहाँ image/video जाएगा, और इसी पर नज़र रहेगी
STICKER_ID = "CAACAgUAAxkBAAICH2qksuIcm_ZAZV8pXDtSbcWP2QuoAAK6IQAC90IBVB6iwsOwbpqjPQQ"

QUIET_SECONDS = 15   # आख़िरी video के इतने सेकंड बाद तक कुछ नया न आए तो batch पूरा

# --- Fubuki_xRobot वाला auto मोड ---
AUTO_FETCH = True            # True: link पर क्लिक की जगह userbot खुद /start भेजकर bot का reply उठाएगा
BOT_QUIET_SECONDS = 8        # bot का आख़िरी message आने के इतने सेकंड बाद reply पूरा माना जाएगा
FIRST_TIMEOUT = 60           # bot से इतने सेकंड तक कुछ न आए तो यह post छोड़ दो
BYPASS_TIMEOUT = 30         # group के bypass bot के जवाब का इंतज़ार (सेकंड)
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
own_sent_ids = set()        # channel में script के अपने भेजे messages
own_group_msg_ids = set()   # group में हमारे userbot या normal bot के भेजे messages
bot_info = {"id": None}     # normal bot का user id
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


URL_RE = re.compile(
    r"(?:https?://[^\s\"<>'\)\]]+|(?:t\.me|telegram\.me|telegram\.dog)/[^\s\"<>'\)\]]+)",
    re.I,
)
DEEP_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/([A-Za-z0-9_]+)\?start=([A-Za-z0-9_=-]+)",
    re.I,
)


def clean_url(u):
    u = (u or "").strip().rstrip(".,;)*_`\"'>]")
    if u.lower().startswith(("t.me/", "telegram.me/", "telegram.dog/")):
        u = "https://" + u
    return u


def parse_deep(url):
    m = DEEP_RE.search(url or "")
    return (m.group(1), m.group(2)) if m else None


def copy_full_text_from_msg(m):
    """
    Bypass bot के message से सारा text कॉपी करो।
    अगर कोई link hyperlink या inline button के अंदर छुपा है, तो उसे भी text में शामिल करो।
    """
    base_text = m.text or m.caption or ""
    extra_urls = []

    ents = (m.entities if m.text else m.caption_entities) or []
    for e in ents:
        if getattr(e, "url", None):
            extra_urls.append(e.url)
        elif getattr(e.type, "name", str(e.type)) == "URL":
            extra_urls.append(base_text[e.offset : e.offset + e.length])

    kb = m.reply_markup
    if kb and hasattr(kb, "inline_keyboard"):
        for row in kb.inline_keyboard:
            for btn in row:
                if getattr(btn, "url", None):
                    extra_urls.append(btn.url)

    copied = str(base_text)
    for u in extra_urls:
        if u and u not in copied:
            copied += f"\n{u}"
    return copied.strip()


def message_urls(m):
    """message के कॉपी किए गए पूरे text से सारे URLs (क्रम में)"""
    copied_text = copy_full_text_from_msg(m)
    urls = [clean_url(u) for u in URL_RE.findall(copied_text)]
    seen, out = set(), []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def extract_bypassed_link_from_text(copied_text, sent_url=""):
    """
    Normal bot द्वारा कॉपी किए गए bypass bot के पूरे text में से
    असली bypassed link निकालो (original sent_url को छोड़कर)।
    """
    if not copied_text:
        return None

    sent_clean = clean_url(sent_url).lower()
    all_urls = [clean_url(u) for u in URL_RE.findall(copied_text)]

    # 1) सबसे पहले देखो कि कोई bot deep link (?start=) आया है क्या
    for u in all_urls:
        if u.lower() == sent_clean:
            continue
        deep = parse_deep(u)
        if deep:
            return f"https://t.me/{deep[0]}?start={deep[1]}"

    # 2) अगर ?start= नहीं है, तो 'bypassed' / 'destination' / 'unlocked' लाइन के बाद वाला बाहरी link देखो
    lines = copied_text.splitlines()
    for idx, line in enumerate(lines):
        if re.search(r"bypass|unlock|dest|result|final|link\s*:-?", line, re.I):
            if re.search(r"orig|source|input", line, re.I):
                continue
            chunk = "\n".join(lines[idx : idx + 2])
            for u in [clean_url(x) for x in URL_RE.findall(chunk)]:
                if u.lower() != sent_clean and not any(
                    d in u.lower() for d in ("t.me/", "telegram.me/", "telegram.dog/")
                ):
                    return u

    # 3) वरना आख़िरी बाहरी link (जो original link या कोई साधारण telegram channel न हो)
    ext_links = [
        u
        for u in all_urls
        if u.lower() != sent_clean
        and not any(d in u.lower() for d in ("t.me/", "telegram.me/", "telegram.dog/"))
    ]
    if ext_links:
        return ext_links[-1]

    return None


def pick_next_link(urls):
    """bot-link (?start=) या बाहरी link चुनो; t.me के channel/join links छोड़ दो"""
    for u in urls:
        deep = parse_deep(u)
        if deep:
            return f"https://t.me/{deep[0]}?start={deep[1]}"
        if not any(d in u.lower() for d in ("t.me/", "telegram.me/", "telegram.dog/")):
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
@bot.on_message(filters.chat(GROUP_ID) & filters.incoming)
@bot.on_edited_message(filters.chat(GROUP_ID) & filters.incoming)
async def group_watch(_, m):
    if m.id in own_group_msg_ids:
        return
    if m.from_user and bot_info["id"] and m.from_user.id == bot_info["id"]:
        return
    group_inbox[m.id] = m
    if len(group_inbox) > 100:
        for k in sorted(group_inbox)[:-100]:
            group_inbox.pop(k, None)


async def raw_group_messages(sent_id):
    """Pyrogram के parse से बचकर: group के नए messages सीधे Telegram से (raw), ताज़ा रूप में"""
    peer = await userbot.resolve_peer(GROUP_ID)
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
        if m.id in own_group_msg_ids:
            continue
        from_id = getattr(getattr(m, "from_id", None), "user_id", None)
        if bot_info["id"] and from_id == bot_info["id"]:
            continue

        text = m.message or ""
        extra = []
        for e in (getattr(m, "entities", None) or []):
            u = getattr(e, "url", None)
            if u:
                extra.append(u)
        mk = getattr(m, "reply_markup", None)
        for row in (getattr(mk, "rows", None) or []):
            for btn in row.buttons:
                u = getattr(btn, "url", None)
                if u:
                    extra.append(u)

        copied_text = text
        for u in extra:
            if u and u not in copied_text:
                copied_text += f"\n{u}"

        rt = getattr(m, "reply_to", None)
        media = type(m.media).__name__ if getattr(m, "media", None) else "-"
        out.append({
            "id": m.id,
            "reply_to": getattr(rt, "reply_to_msg_id", None),
            "copied_text": copied_text.strip(),
            "summary": f"#{m.id} media={media} len={len(text)} '{text[:40]}'".replace("\n", " "),
        })
    return out


async def wait_bypass_and_repost(sent_id, sent_url, notify, pid):
    """
    1. Group में bypass bot के reply का इंतज़ार करो।
    2. Normal bot की मदद से bypass bot का सारा text कॉपी करके उसमें से bypassed link निकालो।
    3. Normal bot (`bot`) उस bypassed link को वापस GROUP_ID में भेजेगा।
    4. वही link लौटाओ ताकि userbot उस link वाले bot के पास जा सके।
    """
    t0 = time.time()
    cands = {}
    last_raw = []

    while time.time() - t0 < BYPASS_TIMEOUT:
        await asyncio.sleep(3)
        if running["stop"]:
            return None, []

        found_link = None

        # --- रास्ता 1: raw (सबसे भरोसेमंद) ---
        try:
            last_raw = await raw_group_messages(sent_id)
        except Exception as e:
            print("[bypass] raw error:", repr(e))
            last_raw = []

        r_replies, r_others = [], []
        for r in last_raw:
            link = extract_bypassed_link_from_text(r["copied_text"], sent_url)
            if link:
                (r_replies if r["reply_to"] == sent_id else r_others).append(link)
        if r_replies:
            found_link = r_replies[0]
        elif r_others:
            found_link = r_others[0]

        # --- रास्ता 2: Pyrogram से (live handler + history + ताज़ा fetch) ---
        if not found_link:
            ids = {i for i in group_inbox if i > sent_id}
            try:
                async for m in userbot.get_chat_history(GROUP_ID, limit=30):
                    if m.id <= sent_id:
                        break
                    if m.id not in own_group_msg_ids:
                        ids.add(m.id)
            except Exception as e:
                print("[bypass] history error:", repr(e))

            cands = {i: group_inbox[i] for i in ids if i in group_inbox}
            if ids:
                try:
                    fresh = await userbot.get_messages(GROUP_ID, sorted(ids))
                    if not isinstance(fresh, list):
                        fresh = [fresh]
                    for fm in fresh:
                        if fm and not fm.empty and not fm.outgoing and fm.id not in own_group_msg_ids:
                            if fm.from_user and bot_info["id"] and fm.from_user.id == bot_info["id"]:
                                continue
                            cands[fm.id] = fm
                except Exception as e:
                    print("[bypass] get_messages error:", repr(e))

            replies, others = [], []
            for i in sorted(cands, reverse=True):
                m = cands[i]
                try:
                    copied_text = copy_full_text_from_msg(m)
                    link = extract_bypassed_link_from_text(copied_text, sent_url)
                except Exception as e:
                    print("[bypass] parse error:", repr(e))
                    continue
                if link:
                    (replies if m.reply_to_message_id == sent_id else others).append(link)

            if replies:
                found_link = replies[0]
            elif others:
                found_link = others[0]

        # --- अगर bypassed link मिल गया तो Normal Bot उसे वापस group में भेजेगा ---
        if found_link:
            try:
                bm = await bot.send_message(
                    GROUP_ID,
                    found_link,
                    disable_web_page_preview=True,
                )
                own_group_msg_ids.add(bm.id)
                await notify(f"Post {pid}: Normal bot ने bypass text से link निकालकर group में भेज दिया:\n{found_link}")
            except Exception as e:
                print("Normal bot से group में भेजने में error (शायद bot group में नहीं है):", e)
                um = await userbot.send_message(
                    GROUP_ID,
                    found_link,
                    disable_web_page_preview=True,
                )
                own_group_msg_ids.add(um.id)
                await notify(
                    f"Post {pid}: Bypassed link मिल गया (Normal bot group में न होने से userbot ने भेजा):\n{found_link}"
                )
            return found_link, []

    # --- नहीं मिला: क्या दिखा, वह बताओ ---
    diag = [r["summary"] for r in last_raw[:4]]
    if not diag:
        for i in sorted(cands)[:3]:
            m = cands[i]
            who = (m.from_user.first_name if m.from_user else "?")
            snippet = str(m.text or m.caption or "[media]")[:60].replace("\n", " ")
            diag.append(f"{who}: {snippet}")
    print(f"[bypass] timeout, raw={len(last_raw)} pyro={len(cands)}")
    return None, diag


async def resolve_chain(start_link, notify, pid):
    """
    link -> (bot को /start) -> reply में नया link -> (बाहरी link हो तो group का bypass bot)
    -> Normal bot द्वारा bypassed link वापस group में भेजना -> Userbot का उस bot के पास जाना।
    """
    current = start_link
    last_bot = None
    just_bypassed = False

    for hop in range(MAX_HOPS):
        if running["stop"]:
            return None
        deep = parse_deep(current)

        if deep:
            bot_user, payload = deep
            # अगर बिना bypass के सीधे किसी दूसरे फ़ाइनल bot का link मिला है तो वही आख़िरी link है;
            # लेकिन अगर अभी-अभी bypass होकर link आया है तो userbot उस bot के पास ज़रूर जाएगा!
            if last_bot and bot_user.lower() != last_bot.lower() and not just_bypassed:
                return f"https://t.me/{bot_user}?start={payload}"

            just_bypassed = False
            await notify(f"Post {pid}: Userbot @{bot_user} के पास जाकर /start भेज रहा है…")
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
                await notify(f"Post {pid}: @{bot_user} के messages group में forward किए। अब videos का इंतज़ार…")
                return "FORWARDED"
            current = nxt
        else:
            # बाहरी (shortener) link: group के bypass bot से bypass कराओ
            sent = await userbot.send_message(GROUP_ID, current)
            own_group_msg_ids.add(sent.id)
            await notify(f"Post {pid}: shortener link group में भेजा, bypass bot के reply का इंतज़ार…")
            bypassed, diag = await wait_bypass_and_repost(sent.id, current, notify, pid)
            if not bypassed:
                extra = ("\nदेखे गए messages:\n" + "\n".join(diag)) if diag else "\n(group में कोई नया message नहीं दिखा)"
                await notify(f"Post {pid}: bypass bot से link नहीं मिला{extra}")
                return None
            current = bypassed
            just_bypassed = True

    await notify(f"Post {pid}: बहुत ज़्यादा कड़ियाँ हो गईं, रुक गया")
    return None


async def forward_to_group(m):
    """bot का message group में forward करो; न हो पाए तो text की copy भेजो"""
    try:
        fm = await userbot.forward_messages(GROUP_ID, m.chat.id, m.id)
        if hasattr(fm, "id"):
            own_group_msg_ids.add(fm.id)
    except Exception as e:
        print("forward fail, text भेज रहा हूँ:", e)
        txt = m.text.html if m.text else (m.caption.html if m.caption else "")
        if txt:
            sm = await userbot.send_message(GROUP_ID, txt, disable_web_page_preview=True)
            own_group_msg_ids.add(sm.id)


async def fetch_from_bot(username, payload):
    """deep link पर क्लिक करने जैसा: bot को /start <payload> भेजो और उसके media/messages जमा करो"""
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

            if AUTO_FETCH:
                # link-चेन चलाओ: bot -> shortener -> bypass (normal bot reposts link) -> userbot goes to bot
                final_link = await resolve_chain(link, notify, pid)
                if not final_link:
                    continue
                if final_link != "FORWARDED":
                    sm = await userbot.send_message(GROUP_ID, final_link)
                    own_group_msg_ids.add(sm.id)
                    await notify(f"Post {pid}: आख़िरी link group में भेजा। अब videos channel में forward करो…")
                await wait_for_videos()
            else:
                # पुराना तरीका: link group में भेजो, videos आप forward करोगे
                sm = await userbot.send_message(GROUP_ID, link)
                own_group_msg_ids.add(sm.id)
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

    me_bot = await bot.get_me()
    bot_info["id"] = me_bot.id

    # सारे chats/channels की जानकारी cache करो, वरना "Peer id invalid" आ सकता है
    async for _ in userbot.get_dialogs():
        pass

    print("Userbot + Bot चालू हैं। Bot को start id aur end id भेजो।")
    await idle()
    await bot.stop()
    await userbot.stop()


if __name__ == "__main__":
    userbot.run(main())
