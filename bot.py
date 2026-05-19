import logging
import asyncio
import functools
from collections import Counter, defaultdict
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from datetime import datetime

from config import BOT_TOKEN, ADMIN_ID
from sqlalchemy import select, delete, func

logging.basicConfig(level=logging.INFO)

from database import init_db, async_session
from models import TaskList, TaskItem

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def normalize_task(text: str) -> str:
    return text.lower().strip()


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
    if message.checklist:
        lines = [message.checklist.title] + [t.text for t in message.checklist.tasks]
        return "\n".join(lines)
    return message.text or ""


# ---------------------------------------------------------------------------
# DB write
# ---------------------------------------------------------------------------

async def save_task_list(message: types.Message):
    total, completed, tasks = parse_entities(message)
    if total == 0:
        return

    sender = message.from_user
    if not sender:
        return

    async with async_session() as session:
        stmt = select(TaskList).where(
            TaskList.message_id == message.message_id,
            TaskList.chat_id == message.chat.id,
        )
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


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

async def _get_latest_per_user(session):
    """Return the most recent TaskList for each user_id."""
    subq = (
        select(TaskList.user_id, func.max(TaskList.created_at).label("max_date"))
        .group_by(TaskList.user_id)
        .subquery()
    )
    stmt = (
        select(TaskList)
        .join(subq, (TaskList.user_id == subq.c.user_id) & (TaskList.created_at == subq.c.max_date))
        .order_by(TaskList.user_full_name)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def _pending_items(session, list_id: int) -> list:
    stmt = select(TaskItem).where(TaskItem.list_id == list_id, TaskItem.is_completed == False)
    result = await session.execute(stmt)
    return result.scalars().all()


async def _all_lists_for_user(session, user_id: int) -> list:
    stmt = select(TaskList).where(TaskList.user_id == user_id).order_by(TaskList.created_at.desc())
    result = await session.execute(stmt)
    return result.scalars().all()


async def _all_items_for_user(session, user_id: int) -> list:
    """All TaskItems across all task lists of a user, with created_at from their parent list."""
    stmt = (
        select(TaskItem, TaskList.created_at)
        .join(TaskList, TaskItem.list_id == TaskList.id)
        .where(TaskList.user_id == user_id)
        .order_by(TaskList.created_at.desc())
    )
    result = await session.execute(stmt)
    return result.all()  # list of (TaskItem, datetime)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def is_admin(message: types.Message) -> bool:
    return message.from_user.id == ADMIN_ID


def _guard(fn):
    """Decorator: reject non-admins in private chat."""
    @functools.wraps(fn)
    async def wrapper(message: types.Message, *args, **kwargs):
        if not is_admin(message):
            await message.answer("ты не Вера, я тебе не помогу)")
            return
        return await fn(message, *args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# /start  /help
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "🤖 *Команды бота:*\n\n"
    "📊 /report — текущий срез\n"
    "Последний чек\\-лист каждого участника: прогресс и незакрытые задачи\\.\n\n"
    "🌙 /eod — итоги дня\n"
    "Кто что не закрыл\\. Задачи, которые висят 2\\+ дня — помечены 🟡, 3\\+ — 🔴\\.\n\n"
    "🔍 /person \\[имя\\] — аналитика по человеку\n"
    "Среднее выполнение, хронические задачи \\(3\\+ раз не закрыты\\), задачи которые тихо исчезли незакрытыми\\.\n"
    "_Пример:_ /person Герман\n\n"
    "❓ /help — это сообщение"
)


@dp.message(Command("start"), F.chat.type == "private")
@_guard
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет, Вера\\! Я — твой личный Аналитик планов 🕵️‍♀️\n\n"
        "Тихо слежу за чек\\-листами в группе, тебе отвечаю здесь\\.\n\n"
        + HELP_TEXT,
        parse_mode="MarkdownV2",
    )


@dp.message(Command("help"), F.chat.type == "private")
@_guard
async def cmd_help(message: types.Message):
    await message.answer(HELP_TEXT, parse_mode="MarkdownV2")


# ---------------------------------------------------------------------------
# /report — актуальный срез
# ---------------------------------------------------------------------------

@dp.message(Command("report"), F.chat.type == "private")
@_guard
async def cmd_report(message: types.Message):
    async with async_session() as session:
        latest_lists = await _get_latest_per_user(session)

        if not latest_lists:
            await message.answer("Данных пока нет. Я начну собирать их, как только в группе появятся новые списки задач.")
            return

        report = "📊 **Анализ выполнения задач:**\n\n"
        for tl in latest_lists:
            percent = (tl.completed_tasks / tl.total_tasks * 100) if tl.total_tasks > 0 else 0
            date_str = tl.created_at.strftime("%d.%m") if tl.created_at else "?"
            report += f"👤 **{tl.user_full_name}** _(список от {date_str})_\n"
            report += f"└ Прогресс: {tl.completed_tasks}/{tl.total_tasks} ({percent:.1f}%)\n"

            pending = await _pending_items(session, tl.id)
            if pending:
                report += "└ Что тянет:\n" + "\n".join(f"• {p.text}" for p in pending[:3]) + "\n"

            report += "\n"

        await message.answer(report, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /eod — итоги дня
# ---------------------------------------------------------------------------

@dp.message(Command("eod"), F.chat.type == "private")
@_guard
async def cmd_eod(message: types.Message):
    async with async_session() as session:
        latest_lists = await _get_latest_per_user(session)

        if not latest_lists:
            await message.answer("Данных пока нет.")
            return

        report = "🌙 **Итоги дня:**\n\n"
        all_done = True

        for tl in latest_lists:
            pending = await _pending_items(session, tl.id)
            if not pending:
                continue

            all_done = False
            date_str = tl.created_at.strftime("%d.%m") if tl.created_at else "?"
            report += f"👤 **{tl.user_full_name}** — {tl.completed_tasks}/{tl.total_tasks} _(от {date_str})_\n"

            # For each incomplete task, count how many OTHER lists of this user also had it incomplete
            all_rows = await _all_items_for_user(session, tl.user_id)
            # Build: normalized_text → count of lists where it appeared as incomplete (excluding current list)
            other_incomplete: Counter = Counter()
            for item, _ in all_rows:
                if item.list_id != tl.id and not item.is_completed:
                    other_incomplete[normalize_task(item.text)] += 1

            for item in pending:
                key = normalize_task(item.text)
                prev_count = other_incomplete[key]
                total_days = prev_count + 1  # today + previous appearances

                if total_days >= 3:
                    marker = f" 🔴 _{total_days} раза в списках_"
                elif total_days == 2:
                    marker = " 🟡 _2-й раз_"
                else:
                    marker = ""

                report += f"• {item.text}{marker}\n"

            report += "\n"

        if all_done:
            report = "✅ Все задачи закрыты! Отличный день."

        await message.answer(report, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /person [имя] — история по участнику
# ---------------------------------------------------------------------------

@dp.message(Command("person"), F.chat.type == "private")
@_guard
async def cmd_person(message: types.Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Укажи имя: /person Герман")
        return

    name_query = args[1].strip().lower()

    async with async_session() as session:
        # Find matching user
        stmt = select(TaskList.user_id, TaskList.user_full_name).distinct()
        result = await session.execute(stmt)
        all_users = result.all()

        match = next(
            ((uid, name) for uid, name in all_users if name_query in name.lower()),
            None,
        )
        if not match:
            await message.answer(f"Не нашла участника «{args[1]}». Попробуй часть имени, например: /person Герман")
            return

        user_id, user_full_name = match
        all_lists = await _all_lists_for_user(session, user_id)

        if not all_lists:
            await message.answer(f"Нет данных по {user_full_name}.")
            return

        all_rows = await _all_items_for_user(session, user_id)  # (TaskItem, created_at)

        # --- Average completion ---
        total_tasks = sum(tl.total_tasks for tl in all_lists)
        done_tasks = sum(tl.completed_tasks for tl in all_lists)
        avg_pct = (done_tasks / total_tasks * 100) if total_tasks > 0 else 0

        # --- Monthly breakdown ---
        monthly: dict = defaultdict(lambda: {"total": 0, "done": 0})
        for tl in all_lists:
            if tl.created_at:
                key = tl.created_at.strftime("%m.%Y")
                monthly[key]["total"] += tl.total_tasks
                monthly[key]["done"] += tl.completed_tasks

        # --- Chronic tasks: incomplete 3+ times ---
        incomplete_counts: Counter = Counter()
        for item, _ in all_rows:
            if not item.is_completed:
                incomplete_counts[normalize_task(item.text)] += 1

        # Canonical text (original case) for each normalized key
        canonical: dict[str, str] = {}
        for item, _ in all_rows:
            key = normalize_task(item.text)
            if key not in canonical:
                canonical[key] = item.text

        chronic = [(canonical[k], cnt) for k, cnt in incomplete_counts.most_common() if cnt >= 3]

        # --- Dropped tasks: incomplete in older lists, absent from latest list, never completed ---
        latest_list = all_lists[0]
        latest_texts: set[str] = set()
        ever_completed: set[str] = set()

        for item, _ in all_rows:
            key = normalize_task(item.text)
            if item.is_completed:
                ever_completed.add(key)
            if item.list_id == latest_list.id:
                latest_texts.add(key)

        # Collect incomplete tasks from older lists that disappeared
        dropped_seen: dict[str, tuple[str, datetime]] = {}
        for item, created_at in all_rows:
            if item.list_id == latest_list.id:
                continue
            key = normalize_task(item.text)
            if not item.is_completed and key not in latest_texts and key not in ever_completed:
                if key not in dropped_seen:
                    dropped_seen[key] = (item.text, created_at)

        dropped = list(dropped_seen.values())  # (text, last_seen_date)

        # --- Build report ---
        report = f"🔍 **Аналитика: {user_full_name}**\n\n"
        report += f"📋 Списков в базе: {len(all_lists)}\n"
        report += f"📊 Среднее выполнение: {avg_pct:.1f}%\n"
        report += f"✅ Всего: {done_tasks}/{total_tasks} задач закрыто\n\n"

        if len(monthly) > 1:
            report += "📅 **По месяцам:**\n"
            for month_key in sorted(monthly):
                d = monthly[month_key]
                pct = (d["done"] / d["total"] * 100) if d["total"] > 0 else 0
                report += f"• {month_key}: {d['done']}/{d['total']} ({pct:.1f}%)\n"
            report += "\n"

        if chronic:
            report += "🔴 **Хронические задачи** (3+ раз не закрыты):\n"
            for text, cnt in chronic[:7]:
                report += f"• {text} — {cnt}x\n"
            report += "\n"

        if dropped:
            shown = dropped[:5]
            total_dropped = len(dropped)
            report += f"👻 **Исчезли незакрытыми** ({total_dropped} шт.):\n"
            for text, dt in shown:
                date_str = dt.strftime("%d.%m") if dt else "?"
                report += f"• {text} _(последний раз {date_str})_\n"
            if total_dropped > 5:
                report += f"_...и ещё {total_dropped - 5}_\n"
            report += "\n"

        if not chronic and not dropped:
            report += "🟢 Хронических и брошенных задач не обнаружено.\n"

        await message.answer(report, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Group message handlers
# ---------------------------------------------------------------------------

@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_group_message(message: types.Message):
    if message.text and "поставил(а) отметку о выполнении" in message.text:
        return
    if message.checklist_tasks_done:
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
    await message.answer("Я понимаю команды: /report, /eod, /person [имя], /help")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    await init_db()
    logging.info("Бот запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
