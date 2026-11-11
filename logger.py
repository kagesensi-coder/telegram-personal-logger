import asyncio
import getpass
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import qrcode
from dotenv import load_dotenv
from telethon import TelegramClient, events, types
from telethon.errors import RPCError, SessionPasswordNeededError

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION = os.getenv("SESSION", "personal_logger")
LOG_GROUP_ID = int(os.getenv("LOG_GROUP_ID", "-5387432260"))
DB_PATH = Path(os.getenv("DB_PATH", "message_logs.sqlite3"))
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "media_cache"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

client = TelegramClient(SESSION, API_ID, API_HASH)
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row


def init_db():
    db.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            user_id INTEGER,
            text TEXT NOT NULL DEFAULT '',
            media_path TEXT,
            timestamp TEXT NOT NULL,
            is_edited INTEGER NOT NULL DEFAULT 0,
            is_deleted INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, id)
        );
        CREATE TABLE IF NOT EXISTS versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            old_text TEXT NOT NULL,
            new_text TEXT NOT NULL,
            timestamp TEXT NOT NULL
        );
    """)
    db.commit()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


async def sender_name(message):
    try:
        sender = await message.get_sender()
        if sender:
            name = " ".join(part for part in (
                getattr(sender, "first_name", None),
                getattr(sender, "last_name", None),
            ) if part)
            return name or getattr(sender, "username", None) or str(sender.id)
    except RPCError:
        log.exception("Не удалось получить отправителя")
    return "Неизвестный отправитель"

def clean_old_logs(db_path="message_logs.sqlite3", days_to_keep=30):
    """
    Удаляет записи старше указанного количества дней и сжимает базу данных.
    """
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        limit_date = (datetime.now() - timedelta(days=days_to_keep)).strftime('%Y-%m-%d %H:%M:%S')
        
        # Удаляем старые записи (проверь, чтобы таблица называлась messages, а колонка timestamp)
        cursor.execute("DELETE FROM messages WHERE timestamp < ?", (limit_date,))
        deleted_rows = cursor.rowcount
        
        conn.commit()
        cursor.execute("VACUUM")
        conn.close()
        
        print(f"[CleanUp] Удалено старых записей: {deleted_rows}. База данных сжата.")
    except Exception as e:
        print(f"[CleanUp] Ошибка при очистке: {e}")

async def save_media(message):
    if not message.media:
        return None
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        return await message.download_media(file=str(MEDIA_DIR))
    except (RPCError, OSError):
        log.exception("Не удалось скачать медиа сообщения %s", message.id)
        return None


async def notify_group(text, media_path=None):
    """Отправляет лог в группу. ID явно приводится к int."""
    try:
        if media_path and Path(media_path).is_file():
            await client.send_file(int(LOG_GROUP_ID), media_path, caption=text[:1024])
        else:
            await client.send_message(int(LOG_GROUP_ID), text[:4096])
    except (RPCError, OSError) as exc:
        log.error("Не удалось отправить лог в группу %s: %s", LOG_GROUP_ID, exc)


def get_message(chat_id, message_id):
    return db.execute(
        "SELECT * FROM messages WHERE chat_id = ? AND id = ?",
        (chat_id, message_id),
    ).fetchone()


async def ensure_authorized():
    await client.connect()
    if await client.is_user_authorized():
        log.info("Существующая сессия авторизована.")
        return

    log.info("Сессия не авторизована. Отсканируйте QR-код в Telegram:")
    qr_login = await client.qr_login()
    qr = qrcode.QRCode()
    qr.add_data(qr_login.url)
    qr.print_ascii(invert=True)
    print("\nОткройте Telegram → Настройки → Устройства → Подключить устройство")

    try:
        await qr_login.wait()
    except SessionPasswordNeededError:
        await client.sign_in(password=getpass.getpass("Введите пароль 2FA: "))

    if not await client.is_user_authorized():
        raise RuntimeError("QR-авторизация не завершилась успешно")
    log.info("Аккаунт успешно авторизован через QR-код.")


@client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def cache_private_message(event):
    message = event.message
    media_path = await save_media(message)
    
    # Вот здесь исправлен список передаваемых значений (добавлен media_path)
    db.execute("""
        INSERT OR IGNORE INTO messages (id, chat_id, user_id, text, media_path, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (message.id, event.chat_id, event.sender_id, message.message or "", str(media_path) if media_path else None, now_iso()))
    db.commit()

    log_text = (
        f"📩 Новое сообщение в личном чате\n"
        f"Отправитель: {await sender_name(message)} (ID: {event.sender_id})\n"
        f"Время: {now_iso()}\n\n{message.message or '[без текста]'}"
    )
    await notify_group(log_text, media_path)


@client.on(events.MessageEdited(func=lambda e: e.is_private))
async def edited_private_message(event):
    message = event.message
    row = get_message(event.chat_id, message.id)
    old_text = row["text"] if row else "[предыдущая версия не сохранена]"
    new_text = message.message or ""
    db.execute("""
        INSERT INTO versions (chat_id, message_id, old_text, new_text, timestamp)
        VALUES (?, ?, ?, ?, ?)
    """, (event.chat_id, message.id, old_text, new_text, now_iso()))

    if row:
        db.execute(
            "UPDATE messages SET text = ?, is_edited = 1 WHERE chat_id = ? AND id = ?",
            (new_text, event.chat_id, message.id),
        )
        media_path = row["media_path"]
    else:
        media_path = await save_media(message)
        db.execute("""
            INSERT OR REPLACE INTO messages
                (id, chat_id, user_id, text, media_path, timestamp, is_edited)
            VALUES (?, ?, ?, ?, ?, ?, 1)
        """, (message.id, event.chat_id, event.sender_id, new_text, media_path, now_iso()))
    db.commit()

    log_text = (
        f"✏️ Сообщение отредактировано в чате {await sender_name(message)} "
        f"(ID: {event.sender_id}):\n"
        f"Было: {old_text or '[без текста]'}\n"
        f"Стало: {new_text or '[без текста]'}"
    )
    await notify_group(log_text, media_path)


async def handle_deleted_ids(ids):
    for message_id in ids:
        rows = db.execute(
            "SELECT * FROM messages WHERE id = ? AND is_deleted = 0", (message_id,)
        ).fetchall()
        if len(rows) != 1:
            if len(rows) > 1:
                log.warning("Удаление ID %s неоднозначно; пропускаю", message_id)
            continue

        row = rows[0]
        db.execute(
            "UPDATE messages SET is_deleted = 1 WHERE chat_id = ? AND id = ?",
            (row["chat_id"], message_id),
        )
        db.commit()

        try:
            entity = await client.get_entity(row["chat_id"])
            name = " ".join(part for part in (
                getattr(entity, "first_name", None), getattr(entity, "last_name", None)
            ) if part) or getattr(entity, "username", None) or str(row["chat_id"])
        except (RPCError, ValueError):
            name = str(row["chat_id"])

        log_text = (
            f"❌ Сообщение удалено в чате {name} (ID: {row['user_id']}): "
            f"{row['text'] or '[без текста]'}"
        )
        await notify_group(log_text, row["media_path"])


@client.on(events.Raw)
async def raw_updates(update):
    if isinstance(update, types.UpdateDeleteMessages) and update.pts_count:
        await handle_deleted_ids(update.messages)


async def main():
    init_db()
    
    # Добавляем очистку старых логов при старте бота
    clean_old_logs(db_path="message_logs.sqlite3", days_to_keep=30)
    
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            log.info("Подключение к Telegram...")
            await ensure_authorized()
            log.info("Юзербот запущен. Ctrl+C для остановки.")
            await client.run_until_disconnected()
        except asyncio.CancelledError:
            raise
        except (RPCError, OSError) as exc:
            log.warning("Соединение прервано (%s); повтор через 5 секунд", exc)
            await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановка.")
    finally:
        db.close()
