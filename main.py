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
from googleapiclient.errors import HttpError
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


def parse_http_error_reason(err: HttpError) -> Tuple[Optional[str], Optional[str]]:
    content = getattr(err, "content", None)
    if not content:
        return None, None

    try:
        if isinstance(content, (bytes, bytearray)):
            content = content.decode("utf-8", errors="ignore")
        payload = json.loads(content)
    except (ValueError, TypeError):
        return None, None

    error = payload.get("error", {})
    message = error.get("message")
    errors = error.get("errors", [])
    if errors and isinstance(errors, list):
        reason = errors[0].get("reason")
        return reason, message
    return None, message


def describe_http_error(err: HttpError) -> str:
    status = getattr(err.resp, "status", None)
    reason, message = parse_http_error_reason(err)
    reason_map = {
        "insufficientPermissions": "Доступ ограничен. Файл не расшарен на сервисный аккаунт.",
        "insufficientFilePermissions": "Доступ ограничен. Файл не расшарен на сервисный аккаунт.",
        "appNotAuthorizedToFile": "Приложение не авторизовано для файла. Нужен доступ сервисного аккаунта.",
        "downloadFileRestricted": "Запрет на скачивание. Владелец запретил экспорт.",
        "fileNotDownloadable": "Запрет на скачивание. Владелец запретил экспорт.",
        "exportSizeLimitExceeded": "Файл слишком большой для экспорта.",
        "cannotDownloadAbusiveFile": "Google заблокировал скачивание файла как потенциально опасного.",
        "userRateLimitExceeded": "Слишком много запросов. Попробуй позже.",
        "rateLimitExceeded": "Слишком много запросов. Попробуй позже.",
        "sharingRateLimitExceeded": "Превышен лимит общего доступа. Попробуй позже.",
        "quotaExceeded": "Превышена квота Google Drive. Попробуй позже.",
    }

    if status == 404:
        return "Не найден документ по ссылке. Проверь ссылку и доступ."
    if status == 401:
        return "Нет доступа к Google Drive. Проверь сервисный аккаунт."
    if status == 429:
        return "Слишком много запросов. Попробуй позже."
    if status in {500, 502, 503, 504}:
        return "Google Drive временно недоступен. Попробуй позже."
    if status == 403:
        if reason in reason_map:
            return reason_map[reason]
        return "Доступ ограничен или запрещен. Проверь права."
    if status == 400 and reason in reason_map:
        return reason_map[reason]

    if message:
        return f"Ошибка Google Drive: {message}"
    return "Ошибка Google Drive при обработке файла."


def describe_value_error(err: ValueError) -> str:
    message = str(err)
    if message.startswith("Unsupported mimeType:"):
        return (
            "Формат файла не поддерживается. Подходят только Google Docs и Google Sheets."
        )
    return f"Ошибка обработки: {message}"


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

    text = (update.message.text or "").strip()
    file_ids = extract_file_ids(text)
    if not file_ids:
        await update.message.reply_text(
            "Не найдена ссылка на документ. Пришли ссылку на Google Docs/Sheets."
        )
        return

    for file_id in file_ids:
        try:
            await update.message.reply_text("Конвертирую и скачиваю…")
            data, filename = await asyncio.to_thread(export_google_file, file_id)
            bio = io.BytesIO(data)
            bio.name = filename
            bio.seek(0)
            await update.message.reply_document(document=bio, filename=filename)
        except HttpError as he:
            logging.exception("Failed to export: http error")
            await update.message.reply_text(describe_http_error(he))
        except ValueError as ve:
            await update.message.reply_text(describe_value_error(ve))
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
