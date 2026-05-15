from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
import os
import logging
from config import DB_URL

# Создаем движок
engine = create_async_engine(DB_URL)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

async def init_db():
    # Проверяем путь перед стартом для отладки
    db_path = DB_URL.replace('sqlite+aiosqlite:///', '')
    if not db_path.startswith('/'):
        db_path = os.path.join(os.getcwd(), db_path)
    
    logging.info(f"Initializing DB at: {db_path}")
    
    # Создаем папку если надо
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
