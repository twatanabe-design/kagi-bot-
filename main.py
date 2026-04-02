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
    from datetime import datetime
    now = datetime.now().strftime("%Y/%m/%d %H:%M")
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


def load_buken_history_from_gas(limit=30) -> list:
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


def build_sheet_context() -> str:
    """スプレッドシートの進行中物件データをテキスト化してClaudeに渡す"""
    try:
        from property_query import load_properties, row_to_summary, get_missing_docs
        rows = load_properties()
        active_rows = [r for r in rows if r.get("状態", "") in ["計画", "実施"]]
        ctx = f"【現在の進行中物件データ（{len(active_rows)}件）】\n"
        for r in active_rows:
            s = row_to_summary(r)
            s["不足書類"] = get_missing_docs(r)
            代願元 = r.get("自社/他社", "").strip() or "不明"
            ctx += (
                f"- {s['物件名']}（{s['物件ID']}）: "
                f"代願元={代願元} 状態={s['状態']} 実施={s['実施']} "
                f"提出目標={s['確認申請_提出目標']} "
                f"不足書類={s['不足書類']}\n"
            )
        return ctx
    except Exception as e:
        return f"※スプレッドシートデータ取得失敗: {str(e)}"


def buken_ask(question: str) -> str:
    """
    物件管理AIの共通処理（全デバイス共通履歴）。
    question: ユーザー入力
    戻り値: Claudeの回答文字列
    """
    # 初回（サーバー再起動後）はGASから履歴を復元
    if BUKEN_HISTORY_KEY not in buken_histories:
        buken_histories[BUKEN_HISTORY_KEY] = load_buken_history_from_gas()

    # 更新コマンドなら先に実行
    update_result = None
    if is_update_command(question):
        parsed = parse_update_command(question)
        if parsed:
            update_result = execute_update(**parsed)
        else:
            update_result = "⚠️ 更新内容を解析できませんでした。例：「中島邸の構造図を受領済みに更新して」"

    # ユーザーメッセージ（更新結果があれば付加）
    user_content = question
    if update_result:
        user_content += f"\n\n（システム実行結果: {update_result}）"

    buken_histories[BUKEN_HISTORY_KEY].append({"role": "user", "content": user_content})
    if len(buken_histories[BUKEN_HISTORY_KEY]) > 30:
        buken_histories[BUKEN_HISTORY_KEY] = buken_histories[BUKEN_HISTORY_KEY][-30:]

    # GASに保存
    save_buken_message_to_gas("user", user_content)

    # スプレッドシートデータを毎回取得してsystemに渡す
    sheet_context = build_sheet_context()

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=BUKEN_SYSTEM_PROMPT + "\n\n" + sheet_context,
            messages=buken_histories[BUKEN_HISTORY_KEY],
        )
        answer = response.content[0].text
    except Exception as e:
        answer = f"❌ Claude APIエラー: {str(e)}"

    buken_histories[BUKEN_HISTORY_KEY].append({"role": "assistant", "content": answer})
    save_buken_message_to_gas("assistant", answer)
    return answer


@app.route("/buken/chat", methods=["POST"])
def buken_chat():
    if not session.get("buken_auth"):
        return jsonify({"answer": "セッションが切れました。再ログインしてください。"}), 401

    question = (request.json or {}).get("question", "").strip()
    if not question:
        return jsonify({"answer": "質問を入力してください。"})

    answer = buken_ask(question)
    return jsonify({"answer": answer})

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
