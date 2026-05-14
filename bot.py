import logging
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from datetime import datetime

from config import BOT_TOKEN, ADMIN_ID
from database import init_db, async_session
from models import TaskList, TaskItem
from sqlalchemy import select, update, delete

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def parse_entities(message: types.Message):
    """
    Пытаемся вытащить структуру чек-листа из текста.
    """
    total = 0
    completed = 0
    tasks = []
    
    if not message.text:
        return total, completed, tasks

    lines = message.text.split('\n')
    for line in lines:
        line = line.strip()
        if not line: continue
        
        # Эвристика для чек-листов
        is_done = False
        if "✅" in line or "☑️" in line or "✓" in line:
            is_done = True
            completed += 1
            total += 1
            tasks.append((line, True))
        elif "🔘" in line or "⚪" in line or "○" in line or (line[0].isdigit() and (line.find('.') != -1 or line.find('/') != -1)):
            total += 1
            tasks.append((line, False))
            
    return total, completed, tasks

async def save_task_list(message: types.Message):
    total, completed, tasks = parse_entities(message)
    if total == 0: return # Не список или не распознали

    async with async_session() as session:
        # Проверяем, есть ли уже такой список
        stmt = select(TaskList).where(TaskList.message_id == message.message_id, TaskList.chat_id == message.chat.id)
        result = await session.execute(stmt)
        task_list = result.scalar_one_or_none()

        if not task_list:
            task_list = TaskList(
                message_id=message.message_id,
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                user_full_name=message.from_user.full_name,
                raw_text=message.text
            )
            session.add(task_list)
            await session.flush()
        else:
            task_list.raw_text = message.text
            # Очищаем старые пункты
            await session.execute(delete(TaskItem).where(TaskItem.list_id == task_list.id))

        task_list.total_tasks = total
        task_list.completed_tasks = completed
        
        for t_text, t_done in tasks:
            session.add(TaskItem(list_id=task_list.id, text=t_text, is_completed=t_done))
            
        await session.commit()
        logging.info(f"Updated task list from {task_list.user_full_name}: {completed}/{total}")

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет! Я — Аналитик планов. Добавь меня в группу, сделай админом, и я буду следить за выполнением задач. 🚀")

@dp.message(F.text)
async def handle_message(message: types.Message):
    # Если это сообщение с текстом '... поставил(а) отметку о выполнении ...'
    # Это системное уведомление от бота 'Далее' или нативного функционала ТГ.
    # Мы можем использовать это как сигнал к тому, что нужно перечитать последний список пользователя.
    if "поставил(а) отметку о выполнении" in message.text:
        logging.info(f"Detected task completion notification: {message.text}")
        # Здесь можно добавить логику поиска сообщения, к которому это относится, 
        # но проще всего дождаться edited_message от самого списка.
        return

    await save_task_list(message)

@dp.edited_message(F.text)
async def handle_edited_message(edited_message: types.Message):
    await save_task_list(edited_message)

@dp.message(Command("report"))
async def cmd_report(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    
    async with async_session() as session:
        stmt = select(TaskList).order_by(TaskList.created_at.desc()).limit(10)
        result = await session.execute(stmt)
        lists = result.scalars().all()
        
        if not lists:
            await message.answer("Пока нет данных для отчета.")
            return
            
        report = "📊 **Последние 10 списков задач:**\n\n"
        for l in lists:
            status = "✅" if l.completed_tasks == l.total_tasks and l.total_tasks > 0 else "⏳"
            report += f"{status} {l.user_full_name} ({l.created_at.strftime('%d.%m')}): {l.completed_tasks}/{l.total_tasks}\n"
            
        await message.answer(report, parse_mode="Markdown")

async def main():
    await init_db()
    logging.info("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
