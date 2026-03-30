"""
KAGI物件更新システム
「〇〇邸の構造図を受領済みに更新して」→ スプレッドシートを自動更新
"""

import re
import os
import requests
import json

GAS_URL = os.environ.get("GAS_URL", "").strip()

# 更新可能なカラム一覧
UPDATABLE_COLUMNS = [
    "案内図", "公図", "確定式地図", "インフラ計画", "レベル", "道路情報",
    "物件概要", "施主情報", "地盤調査データ", "構造図", "申請予定", "CADデータ",
    "状態", "実施",
]

# 値のキーワードマッピング
VALUE_MAP = {
    "受領済み": "☑",
    "受領": "☑",
    "済み": "☑",
    "チェック": "☑",
    "未受領": "☐",
    "未": "☐",
    "申請準備中": "申請準備中",
    "申請中": "申請中",
    "是正対応中": "是正対応中",
    "交付済": "交付済",
    "計画": "計画",
    "実施中": "実施",
    "完了": "完了",
}


def is_update_command(text: str) -> bool:
    """更新コマンドかどうかを判定"""
    return bool(re.search(r"更新|変更|にして|に変えて|済みに|受領済", text))


def parse_update_command(text: str) -> dict | None:
    """
    「〇〇邸の構造図を受領済みに更新して」を解析して辞書を返す
    解析できなければ None を返す
    """
    # 物件名を抽出（〇〇邸 または 〇〇様邸）
    prop_match = re.search(r"([^\s「」『』【】（）()\n]{1,20}(?:邸|様邸))", text)
    if not prop_match:
        return None
    property_name = prop_match.group(1)

    # カラム名を検索
    target_col = None
    for col in UPDATABLE_COLUMNS:
        if col in text:
            target_col = col
            break
    if not target_col:
        return None

    # 値を検索
    target_value = None
    for keyword, value in VALUE_MAP.items():
        if keyword in text:
            target_value = value
            break
    if not target_value:
        return None

    return {
        "property_name": property_name,
        "column": target_col,
        "value": target_value,
    }


def execute_update(property_name: str, column: str, value: str) -> str:
    """GAS doPost() を呼び出してスプレッドシートを更新する"""
    if not GAS_URL:
        return "❌ GAS_URL が設定されていません。Render の環境変数を確認してください。"

    try:
        payload = {
            "action": "update_property",
            "property_name": property_name,
            "column": column,
            "value": value,
        }
        resp = requests.post(GAS_URL, json=payload, timeout=30, allow_redirects=True)
        resp.raise_for_status()

        result = resp.json()
        if result.get("success"):
            display_value = value if value not in ("☑", "☐") else ("受領済み" if value == "☑" else "未受領")
            return f"✅ 「{property_name}」の「{column}」を「{display_value}」に更新しました。"
        else:
            return f"❌ 更新失敗: {result.get('error', '不明なエラー')}"

    except requests.exceptions.Timeout:
        return "❌ タイムアウト：GAS の応答が遅すぎます。"
    except Exception as e:
        return f"❌ 更新エラー: {str(e)}"
