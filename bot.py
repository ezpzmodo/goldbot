import os, json
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes
)

BOT_TOKEN   = os.getenv("BOT_TOKEN")
WEB_APP_URL = os.getenv("WEB_APP_URL")  # spinner-web.fly.dev

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    btn = InlineKeyboardButton(
        text="돌림판 돌리기 ▶️",
        web_app={"url": WEB_APP_URL}
    )
    await update.message.reply_text(
        "참여자를 추가하고 버튼을 눌러 돌림판을 실행하세요!",
        reply_markup=InlineKeyboardMarkup([[btn]])
    )

async def webapp_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.web_app_data.data
    try:
        data = json.loads(raw)
        winner = data["winner"]
        plist  = data.get("participants", [])
        await update.message.reply_text(
            f"🎉 당첨자: {winner} 🎉\n참가자: {', '.join(plist)}"
        )
    except Exception as e:
        await update.message.reply_text(f"❗ 오류: {e}")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.StatusUpdate.WEB_APP_DATA, webapp_handler)
    )
    print("Bot running…")
    app.run_polling()

if __name__ == "__main__":
    main()