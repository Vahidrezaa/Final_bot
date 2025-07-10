import os
import logging
import uuid
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ConversationHandler
)
import asyncpg
from dotenv import load_dotenv
from aiohttp import web
import aiohttp
from database import Database
from timer_manager import TimerManager

# تنظیمات محیطی
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_IDS = [int(id) for id in os.getenv('ADMIN_IDS', '').split(',') if id]

# تنظیمات لاگ
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# حالت‌های گفتگو
UPLOADING, WAITING_CHANNEL_INFO, WAITING_TIMER_INPUT = range(3)

class BotManager:
    """مدیریت اصلی ربات"""
    
    def __init__(self):
        self.db = Database()
        self.timer_manager = TimerManager(self.db)
        self.pending_uploads = {}  # {user_id: {'category_id': str, 'files': list}}
        self.pending_channels = {}  # {user_id: {'channel_id': str, 'name': str, 'link': str}}
        self.bot_username = None
    
    async def init(self, bot_username: str):
        """راه‌اندازی اولیه"""
        self.bot_username = bot_username
        await self.db.connect()
    
    def is_admin(self, user_id: int) -> bool:
        """بررسی ادمین بودن کاربر"""
        return user_id in ADMIN_IDS
    
    def generate_link(self, category_id: str) -> str:
        """تولید لینک دسته با یوزرنیم صحیح"""
        if self.bot_username:
            return f"https://t.me/{self.bot_username}?start=cat_{category_id}"
        # Fallback در صورت عدم وجود یوزرنیم
        bot_id = BOT_TOKEN.split(':')[0]
        return f"https://t.me/{bot_id}?start=cat_{category_id}"
    
    def extract_file_info(self, update: Update) -> dict:
        msg = update.message

        if msg.document:
            file = msg.document
            file_type = 'document'
            file_name = file.file_name or f"document_{file.file_id[:8]}"
        elif msg.photo:
            file = msg.photo[-1]  # بالاترین کیفیت
            file_type = 'photo'
            file_name = f"photo_{file.file_id[:8]}.jpg"
        elif msg.video:
            file = msg.video
            file_type = 'video'
            file_name = f"video_{file.file_id[:8]}.mp4"
        elif msg.audio:
            file = msg.audio
            file_type = 'audio'
            file_name = f"audio_{file.file_id[:8]}.mp3"
        else:
            return None

        return {
            'file_id': file.file_id,
            'file_name': file_name,
            'file_size': file.file_size,
            'file_type': file_type,
            'caption': msg.caption or ''
        }

# ایجاد نمونه
bot_manager = BotManager()

# ========================
# ==== HANDLER FUNCTIONS ===
# ========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دستور شروع"""
    user_id = update.effective_user.id
    
    # دسترسی از طریق لینک دسته
    if context.args and context.args[0].startswith('cat_'):
        category_id = context.args[0][4:]
        await handle_category(update, context, category_id)
        return
    
    if bot_manager.is_admin(user_id):
        await update.message.reply_text(
            "👋 سلام ادمین!\n\n"
            "دستورات:\n"
            "/new_category - ساخت دسته جدید\n"
            "/upload - شروع آپلود فایل\n"
            "/finish_upload - پایان آپلود\n"
            "/categories - نمایش دسته‌ها\n"
            "/timer - تنظیم تایمر پیش‌فرض\n"
            "/add_channel - افزودن کانال\n"
            "/remove_channel - حذف کانال\n"
            "/channels - لیست کانال‌ها"
        )
    else:
        await update.message.reply_text("👋 سلام! برای دریافت فایل‌ها از لینک‌ها استفاده کنید.")

async def is_user_member(context, channel_id, user_id):
    """بررسی عضویت کاربر با تلاش مجدد"""
    for _ in range(3):  # 3 بار تلاش
        try:
            member = await context.bot.get_chat_member(chat_id=channel_id, user_id=user_id)
            if member.status in ['member', 'administrator', 'creator']:
                return True
        except Exception as e:
            logger.warning(f"خطا در بررسی عضویت: {e}")
        
        await asyncio.sleep(2)  # تاخیر 2 ثانیه‌ای بین هر تلاش
    
    return False

async def handle_category(update: Update, context: ContextTypes.DEFAULT_TYPE, category_id: str):
    """مدیریت دسترسی به دسته"""
    if update.message:
        user_id = update.message.from_user.id
        message = update.message
    elif update.callback_query:
        user_id = update.callback_query.from_user.id
        message = update.callback_query.message
    else:
        logger.error("Unsupported update type")
        return

    # بررسی ادمین
    if bot_manager.is_admin(user_id):
        await admin_category_menu(message, category_id)
        return
    
    # بررسی عضویت در کانال‌ها
    channels = await bot_manager.db.get_channels()
    if not channels:
        await send_category_files(message, context, category_id)
        return
    
    non_joined = []
    for channel in channels:
        is_member = await is_user_member(context, channel['channel_id'], user_id)
        if not is_member:
            non_joined.append(channel)
    
    if not non_joined:
        await send_category_files(message, context, category_id)
        return
    
    # ایجاد صفحه عضویت
    keyboard = []
    for channel in non_joined:
        button = InlineKeyboardButton(
            text=f"📢 {channel['channel_name']}",
            url=channel['invite_link']
        )
        keyboard.append([button])
    
    keyboard.append([
        InlineKeyboardButton(
            "✅ عضو شدم", 
            callback_data=f"check_{category_id}"
        )
    ])
    
    await message.reply_text(
        "⚠️ برای دسترسی ابتدا در کانال‌های زیر عضو شوید:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def admin_category_menu(message: Message, category_id: str):
    """منوی مدیریت دسته برای ادمین"""
    try:
        category = await bot_manager.db.get_category(category_id)
        if not category:
            await message.reply_text("❌ دسته یافت نشد!")
            return
        
        # دریافت وضعیت تایمر
        category_timer = await bot_manager.db.get_category_timer(category_id)
        default_timer = await bot_manager.db.get_default_timer()
        timer_status = "⏱ تایمر: "
        
        if category_timer == -1:
            timer_status += f"پیش‌فرض ({default_timer} ثانیه)" if default_timer > 0 else "پیش‌فرض (غیرفعال)"
        else:
            timer_status += f"اختصاصی ({category_timer} ثانیه)" if category_timer > 0 else "غیرفعال"
        
        keyboard = [
            [InlineKeyboardButton("📁 مشاهده فایل‌ها", callback_data=f"view_{category_id}")],
            [InlineKeyboardButton("➕ افزودن فایل", callback_data=f"add_{category_id}")],
            [InlineKeyboardButton("⏱ تنظیم تایمر", callback_data=f"timer_{category_id}")],
            [InlineKeyboardButton("🗑 حذف دسته", callback_data=f"delcat_{category_id}")]
        ]
        
        await message.reply_text(
            f"📂 دسته: {category['name']}\n"
            f"📦 تعداد فایل‌ها: {len(category['files'])}\n"
            f"{timer_status}\n\n"
            "لطفا عملیات مورد نظر را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"خطا در منوی ادمین: {e}")
        await message.reply_text("❌ خطایی در نمایش منو رخ داد")

async def send_category_files(message: Message, context: ContextTypes.DEFAULT_TYPE, category_id: str):
    """ارسال فایل‌های یک دسته"""
    try:
        chat_id = message.chat_id
        
        category = await bot_manager.db.get_category(category_id)
        if not category or not category['files']:
            await message.reply_text("❌ فایلی برای نمایش وجود ندارد!")
            return
        
        # دریافت تایمر مؤثر
        timer_seconds = await bot_manager.timer_manager.get_effective_timer(category_id)
        
        await message.reply_text(f"📤 ارسال فایل‌های '{category['name']}'...")
        
        # ارسال فایل‌ها
        for file in category['files']:
            try:
                await bot_manager.timer_manager.send_with_timer(
                    context, chat_id, file, timer_seconds
                )
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"ارسال فایل خطا: {e}")
                await asyncio.sleep(2)
        
        # ارسال پیام هشدار در صورت فعال بودن تایمر
        if timer_seconds > 0:
            await bot_manager.timer_manager.send_warning(context, chat_id, timer_seconds)
            
    except Exception as e:
        logger.error(f"خطا در ارسال فایل‌ها: {e}")
        await message.reply_text("❌ خطایی در ارسال فایل‌ها رخ داد")

# ========================
# ==== ADMIN COMMANDS ====
# ========================

async def new_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ایجاد دسته جدید"""
    user_id = update.effective_user.id
    if not bot_manager.is_admin(user_id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    if not context.args:
        await update.message.reply_text("لطفا نام دسته را وارد کنید.\nمثال: /new_category نام_دسته")
        return
    
    name = ' '.join(context.args)
    category_id = await bot_manager.db.add_category(name, user_id)
    link = bot_manager.generate_link(category_id)
    
    await update.message.reply_text(
        f"✅ دسته '{name}' ایجاد شد!\n\n"
        f"🔗 لینک دسته:\n{link}\n\n"
        f"برای آپلود فایل:\n/upload {category_id}")

async def upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """شروع آپلود فایل"""
    user_id = update.effective_user.id
    if not bot_manager.is_admin(user_id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    if not context.args:
        await update.message.reply_text("لطفا آیدی دسته را مشخص کنید.\nمثال: /upload CAT_ID")
        return
    
    category_id = context.args[0]
    category = await bot_manager.db.get_category(category_id)
    if not category:
        await update.message.reply_text("❌ دسته یافت نشد!")
        return
    
    bot_manager.pending_uploads[user_id] = {
        'category_id': category_id,
        'files': []
    }
    
    await update.message.reply_text(
        f"📤 حالت آپلود فعال شد! فایل‌ها را ارسال کنید.\n"
        f"برای پایان: /finish_upload\n"
        f"برای لغو: /cancel")
    return UPLOADING

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پردازش فایل‌های ارسالی"""
    user_id = update.effective_user.id
    if user_id not in bot_manager.pending_uploads:
        return
    
    file_info = bot_manager.extract_file_info(update)
    if not file_info:
        await update.message.reply_text("❌ نوع فایل پشتیبانی نمی‌شود!")
        return
    
    upload = bot_manager.pending_uploads[user_id]
    upload['files'].append(file_info)
    
    await update.message.reply_text(f"✅ فایل دریافت شد! (تعداد: {len(upload['files'])})")

async def finish_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پایان آپلود فایل‌ها"""
    user_id = update.effective_user.id
    if user_id not in bot_manager.pending_uploads:
        await update.message.reply_text("❌ هیچ آپلودی فعال نیست!")
        return ConversationHandler.END
    
    upload = bot_manager.pending_uploads.pop(user_id)
    if not upload['files']:
        await update.message.reply_text("❌ فایلی دریافت نشد!")
        return ConversationHandler.END
    
    count = await bot_manager.db.add_files(upload['category_id'], upload['files'])
    link = bot_manager.generate_link(upload['category_id'])
    
    await update.message.reply_text(
        f"✅ {count} فایل با موفقیت ذخیره شد!\n\n"
        f"🔗 لینک دسته:\n{link}")
    return ConversationHandler.END

async def categories_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش لیست دسته‌ها"""
    if not bot_manager.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    categories = await bot_manager.db.get_categories()
    if not categories:
        await update.message.reply_text("📂 هیچ دسته‌ای وجود ندارد!")
        return
    
    message = "📁 لیست دسته‌ها:\n\n"
    for cid, name in categories.items():
        message += f"• {name} [ID: {cid}]\n"
        message += f"  لینک: {bot_manager.generate_link(cid)}\n\n"
    
    await update.message.reply_text(message)

# ========================
# === TIMER MANAGEMENT ===
# ========================

async def set_timer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تنظیم تایمر پیش‌فرض"""
    if not bot_manager.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    try:
        seconds = int(context.args[0])
        if seconds < 0:
            raise ValueError
    except (ValueError, IndexError):
        await update.message.reply_text("❌ مقدار نامعتبر! لطفا عدد ثانیه را وارد کنید.\nمثال: /timer 60")
        return
    
    await bot_manager.db.set_default_timer(seconds)
    status = "✅ تایمر پیش‌فرض تنظیم شد به: " + (
        f"{seconds} ثانیه" if seconds > 0 else "غیرفعال"
    )
    await update.message.reply_text(status)

async def handle_timer_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پردازش ورودی تایمر"""
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    try:
        seconds = int(text)
    except ValueError:
        await update.message.reply_text("❌ لطفا یک عدد وارد کنید!")
        return WAITING_TIMER_INPUT
    
    category_id = context.user_data.get('timer_category')
    if not category_id:
        await update.message.reply_text("❌ خطا در پردازش!")
        return ConversationHandler.END
    
    if seconds == -1:
        await bot_manager.db.set_category_timer(category_id, None)
        await update.message.reply_text("✅ تایمر اختصاصی حذف شد، از تایمر پیش‌فرض استفاده می‌شود")
    else:
        await bot_manager.db.set_category_timer(category_id, seconds)
        status = f"✅ تایمر اختصاصی تنظیم شد به: {seconds} ثانیه" if seconds > 0 else "✅ تایمر غیرفعال شد"
        await update.message.reply_text(status)
    
    # بازگشت به منوی دسته
    await admin_category_menu(update.message, category_id)
    return ConversationHandler.END

# ========================
# === CHANNEL MANAGEMENT ==
# ========================

async def add_channel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """شروع افزودن کانال"""
    if not bot_manager.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    bot_manager.pending_channels[update.effective_user.id] = {}
    await update.message.reply_text(
        "لطفا اطلاعات کانال را به ترتیب ارسال کنید:\n\n"
        "1. آیدی کانال (مثال: -1001234567890)\n"
        "2. نام کانال\n"
        "3. لینک دعوت")
    return WAITING_CHANNEL_INFO

async def handle_channel_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پردازش اطلاعات کانال"""
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    if user_id not in bot_manager.pending_channels:
        return ConversationHandler.END
    
    chan_data = bot_manager.pending_channels[user_id]
    
    if 'channel_id' not in chan_data:
        chan_data['channel_id'] = text
        await update.message.reply_text("✅ آیدی دریافت شد! لطفا نام کانال را ارسال کنید:")
        return WAITING_CHANNEL_INFO
    
    if 'name' not in chan_data:
        chan_data['name'] = text
        await update.message.reply_text("✅ نام دریافت شد! لطفا لینک دعوت را ارسال کنید:")
        return WAITING_CHANNEL_INFO
    
    chan_data['link'] = text
    success = await bot_manager.db.add_channel(
        chan_data['channel_id'], 
        chan_data['name'], 
        chan_data['link']
    )
    
    del bot_manager.pending_channels[user_id]
    
    if success:
        await update.message.reply_text("✅ کانال با موفقیت افزوده شد!")
    else:
        await update.message.reply_text("❌ خطا در افزودن کانال (احتمالا تکراری است)")
    
    return ConversationHandler.END

async def remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """حذف کانال"""
    if not bot_manager.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    if not context.args:
        await update.message.reply_text("لطفا آیدی کانال را مشخص کنید.\nمثال: /remove_channel -1001234567890")
        return
    
    success = await bot_manager.db.delete_channel(context.args[0])
    await update.message.reply_text(
        "✅ کانال حذف شد!" if success else "❌ کانال یافت نشد!")

async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش لیست کانال‌ها"""
    if not bot_manager.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    channels = await bot_manager.db.get_channels()
    if not channels:
        await update.message.reply_text("📢 هیچ کانالی ثبت نشده است!")
        return
    
    message = "📢 کانال‌های اجباری:\n\n"
    for i, ch in enumerate(channels, 1):
        message += (
            f"{i}. {ch['channel_name']}\n"
            f"   آیدی: {ch['channel_id']}\n"
            f"   لینک: {ch['invite_link']}\n\n"
        )
    
    await update.message.reply_text(message)

# ========================
# === BUTTON HANDLERS ====
# ========================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت کلیک روی دکمه‌ها"""
    query = update.callback_query
    await query.answer()
    data = query.data
    
    # بررسی عضویت در کانال‌ها
    if data.startswith('check_'):
        category_id = data[6:]
        user_id = query.from_user.id
        
        # بررسی مجدد عضویت
        channels = await bot_manager.db.get_channels()
        non_joined = []
        for channel in channels:
            is_member = await is_user_member(context, channel['channel_id'], user_id)
            if not is_member:
                non_joined.append(channel)
        
        if non_joined:
            # هنوز در برخی کانال‌ها عضو نیست
            keyboard = []
            for channel in non_joined:
                button = InlineKeyboardButton(
                    text=f"📢 {channel['channel_name']}",
                    url=channel['invite_link']
                )
                keyboard.append([button])
            
            keyboard.append([
                InlineKeyboardButton(
                    "✅ عضو شدم", 
                    callback_data=f"check_{category_id}"
                )
            ])
            
            await query.edit_message_text(
                "⚠️ هنوز در کانال‌های زیر عضو نشده‌اید:",
                reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            # حالا عضو شده است
            await query.edit_message_text("✅ عضویت شما تأیید شد! در حال آماده‌سازی فایل‌ها...")
            await send_category_files(query.message, context, category_id)
        return
    
    # دستورات ادمین
    user_id = query.from_user.id
    if not bot_manager.is_admin(user_id):
        await query.edit_message_text("❌ دسترسی ممنوع!")
        return
    
    if data.startswith('view_'):
        category_id = data[5:]
        await send_category_files(query.message, context, category_id)
    
    elif data.startswith('add_'):
        category_id = data[4:]
        bot_manager.pending_uploads[user_id] = {
            'category_id': category_id,
            'files': []
        }
        await query.edit_message_text(
            "📤 فایل‌ها را ارسال کنید.\n"
            "برای پایان: /finish_upload\n"
            "برای لغو: /cancel")
    
    elif data.startswith('timer_'):
        category_id = data[6:]
        context.user_data['timer_category'] = category_id
        await query.edit_message_text(
            "⏱ لطفا زمان تایمر را به ثانیه وارد کنید:\n"
            "• 0 برای غیرفعال کردن\n"
            "• -1 برای استفاده از تایمر پیش‌فرض\n"
            "• عدد مثبت برای زمان دلخواه (ثانیه)"
        )
        return WAITING_TIMER_INPUT
    
    elif data.startswith('delcat_'):
        category_id = data[7:]
        category = await bot_manager.db.get_category(category_id)
        if not category:
            await query.edit_message_text("❌ دسته یافت نشد!")
            return
        
        # حذف دسته
        async with bot_manager.db.pool.acquire() as conn:
            await conn.execute("DELETE FROM categories WHERE id = $1", category_id)
        
        await query.edit_message_text(f"✅ دسته '{category['name']}' حذف شد!")

# ========================
# === UTILITY HANDLERS ===
# ========================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """لغو عملیات جاری"""
    user_id = update.effective_user.id
    if user_id in bot_manager.pending_uploads:
        del bot_manager.pending_uploads[user_id]
    if user_id in bot_manager.pending_channels:
        del bot_manager.pending_channels[user_id]
    
    await update.message.reply_text("❌ عملیات لغو شد.")
    return ConversationHandler.END

# ========================
# === WEB SERVER SETUP ===
# ========================

async def health_check(request):
    """صفحه سلامت برای بررسی وضعیت ربات"""
    return web.Response(text="🤖 Telegram Bot is Running!")

async def keep_alive():
    """ارسال درخواست به health endpoint هر 5 دقیقه"""
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://final-bot-d3dk.onrender.com/health") as resp:
                    if resp.status == 200:
                        logger.info("✅ Keep-alive ping sent successfully")
                    else:
                        logger.warning(f"⚠️ Keep-alive failed: {resp.status}")
        except Exception as e:
            logger.warning(f"⚠️ Keep-alive exception: {e}")
        
        await asyncio.sleep(450)  # هر ۵ دقیقه (۳۰۰ ثانیه)


async def run_web_server():
    """اجرای سرور وب ساده"""
    app = web.Application()
    app.router.add_get('/health', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 10000)
    await site.start()
    logger.info("Web server started at port 10000")
    
    # اجرای نامحدود
    while True:
        await asyncio.sleep(3600)

# ========================
# ==== BOT SETUP =========
# ========================

async def run_telegram_bot():
    """اجرای اصلی ربات تلگرام"""
    application = Application.builder().token(BOT_TOKEN).build()
    
    # دریافت یوزرنیم ربات
    await application.initialize()
    bot = await application.bot.get_me()
    bot_username = bot.username
    logger.info(f"Bot username: @{bot_username}")
    await bot_manager.init(bot_username)
    
    # دستورات اصلی
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("new_category", new_category))
    application.add_handler(CommandHandler("categories", categories_list))
    application.add_handler(CommandHandler("timer", set_timer_command))
    
    # آپلود فایل‌ها
    upload_handler = ConversationHandler(
        entry_points=[CommandHandler("upload", upload_command)],
        states={
            UPLOADING: [
                MessageHandler(
                    filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO,
                    handle_file
                )
            ]
        },
        fallbacks=[
            CommandHandler("finish_upload", finish_upload),
            CommandHandler("cancel", cancel)
        ]
    )
    application.add_handler(upload_handler)
    
    # مدیریت کانال‌ها
    channel_handler = ConversationHandler(
        entry_points=[CommandHandler("add_channel", add_channel_cmd)],
        states={
            WAITING_CHANNEL_INFO: [MessageHandler(filters.TEXT, handle_channel_info)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(channel_handler)
    application.add_handler(CommandHandler("remove_channel", remove_channel))
    application.add_handler(CommandHandler("channels", list_channels))
    
    # مدیریت تایمر اختصاصی
    timer_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler)],
        states={
            WAITING_TIMER_INPUT: [MessageHandler(filters.TEXT, handle_timer_input)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        map_to_parent={
            ConversationHandler.END: ConversationHandler.END
        }
    )
    application.add_handler(timer_handler)
    
    # دکمه‌های اینلاین
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # اجرای ربات
    logger.info("Starting Telegram bot...")
    await application.start()
    await application.updater.start_polling()
    
    # نگه داشتن ربات در حالت اجرا
    while True:
        await asyncio.sleep(3600)

async def main():
    """اجرای همزمان سرور وب و ربات تلگرام"""
    await asyncio.gather(
        run_web_server(),
        run_telegram_bot(),
        keep_alive()
    )

if __name__ == '__main__':
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.exception(f"Critical error: {e}")
    finally:
        loop.close()
