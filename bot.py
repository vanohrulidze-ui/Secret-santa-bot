import os
import sqlite3
import random
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

BOT_TOKEN = os.environ["BOT_TOKEN"]

DB_PATH = os.environ.get("DB_PATH", "/data/santa.sqlite")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with db() as conn:
        conn.executescript("""
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
        """)


def display(username, name):
    return f"@{username}" if username else name


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id

    with db() as conn:
        conn.execute("""
        INSERT OR IGNORE INTO participants
        (chat_id, user_id, username, full_name)
        VALUES (?, ?, ?, ?)
        """, (chat_id, user.id, user.username, user.full_name))

    await update.message.reply_text(
        "Ты зарегистрирован(а).\n"
        "Напиши /wish <текст>, чтобы указать или изменить пожелания."
    )


async def wish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    text = " ".join(context.args)

    if not text:
        await update.message.reply_text("Напиши: /wish <пожелание>")
        return

    with db() as conn:
        conn.execute("""
        UPDATE participants
        SET wishlist=?
        WHERE chat_id=? AND user_id=?
        """, (text, chat_id, user.id))

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Отправить дарителю", callback_data=f"notify:{chat_id}:{user.id}")
    ]])

    await update.message.reply_text(
        "Пожелание сохранено.\nСообщить обновление твоему Санте?",
        reply_markup=kb
    )


async def notify_giver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, chat_id, receiver_id = query.data.split(":")
    chat_id = int(chat_id)
    receiver_id = int(receiver_id)

    with db() as conn:
        row = conn.execute("""
        SELECT giver_id FROM pairs
        WHERE chat_id=? AND receiver_id=?
        """, (chat_id, receiver_id)).fetchone()

        if not row:
            await query.edit_message_text("Даритель ещё не определён.")
            return

        giver_id = row[0]

        wish, uname, fname = conn.execute("""
        SELECT wishlist, username, full_name
        FROM participants
        WHERE chat_id=? AND user_id=?
        """, (chat_id, receiver_id)).fetchone()

    await context.bot.send_message(
        giver_id,
        f"Обновились пожелания получателя:\n\n{wish}"
    )

    await query.edit_message_text("✅ Пожелание отправлено дарителю.")


def make_pairs(chat_id):
    with db() as conn:
        users = [r[0] for r in conn.execute(
            "SELECT user_id FROM participants WHERE chat_id=?", (chat_id,)
        )]

    shuffled = users[:]
    while True:
        random.shuffle(shuffled)
        if all(a != b for a, b in zip(users, shuffled)):
            break

    with db() as conn:
        conn.execute("DELETE FROM pairs WHERE chat_id=?", (chat_id,))
        for g, r in zip(users, shuffled):
            conn.execute(
                "INSERT INTO pairs VALUES (?, ?, ?)",
                (chat_id, g, r)
            )


async def draw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    make_pairs(chat_id)
    await update.message.reply_text("Жеребьёвка проведена.")


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("wish", wish))
    app.add_handler(CommandHandler("draw", draw))
    app.add_handler(CallbackQueryHandler(notify_giver))

    app.run_polling()


if __name__ == "__main__":
    main()
