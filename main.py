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
    """
    Поддерживаем популярные форматы ссылок:
    - https://docs.google.com/document/d/<id>/...
    - https://docs.google.com/spreadsheets/d/<id>/...
    - https://drive.google.com/file/d/<id>/...
    - https://drive.google.com/open?id=<id>
    """
    patterns = [
        r"/document/d/([a-zA-Z0-9_-]+)",
        r"/spreadsheets/d/([a-zA-Z0-9_-]+)",
        r"/file/d/([a-zA-Z0-9_-]+)",
        r"[?&]id=([a-zA-Z0-9_-]+)",
    ]
    ids = []
    for p in patterns:
        ids += re.findall(p, text)

    # unique keep order
    seen = set()
    out = []
    for x in ids:
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out


def prepare_service_account_file() -> str:
    """
    Railway удобно хранить ключ в GOOGLE_SA_JSON_B64.
    Декодируем в /tmp/service_account.json и используем как файл.
    """
    global _sa_json_path
    if _sa_json_path:
        return _sa_json_path

    b64 = _env("GOOGLE_SA_JSON_B64")
    raw = base64.b64decode(b64.encode("utf-8"))
    json.loads(raw.decode("utf-8"))  # quick check it’s JSON

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
    """
    Скачивает Google Docs/Sheets через Drive API export и возвращает (bytes, filename)
    """
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
    downloader = MediaIoBaseDownload(fh, re
