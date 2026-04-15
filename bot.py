import asyncio
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChosenInlineResultHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)
from telegram import InlineQueryResultArticle, InputTextMessageContent


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "quiz_bot.db"
QUESTIONS_PATH = BASE_DIR / "questions.json"
QUESTION_TIMEOUT_SECONDS = 20
QUICK_CLOSE_SECONDS = 3
BOT_USERNAME = "Quiby_bot"

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Question:
    id: str
    question: str
    left_option: str
    right_option: str
    correct_option: str
    explanation: str
    left_year: Optional[int] = None
    right_year: Optional[int] = None


def load_env() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def load_questions() -> list[Question]:
    payload = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    return [Question(**item) for item in payload]


class QuizStorage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS players (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT NOT NULL,
                    total_answers INTEGER NOT NULL DEFAULT 0,
                    correct_answers INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS chat_player_stats (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    answers INTEGER NOT NULL DEFAULT 0,
                    correct_answers INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (chat_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS asked_questions (
                    chat_id INTEGER NOT NULL,
                    question_id TEXT NOT NULL,
                    asked_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, question_id)
                );

                CREATE TABLE IF NOT EXISTS active_questions (
                    chat_id INTEGER PRIMARY KEY,
                    question_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    is_closed INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS inline_active_questions (
                    inline_message_id TEXT PRIMARY KEY,
                    question_id TEXT NOT NULL,
                    is_closed INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS inline_asked_questions (
                    chat_instance TEXT NOT NULL,
                    question_id TEXT NOT NULL,
                    asked_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_instance, question_id)
                );

                CREATE TABLE IF NOT EXISTS inline_chat_player_stats (
                    chat_instance TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    answers INTEGER NOT NULL DEFAULT 0,
                    correct_answers INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (chat_instance, user_id)
                );
                """
            )
            conn.execute("DROP TABLE IF EXISTS answers")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS answers (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    question_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    selected_option TEXT NOT NULL,
                    answered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, message_id, user_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inline_answers (
                    inline_message_id TEXT NOT NULL,
                    question_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    selected_option TEXT NOT NULL,
                    answered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (inline_message_id, user_id)
                )
                """
            )

    def save_player(self, user_id: int, username: Optional[str], full_name: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO players (user_id, username, full_name)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    full_name = excluded.full_name
                """,
                (user_id, username, full_name),
            )

    def used_question_ids(self, chat_id: int) -> set[str]:
        with self._connect() as conn:
            # Возвращаем только вопросы, заданные сегодня (с 00:00 UTC)
            rows = conn.execute(
                """
                SELECT question_id FROM asked_questions
                WHERE chat_id = ?
                AND date(asked_at) = date('now')
                """,
                (chat_id,),
            ).fetchall()
        return {row["question_id"] for row in rows}

    def reset_question_history(self, chat_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM asked_questions WHERE chat_id = ?", (chat_id,))

    def mark_question_asked(self, chat_id: int, question_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO asked_questions (chat_id, question_id) VALUES (?, ?)",
                (chat_id, question_id),
            )

    def set_active_question(self, chat_id: int, question_id: str, message_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO active_questions (chat_id, question_id, message_id, is_closed)
                VALUES (?, ?, ?, 0)
                ON CONFLICT(chat_id) DO UPDATE SET
                    question_id = excluded.question_id,
                    message_id = excluded.message_id,
                    is_closed = 0
                """,
                (chat_id, question_id, message_id),
            )

    def get_active_question(self, chat_id: int) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM active_questions WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()

    def close_active_question(self, chat_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE active_questions SET is_closed = 1 WHERE chat_id = ?", (chat_id,))

    def reopen_stale_question_if_needed(self, chat_id: int) -> bool:
        active = self.get_active_question(chat_id)
        if not active or active["is_closed"] == 1:
            return False

        self.close_active_question(chat_id)
        return True

    def close_all_active_questions(self) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE active_questions SET is_closed = 1 WHERE is_closed = 0")

    def record_answer(
        self,
        chat_id: int,
        message_id: int,
        question_id: str,
        user_id: int,
        username: Optional[str],
        full_name: str,
        selected_option: str,
        is_correct: bool,
    ) -> bool:
        self.save_player(user_id, username, full_name)
        with self._connect() as conn:
            inserted = conn.execute(
                """
                INSERT OR IGNORE INTO answers (chat_id, message_id, question_id, user_id, selected_option)
                VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, message_id, question_id, user_id, selected_option),
            )
            if inserted.rowcount == 0:
                return False

            conn.execute(
                """
                UPDATE players
                SET total_answers = total_answers + 1,
                    correct_answers = correct_answers + ?
                WHERE user_id = ?
                """,
                (1 if is_correct else 0, user_id),
            )
            conn.execute(
                """
                INSERT INTO chat_player_stats (chat_id, user_id, answers, correct_answers)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                    answers = answers + 1,
                    correct_answers = correct_answers + excluded.correct_answers
                """,
                (chat_id, user_id, 1 if is_correct else 0),
            )
        return True

    def get_player_stats(self, user_id: int) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM players WHERE user_id = ?", (user_id,)).fetchone()
    
    def get_player_stats_today(self, user_id: int) -> tuple[int, int]:
        with self._connect() as conn:
            # Считаем общее количество ответов с начала текущего дня UTC (00:00 UTC)
            result = conn.execute(
                """
                SELECT COUNT(*) as total
                FROM answers
                WHERE user_id = ?
                AND date(answered_at) = date('now')
                """,
                (user_id,)
            ).fetchone()

            inline_result = conn.execute(
                """
                SELECT COUNT(*) as total
                FROM inline_answers
                WHERE user_id = ?
                AND date(answered_at) = date('now')
                """,
                (user_id,)
            ).fetchone()

            total_today = (result["total"] or 0) + (inline_result["total"] or 0)

            if total_today == 0:
                return (0, 0)

            # Считаем правильные ответы за сегодня, проверяя каждый ответ
            correct_count = 0

            answers_today = conn.execute(
                """
                SELECT question_id, selected_option
                FROM answers
                WHERE user_id = ?
                AND date(answered_at) = date('now')
                """,
                (user_id,)
            ).fetchall()

            for ans in answers_today:
                question = quiz_game.questions_by_id.get(ans["question_id"])
                if question and ans["selected_option"] == question.correct_option:
                    correct_count += 1

            inline_answers_today = conn.execute(
                """
                SELECT question_id, selected_option
                FROM inline_answers
                WHERE user_id = ?
                AND date(answered_at) = date('now')
                """,
                (user_id,)
            ).fetchall()

            for ans in inline_answers_today:
                question = quiz_game.questions_by_id.get(ans["question_id"])
                if question and ans["selected_option"] == question.correct_option:
                    correct_count += 1

            return (correct_count, total_today)

    def get_chat_top(self, chat_id: int, limit: int = 10) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT p.full_name, cps.answers, cps.correct_answers
                FROM chat_player_stats cps
                JOIN players p ON p.user_id = cps.user_id
                WHERE cps.chat_id = ?
                ORDER BY cps.correct_answers DESC, cps.answers DESC, p.full_name ASC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()

    def get_chat_stats(self, chat_id: int) -> sqlite3.Row:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT
                    COUNT(*) AS players_count,
                    COALESCE(SUM(answers), 0) AS total_answers,
                    COALESCE(SUM(correct_answers), 0) AS correct_answers
                FROM chat_player_stats
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()

    def get_question_answer_stats(self, chat_id: int, message_id: int) -> sqlite3.Row:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT
                    COUNT(*) AS total_answers,
                    SUM(CASE WHEN selected_option = 'left' THEN 1 ELSE 0 END) AS left_answers,
                    SUM(CASE WHEN selected_option = 'right' THEN 1 ELSE 0 END) AS right_answers
                FROM answers
                WHERE chat_id = ? AND message_id = ?
                """,
                (chat_id, message_id),
            ).fetchone()

    def get_round_answers(self, chat_id: int, message_id: int) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT
                    a.selected_option,
                    a.user_id,
                    p.full_name
                FROM answers a
                JOIN players p ON p.user_id = a.user_id
                WHERE a.chat_id = ? AND a.message_id = ?
                ORDER BY a.answered_at ASC, p.full_name ASC
                """,
                (chat_id, message_id),
            ).fetchall()

    def count_unique_players(self, chat_id: int, message_id: int) -> int:
        """Подсчитывает количество уникальных игроков, ответивших на вопрос"""
        with self._connect() as conn:
            result = conn.execute(
                "SELECT COUNT(DISTINCT user_id) as count FROM answers WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            ).fetchone()
            return result["count"] if result else 0

    def inline_used_question_ids(self, chat_instance: str) -> set[str]:
        with self._connect() as conn:
            # Возвращаем только вопросы, заданные сегодня (с 00:00 UTC)
            rows = conn.execute(
                """
                SELECT question_id FROM inline_asked_questions
                WHERE chat_instance = ?
                AND date(asked_at) = date('now')
                """,
                (chat_instance,),
            ).fetchall()
        return {row["question_id"] for row in rows}

    def inline_mark_question_asked(self, chat_instance: str, question_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO inline_asked_questions (chat_instance, question_id) VALUES (?, ?)",
                (chat_instance, question_id),
            )

    def set_inline_active_question(self, inline_message_id: str, question_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO inline_active_questions (inline_message_id, question_id, is_closed)
                VALUES (?, ?, 0)
                ON CONFLICT(inline_message_id) DO UPDATE SET
                    question_id = excluded.question_id,
                    is_closed = 0
                """,
                (inline_message_id, question_id),
            )

    def get_inline_active_question(self, inline_message_id: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM inline_active_questions WHERE inline_message_id = ?",
                (inline_message_id,),
            ).fetchone()

    def close_inline_active_question(self, inline_message_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE inline_active_questions SET is_closed = 1 WHERE inline_message_id = ?",
                (inline_message_id,),
            )

    def record_inline_answer(
        self,
        inline_message_id: str,
        chat_instance: str,
        question_id: str,
        user_id: int,
        username: Optional[str],
        full_name: str,
        selected_option: str,
        is_correct: bool,
    ) -> bool:
        self.save_player(user_id, username, full_name)
        with self._connect() as conn:
            inserted = conn.execute(
                """
                INSERT OR IGNORE INTO inline_answers (inline_message_id, question_id, user_id, selected_option)
                VALUES (?, ?, ?, ?)
                """,
                (inline_message_id, question_id, user_id, selected_option),
            )
            if inserted.rowcount == 0:
                return False

            conn.execute(
                """
                UPDATE players
                SET total_answers = total_answers + 1,
                    correct_answers = correct_answers + ?
                WHERE user_id = ?
                """,
                (1 if is_correct else 0, user_id),
            )
            conn.execute(
                """
                INSERT INTO inline_chat_player_stats (chat_instance, user_id, answers, correct_answers)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(chat_instance, user_id) DO UPDATE SET
                    answers = answers + 1,
                    correct_answers = correct_answers + excluded.correct_answers
                """,
                (chat_instance, user_id, 1 if is_correct else 0),
            )
        return True

    def get_inline_question_answer_stats(self, inline_message_id: str) -> sqlite3.Row:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT
                    COUNT(*) AS total_answers,
                    SUM(CASE WHEN selected_option = 'left' THEN 1 ELSE 0 END) AS left_answers,
                    SUM(CASE WHEN selected_option = 'right' THEN 1 ELSE 0 END) AS right_answers
                FROM inline_answers
                WHERE inline_message_id = ?
                """,
                (inline_message_id,),
            ).fetchone()

    def get_inline_round_answers(self, inline_message_id: str) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT
                    ia.selected_option,
                    ia.user_id,
                    p.full_name
                FROM inline_answers ia
                JOIN players p ON p.user_id = ia.user_id
                WHERE ia.inline_message_id = ?
                ORDER BY ia.answered_at ASC, p.full_name ASC
                """,
                (inline_message_id,),
            ).fetchall()


class QuizGame:
    def __init__(self) -> None:
        self.storage = QuizStorage(DB_PATH)
        self.questions = load_questions()
        self.questions_by_id = {question.id: question for question in self.questions}

    def next_question(self, chat_id: int) -> Optional[Question]:
        import random
        used_ids = self.storage.used_question_ids(chat_id)
        available = [question for question in self.questions if question.id not in used_ids]
        if not available:
            return None

        question = random.choice(available)
        self.storage.mark_question_asked(chat_id, question.id)
        return question

    def next_inline_question(self, chat_instance: str, seed: int) -> Question:
        import random
        used_ids = self.storage.inline_used_question_ids(chat_instance)
        available = [question for question in self.questions if question.id not in used_ids]
        if not available:
            available = list(self.questions)

        random.seed(seed + hash(chat_instance))
        return random.choice(available)


quiz_game = QuizGame()
close_tasks: dict[int, asyncio.Task] = {}
inline_close_tasks: dict[str, asyncio.Task] = {}


def accuracy(correct_answers: int, total_answers: int) -> str:
    if total_answers == 0:
        return "0%"
    return f"{(correct_answers / total_answers) * 100:.1f}%"


def infer_option_years(question: Question) -> tuple[Optional[int], Optional[int]]:
    if question.left_year or question.right_year:
        return question.left_year, question.right_year

    explanation = question.explanation
    years = [(match.start(), int(match.group())) for match in re.finditer(r"(19|20)\d{2}", explanation)]
    if not years:
        return None, None

    def pick_year(option: str) -> Optional[int]:
        option_pos = explanation.lower().find(option.lower())
        if option_pos == -1:
            return None

        for pos, year in years:
            if pos >= option_pos:
                return year

        return min(years, key=lambda item: abs(item[0] - option_pos))[1]

    return pick_year(question.left_option), pick_year(question.right_option)


def question_text(question: Question) -> str:
    return (
        f"<b>{escape(question.question)}</b>\n\n"
        f"1. {escape(question.left_option)}\n"
        f"2. {escape(question.right_option)}\n\n"
        "Выбери вариант ниже."
    )


def question_keyboard(question_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("1", callback_data=f"answer:{question_id}:left"),
            InlineKeyboardButton("2", callback_data=f"answer:{question_id}:right"),
        ]]
    )

def next_question_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("➡️ Следующий вопрос", callback_data="next_question")]]
    )


def build_result_text(question: Question, answer_stats: sqlite3.Row) -> str:
    correct_num = "1" if question.correct_option == "left" else "2"
    correct_value = question.left_option if question.correct_option == "left" else question.right_option

    return (
        f"<b>{escape(question.question)}</b>\n\n"
        f"✅ Правильный ответ: <b>{correct_num}. {escape(correct_value)}</b>\n\n"
        f"{escape(question.explanation)}"
    )


def build_players_result_block(question: Question, round_answers: list[sqlite3.Row], chat_id: int = None, inline_message_id: str = None) -> str:
    if not round_answers:
        return "\n\n👥 Никто не ответил."

    results: list[str] = []
    for row in round_answers:
        name = row["full_name"]
        user_id = row["user_id"]
        
        correct_hour, total_hour = quiz_game.storage.get_player_stats_today(user_id)
        
        if row["selected_option"] == question.correct_option:
            results.append(f"✅ {escape(name)} : +1 (текущий: {correct_hour}/{total_hour})")
        else:
            results.append(f"❌ {escape(name)} : 0 (текущий: {correct_hour}/{total_hour})")

    return "\n\n👥 <b>Результаты:</b>\n" + "\n".join(results)


async def close_question_by_ids(
    application: Application,
    chat_id: int,
    question_id: str,
    message_id: int,
) -> None:
    active = quiz_game.storage.get_active_question(chat_id)
    if not active or active["is_closed"] == 1 or active["question_id"] != question_id:
        return

    quiz_game.storage.close_active_question(chat_id)
    question = quiz_game.questions_by_id[question_id]
    answer_stats = quiz_game.storage.get_question_answer_stats(chat_id, message_id)
    round_answers = quiz_game.storage.get_round_answers(chat_id, message_id)
    result_text = build_result_text(question, answer_stats)
    if chat_id < 0:
        result_text += build_players_result_block(question, round_answers)

    await application.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=result_text,
        parse_mode=ParseMode.HTML,
        reply_markup=next_question_keyboard(),
    )


async def schedule_close(
    application: Application,
    chat_id: int,
    question_id: str,
    message_id: int,
    delay_seconds: int,
) -> None:
    try:
        await asyncio.sleep(delay_seconds)
        await close_question_by_ids(application, chat_id, question_id, message_id)
    finally:
        close_tasks.pop(chat_id, None)


async def close_inline_question_by_id(
    application: Application,
    inline_message_id: str,
    question_id: str,
) -> None:
    active = quiz_game.storage.get_inline_active_question(inline_message_id)
    if not active or active["is_closed"] == 1 or active["question_id"] != question_id:
        return

    quiz_game.storage.close_inline_active_question(inline_message_id)
    question = quiz_game.questions_by_id[question_id]
    answer_stats = quiz_game.storage.get_inline_question_answer_stats(inline_message_id)
    round_answers = quiz_game.storage.get_inline_round_answers(inline_message_id)
    result_text = build_result_text(question, answer_stats) + build_players_result_block(question, round_answers)

    await application.bot.edit_message_text(
        inline_message_id=inline_message_id,
        text=result_text,
        parse_mode=ParseMode.HTML,
    )


async def schedule_inline_close(
    application: Application,
    inline_message_id: str,
    question_id: str,
    delay_seconds: int,
) -> None:
    try:
        await asyncio.sleep(delay_seconds)
        await close_inline_question_by_id(application, inline_message_id, question_id)
    finally:
        inline_close_tasks.pop(inline_message_id, None)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not message:
        return

    if user:
        quiz_game.storage.save_player(user.id, user.username, user.full_name)

    await message.reply_text(
        "Я квиз-бот для Telegram.\n\n"
        "Основные команды:\n"
        "/quiz — новый вопрос\n"
        "/stats — твоя статистика\n"
        "/top — топ игроков чата\n"
        "/chatstats — статистика чата\n"
        "/quizreset — сбросить историю вопросов чата\n"
        "/help — помощь\n\n"
        f"В группе можно запускать через /quiz или просто написать @{BOT_USERNAME}."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return

    active = quiz_game.storage.get_active_question(chat.id)
    if active and active["is_closed"] == 0 and chat.id not in close_tasks:
        quiz_game.storage.close_active_question(chat.id)
        active = None

    if active and active["is_closed"] == 0:
        await message.reply_text("В чате уже есть активный вопрос. Сначала дождитесь его завершения.")
        return

    question = quiz_game.next_question(chat.id)
    if question is None:
        await message.reply_text(
            "В этом чате закончились уникальные вопросы из базы. "
            "Добавь новые вопросы в questions.json или выполни /quizreset."
        )
        return

    sent = await message.reply_text(
        question_text(question),
        parse_mode=ParseMode.HTML,
        reply_markup=question_keyboard(question.id),
    )
    quiz_game.storage.set_active_question(chat.id, question.id, sent.message_id)


async def mention_start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return

    text = message.text.lower()
    mention = f"@{BOT_USERNAME.lower()}"
    has_text_mention = mention in text
    has_entity_mention = False

    if message.entities:
        for entity in message.entities:
            if entity.type == "mention":
                value = message.text[entity.offset:entity.offset + entity.length].lower()
                if value == mention:
                    has_entity_mention = True
                    break

    if not has_text_mention and not has_entity_mention:
        return

    await quiz(update, context)


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.inline_query
    if not query or query.from_user is None:
        return

    chat_key = f"{query.chat_type or 'inline'}:{query.from_user.id}:{query.id}"
    question = quiz_game.next_inline_question(chat_key, query.from_user.id)
    result = InlineQueryResultArticle(
        id=question.id,
        title="Запустить квиз",
        description="Нажми, чтобы отправить вопрос в чат",
        input_message_content=InputTextMessageContent(
            question_text(question),
            parse_mode=ParseMode.HTML,
        ),
        reply_markup=question_keyboard(question.id),
    )
    await query.answer([result], cache_time=0, is_personal=True)


async def chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    result = update.chosen_inline_result
    if not result or not result.inline_message_id:
        return

    question_id = result.result_id
    quiz_game.storage.set_inline_active_question(result.inline_message_id, question_id)
    quiz_game.storage.inline_mark_question_asked(result.inline_message_id, question_id)
    if result.inline_message_id in inline_close_tasks:
        inline_close_tasks[result.inline_message_id].cancel()
    inline_close_tasks[result.inline_message_id] = asyncio.create_task(
        schedule_inline_close(
            context.application,
            result.inline_message_id,
            question_id,
            QUESTION_TIMEOUT_SECONDS,
        )
    )


async def next_question_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        logger.error("next_question_handler: no query")
        return

    logger.info(f"next_question_handler called: inline={query.inline_message_id}, data={query.data}")

    if query.inline_message_id:
        await query.answer("Используй @Quiby_bot для нового вопроса", show_alert=True)
        return

    if query.message:
        chat_id = query.message.chat_id

        # Проверяем, нет ли уже активного вопроса
        active = quiz_game.storage.get_active_question(chat_id)
        if active and active["is_closed"] == 0:
            await query.answer("Вопрос уже отправлен, подожди немного", show_alert=True)
            return

        await query.answer()

        question = quiz_game.next_question(chat_id)
        if question is None:
            await query.answer("Вопросы закончились! Используй /quizreset", show_alert=True)
            return

        try:
            await context.application.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=query.message.message_id,
                reply_markup=None,
            )
        except:
            pass

        sent = await context.application.bot.send_message(
            chat_id=chat_id,
            text=question_text(question),
            parse_mode=ParseMode.HTML,
            reply_markup=question_keyboard(question.id),
        )

        quiz_game.storage.set_active_question(chat_id, question.id, sent.message_id)


async def answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not query.data:
        return

    if query.inline_message_id:
        await answer_inline(update, context)
        return

    if not query.message:
        return

    chat_id = query.message.chat_id
    active = quiz_game.storage.get_active_question(chat_id)
    if not active or active["is_closed"] == 1:
        await query.answer("Этот вопрос уже закрыт.", show_alert=True)
        return

    _, question_id, selected_option = query.data.split(":")
    if active["question_id"] != question_id:
        await query.answer("Этот вопрос уже неактуален.", show_alert=True)
        return

    question = quiz_game.questions_by_id[question_id]
    is_correct = selected_option == question.correct_option
    
    # Сохраняем ответ
    saved = quiz_game.storage.record_answer(
        chat_id=chat_id,
        message_id=query.message.message_id,
        question_id=question_id,
        user_id=query.from_user.id,
        username=query.from_user.username,
        full_name=query.from_user.full_name,
        selected_option=selected_option,
        is_correct=is_correct,
    )

    if not saved:
        await query.answer("Ты уже отвечал на этот вопрос.", show_alert=True)
        return

    await query.answer("Ответ принят.")

    # Закрываем вопрос когда 2 игрока ответили (и в приватном, и в групповом чате)
    active_check = quiz_game.storage.get_active_question(chat_id)
    if not active_check or active_check["is_closed"] == 1:
        return

    unique_players = quiz_game.storage.count_unique_players(chat_id, query.message.message_id)
    logger.info(f"Answer recorded: user={query.from_user.id} ({query.from_user.full_name}), chat={chat_id}, msg={query.message.message_id}, unique_players={unique_players}")

    # Логируем всех ответивших
    with quiz_game.storage._connect() as conn:
        all_answers = conn.execute(
            "SELECT user_id, selected_option FROM answers WHERE chat_id = ? AND message_id = ?",
            (chat_id, query.message.message_id)
        ).fetchall()
        logger.info(f"All answers: {[(a['user_id'], a['selected_option']) for a in all_answers]}")

    if unique_players >= 2:
        task = close_tasks.pop(chat_id, None)
        if task is not None:
            task.cancel()
        await close_question_by_ids(
            context.application,
            chat_id,
            question_id,
            query.message.message_id,
        )


async def answer_inline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.inline_message_id or not query.from_user or not query.data:
        return

    _, question_id, selected_option = query.data.split(":")
    
    active = quiz_game.storage.get_inline_active_question(query.inline_message_id)
    if not active:
        quiz_game.storage.set_inline_active_question(query.inline_message_id, question_id)
        if query.inline_message_id in inline_close_tasks:
            inline_close_tasks[query.inline_message_id].cancel()
        inline_close_tasks[query.inline_message_id] = asyncio.create_task(
            schedule_inline_close(
                context.application,
                query.inline_message_id,
                question_id,
                QUESTION_TIMEOUT_SECONDS,
            )
        )
        active = quiz_game.storage.get_inline_active_question(query.inline_message_id)
    
    if active["is_closed"] == 1:
        await query.answer("Этот вопрос уже закрыт.", show_alert=True)
        return

    if active["question_id"] != question_id:
        await query.answer("Этот вопрос уже неактуален.", show_alert=True)
        return

    question = quiz_game.questions_by_id[question_id]
    is_correct = selected_option == question.correct_option
    saved = quiz_game.storage.record_inline_answer(
        inline_message_id=query.inline_message_id,
        chat_instance=query.chat_instance,
        question_id=question_id,
        user_id=query.from_user.id,
        username=query.from_user.username,
        full_name=query.from_user.full_name,
        selected_option=selected_option,
        is_correct=is_correct,
    )
    if not saved:
        await query.answer("Ты уже отвечал на этот вопрос.", show_alert=True)
        return

    if is_correct:
        await query.answer("✅ Правильно!", show_alert=False)
    else:
        await query.answer("❌ Неправильно", show_alert=False)
    
    if query.inline_message_id in inline_close_tasks:
        inline_close_tasks[query.inline_message_id].cancel()
    inline_close_tasks[query.inline_message_id] = asyncio.create_task(
        schedule_inline_close(
            context.application,
            query.inline_message_id,
            question_id,
            QUICK_CLOSE_SECONDS,
        )
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    row = quiz_game.storage.get_player_stats(user.id)
    if not row or row["total_answers"] == 0:
        await message.reply_text("У тебя пока нет ответов. Сыграй хотя бы один раунд через /quiz или @Quiby_bot в чате.")
        return

    await message.reply_text(
        f"👤 Игрок: {row['full_name']}\n"
        f"📊 Ответов: {row['total_answers']}\n"
        f"✅ Верных: {row['correct_answers']}\n"
        f"🎯 Точность: {accuracy(row['correct_answers'], row['total_answers'])}"
    )


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.effective_message
    if not chat or not message:
        return

    rows = quiz_game.storage.get_chat_top(chat.id)
    if not rows:
        await message.reply_text("В этом чате пока нет статистики. Начните с /quiz.")
        return

    lines = ["Топ игроков чата:"]
    for index, row in enumerate(rows, start=1):
        lines.append(f"{index}. {row['full_name']} — {row['correct_answers']} из {row['answers']}")
    await message.reply_text("\n".join(lines))


async def chatstats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.effective_message
    if not chat or not message:
        return

    row = quiz_game.storage.get_chat_stats(chat.id)
    await message.reply_text(
        f"Статистика чата:\n"
        f"Игроков: {row['players_count']}\n"
        f"Ответов: {row['total_answers']}\n"
        f"Верных: {row['correct_answers']}\n"
        f"Точность: {accuracy(row['correct_answers'], row['total_answers'])}"
    )


async def quizreset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.effective_message
    if not chat or not message:
        return

    quiz_game.storage.reset_question_history(chat.id)
    await message.reply_text("История вопросов этого чата сброшена. Старые вопросы снова могут выпадать.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error while processing update", exc_info=context.error)


def main() -> None:
    load_env()
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("Не найден BOT_TOKEN. Добавь его в окружение или в файл .env")

    quiz_game.storage.close_all_active_questions()
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("quiz", quiz))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("chatstats", chatstats))
    app.add_handler(CommandHandler("quizreset", quizreset))
    app.add_handler(CallbackQueryHandler(answer, pattern=r"^answer:"))
    app.add_handler(CallbackQueryHandler(next_question_handler, pattern=r"^next_question$"))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            mention_start_quiz,
        )
    )
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(ChosenInlineResultHandler(chosen_inline_result))
    app.add_error_handler(error_handler)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
