from flask import Flask, request, abort, session, redirect, render_template, jsonify
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
import anthropic
import os
import requests
import uuid
import json
import threading
from datetime import datetime as dt
from dotenv import load_dotenv
from property_update import is_update_command, parse_update_command, execute_update

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")

LINE_CHANNEL_SECRET = os.environ['LINE_CHANNEL_SECRET']
LINE_CHANNEL_ACCESS_TOKEN = os.environ['LINE_CHANNEL_ACCESS_TOKEN']
ANTHROPIC_API_KEY = os.environ['ANTHROPIC_API_KEY']

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

GAS_URL = "https://script.google.com/macros/s/AKfycby0fmGuARxYhY3-z0Q-BMgW69XfMETLSEcA1-2qLMAUvhW6EYHXKAAY5PMuzHZbTYgs/exec"

# -----------------------------------------------
# カイメモ機能（memo: プレフィックス）
# -----------------------------------------------
# プレフィックスを変更・無効化したい場合はここだけ修正する
MEMO_PREFIX = "memo:"

def is_memo_command(text: str) -> bool:
    """memo: から始まるメッセージかどうか判定"""
    return text.strip().lower().startswith(MEMO_PREFIX)

def extract_memo_body(text: str) -> str:
    """memo: プレフィックスを除いたメモ本文を返す"""
    return text.strip()[len(MEMO_PREFIX):].strip()

def classify_kai_memo(memo: str) -> str:
    """Claude APIでメモを「業務」か「プライベート」に分類"""
    try:
        result = anthropic_client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=20,
            messages=[{
                "role": "user",
                "content": (
                    "以下のメモを「業務」か「プライベート」の1語だけで分類してください。\n"
                    "・業務：建築設計、確認申請、代願業務、民泊事業、取引先、仕事全般\n"
                    "・プライベート：個人の買い物、家族、趣味、健康、食事、日常生活\n"
                    "迷う場合は「業務」にしてください。\n\n"
                    f"メモ：{memo}\n\n"
                    "「業務」または「プライベート」のみ返してください。"
                )
            }]
        )
        result_text = result.content[0].text.strip()
        return "プライベート" if "プライベート" in result_text else "業務"
    except Exception:
        return "業務"

def generate_kai_memo_tags(memo: str) -> str:
    """Claude APIでメモから自動タグを1〜3個生成"""
    try:
        result = anthropic_client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=80,
            messages=[{
                "role": "user",
                "content": (
                    "以下のメモ内容に対して、適切なタグを1〜3個生成してください。\n"
                    "タグは日本語のキーワードで、カンマ区切りで返してください。\n"
                    "候補例：場所, 持ち物, アイデア, 買い物, 仕事, プライベート, "
                    "確認待ち, 設計, 民泊, 代願業務, 締め切り, 連絡 など\n\n"
                    f"メモ：{memo}\n\n"
                    "タグのみ返してください（例：仕事, 確認待ち）"
                )
            }]
        )
        return result.content[0].text.strip()
    except Exception:
        return "その他"

def save_kai_memo(memo: str, destination: str, tags: str) -> bool:
    """GAS経由でカイメモを保存。業務→KAGI記憶帳、プライベート→T.W. LOG"""
    now = dt.now().strftime("%Y/%m/%d %H:%M")
    action = "write_memo" if destination == "業務" else "write_tw_memo"
    try:
        requests.get(
            GAS_URL,
            params={
                "action": action,
                "timestamp": now,
                "memo": memo,
                "tags": tags,
                "status": "未確認"
            },
            allow_redirects=True,
            timeout=15
        )
        return True
    except Exception:
        return False

def handle_memo_command(memo_text: str) -> str:
    """メモコマンドの一連処理。返信メッセージを返す"""
    destination = classify_kai_memo(memo_text)
    tags = generate_kai_memo_tags(memo_text)
    success = save_kai_memo(memo_text, destination, tags)
    if success:
        dest_label = "KAGI記憶帳（業務）" if destination == "業務" else "T.W. LOG（プライベート）"
        return f"📝 メモ記録したよ！\n振り分け：{dest_label}\nタグ：{tags}"
    else:
        return "❌ メモの保存に失敗しました。"

# -----------------------------------------------
# カテゴリ自動判定（民泊/代願業務/設計業務/その他）
# -----------------------------------------------
def classify_message(user_message, reply_text):
    try:
        result = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=50,
            messages=[{
                "role": "user",
                "content": f"""以下の会話を読んで、最も近いカテゴリを1つだけ返してください。
カテゴリ：「民泊」「代願業務」「設計業務」「その他」

ユーザー: {user_message}
AI: {reply_text[:200]}

カテゴリ名だけ返してください。"""
            }]
        )
        category = result.content[0].text.strip()
        for cat in ["民泊", "代願業務", "設計業務"]:
            if cat in category:
                return cat
        return "その他"
    except Exception:
        return "その他"

# -----------------------------------------------
# スプレッドシートに記録（カテゴリも送る）
# -----------------------------------------------
def save_to_sheet(user_id, message, response, category):
    try:
        requests.get(
            GAS_URL,
            params={
                "user_id": user_id,
                "message": message,
                "response": response,
                "category": category
            },
            allow_redirects=True,
            timeout=10
        )
    except Exception:
        pass

conversation_histories = {}

SYSTEM_PROMPT = """あなたはKAGI秘書、通称「カイ」です。株式会社KAGIYAの代表・渡辺貴正さんの専属AI秘書です。
【渡辺さんについて】
- 設計事務所（株式会社KAGIYA）を経営。用途を問わず建築設計全般を担当。
- 代願業務：ハウスメーカー向け建築確認申請を月4件程度担当。ExcelでDocument作成、まちセンNICE WEB申請でオンライン提出、VectorWorksで図面作成。
- 民泊事業：静岡県伊豆市姫之湯350に物件所有。改修ほぼ完了。保健所・消防への申請が次のステップ。
- 記憶・整理が苦手なため、サポートが最重要。
【あなたの役割】
1. 会話の内容を記録・整理する
2. タスクや次のアクションを整理してリスト化する
3. 民泊申請・代願業務・設計業務のサポート
4. 決定事項をわかりやすくまとめる
5. リマインドが必要なことを指摘する
【書類自動生成システム（開発中）】
現在Claude Codeで確認申請関連の書類を自動生成する仕組みを構築中。
- データソース：Googleスプレッドシート「各物件一覧」（リアルタイムCSV取得）
- 代願元マスター：デバイス→伊海、藤井建築→藤井、センス→濱、自社物件→渡邉
- 完成済みスクリプト：
  ・建築工事届（kouji_todoke.py）— ZIP XML patching方式でチェックボックスも自動チェック
  ・委任状 兼 同意書（ininjo.py）— 連名者がいれば自動で2人目追加
- 今後の構想：
  ・雛形フォルダ内の全書類（確認申請書、工事監理報告書等）を順次対応
  ・統合ランチャー（master_transfer.py）で「〇〇邸の書類全部作って」を実現
  ・最終的にカイ（LINE Bot）から「中島邸の書類作って」で一括生成→通知
- 渡辺さんから書類生成の話が出たら、現状と今後の流れを把握した上で会話すること。
  まだLINEからの直接実行はできないが、Claude Codeで物件ID指定で即生成可能な状態。
【話し方】
- 親しみやすい相棒のような口調で
- 要点を簡潔に、必要に応じてリスト形式で
- 自然な日本語。敬語は軽めでOK。
- 次のアクションを必ず提示する"""

@app.route("/")
def health():
    return 'カイ（KAGI秘書）稼働中！', 200


# -----------------------------------------------
# 物件管理チャット（/buken）
# -----------------------------------------------
BUKEN_PASSWORD = os.environ.get("BUKEN_PASSWORD", "kagiya2024")

@app.route("/buken")
def buken_index():
    if not session.get("buken_auth"):
        return redirect("/buken/login")
    return render_template("buken_chat.html")

@app.route("/buken/login", methods=["GET", "POST"])
def buken_login():
    error = False
    if request.method == "POST":
        if request.form.get("password") == BUKEN_PASSWORD:
            session["buken_auth"] = True
            return redirect("/buken")
        error = True
    return f"""<!DOCTYPE html>
<html lang="ja">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>KAGIYA 物件管理 - ログイン</title>
<style>
  body {{ font-family: -apple-system, sans-serif; background: #f0f2f5;
         display: flex; justify-content: center; align-items: center; height: 100dvh; }}
  .box {{ background: #fff; padding: 40px; border-radius: 12px;
          box-shadow: 0 2px 12px rgba(0,0,0,0.1); width: 300px; }}
  h2 {{ color: #1a1a2e; margin-bottom: 24px; font-size: 18px; text-align: center; }}
  input {{ width: 100%; padding: 10px 14px; border: 1px solid #ddd;
           border-radius: 8px; font-size: 15px; margin-bottom: 12px; }}
  button {{ width: 100%; padding: 11px; background: #1a1a2e; color: #fff;
            border: none; border-radius: 8px; font-size: 15px; cursor: pointer; }}
  button:hover {{ background: #2d2d5e; }}
  .err {{ color: #e00; font-size: 13px; margin-bottom: 10px; text-align: center; }}
</style></head>
<body><div class="box">
  <h2>🏠 KAGIYA 物件管理</h2>
  {"<p class='err'>パスワードが違います</p>" if error else ""}
  <form method="POST">
    <input type="password" name="password" placeholder="パスワード" autofocus>
    <button type="submit">ログイン</button>
  </form>
</div></body></html>"""

@app.route("/buken/logout")
def buken_logout():
    session.pop("buken_auth", None)
    return redirect("/buken/login")

BUKEN_SYSTEM_PROMPT = """あなたはKAGIYA建築設計事務所の物件管理AIアシスタントです。
代表の渡辺さんとの会話をサポートします。

【重要：データについて】
- このシステムプロンプトの末尾に「現在の全物件データ」が含まれています
- このデータは**質問のたびにGoogleスプレッドシートから直接取得した最新データ**です
- 渡辺さんがスプレッドシートを直接編集した場合も、次の質問時には最新状態が反映されます
- 「スプレッドシートにアクセスできない」は誤りです。必ず末尾のデータを参照してください

【物件状況への回答ルール】
「○○邸の状況は？」などの個別物件への質問には、必ず以下の2つを組み合わせて回答すること：

① スプレッドシートデータ（末尾の物件データを参照）
  - 確認申請・長期申請の状態
  - 提出目標日・交付目標日
  - 不足書類
  - 着工・検査予定日

② 会話履歴内のメモ（messagesを過去に遡って検索）
  - その物件名が含まれる発言をすべて拾う
  - 「〇〇が来たら△△する」「〇〇待ち」「次のアクション」などの記述を抽出
  - 見つかった場合は「📝 メモ（会話履歴より）」として明示して回答に含める
  - 見つからない場合は省略してよい

【スプレッドシート更新コマンドへの回答ルール】
ユーザーの入力に「（システム実行結果: ...）」が含まれている場合、必ずその内容を回答に含めること：
- ✅ 成功なら「〇〇を受領済みに更新しました」と明記する
- ❌ エラーなら「更新に失敗しました：（理由）」と明記する
- 絶対に「了解」だけで済ませず、更新結果を報告すること

【送信者について】
会話の送信者は「たかまさ」「ともこ」「デバイス」の3種類。

- **たかまさ・ともこ**：通常の質問・報告として処理する
- **デバイス**：PC・システム・自動化ツールからの送信。指示・依頼として扱い、**最優先で処理**すること。
  - 曖昧な点があっても推測して即実行・回答する
  - 「了解しました」より先に結果・回答を示す
  - タスクとして登録が必要な内容は必ずTASKに追加する

【スタンス】
- 問題・矛盾があれば率直に指摘する
- 問題なければ「了解」「記録しました」など短く返す
- 長々と説明しない。端的に、箇条書きを活用する
- 日付は YYYY/MM/DD 形式で表示

【できること】
- 物件の状況確認（スケジュール・書類不足・申請状況）
- 書類の受領状況を確認・記録
- 全申請状況の把握
- スケジュールの矛盾チェック（例：着工が交付より前など）
"""

# 会話履歴（全デバイス共通の単一キー）
BUKEN_HISTORY_KEY = "buken_main"
buken_histories = {}

# -----------------------------------------------
# タスク管理（TASKパネル）
# -----------------------------------------------
buken_tasks: list = []
buken_tasks_loaded: bool = False


def load_tasks_from_gas() -> list:
    """GASからタスク一覧を取得"""
    try:
        resp = requests.get(GAS_URL, params={"action": "read_tasks"}, timeout=10)
        tasks = resp.json().get("tasks", [])
        for t in tasks:
            if isinstance(t.get("checked"), str):
                t["checked"] = t["checked"].lower() == "true"
        return tasks
    except Exception:
        return []


def save_task_to_gas(task: dict):
    """GASにタスクを1件追記"""
    try:
        requests.get(
            GAS_URL,
            params={
                "action": "write_task",
                "id": task["id"],
                "type": task.get("type", "確認事項"),
                "content": task.get("content", ""),
                "target": task.get("target") or "",
                "sender": task.get("sender", ""),
                "checked": "false",
                "created_at": task.get("created_at", ""),
            },
            timeout=10
        )
    except Exception:
        pass


def update_task_in_gas(task_id: str, checked: bool):
    """GASのタスクの確認済みフラグを更新"""
    try:
        requests.get(
            GAS_URL,
            params={"action": "update_task", "id": task_id, "checked": "true" if checked else "false"},
            timeout=10
        )
    except Exception:
        pass


def get_buken_tasks() -> list:
    """タスク一覧を取得（初回のみGASから読み込み）"""
    global buken_tasks, buken_tasks_loaded
    if not buken_tasks_loaded:
        buken_tasks = load_tasks_from_gas()
        buken_tasks_loaded = True
    return buken_tasks


def extract_tasks(sender: str, message: str, answer: str) -> list:
    """
    会話からAIが未解決の確認事項・未確定事項を抽出。
    物件関連の話題のみ対象。AIが既に回答済みの内容は登録しない。
    """
    try:
        result = anthropic_client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": (
                    "以下の会話を分析し、人間が実際に行動・確認・決定しなければならない未完了事項のみをJSON配列で返してください。\n\n"
                    "【タスクとして登録する条件（すべて満たすこと）】\n"
                    "1. 物件に関する話題である\n"
                    "2. AIの回答で完全には解決しておらず、追加の行動が必要\n"
                    "3. 具体的な作業・確認・決定が必要な未完了事項\n\n"
                    "【タスクにしてはいけないもの】\n"
                    "- AIが質問に回答済みの内容（状況確認・情報確認でAIが答えた場合）\n"
                    "- スプレッドシートの申請状況・物件状況の確認（AIが答えた場合）\n"
                    "- 単純な質問・雑談・報告\n"
                    "- AIが「〇〇を確認してください」と言っただけで具体的なアクションが不明なもの\n\n"
                    "【タスクタイプ】\n"
                    '- "確認事項"：誰かが実際に確認・取得しなければならない書類・情報\n'
                    '- "未確定事項"：まだ決定されていない日程・方針・情報\n\n'
                    "【出力形式（JSONのみ）】\n"
                    '[{"type": "確認事項", "content": "具体的なタスク内容", "target": "対象物件名またはnull"}]\n'
                    "抽出なしの場合は [] を返す。JSONのみ返すこと。\n\n"
                    f"ユーザーの発言：{message}\n"
                    f"AIの回答：{answer[:600]}\n\n"
                    "JSONのみ："
                )
            }]
        )
        import re as _re
        text = result.content[0].text.strip()
        m = _re.search(r'\[.*\]', text, _re.DOTALL)
        if not m:
            return []
        tasks_raw = json.loads(m.group())
        now = dt.now().strftime("%Y/%m/%d %H:%M")
        tasks = []
        for t in tasks_raw:
            if isinstance(t, dict) and t.get("content"):
                tasks.append({
                    "id": str(uuid.uuid4()),
                    "type": t.get("type", "確認事項"),
                    "content": t["content"],
                    "target": t.get("target") or None,
                    "sender": sender,
                    "checked": False,
                    "created_at": now,
                })
        return tasks
    except Exception:
        return []


def load_buken_history_from_gas(limit=50) -> list:
    """GASから物件管理履歴を読み込む（サーバー再起動時に復元）"""
    try:
        resp = requests.get(
            GAS_URL,
            params={"action": "read_buken", "limit": limit},
            timeout=10
        )
        data = resp.json()
        return data.get("messages", [])
    except Exception:
        return []


def save_buken_message_to_gas(role: str, content: str):
    """GASに物件管理の1メッセージを追記"""
    try:
        requests.get(
            GAS_URL,
            params={
                "action": "write_buken",
                "role": role,
                "content": content[:2000]
            },
            timeout=10
        )
    except Exception:
        pass


def build_sheet_context(rows=None) -> str:
    """スプレッドシートの全物件データをリアルタイムで取得してClaudeに渡す。rowsを渡すと再フェッチしない。"""
    try:
        from property_query import load_properties, row_to_summary, get_missing_docs
        if rows is None:
            rows = load_properties()
        active_rows = [r for r in rows if r.get("状態", "") in ["計画", "実施"]]
        completed_rows = [r for r in rows if r.get("状態", "") == "完了"]

        ctx = f"【現在の全物件データ（Googleスプレッドシートより毎回リアルタイム取得）】\n"
        ctx += f"※このデータは今このリクエスト時点でスプレッドシートから取得した最新情報です\n\n"

        ctx += f"■ 進行中物件（{len(active_rows)}件）\n"
        for r in active_rows:
            s = row_to_summary(r)
            s["不足書類"] = get_missing_docs(r)
            代願元 = r.get("自社/他社", "").strip() or "不明"
            長期申請 = r.get("長期申請", "").strip() or "未選択"
            ctx += (
                f"- {s['物件名']}（{s['物件ID']}）: "
                f"代願元={代願元} 状態={s['状態']} "
                f"確認申請={s['実施']} 長期申請={長期申請} "
                f"提出目標={s['確認申請_提出目標']} "
                f"不足書類={s['不足書類']}\n"
            )

        if completed_rows:
            ctx += f"\n■ 完了物件（{len(completed_rows)}件）\n"
            for r in completed_rows:
                s = row_to_summary(r)
                長期申請 = r.get("長期申請", "").strip() or "未選択"
                ctx += (
                    f"- {s['物件名']}（{s['物件ID']}）: "
                    f"確認申請={s['実施']} 長期申請={長期申請}\n"
                )

        return ctx
    except Exception as e:
        return f"※スプレッドシートデータ取得失敗: {str(e)}"


def buken_ask(question: str, sender: str = "たかまさ") -> str:
    """
    物件管理AIの共通処理（全デバイス共通履歴）。
    question: ユーザー入力
    sender: 送信者名（たかまさ / ともこ）
    戻り値: Claudeの回答文字列
    """
    # 初回（サーバー再起動後）はGASから履歴を復元
    if BUKEN_HISTORY_KEY not in buken_histories:
        buken_histories[BUKEN_HISTORY_KEY] = load_buken_history_from_gas()

    # スプレッドシートデータを1回だけ取得（更新・コンテキスト構築で共有）
    cached_rows = None
    try:
        from property_query import load_properties
        cached_rows = load_properties()
    except Exception:
        pass

    # 更新コマンドなら先に実行（キャッシュ済みrowsを渡して二重フェッチ防止）
    update_result = None
    if is_update_command(question):
        parsed = parse_update_command(question)
        if parsed:
            update_result = execute_update(**parsed, rows=cached_rows)
        else:
            update_result = "⚠️ 更新内容を解析できませんでした。例：「中島邸の構造図を受領済みに更新して」"

    # ユーザーメッセージ（更新結果があれば付加）
    user_content = question
    if update_result:
        user_content += f"\n\n（システム実行結果: {update_result}）"

    # Claude向けメッセージにsenderラベルを付加
    sender_label = f"[{sender}] " if sender != "たかまさ" else ""
    claude_content = sender_label + user_content

    # 履歴にsenderも保存（表示用）
    buken_histories[BUKEN_HISTORY_KEY].append({"role": "user", "content": claude_content, "sender": sender})
    if len(buken_histories[BUKEN_HISTORY_KEY]) > 50:
        buken_histories[BUKEN_HISTORY_KEY] = buken_histories[BUKEN_HISTORY_KEY][-50:]

    # GASに保存
    save_buken_message_to_gas("user", claude_content)

    # スプレッドシートデータをsystemに渡す（キャッシュ済みrowsを使い回し）
    sheet_context = build_sheet_context(rows=cached_rows)

    # Claudeにはrole+contentのみ渡す（senderは除外）
    claude_messages = [{"role": m["role"], "content": m["content"]} for m in buken_histories[BUKEN_HISTORY_KEY]]

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=BUKEN_SYSTEM_PROMPT + "\n\n" + sheet_context,
            messages=claude_messages,
        )
        answer = response.content[0].text
    except Exception as e:
        answer = f"❌ Claude APIエラー: {str(e)}"

    buken_histories[BUKEN_HISTORY_KEY].append({"role": "assistant", "content": answer})
    save_buken_message_to_gas("assistant", answer)

    # タスク自動抽出をバックグラウンドで実行（レスポンス速度・メモリ節約）
    def _extract_and_store():
        try:
            new_tasks = extract_tasks(sender, question, answer)
            if new_tasks:
                task_list = get_buken_tasks()
                for task in new_tasks:
                    task_list.append(task)
                    save_task_to_gas(task)
        except Exception:
            pass

    threading.Thread(target=_extract_and_store, daemon=True).start()

    return answer


@app.route("/buken/history", methods=["GET"])
def buken_history():
    if not session.get("buken_auth"):
        return jsonify({"messages": []}), 401
    if BUKEN_HISTORY_KEY not in buken_histories:
        buken_histories[BUKEN_HISTORY_KEY] = load_buken_history_from_gas()
    messages = buken_histories[BUKEN_HISTORY_KEY][-50:]
    return jsonify({"messages": messages})


@app.route("/buken/chat", methods=["POST"])
def buken_chat():
    if not session.get("buken_auth"):
        return jsonify({"answer": "セッションが切れました。再ログインしてください。"}), 401

    data = request.json or {}
    question = data.get("question", "").strip()
    sender = data.get("sender", "たかまさ")
    if sender not in ["たかまさ", "ともこ", "デバイス"]:
        sender = "たかまさ"
    if not question:
        return jsonify({"answer": "質問を入力してください。"})

    answer = buken_ask(question, sender)
    return jsonify({"answer": answer})


@app.route("/buken/tasks", methods=["GET"])
def buken_tasks_get():
    if not session.get("buken_auth"):
        return jsonify({"tasks": []}), 401
    return jsonify({"tasks": get_buken_tasks()})


@app.route("/buken/tasks", methods=["POST"])
def buken_tasks_post():
    if not session.get("buken_auth"):
        return jsonify({"error": "unauthorized"}), 401
    data = request.json or {}
    task = {
        "id": str(uuid.uuid4()),
        "type": data.get("type", "確認事項"),
        "content": data.get("content", "").strip(),
        "target": data.get("target") or None,
        "sender": data.get("sender", "manual"),
        "checked": False,
        "created_at": dt.now().strftime("%Y/%m/%d %H:%M"),
    }
    if not task["content"]:
        return jsonify({"error": "content is required"}), 400
    get_buken_tasks().append(task)
    save_task_to_gas(task)
    return jsonify({"task": task})


@app.route("/buken/tasks/<task_id>", methods=["PATCH"])
def buken_tasks_patch(task_id):
    if not session.get("buken_auth"):
        return jsonify({"error": "unauthorized"}), 401
    data = request.json or {}
    checked = bool(data.get("checked", False))
    for task in get_buken_tasks():
        if task["id"] == task_id:
            task["checked"] = checked
            update_task_in_gas(task_id, checked)
            return jsonify({"task": task})
    return jsonify({"error": "not found"}), 404

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text

    # memo: プレフィックスならカイメモ処理
    if is_memo_command(user_message):
        memo_body = extract_memo_body(user_message)
        reply_text = handle_memo_command(memo_body)
        save_to_sheet(user_id, user_message, reply_text, "その他")
        reply_text = reply_text[:4990] + "…" if len(reply_text) > 4990 else reply_text
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )
        return

    # LINEはKAGI秘書のみ（各物件一覧への書き込みはClaudeCode経由のみ）
    if user_id not in conversation_histories:
        conversation_histories[user_id] = []
    conversation_histories[user_id].append({"role": "user", "content": user_message})
    if len(conversation_histories[user_id]) > 20:
        conversation_histories[user_id] = conversation_histories[user_id][-20:]

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=conversation_histories[user_id]
    )
    reply_text = response.content[0].text
    category = classify_message(user_message, reply_text)
    conversation_histories[user_id].append({"role": "assistant", "content": reply_text})

    # KAGI記憶帳に記録
    save_to_sheet(user_id, user_message, reply_text, category)

    # LINEに返信（5000文字制限対応）
    reply_text = reply_text[:4990] + "…" if len(reply_text) > 4990 else reply_text

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
