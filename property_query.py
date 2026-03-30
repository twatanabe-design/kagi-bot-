#!/usr/bin/env python3
"""
KAGI物件クエリシステム - Phase 2 GAS版（Render対応）

使い方:
    python property_query.py "中島清和様邸の状況は？"
    python property_query.py "今の全申請状況は？"
    python property_query.py "申請準備中の物件は？"
"""

import sys
import os
import re
import csv
import io
import requests
import anthropic
from dotenv import dotenv_values

# .envファイルを読み込む
_script_dir = os.path.dirname(os.path.abspath(__file__))
_env_path = os.path.join(_script_dir, ".env")
if os.path.exists(_env_path):
    os.environ.update(dotenv_values(_env_path))

# ── 設定 ──────────────────────────────────────────────
# GoogleスプレッドシートのCSV公開URL（認証不要）
CSV_URL = os.environ.get(
    "SHEET_CSV_URL",
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vRJmVTUuQK104vf4L0jbWzjM6dW57uD6S-"
    "JdNvv4kXBebqCpRTE58XPYsVWix4KV2CyP89tsgoGeGLL"
    "/pub?gid=348234433&single=true&output=csv"
)
MODEL = "claude-sonnet-4-5"
CHECKLIST_COLS = [
    "案内図", "公図", "確定式地図", "インフラ計画", "レベル", "道路情報",
    "物件概要", "施主情報", "地盤調査データ", "構造図", "申請予定", "CADデータ",
]
JISSHI_STATUSES = ["申請準備中", "申請中", "是正対応中", "交付済", "未選択"]
JOTAI_STATUSES  = ["計画", "実施", "完了"]


# ── データ読み込み（CSV公開URL経由）──────────────────────
def load_properties() -> list[dict]:
    """GoogleスプレッドシートのCSV公開URLからデータを取得（認証不要）"""
    resp = requests.get(CSV_URL, timeout=30)
    resp.raise_for_status()

    # BOM除去 + CSVパース
    text = resp.content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    rows = []
    for row in reader:
        name = row.get("物件名", "").strip()
        if not name or name == "物件名":
            continue
        rows.append(row)

    if not rows:
        raise RuntimeError("スプレッドシートからデータを取得できませんでした。")
    return rows


# ── ユーティリティ ────────────────────────────────────
def get_missing_docs(row: dict) -> list[str]:
    """未受領書類を返す。プルダウン（☑/☐）とチェックボックス（TRUE/FALSE）両方に対応"""
    missing = []
    for col in CHECKLIST_COLS:
        val = str(row.get(col, "")).strip()
        # 受領済み・不要はスキップ
        if val.upper() in ("TRUE", "☑", "不要"):
            continue
        # それ以外（FALSE・☐・空）→ 未受領
        if val.upper() in ("FALSE", "☐", "") or not val:
            missing.append(col)
    return missing


def row_to_summary(row: dict) -> dict:
    return {
        "物件名":          row.get("物件名", ""),
        "物件ID":          row.get("物件ID", ""),
        "状態":            row.get("状態", ""),
        "実施":            row.get("実施", ""),
        "確認申請_提出目標": row.get("確認申請 提出目標", "未設定") or "未設定",
        "確認申請_下付目標": row.get("確認申請 下付目標", "未設定") or "未設定",
        "工事着工予定日":   row.get("工事着工予定日", "未設定") or "未設定",
        "中間検査予定日":   row.get("中間検査予定日", "未設定") or "未設定",
        "完了検査予定日":   row.get("完了検査予定日", "未設定") or "未設定",
    }


# ── クエリ処理 ────────────────────────────────────────
def find_property(rows: list[dict], keyword: str) -> list[dict]:
    # ① そのまま部分一致
    matches = [r for r in rows if keyword in r.get("物件名", "")]
    if matches:
        return matches
    # ② 語幹で再検索（「中島邸」→「中島」、「鈴木邸」→「鈴木」）
    stem = re.sub(r"(様邸|邸|の家)$", "", keyword)
    if stem and stem != keyword:
        matches = [r for r in rows if stem in r.get("物件名", "")]
    return matches


def query_property_detail(rows: list[dict], keyword: str) -> tuple[dict | None, str | None]:
    results = find_property(rows, keyword)
    if not results:
        return None, f"「{keyword}」に一致する物件が見つかりませんでした。"
    if len(results) > 1:
        names = "、".join(r["物件名"] for r in results)
        return None, f"複数の物件がヒットしました：{names}\nもう少し詳しい名前で聞いてください。"
    row = results[0]
    data = row_to_summary(row)
    data["不足書類"] = get_missing_docs(row)
    return data, None


def query_all_status(rows: list[dict]) -> list[dict]:
    return [row_to_summary(r) for r in rows]


def query_by_jisshi(rows: list[dict], status: str) -> list[dict]:
    return [row_to_summary(r) for r in rows if status in r.get("実施", "")]


def query_by_jotai(rows: list[dict], status: str) -> list[dict]:
    return [row_to_summary(r) for r in rows if status in r.get("状態", "")]


# ── クエリ分類 ────────────────────────────────────────
def classify_query(query: str) -> tuple[str, str | None]:
    if re.search(r"全(申請|物件|部|体)|一覧|すべて", query):
        return "all_status", None
    for s in JISSHI_STATUSES:
        if s in query:
            return "filter_jisshi", s
    for s in JOTAI_STATUSES:
        if s in query:
            return "filter_jotai", s
    match = re.search(r"([^\s「」『』【】（）()\n]{1,20}(?:邸|様邸))", query)
    if match:
        return "property_detail", match.group(1)
    match2 = re.search(r"([^\sのはがをに「」]{2,8})の状況", query)
    if match2:
        return "property_detail", match2.group(1)
    return "unknown", None


# ── Claude APIでレスポンス生成 ────────────────────────
def build_response(query: str, data, query_type: str) -> str:
    client = anthropic.Anthropic()
    system_prompt = """あなたはKAGIYA建築設計事務所の物件管理AIアシスタントです。
建築確認申請の実務担当者（渡辺さん）への報告を行います。
以下の方針で回答してください：
- 簡潔・明瞭に、箇条書きを活用する
- 日付は「YYYY/MM/DD」形式で表示
- 不足書類がある場合は「⚠️ 不足書類」として明記
- 「未設定」の日付はまとめて省略してもよい
- 物件数が多い場合は表形式で整理する"""

    message = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=system_prompt,
        messages=[{"role": "user", "content": f"質問: {query}\n\n取得データ:\n{data}\n\n上記データをもとに質問に答えてください。"}],
    )
    return message.content[0].text


# ── 外部から呼び出し可能なメイン関数 ─────────────────────
def answer_property_query(query: str) -> str:
    """LINE Botから呼び出す用。クエリを受け取り返答文字列を返す。"""
    rows = load_properties()
    query_type, param = classify_query(query)

    if query_type == "property_detail":
        data, error = query_property_detail(rows, param)
        if error:
            return error
    elif query_type == "all_status":
        data = query_all_status(rows)
        if not data:
            return "物件データが見つかりませんでした。"
    elif query_type == "filter_jisshi":
        data = query_by_jisshi(rows, param)
        if not data:
            return f"「{param}」の物件はありません。"
    elif query_type == "filter_jotai":
        data = query_by_jotai(rows, param)
        if not data:
            return f"「{param}」の物件はありません。"
    else:
        data = query_all_status(rows)

    return build_response(query, data, query_type)


def is_property_query(text: str) -> bool:
    """物件関連の質問かどうかを判定する（main.pyから呼び出す用）"""
    keywords = ["邸", "物件", "申請", "着工", "検査", "確認申請", "交付", "是正", "書類"]
    return any(kw in text for kw in keywords)


# ── CLI実行 ───────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print("使い方: python property_query.py \"質問文\"")
        print()
        print("例:")
        print('  python property_query.py "中島清和様邸の状況は？"')
        print('  python property_query.py "今の全申請状況は？"')
        print('  python property_query.py "申請準備中の物件は？"')
        sys.exit(1)

    query = sys.argv[1]
    print(f"📋 質問: {query}")
    print("─" * 40)

    try:
        rows = load_properties()
        print(f"✅ {len(rows)}件の物件データを取得しました\n")
    except Exception as e:
        print(f"❌ データ取得エラー: {e}")
        sys.exit(1)

    query_type, param = classify_query(query)

    if query_type == "property_detail":
        data, error = query_property_detail(rows, param)
        if error:
            print(error)
            sys.exit(0)
    elif query_type == "all_status":
        data = query_all_status(rows)
        if not data:
            print("物件データが見つかりませんでした。")
            sys.exit(0)
    elif query_type == "filter_jisshi":
        data = query_by_jisshi(rows, param)
        if not data:
            print(f"「{param}」の物件はありません。")
            sys.exit(0)
    elif query_type == "filter_jotai":
        data = query_by_jotai(rows, param)
        if not data:
            print(f"「{param}」の物件はありません。")
            sys.exit(0)
    else:
        data = query_all_status(rows)

    try:
        response = build_response(query, data, query_type)
        print(response)
    except Exception as e:
        print(f"❌ Claude APIエラー: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
