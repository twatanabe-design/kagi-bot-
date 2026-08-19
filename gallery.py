"""
施工事例ギャラリー（/gallery）
Googleドライブの「05_書庫/99施工事例」フォルダを参照し、
サブフォルダ＝物件名として配下の画像を一覧表示するためのモジュール。
フォルダの追加・削除・リネームをするだけで自動反映される（コード変更不要）。
"""
import os
import io
import json
import time

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# 対象フォルダ（05_書庫/99施工事例）のGoogleドライブID
GALLERY_FOLDER_ID = os.environ.get("GALLERY_FOLDER_ID", "1Iu3PVPdh9TdpO1dnO9nR0SH2y1gdvHqI")

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

CACHE_TTL_SEC = 300  # 5分キャッシュ（Drive APIの呼び出しを抑える）
_cache = {"data": None, "at": 0}
_service = None


def _get_service():
    """サービスアカウントでDrive APIクライアントを生成（プロセス内で使い回し）"""
    global _service
    if _service is not None:
        return _service

    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if creds_json:
        info = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        # ローカル動作確認用フォールバック（credentials/kagiya-sheets-credentials.json）
        local_path = os.environ.get(
            "GOOGLE_SERVICE_ACCOUNT_FILE",
            os.path.join(os.path.dirname(__file__), "..", "credentials", "kagiya-sheets-credentials.json"),
        )
        creds = service_account.Credentials.from_service_account_file(local_path, scopes=SCOPES)

    _service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _service


def list_gallery(force_refresh=False):
    """
    物件（サブフォルダ）ごとの画像一覧を返す。
    戻り値: [{"name": "物件名", "files": [{"id": .., "name": ..}, ...]}, ...]
    """
    now = time.time()
    if not force_refresh and _cache["data"] is not None and (now - _cache["at"]) < CACHE_TTL_SEC:
        return _cache["data"]

    service = _get_service()

    # 1. 対象フォルダ配下のサブフォルダ（＝物件）を取得
    folders_res = service.files().list(
        q=f"'{GALLERY_FOLDER_ID}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id, name)",
        orderBy="name",
        pageSize=200,
    ).execute()
    folders = folders_res.get("files", [])

    result = []
    for folder in folders:
        images_res = service.files().list(
            q=f"'{folder['id']}' in parents and mimeType contains 'image/' and trashed=false",
            fields="files(id, name, mimeType)",
            orderBy="name",
            pageSize=200,
        ).execute()
        result.append({"name": folder["name"], "files": images_res.get("files", [])})

    _cache["data"] = result
    _cache["at"] = now
    return result


def get_image_bytes(file_id):
    """指定ファイルIDの画像バイナリとMIMEタイプを取得"""
    service = _get_service()
    meta = service.files().get(fileId=file_id, fields="mimeType").execute()

    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

    return buf.getvalue(), meta.get("mimeType", "application/octet-stream")
