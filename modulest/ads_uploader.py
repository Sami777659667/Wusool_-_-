import logging
import os
import sys
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler, MessageHandler, filters

try:
    from config import Config
    from db import db
except ImportError:
    sys.path.append(os.path.dirname(os.path.dirname(__file__)))
    from config import Config
    from db import db

logger = logging.getLogger("AdsUploader_V4")

# --- [ الإعدادات ] ---
ADS_CHANNEL = -1003995849782  # قناتك السرية
MAIN_BUTTON = "📢 رفع إعلان جديد"

# الأقسام والمدن الموسعة مع خيار "أخرى"
CATEGORIES = [
    ["🚗 سيارات", "🏠 عقارات", "📱 جوالات"],
    ["🛠 خدمات", "📦 تجارة", "➕ أخرى"]
]

SAUDI_CITIES = [
    "الرياض", "جدة", "الدمام", "مكة", "المدينة", "أخرى"
]

async def show_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """الواجهة الرئيسية للرفع"""
    text = (
        "🚀 **منصة النشر الاحترافية - لوحة التحكم**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "يرجى اختيار نوع الإعلان المراد نشره. الإعلانات الممولة تحظى بنسبة وصول أعلى بـ 10 أضعاف.\n\n"
        "💡 **نظام الترقيم التسلسلي مفعل آلياً.**"
    )
    keyboard = [
        [InlineKeyboardButton("🆓 نشر إعلان مجاني", callback_data="up_start_free")],
        [InlineKeyboardButton("💎 نشر إعلان ممول (Premium)", callback_data="up_start_paid")],
        [InlineKeyboardButton("👨‍💼 شحن رصيد / إدارة", url=f"tg://user?id={Config.ADMIN_ID}")]
    ]
    # دعم استدعاء الزر من الرسالة أو من الكولباك
    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def handle_steps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    # 1. فحص الإعلان الممول
    if data == "up_start_paid":
        user_info = db.get_user_balance(user_id)
        if user_info['balance'] <= 0 and user_info['is_vip'] == 0:
            return await query.answer("⚠️ عذراً، لا يوجد رصيد كافٍ للإعلان الممول. يرجى الشحن أولاً.", show_alert=True)
        context.user_data['is_premium_post'] = True
        data = "up_start_free"

    # 2. بداية الخطوات - النوع
    if data == "up_start_free":
        context.user_data['ad_flow'] = {'paid': context.user_data.get('is_premium_post', False)}
        keyboard = [[InlineKeyboardButton("🛒 عرض بيع", callback_data="st_type_بيع"),
                     InlineKeyboardButton("📩 طلب شراء", callback_data="st_type_شراء")]]
        await query.edit_message_text("🔹 **الخطوة (1/6):** حدد غرض الإعلان:", reply_markup=InlineKeyboardMarkup(keyboard))

    # 3. اختيار القسم
    elif data.startswith("st_type_"):
        context.user_data['ad_flow']['type'] = data.split("_")[-1]
        keyboard = [[InlineKeyboardButton(c, callback_data=f"st_cat_{c}") for c in row] for row in CATEGORIES]
        await query.edit_message_text("🔹 **الخطوة (2/6):** اختر تصنيف الفرصة:", reply_markup=InlineKeyboardMarkup(keyboard))

    # 4. اختيار المدينة
    elif data.startswith("st_cat_"):
        cat_raw = data.split("_")[-1]
        context.user_data['ad_flow']['cat'] = "".join(c for c in cat_raw if c not in "🚗🏠📱🛠📦➕")
        keyboard = []
        for i in range(0, len(SAUDI_CITIES), 2):
            row = [InlineKeyboardButton(SAUDI_CITIES[i], callback_data=f"st_city_{SAUDI_CITIES[i]}")]
            if i+1 < len(SAUDI_CITIES):
                row.append(InlineKeyboardButton(SAUDI_CITIES[i+1], callback_data=f"st_city_{SAUDI_CITIES[i+1]}"))
            keyboard.append(row)
        await query.edit_message_text("🔹 **الخطوة (3/6):** حدد المدينة المرتبطة بالإعلان:", reply_markup=InlineKeyboardMarkup(keyboard))

    # 5. طلب الوصف (هنا يبدأ التفاعل الذكي)
    elif data.startswith("st_city_"):
        context.user_data['ad_flow']['city'] = data.split("_")[-1]
        type_str = context.user_data['ad_flow']['type']
        cat_str = context.user_data['ad_flow']['cat']
        
        text = (
            f"📝 **الخطوة (4/6): وصف الإعلان**\n"
            f"أنت الآن بصدد إنشاء إعلان ({type_str}) في قسم ({cat_str}).\n\n"
            f"⚠️ **شروط الوصف:**\n"
            f"- لا يقل عن 10 أحرف.\n"
            f"- لا يزيد عن 100 حرف.\n\n"
            f"أرسل وصف الـ {cat_str} الآن:"
        )
        await query.edit_message_text(text, parse_mode="Markdown")
        context.user_data['waiting_input'] = "wait_desc"

    # 6. اختيار الحالة
    elif data.startswith("st_status_"):
        context.user_data['ad_flow']['status'] = data.split("_")[-1]
        keyboard = [[InlineKeyboardButton("⏭ تخطي وإرسال الآن", callback_data="st_finish")]]
        await query.edit_message_text("📸 **الخطوة (6/6): صورة الإعلان**\nأرسل صورة توضيحية الآن أو اضغط تخطي:", reply_markup=InlineKeyboardMarkup(keyboard))
        context.user_data['waiting_input'] = "wait_photo"

    elif data == "st_finish":
        await process_final_publish(update, context)

async def handle_inputs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة المدخلات النصية والصور"""
    if 'waiting_input' not in context.user_data: return
    step = context.user_data['waiting_input']
    
    # معالجة الوصف مع التحقق من الطول
    if step == "wait_desc":
        desc = update.message.text
        if len(desc) < 10 or len(desc) > 100:
            return await update.message.reply_text(f"❌ **خطأ في الطول!** الوصف الحالي ({len(desc)}) حرف. يجب أن يكون بين 10 و 100 حرف. أرسله مجدداً:")
        
        context.user_data['ad_flow']['desc'] = desc
        cat = context.user_data['ad_flow']['cat']
        await update.message.reply_text(f"💰 **الخطوة (5/6):** ما هو سعر الـ {cat}؟ (أرسل السعر فقط):")
        context.user_data['waiting_input'] = "wait_price"

    # معالجة السعر
    elif step == "wait_price":
        context.user_data['ad_flow']['price'] = update.message.text
        context.user_data['waiting_input'] = None
        keyboard = [[InlineKeyboardButton("🔥 مستعجل جداً", callback_data="st_status_🔥 مستعجل"),
                     InlineKeyboardButton("🔆 عادي", callback_data="st_status_🔆 عادي")]]
        await update.message.reply_text("⚡️ **أولوية الإعلان:**", reply_markup=InlineKeyboardMarkup(keyboard))

    # معالجة الصورة
    elif step == "wait_photo":
        if update.message.photo:
            context.user_data['ad_flow']['photo'] = update.message.photo[-1].file_id
            context.user_data['waiting_input'] = None
            await process_final_publish(update, context)
        else:
            await update.message.reply_text("⚠️ يرجى إرسال صورة صحيحة أو اضغط على زر التخطي.")

async def process_final_publish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """النشر النهائي والتوثيق في القاعدة والقناة"""
    ad = context.user_data.get('ad_flow')
    if not ad: return
    
    user = update.effective_user
    
    # 1. حفظ في DB واستخراج الرقم التسلسلي #1000
    serial = db.save_ad_full(
        owner_id=user.id,
        ad_type=ad['type'],
        category=ad['cat'],
        city=ad['city'],
        desc=ad['desc'],
        price=ad['price'],
        photo_id=ad.get('photo'),
        status_tag=ad['status']
    )

    label = "💎 ممول" if ad.get('paid') else "🆓 مجاني"
    
    # 2. بناء رسالة القناة الرسمية للتوثيق
    channel_text = (
        f"📢 **إعلان جديد مستلم** ({label})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 الرقم التسلسلي: {serial}\n"
        f"🏷 النوع: #{ad['type']} | 📂 القسم: #{ad['cat']}\n"
        f"📍 المدينة: #{ad['city']}\n"
        f"💰 السعر: {ad['price']}\n"
        f"⚡️ الحالة: {ad['status']}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 **الوصف:**\n{ad['desc']}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 المعلن: [{user.first_name}](tg://user?id={user.id})\n"
        f"✅ **موثق في قاعدة البيانات المركزية**"
    )

    try:
        # إرسال للقناة (للتوثيق والحصول على نسخة دائمة للصورة)
        if ad.get('photo'):
            await context.bot.send_photo(ADS_CHANNEL, ad['photo'], caption=channel_text, parse_mode="Markdown")
        else:
            await context.bot.send_message(ADS_CHANNEL, channel_text, parse_mode="Markdown")
        
        # 3. خصم الرصيد إذا كان ممولاً
        if ad.get('paid'):
            db.update_balance(user.id, -20) # تكلفة المميز مثلاً 20

        # رسالة نجاح للمستخدم
        success_text = (
            f"✅ **تم نشر إعلانك بنجاح!**\n\n"
            f"🔢 رقم الإعلان: `{serial}`\n"
            f"تمت أرشفة البيانات في السجل الرسمي وقناة التوثيق."
        )
        if update.callback_query:
            await update.callback_query.message.reply_text(success_text, parse_mode="Markdown")
        else:
            await update.message.reply_text(success_text, parse_mode="Markdown")
        
        context.user_data.clear()
    except Exception as e:
        logger.error(f"Error publishing: {e}")
        await update.effective_message.reply_text("❌ حدث خطأ أثناء النشر، يرجى المحاولة لاحقاً.")

async def setup(application):
    # ربط الهاندلرز
    application.add_handler(CallbackQueryHandler(handle_steps, pattern="^(up_|st_)"))
    application.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_inputs), group=40)
