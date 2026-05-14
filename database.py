from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from config import DB_URL
import os
import logging

# Пытаемся создать папку для БД
try:
    os.makedirs("data", exist_ok=True)
    logging.info(f"Current working directory: {os.getcwd()}")
    logging.info(f"Database folder 'data' created/exists. Permissions: {oct(os.stat('data').st_mode)[-3:]}")
except Exception as e:
    logging.error(f"Failed to create data directory: {e}")

# Используем относительный путь для SQLite внутри контейнера
engine = create_async_engine("sqlite+aiosqlite:///data/analytics.db")
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
