import asyncpg
import os
import uuid
import logging

logger = logging.getLogger(__name__)

class Database:
    """مدیریت دیتابیس PostgreSQL"""
    
    def __init__(self):
        self.pool = None

    async def connect(self):
        """اتصال به دیتابیس"""
        self.pool = await asyncpg.create_pool(os.getenv('DATABASE_URL'))
        await self.init_db()
    
    async def init_db(self):
        """ایجاد جداول مورد نیاز"""
        async with self.pool.acquire() as conn:
            # جداول اصلی
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS categories (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    created_by BIGINT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    timer INTEGER
                )
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS files (
                    id SERIAL PRIMARY KEY,
                    category_id TEXT NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
                    file_id TEXT NOT NULL UNIQUE,
                    file_name TEXT NOT NULL,
                    file_size BIGINT NOT NULL,
                    file_type TEXT NOT NULL,
                    caption TEXT,
                    upload_date TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS channels (
                    id SERIAL PRIMARY KEY,
                    channel_id TEXT NOT NULL UNIQUE,
                    channel_name TEXT NOT NULL,
                    invite_link TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            # جدول تنظیمات تایمر
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS global_settings (
                    id SERIAL PRIMARY KEY,
                    default_timer INTEGER NOT NULL DEFAULT 0
                )
            ''')
            
            # درج مقدار پیش‌فرض
            await conn.execute('''
                INSERT INTO global_settings (id, default_timer)
                VALUES (1, 0)
                ON CONFLICT (id) DO NOTHING
            ''')
            
            # ایندکس‌ها
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_files_category ON files(category_id)')
            logger.info("Database initialized")

    # --- مدیریت دسته‌ها ---
    async def add_category(self, name: str, created_by: int) -> str:
        """ایجاد دسته جدید"""
        category_id = str(uuid.uuid4())[:8]
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO categories(id, name, created_by) VALUES($1, $2, $3)",
                category_id, name, created_by
            )
        return category_id
    
    async def get_categories(self) -> dict:
        """دریافت تمام دسته‌ها"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, name FROM categories")
            return {row['id']: row['name'] for row in rows}
    
    async def get_category(self, category_id: str) -> dict:
        """دریافت اطلاعات یک دسته"""
        async with self.pool.acquire() as conn:
            category = await conn.fetchrow(
                "SELECT name, created_by, timer FROM categories WHERE id = $1", category_id
            )
            if not category:
                return None
                
            files = await conn.fetch(
                "SELECT file_id, file_type, caption FROM files WHERE category_id = $1", category_id
            )
            return {
                'name': category['name'],
                'files': [dict(file) for file in files],
                'timer': category['timer']
            }

    # --- مدیریت فایل‌ها ---
    async def add_file(self, category_id: str, file_info: dict) -> bool:
        """افزودن فایل به دسته"""
        async with self.pool.acquire() as conn:
            try:
                await conn.execute(
                    "INSERT INTO files(category_id, file_id, file_name, file_size, file_type, caption) "
                    "VALUES($1, $2, $3, $4, $5, $6)",
                    category_id,
                    file_info['file_id'],
                    file_info['file_name'],
                    file_info['file_size'],
                    file_info['file_type'],
                    file_info.get('caption', '')
                )
                return True
            except asyncpg.UniqueViolationError:
                return False
    
    async def add_files(self, category_id: str, files: list) -> int:
        async with self.pool.acquire() as conn:
            inserted_count = 0
            for f in files:
                try:
                    await conn.execute(
                        "INSERT INTO files(category_id, file_id, file_name, file_size, file_type, caption) "
                        "VALUES($1, $2, $3, $4, $5, $6)",
                        category_id,
                        f['file_id'],
                        f['file_name'],
                        f['file_size'],
                        f['file_type'],
                        f.get('caption', '')
                    )
                    inserted_count += 1
                except asyncpg.UniqueViolationError:
                    continue
            return inserted_count

    # --- مدیریت کانال‌ها ---
    async def add_channel(self, channel_id: str, name: str, link: str) -> bool:
        """افزودن کانال اجباری"""
        async with self.pool.acquire() as conn:
            try:
                await conn.execute(
                    "INSERT INTO channels(channel_id, channel_name, invite_link) VALUES($1, $2, $3)",
                    channel_id, name, link
                )
                return True
            except asyncpg.UniqueViolationError:
                return False
    
    async def get_channels(self) -> list:
        """دریافت لیست کانال‌ها"""
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT channel_id, channel_name, invite_link FROM channels")
    
    async def delete_channel(self, channel_id: str) -> bool:
        """حذف کانال"""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM channels WHERE channel_id = $1", channel_id
            )
            return result.split()[-1] == '1'

    # --- مدیریت تایمر ---
    async def set_default_timer(self, seconds: int):
        """تنظیم تایمر پیش‌فرض"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE global_settings SET default_timer = $1 WHERE id = 1",
                seconds
            )
    
    async def get_default_timer(self) -> int:
        """دریافت تایمر پیش‌فرض"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT default_timer FROM global_settings WHERE id = 1")
            return row['default_timer'] if row else 0
    
    async def set_category_timer(self, category_id: str, seconds: int):
        """تنظیم تایمر اختصاصی برای دسته"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE categories SET timer = $1 WHERE id = $2",
                seconds, category_id
            )
    
    async def get_category_timer(self, category_id: str) -> int:
        """دریافت تایمر اختصاصی دسته"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT timer FROM categories WHERE id = $1", category_id)
            return row['timer'] if row and row['timer'] is not None else -1