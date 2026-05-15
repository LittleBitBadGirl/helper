import logging
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from datetime import datetime

from config import BOT_TOKEN, ADMIN_ID
from sqlalchemy import select, update, delete

# Настройка логирования
logging.basicConfig(level=logging.INFO)

from database import init_db, async_session
from models import TaskList, TaskItem

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def _parse_native_checklist(checklist) -> tuple[int, int, list]:
    """Parse Telegram native checklist (Bot API 9.1+, aiogram 3.21+)."""
    tasks = []
    completed = 0
    for task in checklist.tasks:
        is_done = bool(task.completion_date and task.completion_date > 0) or task.completed_by_user is not None
        if is_done:
            completed += 1
        tasks.append((task.text, is_done))
    return len(tasks), completed, tasks


def _parse_text_heuristics(text: str) -> tuple[int, int, list]:
    """Fallback: extract checklist items from plain text via heuristics."""
    total = 0
    completed = 0
    tasks = []
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue
        if "✅" in line or "☑️" in line or "✓" in line:
            completed += 1
            total += 1
            tasks.append((line, True))
        elif (
            "🔘" in line or "⚪" in line or "○" in line
            or (line[0].isdigit() and (line.find('.') != -1 or line.find('/') != -1))
        ):
            total += 1
            tasks.append((line, False))
    return total, completed, tasks


def parse_entities(message: types.Message) -> tuple[int, int, list]:
    """Extract checklist structure: tries native Telegram checklist first, then text heuristics."""
    if message.checklist:
        return _parse_native_checklist(message.checklist)
    if message.text:
        return _parse_text_heuristics(message.text)
    return 0, 0, []

def _raw_text(message: types.Message) -> str:
    """Return a human-readable representation of the message content for storage."""
    if message.checklist:
        lines = [message.checklist.title] + [t.text for t in message.checklist.tasks]
        return "\n".join(lines)
    return message.text or ""


async def save_task_list(message: types.Message):
    total, completed, tasks = parse_entities(message)
    if total == 0:
        return

    sender = message.from_user
    if not sender:
        return  # service messages have no sender; skip

    async with async_session() as session:
        stmt = select(TaskList).where(TaskList.message_id == message.message_id, TaskList.chat_id == message.chat.id)
        result = await session.execute(stmt)
        task_list = result.scalar_one_or_none()

        raw = _raw_text(message)
        if not task_list:
            task_list = TaskList(
                message_id=message.message_id,
                chat_id=message.chat.id,
                user_id=sender.id,
                user_full_name=sender.full_name,
                raw_text=raw,
            )
            session.add(task_list)
            await session.flush()
        else:
            task_list.raw_text = raw
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
    # Skip old-style text service messages about task completion
    if message.text and "поставил(а) отметку о выполнении" in message.text:
        return
    # checklist_tasks_done: service message — the linked checklist message was updated,
    # handle via edited_message handler; nothing to do here directly.
    if message.checklist_tasks_done:
        return
    await save_task_list(message)

@dp.edited_message(F.chat.type.in_({"group", "supergroup"}))
async def handle_group_edit(edited_message: types.Message):
    # Fired when checklist tasks are marked done/undone — message.checklist will have fresh state
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
