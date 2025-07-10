import asyncio
import logging
from telegram import Message
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

class TimerManager:
    """مدیریت تایمر برای حذف خودکار پیام‌ها"""
    
    def __init__(self, db):
        self.db = db
    
    async def get_effective_timer(self, category_id: str) -> int:
        """دریافت تایمر مؤثر برای دسته"""
        category_timer = await self.db.get_category_timer(category_id)
        if category_timer >= 0:  # اگر برای دسته تایمر تنظیم شده
            return category_timer
        return await self.db.get_default_timer()  # تایمر پیش‌فرض
    
    async def schedule_deletion(self, context: ContextTypes.DEFAULT_TYPE, message: Message, delay: int):
        """زمان‌بندی حذف پیام پس از تاخیر"""
        if delay <= 0:
            return
        
        try:
            await asyncio.sleep(delay)
            await context.bot.delete_message(
                chat_id=message.chat_id,
                message_id=message.message_id
            )
        except Exception as e:
            logger.warning(f"حذف پیام ناموفق: {e}")
    
    async def send_with_timer(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, file_info: dict, timer_seconds: int) -> Message:
        """ارسال فایل با قابلیت تایمر"""
        file_type = file_info['file_type']
        send_func = {
            'document': context.bot.send_document,
            'photo': context.bot.send_photo,
            'video': context.bot.send_video,
            'audio': context.bot.send_audio
        }.get(file_type)
        
        if not send_func:
            return None
        
        try:
            # ارسال فایل
            sent_message = await send_func(
                chat_id=chat_id,
                **{file_type: file_info['file_id']},
                caption=file_info.get('caption', '')[:1024]
            )
            
            # زمان‌بندی حذف
            if timer_seconds > 0:
                asyncio.create_task(self.schedule_deletion(context, sent_message, timer_seconds))
            
            return sent_message
        except Exception as e:
            logger.error(f"ارسال فایل {file_type} خطا: {e}")
            return None
    
    async def send_warning(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, timer_seconds: int) -> Message:
        """ارسال پیام هشدار تایمر"""
        if timer_seconds <= 0:
            return None
        
        try:
            warning_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ توجه: فایل‌های ارسال شده به صورت خودکار بعد از {timer_seconds} ثانیه حذف خواهند شد.\n"
                     "لطفاً آن‌ها را به پیام‌های ذخیره شده خود ارسال کنید."
            )
            # زمان‌بندی حذف هشدار
            if timer_seconds > 0:
                asyncio.create_task(self.schedule_deletion(context, warning_msg, timer_seconds))
            
            return warning_msg
        except Exception as e:
            logger.error(f"ارسال هشدار خطا: {e}")
            return None