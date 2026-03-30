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

# 値のキーワードマッピング（長い・具体的なキーワードを先に）
VALUE_MAP = {
    "未受領": "☐",      # 「受領」より先にチェック
    "受領済み": "☑",
    "受領": "☑",
    "済み": "☑",
    "チェック": "☑",
    "着": "☑",          # 「構造図着」など業界略語（着工は別途除外）
    "OK": "☑",
    "ok": "☑",
    "✓": "☑",
    "未": "☐",
    "NG": "☐",
    "ng": "☐",
    "×": "☐",
    "申請準備中": "申請準備中",
    "是正対応中": "是正対応中",  # 「申請中」より先にチェック
    "申請中": "申請中",
    "交付済": "交付済",
    "計画": "計画",
    "実施中": "実施",
    "完了": "完了",
}


def is_update_command(text: str) -> bool:
    """更新コマンドかどうかを判定"""
    # 明示的な更新指示
    if re.search(r"更新|変更|にして|に変えて|済みに|受領済", text):
        return True
    # 「〇〇邸　構造図着」のような短縮形：物件名 + 書類名 + 状態略語
    if re.search(r"(?:邸|様邸)", text) and re.search(r"着$|OK$|ok$|✓$|×$|NG$|ng$", text.strip()):
        return True
    # 「〇〇邸　構造図着」スペース区切り形式
    if re.search(r"(?:邸|様邸).+(?:着|OK|ok|✓|×|NG|ng)\s*$", text):
        return True
    return False


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


def resolve_property_name(keyword: str) -> tuple[str | None, str | None]:
    """
    略称（例：「中島邸」）からスプレッドシート上の正式名称を解決する。
    Returns: (正式名称, エラーメッセージ)
    一致なし → (None, エラー文)
    複数一致 → (None, 候補一覧)
    1件一致 → (正式名称, None)
    """
    try:
        from property_query import load_properties
        rows = load_properties()
    except Exception as e:
        return None, f"データ取得エラー: {str(e)}"

    # ① そのまま部分一致検索
    matches = [r["物件名"] for r in rows if keyword in r.get("物件名", "")]

    # ② 見つからなければ語幹で再検索（「中島邸」→「中島」）
    if not matches:
        stem = re.sub(r"(様邸|邸|の家)$", "", keyword)
        if stem and stem != keyword:
            matches = [r["物件名"] for r in rows if stem in r.get("物件名", "")]

    if not matches:
        return None, f"「{keyword}」に一致する物件が見つかりませんでした。正式名称で入力してください。"

    if len(matches) > 1:
        names = "、".join(matches)
        return None, f"複数の物件がヒットしました：{names}\nもう少し詳しい名前で入力してください。"

    return matches[0], None


def execute_update(property_name: str, column: str, value: str) -> str:
    """GAS doPost() を呼び出してスプレッドシートを更新する"""
    if not GAS_URL:
        return "❌ GAS_URL が設定されていません。Render の環境変数を確認してください。"

    # 略称を正式名称に解決
    resolved_name, error = resolve_property_name(property_name)
    if error:
        return f"❌ {error}"

    try:
        payload = {
            "action": "update_property",
            "property_name": resolved_name,
            "column": column,
            "value": value,
        }
        resp = requests.post(GAS_URL, json=payload, timeout=30, allow_redirects=True)
        resp.raise_for_status()

        result = resp.json()
        if result.get("success"):
            display_value = value if value not in ("☑", "☐") else ("受領済み" if value == "☑" else "未受領")
            return f"✅ 「{resolved_name}」の「{column}」を「{display_value}」に更新しました。"
        else:
            return f"❌ 更新失敗: {result.get('error', '不明なエラー')}"

    except requests.exceptions.Timeout:
        return "❌ タイムアウト：GAS の応答が遅すぎます。"
    except Exception as e:
        return f"❌ 更新エラー: {str(e)}"
