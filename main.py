import os
import sys
import asyncio
import logging
import importlib
import inspect

from telegram import Update, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telethon import TelegramClient

from config import Config
from db import db

# ================== Logging ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger("SniperSystem")

# ================== Global Stores ==================
USERBOT_HANDLERS = []
MODULE_ACTIONS = {}
LOADED_MODULES = {}

# ================== Module Loader ==================
async def load_modules(application):
    """تحميل جميع الموديولات بشكل احترافي"""
    
    modules_dir = os.path.join(os.path.dirname(__file__), "modules")

    # تأكد من المسار
    if modules_dir not in sys.path:
        sys.path.append(modules_dir)
        sys.path.append(os.path.dirname(__file__))

    # تنظيف قبل إعادة التحميل
    USERBOT_HANDLERS.clear()
    MODULE_ACTIONS.clear()
    Config.DYNAMIC_BUTTONS = {}

    logger.info("🔄 بدء تحميل الموديولات...")

    for filename in sorted(os.listdir(modules_dir)):
        if not filename.endswith(".py") or filename.startswith("_"):
            continue

        module_name = f"modules.{filename[:-3]}"

        try:
            # استيراد أو إعادة تحميل
            module = sys.modules.get(module_name)
            if module:
                module = importlib.reload(module)
            else:
                module = importlib.import_module(module_name)

            LOADED_MODULES[module_name] = module

            # ====== 1. زر الموديول ======
            btn_text = getattr(module, "MAIN_BUTTON", None)

            if btn_text:
                if btn_text in MODULE_ACTIONS:
                    logger.warning(f"⚠️ زر مكرر: {btn_text} - تم تجاهله")
                else:
                    Config.DYNAMIC_BUTTONS[module_name] = btn_text

                    if hasattr(module, "show_main"):
                        MODULE_ACTIONS[btn_text] = module.show_main
                        logger.info(f"🔘 زر مضاف: {btn_text}")

            # ====== 2. رادار اليوزربوت ======
            handler = getattr(module, "handler", None)
            if handler:
                USERBOT_HANDLERS.append(handler)
                logger.info(f"📡 رادار مفعل: {filename}")

            # ====== 3. setup ======
            setup = getattr(module, "setup", None)
            if setup:
                result = setup(application)
                if inspect.isawaitable(result):
                    await result

            logger.info(f"✅ تم تحميل: {filename}")

        except Exception:
            logger.exception(f"❌ فشل تحميل: {filename}")

    logger.info(f"📦 عدد الموديولات: {len(LOADED_MODULES)}")


# ================== UI ==================
def build_keyboard(user_id):
    """بناء لوحة الأزرار تلقائياً"""

    buttons = list(Config.DYNAMIC_BUTTONS.values())

    # زر البحث الأساسي
    if "🔍 البحث عن فرصة" not in buttons:
        buttons.insert(0, "🔍 البحث عن فرصة")

    # زر الإدارة
    if user_id == Config.ADMIN_ID:
        buttons.append("🛠️ لوحة الإدارة")

    # تحويل إلى Keyboard
    keyboard = [
        [KeyboardButton(text)] if i % 2 == 0 else [KeyboardButton(buttons[i-1]), KeyboardButton(text)]
        for i, text in enumerate(buttons)
    ]

    # تصحيح الصفوف
    keyboard = []
    row = []
    for btn in buttons:
        row.append(KeyboardButton(btn))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# ================== Handlers ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    db.add_user(user.id, user.first_name, user.username)

    await update.message.reply_text(
        f"🔥 نظام القناص يعمل بكفاءة\n\n"
        f"مرحباً {user.first_name}\n"
        f"اختر من القائمة 👇",
        reply_markup=build_keyboard(user.id)
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    action = MODULE_ACTIONS.get(text)

    if action:
        try:
            result = action(update, context)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("❌ خطأ داخل الموديول")
            await update.message.reply_text("⚠️ حدث خطأ أثناء التنفيذ")
    else:
        await update.message.reply_text("❓ الأمر غير معروف")


# ================== Userbot ==================
async def start_userbot():
    client = TelegramClient(
        Config.SESSION_NAME,
        Config.API_ID,
        Config.API_HASH
    )

    for h in USERBOT_HANDLERS:
        client.add_event_handler(h)

    try:
        await client.start()
        logger.info("📡 اليوزربوت يعمل...")
        await client.run_until_disconnected()
    except Exception:
        logger.exception("❌ خطأ في اليوزربوت")


# ================== Main ==================
async def main():
    app = Application.builder().token(Config.BOT_TOKEN).build()

    await load_modules(app)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    async with app:
        await app.initialize()
        await app.start()

        bot_task = asyncio.create_task(app.updater.start_polling())
        userbot_task = asyncio.create_task(start_userbot())

        logger.info("🚀 النظام يعمل بالكامل")

        await asyncio.gather(bot_task, userbot_task)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 تم الإيقاف")