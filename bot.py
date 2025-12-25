import os
import sqlite3
import random
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

# Токен бота и путь к существующей БД
BOT_TOKEN = os.environ["BOT_TOKEN"]
DB_PATH = os.environ.get("DB_PATH", "santa.sqlite")

# Админы (список user_id через запятую в переменной ADMIN_IDS)
ADMIN_IDS = {
    int(x)
    for x in os.environ.get("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with db() as conn:
        conn.executescript(
            """
        CREATE TABLE IF NOT EXISTS participants (
            chat_id INTEGER,
            user_id INTEGER,
            username TEXT,
            full_name TEXT,
            wishlist TEXT,
            PRIMARY KEY (chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS pairs (
            chat_id INTEGER,
            giver_id INTEGER,
            receiver_id INTEGER,
            PRIMARY KEY (chat_id, giver_id)
        );
        """
        )


def display(username, name):
    if username:
        return f"@{username}"
    if name:
        return name
    return ""


# ------------------ КОМАНДЫ УЧАСТНИКА ---------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Регистрация участника в конкретном чате"""
    user = update.effective_user
    chat = update.effective_chat

    # В личке chat_id = user.id; в группе chat_id = id чата
    chat_id = chat.id

    with db() as conn:
        conn.execute(
            """
        INSERT OR IGNORE INTO participants
        (chat_id, user_id, username, full_name)
        VALUES (?, ?, ?, ?)
        """,
            (chat_id, user.id, user.username, user.full_name),
        )

    await update.message.reply_text(
        "Ты зарегистрирован(а).\n"
        "Напиши /wish <текст>, чтобы указать или изменить пожелания."
    )


async def wish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Установка/изменение пожеланий"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    text = " ".join(context.args).strip()

    if not text:
        await update.message.reply_text("Напиши: /wish <пожелание>")
        return

    with db() as conn:
        # На всякий случай регистрируем участника, если его ещё нет
        conn.execute(
            """
        INSERT OR IGNORE INTO participants
        (chat_id, user_id, username, full_name)
        VALUES (?, ?, ?, ?)
        """,
            (chat_id, user.id, user.username, user.full_name),
        )

        conn.execute(
            """
        UPDATE participants
        SET wishlist = ?
        WHERE chat_id = ? AND user_id = ?
        """,
            (text, chat_id, user.id),
        )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Отправить дарителю",
                    callback_data=f"notify:{chat_id}:{user.id}",
                )
            ]
        ]
    )

    await update.message.reply_text(
        "Пожелание сохранено.\n"
        "Сообщить обновление твоему Санте?",
        reply_markup=keyboard,
    )


async def mywish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать своё текущее пожелание"""
    user = update.effective_user
    chat_id = update.effective_chat.id

    with db() as conn:
        row = conn.execute(
            """
            SELECT wishlist
            FROM participants
            WHERE chat_id = ? AND user_id = ?
        """,
            (chat_id, user.id),
        ).fetchone()

    if not row or not row[0]:
        await update.message.reply_text(
            "Пожелание пока не задано. Используй /wish <текст>."
        )
        return

    await update.message.reply_text(f"Твоё текущее пожелание:\n\n{row[0]}")


# ------------------ КНОПКА "ОТПРАВИТЬ ДАРИТЕЛЮ" ---------------------


async def notify_giver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправка обновленных пожеланий только своему Санте"""
    query = update.callback_query
    await query.answer()

    try:
        _, chat_id_str, receiver_id_str = query.data.split(":")
        chat_id = int(chat_id_str)
        receiver_id = int(receiver_id_str)
    except Exception:
        await query.edit_message_text("Не удалось разобрать данные кнопки.")
        return

    with db() as conn:
        # Находим дарителя для этого получателя
        row = conn.execute(
            """
        SELECT giver_id
        FROM pairs
        WHERE chat_id = ? AND receiver_id = ?
        """,
            (chat_id, receiver_id),
        ).fetchone()

        if not row:
            await query.edit_message_text(
                "Даритель ещё не определён (жеребьёвка не проведена)."
            )
            return

        giver_id = row[0]

        wish_row = conn.execute(
            """
        SELECT wishlist, username, full_name
        FROM participants
        WHERE chat_id = ? AND user_id = ?
        """,
            (chat_id, receiver_id),
        ).fetchone()

    if not wish_row:
        await query.edit_message_text("Пожелание не найдено.")
        return

    wish, uname, fname = wish_row

    # Сообщение дарителю
    await context.bot.send_message(
        giver_id,
        f"Обновились пожелания твоего получателя:\n\n{wish}",
    )

    # Подтверждение отправителю
    await query.edit_message_text("✅ Пожелание отправлено дарителю.")


# ------------------ ЖЕРЕБЬЁВКА ---------------------


def make_pairs(chat_id: int):
    """Создаёт пары для одного чата (без отправки сообщений)"""
    with db() as conn:
        users = [
            r[0]
            for r in conn.execute(
                "SELECT user_id FROM participants WHERE chat_id = ?",
                (chat_id,),
            )
        ]

    if len(users) < 2:
        return

    shuffled = users[:]

    # Чтобы никто не дарил сам себе
    for _ in range(1000):
        random.shuffle(shuffled)
        if all(a != b for a, b in zip(users, shuffled)):
            break
    else:
        # На всякий случай, если вдруг не удалось (при 2 участниках такое возможно)
        return

    with db() as conn:
        conn.execute("DELETE FROM pairs WHERE chat_id = ?", (chat_id,))
        for giver, receiver in zip(users, shuffled):
            conn.execute(
                "INSERT INTO pairs (chat_id, giver_id, receiver_id) VALUES (?, ?, ?)",
                (chat_id, giver, receiver),
            )


async def draw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Провести жеребьёвку (для текущего чата)"""
    chat_id = update.effective_chat.id
    make_pairs(chat_id)
    await update.message.reply_text("Жеребьёвка проведена.")


# ------------------ КОМАНДА АДМИНА: ПОКАЗАТЬ ВСЕ ПАРЫ ---------------------


async def pairs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать все пары админу в личке"""
    user = update.effective_user
    chat = update.effective_chat

    if not is_admin(user.id):
        await update.message.reply_text("Эта команда доступна только админу.")
        return

    if chat.type != "private":
        await update.message.reply_text("Пары можно смотреть только в личке с ботом.")
        return

    with db() as conn:
        rows = conn.execute(
            """
        SELECT
            p.chat_id,
            p.giver_id,
            p.receiver_id,
            g.username,
            g.full_name,
            r.username,
            r.full_name
        FROM pairs p
        LEFT JOIN participants g
            ON g.chat_id = p.chat_id AND g.user_id = p.giver_id
        LEFT JOIN participants r
            ON r.chat_id = p.chat_id AND r.user_id = p.receiver_id
        ORDER BY p.chat_id, g.full_name, g.username
        """
        ).fetchall()

    if not rows:
        await update.message.reply_text("Пары ещё не сформированы.")
        return

    lines = []
    current_chat = None
    for (
        chat_id,
        giver_id,
        receiver_id,
        giver_username,
        giver_name,
        receiver_username,
        receiver_name,
    ) in rows:
        if chat_id != current_chat:
            if current_chat is not None:
                lines.append("")  # пустая строка между чатами
            lines.append(f"Чат {chat_id}:")
            current_chat = chat_id

        giver_disp = display(giver_username, giver_name) or str(giver_id)
        receiver_disp = display(receiver_username, receiver_name) or str(receiver_id)

        lines.append(f"{giver_disp} → {receiver_disp}")

    text = "\n".join(lines)
    await update.message.reply_text(text)


# ------------------ MAIN ---------------------


def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # участники
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("wish", wish))
    app.add_handler(CommandHandler("mywish", mywish))

    # жеребьёвка
    app.add_handler(CommandHandler("draw", draw))

    # админ: показать пары (только в личке)
    app.add_handler(CommandHandler("pairs", pairs_cmd))

    # колбэки кнопок
    app.add_handler(CallbackQueryHandler(notify_giver))

    app.run_polling()


if __name__ == "__main__":
    main()
