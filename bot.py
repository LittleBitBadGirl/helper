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

# Фильтр для ограничения команд
def is_admin(message: types.Message):
    return message.from_user.id == ADMIN_ID

@dp.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: types.Message):
    if not is_admin(message):
        await message.answer("ты не Вера, я тебе не помогу)")
        return
    await message.answer("Привет, Вера! Я — твой личный Аналитик планов. 🕵️‍♀️\n\nЯ тихо собираю данные в группе, а здесь буду отвечать только тебе.\n\nИспользуй /report, чтобы получить срез по задачам.")

@dp.message(Command("report"), F.chat.type == "private")
async def cmd_report(message: types.Message):
    if not is_admin(message):
        await message.answer("ты не Вера, я тебе не помогу)")
        return

    async with async_session() as session:
        # Получаем все списки
        stmt = select(TaskList).order_by(TaskList.user_full_name, TaskList.created_at.desc())
        result = await session.execute(stmt)
        all_lists = result.scalars().all()
        
        if not all_lists:
            await message.answer("Данных пока нет. Я начну собирать их, как только в группе появятся новые списки задач.")
            return

        report = "📊 **Анализ выполнения задач:**\n\n"
        
        users_stats = {}
        for l in all_lists:
            if l.user_full_name not in users_stats:
                users_stats[l.user_full_name] = {"total": 0, "done": 0, "pending_examples": []}
            
            users_stats[l.user_full_name]["total"] += l.total_tasks
            users_stats[l.user_full_name]["done"] += l.completed_tasks
            
            if l.completed_tasks < l.total_tasks:
                items_stmt = select(TaskItem).where(TaskItem.list_id == l.id, TaskItem.is_completed == False).limit(2)
                items_res = await session.execute(items_stmt)
                pending = items_res.scalars().all()
                for p in pending:
                    clean_text = p.text.replace("🔘", "").replace("⚪", "").strip()
                    users_stats[l.user_full_name]["pending_examples"].append(f"• {clean_text}")

        for user, data in users_stats.items():
            percent = (data["done"] / data["total"] * 100) if data["total"] > 0 else 0
            report += f"👤 **{user}**\n"
            report += f"└ Прогресс: {data['done']}/{data['total']} ({percent:.1f}%)\n"
            if data["pending_examples"]:
                report += f"└ Что тянет (примеры):\n" + "\n".join(data["pending_examples"][:3]) + "\n"
            report += "\n"

        await message.answer(report, parse_mode="Markdown")

@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_group_message(message: types.Message):
    # Если это сообщение с текстом '... поставил(а) отметку о выполнении ...'
    if message.text and "поставил(а) отметку о выполнении" in message.text:
        return
    await save_task_list(message)

@dp.edited_message(F.chat.type.in_({"group", "supergroup"}))
async def handle_group_edit(edited_message: types.Message):
    await save_task_list(edited_message)

@dp.message(F.chat.type == "private")
async def handle_private_ignore(message: types.Message):
    if not is_admin(message):
        await message.answer("ты не Вера, я тебе не помогу)")
        return
    await message.answer("Я понимаю только команду /report")

async def main():
    await init_db()
    logging.info("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
