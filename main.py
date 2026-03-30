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
from property_query import is_property_query, answer_property_query
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

SYSTEM_PROMPT = """あなたはKAGI秘書です。株式会社KAGIYAの代表・渡辺貴正さんの専属AI秘書です。
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
【話し方】
- 親しみやすい相棒のような口調で
- 要点を簡潔に、必要に応じてリスト形式で
- 自然な日本語。敬語は軽めでOK。
- 次のアクションを必ず提示する"""

@app.route("/")
def health():
    return 'KAGI秘書 稼働中！', 200


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

@app.route("/buken/chat", methods=["POST"])
def buken_chat():
    if not session.get("buken_auth"):
        return jsonify({"answer": "セッションが切れました。再ログインしてください。"}), 401

    question = (request.json or {}).get("question", "").strip()
    if not question:
        return jsonify({"answer": "質問を入力してください。"})

    # 更新コマンド（「〇〇邸の構造図を受領済みに更新して」など）
    if is_update_command(question):
        parsed = parse_update_command(question)
        if parsed:
            answer = execute_update(**parsed)
        else:
            answer = "⚠️ 更新内容を解析できませんでした。\n例：「中島邸の構造図を受領済みに更新して」"
        return jsonify({"answer": answer})

    # 物件クエリ（「〇〇邸の状況は？」「全申請状況は？」など）
    if is_property_query(question):
        try:
            answer = answer_property_query(question)
        except Exception as e:
            answer = f"❌ データ取得エラー: {str(e)}"
        return jsonify({"answer": answer})

    return jsonify({"answer": "物件に関する質問か更新コマンドを入力してください。\n\n**質問例：**\n- 「中島邸の状況は？」\n- 「今の全申請状況は？」\n- 「申請準備中の物件は？」\n\n**更新例：**\n- 「中島邸の構造図を受領済みに更新して」"})

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

    if user_id not in conversation_histories:
        conversation_histories[user_id] = []

    conversation_histories[user_id].append({"role": "user", "content": user_message})

    if len(conversation_histories[user_id]) > 20:
        conversation_histories[user_id] = conversation_histories[user_id][-20:]

    # 物件更新コマンド（「〇〇邸の構造図を受領済みに更新して」など）
    if is_update_command(user_message):
        try:
            parsed = parse_update_command(user_message)
            if parsed:
                reply_text = execute_update(**parsed)
            else:
                reply_text = "更新内容を解析できませんでした。\n例：「中島邸の構造図を受領済みに更新して」"
            category = "代願業務"
        except Exception as e:
            reply_text = f"更新中にエラーが発生しました: {str(e)}"
            category = "その他"
    # 物件クエリ（「〇〇邸の状況」「全申請状況」など）は専用処理
    elif is_property_query(user_message):
        try:
            reply_text = answer_property_query(user_message)
            category = "代願業務"
        except Exception as e:
            reply_text = f"物件データの取得中にエラーが発生しました: {str(e)}"
            category = "その他"
    else:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=conversation_histories[user_id]
        )
        reply_text = response.content[0].text
        category = classify_message(user_message, reply_text)

    # スプレッドシートに保存
    save_to_sheet(user_id, user_message, reply_text, category)

    conversation_histories[user_id].append({"role": "assistant", "content": reply_text})

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
