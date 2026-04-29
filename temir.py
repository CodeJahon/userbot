# -*- coding: utf-8 -*-
import logging
import asyncio
import re
import io
import base64
import time
import requests
from telethon import TelegramClient, events
from playwright.async_api import async_playwright
from PIL import Image

logging.basicConfig(format='%(asctime)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger('telethon').setLevel(logging.WARNING)

# ─── SOZLAMALAR ───────────────────────────────────────────────────────────────
API_ID              = 21817259
API_HASH            = "0c7cb27f70d9d111e4941e092870d7e6"
CHAT_ID             = "@janguzbot"
ORIGINAL_MESSAGE_ID = 678955
SESSION_NAME        = "ak"
DELAY_MINUTES       = 11
CAPTCHA_API_KEY     = "e450c51f480413ded4607f54deac43d9"

current_message_id  = ORIGINAL_MESSAGE_ID
client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

# Cycle ishlamoqdami? (ikki marta ishga tushib ketmasligi uchun)
cycle_running = False

# ─── 2CAPTCHA ─────────────────────────────────────────────────────────────────
def solve_2captcha(image_bytes: bytes) -> str:
    """Rasmni 2captcha ga yuborib javob oladi"""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        # O'lchamni tekshirib kerak bo'lsa kattalashtirish
        w, h = img.size
        if w < 100 or h < 40:
            scale = max(100 / w, 40 / h, 2.0)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            logger.info(f"Rasm kattalashtirildi: {img.size}")
        buf = io.BytesIO()
        img.save(buf, format='PNG', compress_level=0)
        png_bytes = buf.getvalue()
        logger.info(f"PNG hajmi: {len(png_bytes)} bytes")
    except Exception as e:
        logger.error(f"PNG konversiya: {e}")
        return ""

    try:
        r = requests.post(
            "https://2captcha.com/in.php",
            files={"file": ("captcha.png", png_bytes, "image/png")},
            data={
                "method":   "post",
                "key":      CAPTCHA_API_KEY,
                "json":     0,
                "regsense": 0,
                "numeric":  0,
                "min_len":  4,
                "max_len":  6,
            },
            timeout=15,
        )
        text = r.text.strip()
        logger.info(f"2captcha submit: {text[:60]}")
        if text.startswith("OK|"):
            captcha_id = text.split("|", 1)[1].strip()
        elif text.isdigit():
            captcha_id = text
        else:
            logger.error(f"Submit xato: {text}")
            return ""
    except Exception as e:
        logger.error(f"Submit tarmoq: {e}")
        return ""

    logger.info(f"2captcha ID: {captcha_id}, natija kutilmoqda...")

    time.sleep(5)
    for _ in range(12):
        try:
            r = requests.get("https://2captcha.com/res.php", params={
                "key":    CAPTCHA_API_KEY,
                "action": "get",
                "id":     captcha_id,
                "json":   0,
            }, timeout=8)
            text = r.text.strip()
            logger.info(f"Polling: {text[:60]}")
            if text.startswith("OK|"):
                answer = text.split("|", 1)[1].strip()
                logger.info(f"✅ 2captcha javob: '{answer}'")
                return answer
            if "NOT_READY" in text:
                time.sleep(2)
                continue
            logger.error(f"Polling xato: {text}")
            return ""
        except Exception as e:
            logger.warning(f"Polling tarmoq: {e}")
            time.sleep(2)

    logger.error("2captcha vaqt tugadi")
    return ""

# ─── CAPTCHA SOLVER (PLAYWRIGHT) ──────────────────────────────────────────────
async def solve_captcha(url: str) -> bool:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await (await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        )).new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(1.5)

            img_el = page.locator("img").first
            if not await img_el.is_visible():
                logger.warning("CAPTCHA rasmi topilmadi")
                return False

            # Har doim element screenshot — eng ishonchli usul
            img_bytes = await img_el.screenshot(type="png")
            logger.info(f"Rasm olindi: {len(img_bytes)} bytes")

            # Minimal o'lcham tekshiruvi
            check_img = Image.open(io.BytesIO(img_bytes))
            w, h = check_img.size
            logger.info(f"Rasm o'lchami: {w}x{h}")
            if w < 30 or h < 10:
                logger.error(f"Rasm juda kichik: {w}x{h}")
                return False

            loop = asyncio.get_event_loop()

            # Max 3 urinish (rasm o'zgarmasa ham, 2captcha har safar boshqa worker)
            for attempt in range(1, 4):
                logger.info(f"2captcha urinish #{attempt}...")
                captcha_text = await loop.run_in_executor(None, solve_2captcha, img_bytes)

                if not captcha_text:
                    logger.error(f"2captcha javob bermadi (urinish #{attempt})")
                    if attempt < 3:
                        await asyncio.sleep(2)
                    continue

                # Inputni tozalab yozamiz
                for sel in ["input[type='text']", "input:not([type='submit'])"]:
                    try:
                        await page.fill(sel, "")
                        await page.fill(sel, captcha_text)
                        break
                    except:
                        continue

                resp = {}
                async def on_resp(r, _resp=resp):
                    if "captcha" in r.url:
                        try:
                            _resp["body"] = await r.text()
                        except:
                            pass
                page.on("response", on_resp)

                for bsel in ["button:has-text('Tasdiqlash')", "button[type='submit']", "button"]:
                    try:
                        b = page.locator(bsel).first
                        if await b.is_visible():
                            await b.click()
                            break
                    except:
                        continue

                await asyncio.sleep(2.5)
                page.remove_listener("response", on_resp)

                body = resp.get("body", "").lower()
                logger.info(f"Server javob: '{body[:80]}'")

                if "success" in body or "tasdiqlandi" in body:
                    logger.info(f"✅ CAPTCHA hal qilindi: '{captcha_text}'")
                    return True

                logger.warning(f"❌ CAPTCHA noto'g'ri: '{captcha_text}' (urinish #{attempt})")

                # Rasm yangilandimi? (ba'zi CAPTCHA sahifalar refresh qiladi)
                await asyncio.sleep(1)
                new_img_bytes = await img_el.screenshot(type="png")
                if new_img_bytes != img_bytes:
                    logger.info("🔄 Yangi CAPTCHA rasmi aniqlandi, qayta o'qilmoqda...")
                    img_bytes = new_img_bytes

            logger.error("3 urinishdan keyin ham CAPTCHA hal bo'lmadi")
            return False

        except Exception as e:
            logger.error(f"Playwright: {e}", exc_info=True)
            return False
        finally:
            await browser.close()

def extract_captcha_url(event) -> str:
    text = event.raw_text or ""
    m = re.search(r"https?://\S+captcha\S*", text, re.IGNORECASE)
    if m:
        return m.group(0)
    if event.reply_markup:
        try:
            for row in event.reply_markup.rows:
                for btn in row.buttons:
                    if hasattr(btn, "url") and btn.url and "captcha" in btn.url.lower():
                        return btn.url
        except:
            pass
    return ""

# ─── TELEGRAM YORDAMCHI ───────────────────────────────────────────────────────
async def get_message_by_id(message_id):
    try:
        messages = await client.get_messages(CHAT_ID, ids=[message_id])
        if messages:
            return messages[0]
        return None
    except Exception as e:
        logger.warning(f"get_message xato: {e}")
        return None

async def press_inline_button(message, button_text) -> bool:
    if message and message.reply_markup:
        for row in message.reply_markup.rows:
            for button in row.buttons:
                if button.text and button_text in button.text:
                    await message.click(data=button.data)
                    logger.info(f"✅ Tugma bosildi: {button.text}")
                    return True
    logger.warning(f"'{button_text}' tugmasi topilmadi")
    return False

# ─── CAPTCHA KUTISH + TUGMA BOSISH ────────────────────────────────────────────
async def press_with_captcha_retry(message_id: int, button_text: str) -> bool:
    """
    Tugmani bosadi.
    Agar 15 soniya ichida CAPTCHA URL xabari kelsa — hal qilib qayta bosadi.
    """
    message = await get_message_by_id(message_id)
    if not message:
        logger.warning(f"Xabar #{message_id} topilmadi")
        return False

    pressed = await press_inline_button(message, button_text)
    if not pressed:
        return False

    # CAPTCHA kelishini kutamiz
    captcha_event = asyncio.Event()
    captcha_url_holder = []

    async def captcha_waiter(event):
        url = extract_captcha_url(event)
        if url and not captcha_event.is_set():
            captcha_url_holder.append(url)
            captcha_event.set()

    client.add_event_handler(captcha_waiter, events.NewMessage(from_users=CHAT_ID))

    try:
        await asyncio.wait_for(captcha_event.wait(), timeout=15)
    except asyncio.TimeoutError:
        # CAPTCHA kelmadi → muvaffaqiyatli deb hisoblaymiz
        logger.info(f"'{button_text}' — CAPTCHA yo'q, muvaffaqiyatli")
        client.remove_event_handler(captcha_waiter)
        return True

    client.remove_event_handler(captcha_waiter)

    captcha_url = captcha_url_holder[0] if captcha_url_holder else ""
    if not captcha_url:
        logger.error("CAPTCHA URL bo'sh")
        return False

    logger.info(f"🔒 CAPTCHA topildi: {captcha_url}")
    ok = await solve_captcha(captcha_url)

    if not ok:
        logger.warning("❌ CAPTCHA hal bo'lmadi")
        return False

    # CAPTCHA hal bo'ldi → qayta bosamiz
    await asyncio.sleep(1.5)
    logger.info(f"✅ CAPTCHA o'tdi → '{button_text}' qayta bosilmoqda...")
    message = await get_message_by_id(message_id)
    if message:
        return await press_inline_button(message, button_text)

    return False

# ─── ASOSIY SIKL ──────────────────────────────────────────────────────────────
async def run_cycle(start_message_id: int):
    """
    Bir to'liq sikl:
    1. "Temir ishlov berishni boshlash" bosiladi
    2. Bot "Temirga ishlov berish boshlandi" deydi → handler wait_for_sandon ni ishga tushiradi
    """
    global cycle_running, current_message_id
    if cycle_running:
        logger.info("Sikl allaqachon ishlamoqda, qayta ishga tushirilmadi")
        return
    cycle_running = True
    current_message_id = start_message_id
    logger.info("🔁 Sikl boshlandi")
    ok = await press_with_captcha_retry(current_message_id, "Temir ishlov berishni boshlash")
    if not ok:
        logger.error("Boshlash tugmasi bosilmadi, sikl to'xtatildi")
        cycle_running = False

async def wait_and_press_sandon(msg_id: int):
    """11 daqiqa kutib Sandon tugmasini bosadi"""
    logger.info(f"⏳ {DELAY_MINUTES} daqiqa kutilmoqda (Sandon uchun)...")
    await asyncio.sleep(DELAY_MINUTES * 60)
    logger.info("🔨 Sandonni qizdirish bosilmoqda...")
    await press_with_captcha_retry(msg_id, "Sandonni qizdirish")
    # Natija handle_edited_message da ushlanadi

# ─── HANDLERLAR ───────────────────────────────────────────────────────────────
@client.on(events.NewMessage(from_users=CHAT_ID))
async def handle_new_message(event):
    global current_message_id, cycle_running

    raw = event.raw_text or ""

    # CAPTCHA xabarini press_with_captcha_retry o'zi ushlab oladi
    if extract_captcha_url(event):
        return

    # "Temirga ishlov berish boshlandi" — Sandon kutishini ishga tushiramiz
    if "Temirga ishlov berish boshlandi" in raw:
        current_message_id = event.message.id
        logger.info(f"📩 Ishlov boshlandi xabari keldi (id={current_message_id})")
        asyncio.create_task(wait_and_press_sandon(current_message_id))


@client.on(events.MessageEdited(from_users=CHAT_ID))
async def handle_edited_message(event):
    """
    Sandon bosilgandan keyin xabar tahrirlandi.
    "Quyma temirni olish" tugmasi paydo bo'lsa — bosamiz, keyin sikl qayta boshlanadi.
    """
    global current_message_id, cycle_running

    raw = event.raw_text or ""

    # "Quyma temirni olish" tugmasi bor xabar
    has_button = False
    if event.reply_markup:
        try:
            for row in event.reply_markup.rows:
                for btn in row.buttons:
                    if btn.text and "Quyma temirni olish" in btn.text:
                        has_button = True
        except:
            pass

    if not has_button:
        return

    msg_id = event.message.id
    logger.info(f"✏️ Edited xabar: 'Quyma temirni olish' tugmasi topildi (id={msg_id})")
    current_message_id = msg_id

    ok = await press_with_captcha_retry(current_message_id, "Quyma temirni olish")
    if ok:
        logger.info("✅ Quyma temir olindi → Yangi sikl boshlanmoqda...")
        cycle_running = False  # Yangi sikilga ruxsat
        await asyncio.sleep(2)
        await run_cycle(ORIGINAL_MESSAGE_ID)
    else:
        logger.error("❌ 'Quyma temirni olish' bosilmadi")
        cycle_running = False

# ─── MAIN ─────────────────────────────────────────────────────────────────────
async def main():
    await client.start()
    logger.info("✅ Userbot ishga tushdi")
    await run_cycle(ORIGINAL_MESSAGE_ID)
    await client.run_until_disconnected()

with client:
    client.loop.run_until_complete(main())