import os
import re
import io
import json
import base64
import asyncio
import logging
from typing import List, Optional, Tuple

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"

_drive_service = None
_sa_json_path = None


def _env(name: str, default: Optional[str] = None) -> str:
    v = os.environ.get(name, default)
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


def extract_file_ids(text: str) -> List[str]:
    patterns = [
        r"/document/d/([a-zA-Z0-9_-]+)",
        r"/spreadsheets/d/([a-zA-Z0-9_-]+)",
        r"/file/d/([a-zA-Z0-9_-]+)",
        r"[?&]id=([a-zA-Z0-9_-]+)",
    ]
    ids = []
    for p in patterns:
        ids += re.findall(p, text)

    seen = set()
    out = []
    for x in ids:
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out


def extract_google_file_ids_from_message(message) -> List[str]:
    if not message:
        return []

    sources = []
    text = (message.text or "").strip()
    if text:
        sources.append(text)

    for entity in message.entities or []:
        if entity.type == "url":
            url = message.parse_entity(entity)
            if url:
                sources.append(url)
        elif entity.type == "text_link" and entity.url:
            sources.append(entity.url)

    seen = set()
    ids: List[str] = []
    for source in sources:
        for file_id in extract_file_ids(source):
            if file_id not in seen:
                seen.add(file_id)
                ids.append(file_id)
    return ids


def prepare_service_account_file() -> str:
    global _sa_json_path
    if _sa_json_path:
        return _sa_json_path

    b64 = _env("GOOGLE_SA_JSON_B64")
    raw = base64.b64decode(b64.encode("utf-8"))
    json.loads(raw.decode("utf-8"))  # sanity check

    path = "/tmp/service_account.json"
    with open(path, "wb") as f:
        f.write(raw)

    _sa_json_path = path
    return path


def get_drive_service():
    global _drive_service
    if _drive_service is not None:
        return _drive_service

    sa_path = prepare_service_account_file()
    creds = Credentials.from_service_account_file(sa_path, scopes=SCOPES)
    _drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _drive_service


def export_google_file(file_id: str) -> Tuple[bytes, str]:
    drive = get_drive_service()
    meta = drive.files().get(fileId=file_id, fields="name,mimeType").execute()
    name = meta.get("name", "file")
    mime = meta.get("mimeType")

    if mime == GOOGLE_DOC_MIME:
        export_mime = DOCX_MIME
        ext = ".docx"
    elif mime == GOOGLE_SHEET_MIME:
        export_mime = XLSX_MIME
        ext = ".xlsx"
    else:
        raise ValueError(f"Unsupported mimeType: {mime}")

    req = drive.files().export_media(fileId=file_id, mimeType=export_mime)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, req)
    done = False
    while not done:
        _, done = downloader.next_chunk()

    fh.seek(0)
    filename = name if name.lower().endswith(ext) else (name + ext)
    return fh.read(), filename


def is_allowed_user(update: Update) -> bool:
    allowed = os.environ.get("ALLOWED_USER_ID", "").strip()
    if not allowed:
        return True
    return update.effective_user and str(update.effective_user.id) == allowed


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed_user(update):
        return
    await update.message.reply_text(
        "Пришли ссылку на Google Docs или Google Sheets — верну DOCX/XLSX.\n"
        "Команда /id покажет твой Telegram user_id."
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed_user(update):
        return
    uid = update.effective_user.id if update.effective_user else "unknown"
    await update.message.reply_text(f"Твой user_id: {uid}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed_user(update):
        return

    file_ids = extract_google_file_ids_from_message(update.message)
    if not file_ids:
        await update.message.reply_text("Не вижу ссылки на Google Docs/Sheets. Пришли ссылку.")
        return

    for file_id in file_ids:
        try:
            await update.message.reply_text("Конвертирую и скачиваю…")
            data, filename = await asyncio.to_thread(export_google_file, file_id)
            bio = io.BytesIO(data)
            bio.name = filename
            bio.seek(0)
            await update.message.reply_document(document=bio, filename=filename)
        except ValueError as ve:
            await update.message.reply_text(f"Не поддерживается: {ve}")
        except Exception:
            logging.exception("Failed to export")
            await update.message.reply_text(
                "Ошибка скачивания/конвертации.\n"
                "Проверь: файл расшарен на service account email, ссылка верная, Drive API включен."
            )


def main():
    bot_token = _env("BOT_TOKEN")
    # На Render эта переменная автоматически содержит https://<service>.onrender.com
    base_url = os.environ.get("WEBHOOK_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if not base_url:
        raise RuntimeError("Missing WEBHOOK_BASE_URL (or RENDER_EXTERNAL_URL)")

    path = _env("WEBHOOK_PATH")  # любая случайная строка
    port = int(os.environ.get("PORT", "8080"))

    app = Application.builder().token(bot_token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    webhook_url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    logging.info("Webhook URL: %s", webhook_url)

    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=path,
        webhook_url=webhook_url,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
