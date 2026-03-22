from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
import anthropic
import os
import requests
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ['LINE_CHANNEL_SECRET']
LINE_CHANNEL_ACCESS_TOKEN = os.environ['LINE_CHANNEL_ACCESS_TOKEN']
ANTHROPIC_API_KEY = os.environ['ANTHROPIC_API_KEY']

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Google Apps Script WebアプリURL（記憶帳）
GAS_URL = "https://script.google.com/macros/s/AKfycby0fmGuARxYhY3-z0Q-BMgW69XfMETLSEcA1-2qLMAUvhW6EYHXKAAY5PMuzHZbTYgs/exec"

def save_to_sheet(user_id, message, response):
    try:
        requests.post(GAS_URL, json={
            "user_id": user_id,
            "message": message,
            "response": response
        }, timeout=5)
    except Exception:
        pass  # 保存失敗しても会話は続ける

# ユーザーごとの会話履歴
conversation_histories = {}

SYSTEM_PROMPT = """あなたはKAGI秘書です。株式会社KAGIYAの代表・渡辺貴正さんの専属AI秘書です。

【渡辺さんについて】
- 設計事務所（株式会社KAGIYA）を経営。用途を問わず建築設計全般を担当。
- 代願業務：ハウスメーカー向け建築確認申請を月4件程度担当。Excel で書類作成、まちセンNICE WEB申請でオンライン提出、VectorWorks で図面作成。
- 民泊事業：静岡県伊豆市姫之湯350に物件を所有。改修ほぼ完了。今後、保健所・消防への申請、Airbnb/PayPay/ホームページ立ち上げが必要。
- 記憶・整理が苦手なため、サポートが最重要。

【あなたの役割】
1. 会話の内容を記録・整理する（「あの件どうなったっけ？」に答える）
2. タスクや次のアクションを整理してリスト化する
3. 民泊申請・代願業務・設計業務のサポート
4. 決定事項をわかりやすくまとめる
5. リマインドが必要なことを指摘する

【話し方】
- 親しみやすい相棒のような口調で
- 要点を簡潔に、必要に応じてリスト形式で
- 自然な日本語。敬語は軽めでOK。
- 次のアクションを必ず提示する"""


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

    conversation_histories[user_id].append({
        "role": "user",
        "content": user_message
    })

    # 直近20件のみ保持
    if len(conversation_histories[user_id]) > 20:
        conversation_histories[user_id] = conversation_histories[user_id][-20:]

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=conversation_histories[user_id]
    )

    reply_text = response.content[0].text

    # Googleスプレッドシートに記録
    save_to_sheet(user_id, user_message, reply_text)

    conversation_histories[user_id].append({
        "role": "assistant",
        "content": reply_text
    })

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
