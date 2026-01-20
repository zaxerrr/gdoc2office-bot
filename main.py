#!/usr/bin/env python3
import re
import io
import asyncio
import logging
from typing import Optional, List, Tuple

# Telegram-related imports (placeholders — adjust according to your code)
from telegram import Update
from telegram.ext import ContextTypes

# Google API imports
from googleapiclient.http import MediaIoBaseDownload

# Placeholder for drive service singleton
_drive_service = None

def _env(name: str, default: Optional[str] = None) -> str:
    # implementation omitted for brevity in this snippet
    v = default or ""
    return v


def extract_file_refs(text: str) -> List[Tuple[str, Optional[str]]]:
    patterns = [
        r"/document/d/([a-zA-Z0-9_-]+)",
        r"/spreadsheets/d/([a-zA-Z0-9_-]+)",
        r"/file/d/([a-zA-Z0-9_-]+)",
        r"[?\u0026]id=([a-zA-Z0-9_-]+)",
    ]
    resource_key_match = re.search(r"[?\u0026]resourcekey=([a-zA-Z0-9_-]+)", text)
    resource_key = resource_key_match.group(1) if resource_key_match else None
    ids = []
    for p in patterns:
        ids += re.findall(p, text)
    seen = set()
    out: List[Tuple[str, Optional[str]]] = []
    for x in ids:
        if x not in seen:
            out.append((x, resource_key))
            seen.add(x)
    return out


def get_drive_service():
    global _drive_service
    if _drive_service is None:
        # initialize the drive service here (omitted)
        raise RuntimeError("Drive service not initialized")
    return _drive_service


def export_google_file(file_id: str, resource_key: Optional[str] = None) -> Tuple[bytes, str]:
    drive = get_drive_service()
    request_headers = {}
    if resource_key:
        request_headers["X-Goog-Drive-Resource-Keys"] = f"{file_id}/{resource_key}"
    meta = (
        drive.files()
        .get(fileId=file_id, fields="name,mimeType")
        .execute(headers=request_headers if request_headers else None)
    )
    name = meta.get("name", "file")
    mime = meta.get("mimeType")

    if mime == "application/vnd.google-apps.document":
        export_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif mime == "application/vnd.google-apps.spreadsheet":
        export_mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        raise ValueError(f"Unsupported mimeType: {mime}")

    req = drive.files().export_media(fileId=file_id, mimeType=export_mime)
    if request_headers:
        if getattr(req, "headers", None):
            req.headers.update(request_headers)
        else:
            req.headers = dict(request_headers)

    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, req)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    fh.seek(0)
    return fh.read(), name


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = (update.message.text or "").strip()
    file_refs = extract_file_refs(text)
    if not file_refs:
        await update.message.reply_text("Не вижу ссылки на Google Docs/Sheets. Пришли ссылку.")
        return

    for file_id, resource_key in file_refs:
        try:
            await update.message.reply_text("Конвертирую и скачиваю…")
            data, filename = await asyncio.to_thread(export_google_file, file_id, resource_key)
            bio = io.BytesIO(data)
            bio.name = filename
            bio.seek(0)
            # send file to user — actual sending code omitted for brevity
        except Exception:
            logging.exception("Failed to export")
            await update.message.reply_text(
                "Ошибка скачивания/конвертации.\n"
                "Проверь: файл расшарен на service account email, ссылка верная (с resourcekey), Drive API включен."
            )


def main():
    # Bot setup and polling / webhook initialization (omitted)
    pass


if __name__ == "__main__":
    main()