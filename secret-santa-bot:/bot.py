import os
import re
import sqlite3
import random
import secrets
from typing import Optional, List, Tuple

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

DB_PATH = os.environ.get("DB_PATH", "santa.sqlite")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# Telegram numeric user IDs of admins, comma-separated
ADMIN_IDS = set()
for x in os.environ.get("ADMIN_IDS", "").split(","):
    x = x.strip()
    if x.isdigit():
        ADMIN_IDS.add(int(x))

# Bot username (without @) for deep-link URL
BOT_USERNAME = os.environ.get("BOT_USERNAME", "Secret_Santa_GOD_KONYA_bot").strip().lstrip("@")

if not BOT_TOKEN:
    raise RuntimeError("Missing env BOT_TOKEN")
if not ADMIN_IDS:
    raise RuntimeError("Missing env ADMIN_IDS (comma-separated Telegram numeric IDs)")
if not BOT_USERNAME:
    raise RuntimeError("Missing env BOT_USERNAME")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            -- Participants identified primarily by user_id (works even without @username)
            CREATE TABLE IF NOT EXISTS participants (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,            -- may be NULL
                full_name TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, user_id),
                FOREIGN KEY (chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS pairs (
                chat_id INTEGER NOT NULL,
                giver_user_id INTEGER NOT NULL,
                receiver_user_id INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, giver_user_id),
                FOREIGN KEY (chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
            );

            -- Join tokens for deep links (so joining is tied to the bound chat)
            CREATE TABLE IF NOT EXISTS join_tokens (
                token TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                is_open INTEGER NOT NULL DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
            );
            """
        )


def is_admin(user_id: Optional[int]) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


def get_bound_chat_id() -> Optional[int]:
    with db() as conn:
        row = conn.execute("SELECT chat_id FROM chats ORDER BY created_at DESC LIMIT 1").fetchone()
    return int(row[0]) if row else None


def ensure_in_bound_chat(update: Update) -> Tuple[bool, Optional[int], str]:
    bound = get_bound_chat_id()
    if bound is None:
        return False, None, "Сначала админ должен выполнить /bind_chat в нужном чате."
    if update.effective_chat is None:
        return False, bound, "Не удалось определить чат."
    if update.effective_chat.id != bound:
        return False, bound, "Этот бот настроен на другой чат. Команды управления выполняйте в привязанном чате."
    return True, bound, ""


def make_display(username: Optional[str], full_name: str) -> str:
    if username:
        return f"@{username}"
    return full_name


def make_derangement(items: List[int]) -> List[Tuple[int, int]]:
    """
    Returns pairs (giver, receiver) with giver != receiver.
    Shuffle-retry is reliable for n=13.
    """
    n = len(items)
    if n < 3:
        raise ValueError("Need at least 3 participants")

    receivers = items[:]
    for _ in range(2000):
        random.shuffle(receivers)
        if all(g != r for g, r in zip(items, receivers)):
            return list(zip(items, receivers))

    # Fallback swap
    receivers = items[:]
    random.shuffle(receivers)
    for i in range(n):
        if items[i] == receivers[i]:
            j = (i + 1) % n
            receivers[i], receivers[j] = receivers[j], receivers[i]
    if any(g == r for g, r in zip(items, receivers)):
        raise RuntimeError("Failed to create valid assignment")
    return list(zip(items, receivers))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Команды:\n"
        "Админ:\n"
        "  /bind_chat — привязать бота к этому чату (1 раз)\n"
        "  /post_join — опубликовать кнопку «Участвую»\n"
        "  /close_join — закрыть регистрацию\n"
        "  /list — список участников\n"
        "  /status — кто ещё не нажал Start / не зарегистрирован\n"
        "  /draw — жеребьёвка и рассылка результатов в личку\n"
        "  /clear_pairs — удалить пары\n"
        "  /export — пары админу (аварийно)\n\n"
        "Участники:\n"
        "  /start — регистрация (обычно достаточно нажать кнопку в чате и затем Start)\n\n"
        "Важно: Telegram требует, чтобы участник хотя бы один раз нажал Start у бота в личке."
    )
    await update.message.reply_text(text)


async def cmd_bind_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.effective_chat is None:
        return
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Эта команда должна выполняться в групповом чате.")
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Доступно только админу.")
        return

    chat_id = update.effective_chat.id
    title = update.effective_chat.title or ""
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO chats(chat_id, title) VALUES (?, ?)",
            (chat_id, title),
        )
        # Close all old tokens for safety
        conn.execute("UPDATE join_tokens SET is_open=0")

    await update.message.reply_text(
        f"Готово. Чат привязан.\nchat_id: `{chat_id}`",
        parse_mode=ParseMode.MARKDOWN,
    )


def create_join_token(chat_id: int) -> str:
    token = secrets.token_urlsafe(10)  # short but safe
    token = re.sub(r"[^a-zA-Z0-9_-]", "", token)[:24]
    with db() as conn:
        conn.execute("UPDATE join_tokens SET is_open=0 WHERE chat_id=?", (chat_id,))
        conn.execute(
            "INSERT INTO join_tokens(token, chat_id, is_open) VALUES (?, ?, 1)",
            (token, chat_id),
        )
    return token


def get_open_token(chat_id: int) -> Optional[str]:
    with db() as conn:
        row = conn.execute(
            "SELECT token FROM join_tokens WHERE chat_id=? AND is_open=1 ORDER BY created_at DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
    return row[0] if row else None


async def cmd_post_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or not is_admin(update.effective_user.id):
        await update.message.reply_text("Доступно только админу.")
        return

    ok, chat_id, err = ensure_in_bound_chat(update)
    if not ok:
        await update.message.reply_text(err)
        return

    token = create_join_token(chat_id)
    # Deep link. Payload goes to /start <payload> in DM with the bot.
    url = f"https://t.me/{BOT_USERNAME}?start=join_{token}"

    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(text="Участвую", url=url)]]
    )

    text = (
        "Тайный Санта: регистрация участников открыта.\n\n"
        "Нажмите кнопку «Участвую». У вас откроется личка с ботом — нажмите Start.\n"
        "Это нужно один раз, чтобы Telegram разрешил боту прислать вам результат."
    )
    await update.message.reply_text(text, reply_markup=kb)


async def cmd_close_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or not is_admin(update.effective_user.id):
        await update.message.reply_text("Доступно только админу.")
        return
    ok, chat_id, err = ensure_in_bound_chat(update)
    if not ok:
        await update.message.reply_text(err)
        return
    with db() as conn:
        conn.execute("UPDATE join_tokens SET is_open=0 WHERE chat_id=?", (chat_id,))
    await update.message.reply_text("Регистрация закрыта.")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Participant flow:
    - User clicks "Участвую" button in group => opens bot DM with /start join_<token>
    - Here we validate token and enroll participant for that chat.
    """
    user = update.effective_user
    if user is None or update.message is None:
        return

    bound_chat_id = get_bound_chat_id()
    if bound_chat_id is None:
        await update.message.reply_text(
            "Бот ещё не настроен админом. Подожди, пока админ выполнит /bind_chat в общем чате."
        )
        return

    payload = " ".join(context.args).strip() if context.args else ""
    if not payload.startswith("join_"):
        await update.message.reply_text(
            "Привет. Для участия нажми кнопку «Участвую» в общем чате и затем Start здесь."
        )
        return

    token = payload.replace("join_", "", 1).strip()
    with db() as conn:
        row = conn.execute(
            "SELECT chat_id, is_open FROM join_tokens WHERE token=?",
            (token,),
        ).fetchone()

    if not row:
        await update.message.reply_text("Ссылка регистрации недействительна. Попроси админа заново опубликовать кнопку.")
        return

    chat_id, is_open = int(row[0]), int(row[1])
    if chat_id != bound_chat_id:
        await update.message.reply_text("Этот бот сейчас настроен на другой чат.")
        return
    if is_open != 1:
        await update.message.reply_text("Регистрация закрыта. Попроси админа открыть регистрацию заново.")
        return

    username = (user.username or "").strip().lstrip("@") or None
    full_name = (user.full_name or "Участник").strip()

    with db() as conn:
        conn.execute(
            """
            INSERT INTO participants(chat_id, user_id, username, full_name, is_active)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name,
                is_active=1
            """,
            (chat_id, user.id, username.lower() if username else None, full_name),
        )

    await update.message.reply_text(
        "Готово. Ты зарегистрирован(а) в Тайном Санте.\n"
        "Когда админ проведёт жеребьёвку, я пришлю тебе результат в личку."
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ok, chat_id, err = ensure_in_bound_chat(update)
    if not ok:
        await update.message.reply_text(err)
        return

    with db() as conn:
        rows = conn.execute(
            """
            SELECT user_id, username, full_name, is_active
            FROM participants
            WHERE chat_id=?
            ORDER BY lower(coalesce(username, full_name))
            """,
            (chat_id,),
        ).fetchall()

    if not rows:
        await update.message.reply_text("Участников пока нет. Нажмите /post_join и попросите людей нажать кнопку.")
        return

    lines = ["Участники (зарегистрированы у бота):"]
    for user_id, username, full_name, is_active in rows:
        if int(is_active) != 1:
            continue
        lines.append(f"- {make_display(username, full_name)}")
    await update.message.reply_text("\n".join(lines))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # In this model, status is mainly "how many registered"
    ok, chat_id, err = ensure_in_bound_chat(update)
    if not ok:
        await update.message.reply_text(err)
        return

    with db() as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM participants WHERE chat_id=? AND is_active=1",
            (chat_id,),
        ).fetchone()[0]
        token = get_open_token(chat_id)

    text = f"Сейчас зарегистрировано участников: {cnt}."
    if token:
        text += "\nРегистрация открыта (кнопка «Участвую» активна)."
    else:
        text += "\nРегистрация сейчас закрыта."
    await update.message.reply_text(text)


async def cmd_clear_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or not is_admin(update.effective_user.id):
        await update.message.reply_text("Доступно только админу.")
        return
    ok, chat_id, err = ensure_in_bound_chat(update)
    if not ok:
        await update.message.reply_text(err)
        return
    with db() as conn:
        conn.execute("DELETE FROM pairs WHERE chat_id=?", (chat_id,))
    await update.message.reply_text("Пары удалены. Можно делать новую жеребьёвку /draw.")


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or not is_admin(update.effective_user.id):
        await update.message.reply_text("Доступно только админу.")
        return
    ok, chat_id, err = ensure_in_bound_chat(update)
    if not ok:
        await update.message.reply_text(err)
        return

    with db() as conn:
        rows = conn.execute(
            """
            SELECT p1.username, p1.full_name, p2.username, p2.full_name
            FROM pairs pr
            JOIN participants p1 ON p1.chat_id=pr.chat_id AND p1.user_id=pr.giver_user_id
            JOIN participants p2 ON p2.chat_id=pr.chat_id AND p2.user_id=pr.receiver_user_id
            WHERE pr.chat_id=?
            ORDER BY lower(coalesce(p1.username, p1.full_name))
            """,
            (chat_id,),
        ).fetchall()

    if not rows:
        await update.message.reply_text("Пар нет. Сначала /draw.")
        return

    lines = ["Пары (только для админа, аварийный просмотр):"]
    for u1, n1, u2, n2 in rows:
        giver = make_display(u1, n1)
        recv = make_display(u2, n2)
        lines.append(f"- {giver} -> {recv}")
    await update.message.reply_text("\n".join(lines))


async def cmd_draw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or not is_admin(update.effective_user.id):
        await update.message.reply_text("Доступно только админу.")
        return
    ok, chat_id, err = ensure_in_bound_chat(update)
    if not ok:
        await update.message.reply_text(err)
        return

    with db() as conn:
        participants = conn.execute(
            """
            SELECT user_id, username, full_name
            FROM participants
            WHERE chat_id=? AND is_active=1
            """,
            (chat_id,),
        ).fetchall()

    if len(participants) < 3:
        await update.message.reply_text("Нужно минимум 3 зарегистрированных участника. Открой /post_join и собери людей.")
        return

    user_ids = [int(r[0]) for r in participants]
    pairs = make_derangement(user_ids)

    # Save pairs
    with db() as conn:
        conn.execute("DELETE FROM pairs WHERE chat_id=?", (chat_id,))
        conn.executemany(
            "INSERT INTO pairs(chat_id, giver_user_id, receiver_user_id) VALUES (?, ?, ?)",
            [(chat_id, g, r) for g, r in pairs],
        )

    # Map user_id -> display info
    info = {int(uid): (uname, fname) for uid, uname, fname in participants}

    failed = []
    for giver_uid, receiver_uid in pairs:
        r_uname, r_name = info[receiver_uid]
        receiver_display = make_display(r_uname, r_name)
        msg = (
            "Тайный Санта: твой получатель\n\n"
            f"Ты даришь подарок: {receiver_display}\n\n"
            "Пожалуйста, никому не раскрывай результат."
        )
        try:
            await context.bot.send_message(chat_id=giver_uid, text=msg)
        except Exception as e:
            failed.append((giver_uid, str(e)))

    if failed:
        await update.message.reply_text(
            "Жеребьёвка создана, но не всем удалось отправить личное сообщение.\n"
            "Обычно причина: пользователь запретил сообщения от ботов или удалил чат с ботом.\n"
            "Попроси этих людей снова нажать кнопку «Участвую» и Start, затем повтори /draw."
        )
    else:
        await update.message.reply_text("Жеребьёвка проведена. Всем участникам отправлены результаты в личку.")


def main() -> None:
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("bind_chat", cmd_bind_chat))
    app.add_handler(CommandHandler("post_join", cmd_post_join))
    app.add_handler(CommandHandler("close_join", cmd_close_join))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("draw", cmd_draw))
    app.add_handler(CommandHandler("clear_pairs", cmd_clear_pairs))
    app.add_handler(CommandHandler("export", cmd_export))

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()

